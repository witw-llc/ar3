"""Conversation attachment outcomes belong to the recipient that got the copy."""
import json
import threading

import pytest

import convo
import network
from ar3.ulid import new
from commands import cmd_convo
from core import Participant
from mailbox import _write_outbox, route_outboxes
from registry import save_aliases, save_registry
from services import StorageError
from settings import set_setting


@pytest.fixture
def seats(fake_home, tmp_path):
    parts = []
    for name in ['Sender', 'Blue', 'Coral']:
        root = tmp_path / name
        root.mkdir()
        parts.append(Participant(name, root))
    save_registry({p.name: {'root': str(p.root)} for p in parts})
    save_aliases({'group': ['Blue', 'Coral']})
    set_setting('storage_receive_wait_seconds', 0)
    return parts


def _message():
    return {'id': new(), 'from': 'Sender', 'to': 'group', 'content': 'two files',
            'files': [{'filename': name, 'storage': ['owned://' + name]}
                      for name in ['a.txt', 'b.txt']]}


class SplitStore:
    id = 'split'

    def retrieve(self, url, dest):
        if 'Coral' in dest.parts and url.endswith('a.txt'):
            raise StorageError('unavailable on this copy')
        dest.write_bytes(b'actual bytes')
        return True


def _receive(parts, msg=None, service=None):
    msg = _message() if msg is None else msg
    network.receive_envelope(json.dumps(msg).encode(), parts, services=[service or SplitStore()])
    return msg


def _assert_views(capsys):
    for name, unavailable in [('blue', []), ('CORAL', ['a.txt'])]:
        capsys.readouterr()
        assert cmd_convo([name, '--json']) == 0
        row = json.loads(capsys.readouterr().out)
        assert sorted(row['files']) == ['a.txt', 'b.txt']
        assert [item['filename'] for item in row['files_unavailable']] == unavailable
        assert 'files_outcome_unrecorded' not in row
        assert cmd_convo([name]) == 0
        text = capsys.readouterr().out
        assert ('ATTACHMENT UNAVAILABLE: a.txt' in text) == bool(unavailable)
        assert 'ATTACHMENT UNAVAILABLE: b.txt' not in text
    for name in ['Sender', 'group']:
        entry = convo.load_agent_entries(name, limit=1)[0]
        assert not entry.get('files_unavailable')
        assert entry['files_outcome_unrecorded'] == ['a.txt', 'b.txt']


def test_real_receive_fanout_projects_actual_copies(seats, capsys):
    _receive(seats)
    assert len(convo.load_entries()) == 1
    _assert_views(capsys)


def test_later_recipient_does_not_change_earlier_success(seats, monkeypatch, capsys):
    started, release = threading.Event(), threading.Event()

    class Deferred(SplitStore):
        failures = 0

        def retrieve(self, url, dest):
            if 'Coral' in dest.parts and url.endswith('a.txt'):
                self.failures += 1
                if self.failures > 1:
                    started.set()
                    assert release.wait(5)
            return super().retrieve(url, dest)

    monkeypatch.setattr(network, '_receive_wait_seconds', lambda: 1)
    try:
        _receive(seats, service=Deferred())
        assert started.wait(5)
        before = convo.load_agent_entries('Blue', limit=1)[0]
        assert not before.get('files_unavailable')
        assert not convo.load_agent_entries('Coral', limit=1)
    finally:
        release.set()
        network.drain_attachment_waits(timeout_s=5)
    after = convo.load_agent_entries('Blue', limit=1)[0]
    assert not after.get('files_unavailable')
    assert len(convo.load_entries()) == 1
    _assert_views(capsys)


@pytest.mark.parametrize('name,lost', [('Blue', False), ('Coral', True)])
def test_follow_projects_new_receive_rows(seats, monkeypatch, capsys, name, lost):
    convo.record({'id': new(), 'from': 'Sender', 'to': name, 'content': 'seed'}, recipients=[name])
    calls = 0

    def poll(_):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise KeyboardInterrupt
        _receive(seats)
        capsys.readouterr()

    monkeypatch.setattr('convo.time.sleep', poll)
    with pytest.raises(KeyboardInterrupt):
        convo.follow_conversation(name, limit=0)
    text = capsys.readouterr().out
    assert 'two files' in text
    assert ('ATTACHMENT UNAVAILABLE: a.txt' in text) == lost
    assert 'ATTACHMENT UNAVAILABLE: b.txt' not in text


