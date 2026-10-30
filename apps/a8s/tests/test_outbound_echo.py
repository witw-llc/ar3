"""Local publication identities suppress echoes without trusting sender names."""
import json
import os
import subprocess
import sys
from pathlib import Path

from ar3.ulid import new
from core import Participant, inbox_dir, resolve_a8s_home
from mailbox import _write_outbox, route_outboxes
from network import receive_envelope
from registry import save_aliases, save_namespaces, save_registry


def _seat(tmp_path, name='node'):
    root = tmp_path / name
    root.mkdir()
    return Participant(name, root)


def _messages(name):
    return [json.loads(p.read_text()) for p in inbox_dir(name).glob('*.json')]


def test_synchronous_and_later_echo_do_not_return_to_sender(fake_home, tmp_path):
    node = _seat(tmp_path)
    save_registry({'node': {'root': str(node.root)}})
    save_namespaces({'org': 'node'})
    _write_outbox('node', node.root, 'org:remote-seat', 'outbound', [])
    sent = []

    def publish(msg, sender, down):
        sent.append(json.dumps(msg).encode())
        receive_envelope(sent[-1], [node])
        return {'broker': ('sent', '')}

    route_outboxes([node], all_agents=[node], publish_remotes=publish,
                   configured_remote_ids=['broker'])
    assert len(sent) == 1
    assert _messages('node') == []
    receive_envelope(sent[0], [node])
    assert _messages('node') == []
    repo = Path(__file__).resolve().parents[3]
    child = subprocess.run(
        [sys.executable, '-c',
         'import sys; from network import receive_envelope; '
         'from registry import participants_from_registry; '
         'receive_envelope(sys.stdin.buffer.read(), participants_from_registry())'],
        input=sent[0], capture_output=True, timeout=10,
        env={**os.environ, 'A8S_HOME': str(resolve_a8s_home()),
             'PYTHONPATH': os.pathsep.join([str(repo / 'lib'), str(repo / 'apps/a8s')])},
    )
    assert child.returncode == 0, child.stderr
    assert _messages('node') == []
    incoming = json.loads(sent[0])
    incoming['id'] = new()
    incoming['content'] = 'independent same-name sender'
    receive_envelope(json.dumps(incoming).encode(), [node])
    assert [m['content'] for m in _messages('node')] == [incoming['content']]


def test_other_machine_with_same_node_name_receives_publication(fake_home, tmp_path, monkeypatch):
    node = _seat(tmp_path)
    save_registry({'node': {'root': str(node.root)}})
    save_namespaces({'org': 'node'})
    _write_outbox('node', node.root, 'org:remote-seat', 'outbound', [])
    sent = []

    def publish(msg, sender, down):
        sent.append(json.dumps(msg).encode())
        return {'broker': ('sent', '')}

    route_outboxes([node], all_agents=[node], publish_remotes=publish,
                   configured_remote_ids=['broker'])
    monkeypatch.setenv('A8S_HOME', str(tmp_path / 'other-machine'))
    save_registry({'node': {'root': str(node.root)}})
    save_namespaces({'org': 'node'})
    receive_envelope(sent[0], [node])
    assert [m['content'] for m in _messages('node')] == ['outbound']


def test_local_alias_fanout_survives_synchronous_echo(fake_home, tmp_path):
    node, sibling = _seat(tmp_path), _seat(tmp_path, 'sibling')
    save_registry({p.name: {'root': str(p.root)} for p in [node, sibling]})
    save_aliases({'group': ['node', 'sibling']})
    _write_outbox('node', node.root, 'group', 'local copy', [])

    def publish(msg, sender, down):
        receive_envelope(json.dumps(msg).encode(), [node, sibling])
        return {'broker': ('sent', '')}

    route_outboxes([node], all_agents=[node, sibling], publish_remotes=publish,
                   configured_remote_ids=['broker'])
    assert _messages('node') == []
    assert [m['content'] for m in _messages('sibling')] == ['local copy']
