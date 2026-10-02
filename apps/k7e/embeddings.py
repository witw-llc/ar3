"""Optional embedding transports and vector-space identity; stdlib only.

OpenAI credentials are read only from the process environment at request time.
Failures return None without logging inputs, headers, response bodies or keys.
"""
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

import config

OFF = {'off', 'none', 'false', '0', 'no'}
OPENAI_MODELS = {'text-embedding-3-small': 1536, 'text-embedding-3-large': 3072}
TEXT_VERSION = 'title-body-500-reserved-headings-v2'
_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Space:
    provider: str
    model: str
    dimensions: int | None


def space():
    """Validated non-secret configuration. None disables the semantic track."""
    provider = str(config.get('embeddings', 'ollama')).strip().lower()
    if provider in OFF:
        return None
    if provider in {'on', 'true', '1', 'yes'}:
        provider = 'ollama'
    if provider not in {'ollama', 'openai'}:
        return None
    model = config.get('embed_model', 'text-embedding-3-small' if provider == 'openai' else 'nomic-embed-text')
    if not isinstance(model, str) or not model.strip():
        return None
    model = model.strip()
    dimensions = config.get('embed_dimensions')
    if dimensions is not None:
        if type(dimensions) is int:
            pass
        elif isinstance(dimensions, str) and len(dimensions) <= 5 and re.fullmatch(r'[1-9][0-9]*', dimensions):
            dimensions = int(dimensions)
        else:
            return None
        if not 1 <= dimensions <= 65536:
            return None
    if provider == 'openai':
        maximum = OPENAI_MODELS.get(model)
        if maximum is None:
            return None
        dimensions = maximum if dimensions is None else dimensions
        if dimensions > maximum:
            return None
    return Space(provider, model, dimensions)


def valid_vector(vector, dimensions=None):
    try:
        return (isinstance(vector, list) and 0 < len(vector) <= 65536
                and (dimensions is None or len(vector) == dimensions)
                and all(type(x) in (int, float) and math.isfinite(x) and abs(x) <= 3.4028234e38 for x in vector)
                and any(x != 0 for x in vector))
    except (OverflowError, TypeError, ValueError):
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The runtime bearer credential is for the fixed API endpoint only.
        return None


def embed(text, timeout):
    selected = space()
    if selected is None or not isinstance(text, str) or not text.strip():
        return None
    try:
        payload = {'model': selected.model, 'input': text}
        headers = {'Content-Type': 'application/json'}
        if selected.provider == 'openai':
            key = os.environ.get('OPENAI_API_KEY', '').strip()
            if not key:
                return None
            payload.update(dimensions=selected.dimensions, encoding_format='float')
            headers['Authorization'] = f'Bearer {key}'
            url = 'https://api.openai.com/v1/embeddings'
            opener = urllib.request.build_opener(_NoRedirect()).open
        else:
            url = str(config.get('ollama_url', 'http://localhost:11434')).rstrip('/') + '/api/embed'
            opener = urllib.request.urlopen
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
        with opener(req, timeout=timeout) as response:
            raw = response.read(_RESPONSE_BYTES + 1)
        if len(raw) > _RESPONSE_BYTES:
            return None
        result = json.loads(raw)
        if selected.provider == 'openai':
            if result.get('model') != selected.model:
                return None
            rows = result.get('data')
            if not isinstance(rows, list) or len(rows) != 1 or type(rows[0].get('index')) is not int or rows[0]['index'] != 0:
                return None
            vector = rows[0]['embedding']
        else:
            rows = result.get('embeddings')
            if not isinstance(rows, list) or len(rows) != 1:
                return None
            vector = rows[0]
        return vector if valid_vector(vector, selected.dimensions) else None
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, OverflowError):
        return None