def test_duplicate_records_preserve_first_canonical_recipient_outcome(seats):
    msg = _receive(seats)
    convo.record({**msg, 'files': [{'filename': 'a.txt', 'error': 'WRONG'}]}, recipients=[' BLUE '])
    convo.record({**msg, 'files': [{'filename': 'a.txt'}]}, recipients=['coral'])
    assert not convo.load_agent_entries('blue', limit=1)[0].get('files_unavailable')
    assert convo.load_agent_entries('CORAL', limit=1)[0]['files_unavailable'][0]['filename'] == 'a.txt'
    assert len(convo.load_entries()) == 1


def test_legacy_rows_are_preserved_and_explicitly_unrecorded(seats, capsys):
    msg = _message()
    convo.record(msg, recipients=['Blue'])
    with convo._connect() as conn:
        raw = json.loads(conn.execute('SELECT entry_json FROM messages').fetchone()[0])
        raw.pop('attachment_outcomes')
        raw['files_unavailable'] = [{'filename': 'a.txt', 'detail': 'old global outcome'}]
        legacy = json.dumps(raw)
        conn.execute('UPDATE messages SET entry_json = ?', (legacy,))
    capsys.readouterr()
    assert cmd_convo(['Blue', '--json']) == 0
    row = json.loads(capsys.readouterr().out)
    assert row['files_unavailable'] == []
    assert row['files_outcome_unrecorded'] == ['a.txt', 'b.txt']
    assert cmd_convo(['Blue']) == 0
    text = capsys.readouterr().out
    assert 'ATTACHMENT OUTCOME UNRECORDED: a.txt' in text
    assert 'ATTACHMENT UNAVAILABLE:' not in text
    with convo.open_for_read() as conn:
        assert conn.execute('SELECT entry_json FROM messages').fetchone()[0] == legacy
    convo.record(msg, recipients=['Coral'])
    assert convo.load_agent_entries('Blue', limit=1)[0]['files_outcome_unrecorded']
    assert not convo.load_agent_entries('Coral', limit=1)[0].get('files_outcome_unrecorded')
    assert convo.load_entries()[0]['files_unavailable'] == raw['files_unavailable']


def test_local_fanout_records_only_committed_recipients(seats, monkeypatch):
    sender = seats[0]
    payload = sender.root / 'local.txt'
    payload.write_text('bytes')
    _write_outbox(sender.name, sender.root, 'group', 'local', [], attachment_sources=[payload])
    import mailbox
    real = mailbox._commit_staged_inboxes

    def commit(staged):
        keep = [item for item in staged if item[0].name == 'Blue']
        real(keep)
        return {'Coral': 'simulated inbox failure'}

    monkeypatch.setattr(mailbox, '_commit_staged_inboxes', commit)
    route_outboxes([sender], all_agents=seats)
    assert not convo.load_agent_entries('Blue', limit=1)[0].get('files_outcome_unrecorded')
    assert not convo.load_agent_entries('Coral', limit=1)
    assert convo.load_agent_entries('group', limit=1)[0]['files_outcome_unrecorded'] == ['local.txt']


def test_remote_publish_is_not_recipient_delivery(seats):
    sender = seats[0]
    _write_outbox(sender.name, sender.root, 'Elsewhere', 'sent', [{'filename': 'x', 'error': 'upload failed'}])
    # No attachment upload is needed to prove the publication record contract.
    def publish(msg, sender_name, down):
        return {'broker': ('sent', '')}

    import mailbox
    from unittest.mock import patch
    with patch.object(mailbox, '_upload_files_for_remote', return_value=''):
        route_outboxes([sender], all_agents=seats, publish_remotes=publish,
                       configured_remote_ids=['broker'], services=[SplitStore()])
    for name in ['Sender', 'Elsewhere']:
        row = convo.load_agent_entries(name, limit=1)[0]
        assert row['files_outcome_unrecorded'] == ['x']
        assert not row.get('files_unavailable')


def test_local_copy_failure_is_not_archived_as_received(seats, monkeypatch):
    sender = seats[0]
    payload = sender.root / 'local.txt'
    payload.write_text('bytes')
    _write_outbox(sender.name, sender.root, 'group', 'local', [], attachment_sources=[payload])
    import mailbox
    real = mailbox.shutil.copyfile

    def copy(src, dest, *args, **kwargs):
        if 'Coral' in dest.parts:
            raise OSError('simulated copy failure')
        return real(src, dest, *args, **kwargs)

    monkeypatch.setattr(mailbox.shutil, 'copyfile', copy)
    route_outboxes([sender], all_agents=seats)
    blue = convo.load_agent_entries('Blue', limit=1)[0]
    coral = convo.load_agent_entries('Coral', limit=1)[0]
    assert blue['files'] == coral['files'] == ['local.txt']
    assert not blue.get('files_unavailable')
    assert coral['files_unavailable'] == [{'filename': 'local.txt', 'detail': 'not delivered to this recipient'}]
