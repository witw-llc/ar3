"""Load glow_stream for a8s convo rendering."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_GLOW_STREAM_PATH = Path(__file__).resolve().parent / "glow_stream.py"


def load_glow_stream_module():
    spec = importlib.util.spec_from_file_location("_shared_glow_stream", _GLOW_STREAM_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"glow_stream not found at {_GLOW_STREAM_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def open_glow_stdout(theme: str = "auto"):
    mod = load_glow_stream_module()
    return mod.open_glow_stream(sys.stdout, theme)
