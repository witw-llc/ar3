"""Offline regressions for transport and stored-vector security guards."""

import io
import json
import urllib.request
import urllib.response
from email.message import Message
from types import SimpleNamespace

import pytest

import config
import embeddings
import engine


@pytest.fixture
def openai_security(store, monkeypatch):
    monkeypatch.setenv('K7E_EMBEDDINGS', 'openai')
    monkeypatch.setenv('EMBED_MODEL', 'text-embedding-3-small')
    monkeypatch.setenv('K7E_EMBED_DIMENSIONS', '3')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-security-key')
    return store


def embedding_response():
    return json.dumps({
        'model': 'text-embedding-3-small',
        'data': [{'index': 0, 'embedding': [1.0, 0.0, 0.0]}],
    }).encode()


@pytest.mark.parametrize('code', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('destination', [
    'https://example.invalid/embedding-redirect',
    'https://api.openai.com/embedding-redirect',
    'http://example.invalid/embedding-redirect',
])
def test_redirect_refused_before_forwarding_credentials(
    openai_security, monkeypatch, code, destination, capsys,
):
    calls = []

    def request(handler, req):
        calls.append(req)
        headers = Message()
        if len(calls) == 1:
            headers['Location'] = destination
            response = urllib.response.addinfourl(
                io.BytesIO(b''), headers, req.full_url, code,
            )
            response.msg = 'Synthetic redirect'
        else:
            response = urllib.response.addinfourl(
                io.BytesIO(embedding_response()), headers, req.full_url, 200,
            )
            response.msg = 'OK'
        return response

    # Keep urllib's real redirect chain; replace only the socket transports.
    monkeypatch.setattr(urllib.request.HTTPSHandler, 'https_open', request)
    monkeypatch.setattr(urllib.request.HTTPHandler, 'http_open', request)
    monkeypatch.setattr(urllib.request, '_opener', None)
    assert engine.embed_text('Synthetic input', timeout=0.25) is None
    assert len(calls) == 1
    assert calls[0].full_url == 'https://api.openai.com/v1/embeddings'
    assert calls[0].get_header('Authorization') == 'Bearer synthetic-security-key'
    assert calls[0].timeout == 0.25
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('extra_bytes', [-1, 0, 1])
def test_valid_json_response_size_boundary(openai_security, monkeypatch, extra_bytes):
    limit = embeddings._RESPONSE_BYTES
    payload = embedding_response()
    raw = payload + b' ' * (limit + extra_bytes - len(payload))
    reads = []
    calls = []

    class Response(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    def request(req, timeout):
        calls.append(req)
        return Response(raw)

    monkeypatch.setattr(
        urllib.request, 'build_opener',
        lambda *handlers: SimpleNamespace(open=request),
    )
    monkeypatch.setattr(urllib.request, 'urlopen', request)
    result = engine.embed_text('Synthetic input')
    assert result == ([1.0, 0.0, 0.0] if extra_bytes <= 0 else None)
    assert len(calls) == 1
    assert reads == [limit + 1]


@pytest.mark.parametrize('key', [None, '', ' \t\n'])
def test_status_reports_missing_runtime_key_without_network(
    openai_security, monkeypatch, key, capsys,
):
    if key is None:
        monkeypatch.delenv('OPENAI_API_KEY')
    else:
        monkeypatch.setenv('OPENAI_API_KEY', key)

    def unexpected_request(*args, **kwargs):
        pytest.fail('Status must not probe an embedding endpoint')

    monkeypatch.setattr(urllib.request, 'build_opener', unexpected_request)
    monkeypatch.setattr(urllib.request, 'urlopen', unexpected_request)
    assert config.detect_providers()['embeddings:openai']['configured'] is False
    text = config.status()
    assert 'OpenAI missing runtime OPENAI_API_KEY' in text
    assert 'FTS5-only mode' in text
    assert 'API access unverified' not in text
    assert 'synthetic-security-key' not in text
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('blob_size', [0, 8, 11, 13, 16])
@pytest.mark.parametrize('reader', ['coverage', 'search'])
def test_blob_length_sql_guard_excludes_corruption_before_unpacking(
    openai_security, monkeypatch, blob_size, reader,
):
    vector = [1.0, 0.0, 0.0]
    monkeypatch.setattr(engine, 'embed_text', lambda *args, **kwargs: vector)
    current = engine.store_entry('Synthetic current vector', 'Synthetic current note')
    corrupt = engine.store_entry('Synthetic corrupt vector', 'Synthetic corrupt note')
    assert engine.process_pending_embeddings() == 2
    valid_blob = engine._pack_vector(vector)
    corrupt_blob = (valid_blob + b'\0' * blob_size)[:blob_size]
    conn = engine._connect()
    try:
        conn.execute('UPDATE embeddings SET vector = ? WHERE node_id = ?',
                     (corrupt_blob, corrupt))
        conn.commit()
        unpacked = []
        scored = []
        unpack = engine._unpack_vector
        cosine = engine.cosine_similarity

        def track_unpack(blob):
            unpacked.append(blob)
            return unpack(blob)

        def track_score(query_vector, node_vector):
            scored.append((query_vector, node_vector))
            return cosine(query_vector, node_vector)

        monkeypatch.setattr(engine, '_unpack_vector', track_unpack)
        monkeypatch.setattr(engine, 'cosine_similarity', track_score)
        if reader == 'coverage':
            assert engine._valid_current_vector_ids(conn, embeddings.space()) == {current}
            assert scored == []
        else:
            results = engine._search_embeddings(conn, 'Synthetic query', 10)
            assert [row[0] for row in results] == [current]
            assert scored == [(vector, vector)]
        # Downstream dimension checks also reject corrupt rows, but too late.
        assert unpacked == [valid_blob]
    finally:
        conn.close()


@pytest.mark.parametrize('other_dimensions', [2, 4])
def test_query_dimension_sql_guard_excludes_vectors_before_unpacking(
    store, monkeypatch, other_dimensions,
):
    monkeypatch.setenv('K7E_EMBEDDINGS', 'ollama')
    monkeypatch.setenv('EMBED_MODEL', 'synthetic-model')
    monkeypatch.delenv('K7E_EMBED_DIMENSIONS', raising=False)
    assert embeddings.space().dimensions is None
    vector = [1.0, 0.0, 0.0]
    monkeypatch.setattr(engine, 'embed_text', lambda *args, **kwargs: vector)
    current = engine.store_entry('Synthetic query-sized vector', 'Synthetic matching note')
    other = engine.store_entry('Synthetic differently sized vector', 'Synthetic other note')
    assert engine.process_pending_embeddings() == 2
    other_vector = [1.0] + [0.0] * (other_dimensions - 1)
    conn = engine._connect()
    try:
        conn.execute('UPDATE embeddings SET vector = ?, dimensions = ? WHERE node_id = ?',
                     (engine._pack_vector(other_vector), other_dimensions, other))
        conn.commit()
        # Both rows are valid for an unpinned space; only the query fixes its size.
        assert engine._valid_current_vector_ids(conn, embeddings.space()) == {current, other}
        unpacked = []
        scored = []
        unpack = engine._unpack_vector
        cosine = engine.cosine_similarity

        def track_unpack(blob):
            unpacked.append(blob)
            return unpack(blob)

        def track_score(query_vector, node_vector):
            scored.append((query_vector, node_vector))
            return cosine(query_vector, node_vector)

        monkeypatch.setattr(engine, '_unpack_vector', track_unpack)
        monkeypatch.setattr(engine, 'cosine_similarity', track_score)
        results = engine._search_embeddings(conn, 'Synthetic query', 10)
        assert [row[0] for row in results] == [current]
        assert unpacked == [engine._pack_vector(vector)]
        assert scored == [(vector, vector)]
    finally:
        conn.close()
