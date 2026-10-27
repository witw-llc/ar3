"""Background subprocesses use the foundation's hidden-console policy."""
from functools import partial
import io
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ar3.proc import launch_options
import glow_stream
import mcp_server
from services import rclone


@pytest.mark.parametrize('kind', ['mcp', 'glow', 'rclone'])
def test_background_tools_hide_windows_console(monkeypatch, kind):
    module = {'mcp': mcp_server, 'glow': glow_stream, 'rclone': rclone}[kind]
    monkeypatch.setattr(module, 'launch_options', partial(launch_options, windows=True))
    calls = []

    class Result:
        returncode = 0
        stdout = 'rendered'
        stderr = ''

        def communicate(self, **kwargs):
            return self.stdout, self.stderr

    def invoke(argv, **kwargs):
        calls.append(kwargs)
        return Result()

    monkeypatch.setattr(subprocess, 'Popen' if kind == 'mcp' else 'run', invoke)
    if kind == 'mcp':
        assert mcp_server.send('recipient', 'body')[0]
    elif kind == 'glow':
        assert glow_stream.GlowStream(io.StringIO(), 'fake-glow')._run_glow('text') == 'rendered'
    else:
        assert rclone.RcloneService('drive', url='rclone://test/root')._run(['link', 'path']) == 'rendered'
    assert len(calls) == 1
    assert calls[0]['creationflags'] == 0x08000000
    assert 'start_new_session' not in calls[0]


@pytest.mark.parametrize('script, args', [
    ('mcp_server.py', []),
    ('connectors/gmail/gmail_cron.py', ['--help']),
])
def test_standalone_entrypoints_find_foundation_without_pythonpath(tmp_path, script, args):
    entrypoint = Path(__file__).resolve().parents[1] / script
    env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    result = subprocess.run([sys.executable, str(entrypoint), *args], cwd=tmp_path,
                            env=env, input='', capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
