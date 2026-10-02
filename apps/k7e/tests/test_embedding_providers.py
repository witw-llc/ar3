"""Synthetic provider/cache counterexamples. All HTTP requests are mocked."""
import hashlib
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest
import config
import embeddings
import engine
import cli


@pytest.fixture
def openai(store, monkeypatch):
    monkeypatch.setenv('K7E_EMBEDDINGS', 'openai')
    monkeypatch.setenv('EMBED_MODEL', 'text-embedding-3-small')
    monkeypatch.setenv('K7E_EMBED_DIMENSIONS', '3')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-runtime-key')
    return store


def transport(monkeypatch, result=None, error=None):
    calls = []
    def request(req, timeout):
        calls.append((req, timeout))
        if error:
            raise error
        return io.BytesIO(json.dumps(result).encode())
    monkeypatch.setattr(embeddings.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=request))
    monkeypatch.setattr(embeddings.urllib.request, 'urlopen', request)
    return calls


def response(vector=None, model='text-embedding-3-small', index=0):
    return {'model': model, 'data': [{'index': index, 'embedding': vector if vector is not None else [1.0, 0.0, 0.0]}]}


def semantic(query='synthetic unrelated wording'):
    conn = engine._connect()
    try:
        return engine._search_embeddings(conn, query, 10)
    finally:
        conn.close()


def test_openai_request_and_runtime_only_credentials(openai, monkeypatch, capsys):
    calls = transport(monkeypatch, response())
    assert engine.embed_text('SYNTHETIC garden', timeout=0.25) == [1., 0., 0.]
    req, timeout = calls[0]
    assert req.full_url == 'https://api.openai.com/v1/embeddings'
    assert req.get_header('Authorization') == 'Bearer synthetic-runtime-key'
    assert json.loads(req.data) == {'model': 'text-embedding-3-small', 'input': 'SYNTHETIC garden', 'dimensions': 3, 'encoding_format': 'float'}
    assert timeout == 0.25
    assert config.load_config() == {}
    assert capsys.readouterr() == ('', '')


def test_ollama_default_keeps_request_shape(store, monkeypatch):
    monkeypatch.delenv('K7E_EMBEDDINGS')
    monkeypatch.delenv('EMBED_MODEL', raising=False)
    monkeypatch.delenv('K7E_EMBED_DIMENSIONS', raising=False)
    calls = transport(monkeypatch, {'embeddings': [[1., 0.]]})
    assert engine.embed_text('SYNTHETIC') == [1., 0.]
    req, timeout = calls[0]
    assert req.full_url.endswith('/api/embed')
    assert req.get_header('Authorization') is None
    assert json.loads(req.data) == {'model': 'nomic-embed-text', 'input': 'SYNTHETIC'}


@pytest.mark.parametrize('model,dimensions,valid', [
    ('text-embedding-3-small', None, 1536), ('text-embedding-3-large', None, 3072),
    ('text-embedding-3-large', '1536', 1536), ('text-embedding-3-small', '0', None),
    ('text-embedding-3-small', '1537', None), ('text-embedding-3-large', '3073', None),
    ('text-embedding-3-small', '3.0', None), ('nomic-embed-text', '3', None),
])
def test_model_dimensions_are_validated_without_network(openai, monkeypatch, model, dimensions, valid):
    monkeypatch.setenv('EMBED_MODEL', model)
    if dimensions is None:
        monkeypatch.delenv('K7E_EMBED_DIMENSIONS')
    else:
        monkeypatch.setenv('K7E_EMBED_DIMENSIONS', dimensions)
    selected = embeddings.space()
    assert (selected.dimensions if selected else None) == valid


def test_missing_key_and_invalid_provider_do_not_open_network(openai, monkeypatch):
    calls = transport(monkeypatch, response())
    monkeypatch.delenv('OPENAI_API_KEY')
    assert engine.embed_text('SYNTHETIC') is None
    monkeypatch.setenv('K7E_EMBEDDINGS', 'unknown-provider')
    assert not engine._embeddings_enabled()
    assert engine.embed_text('SYNTHETIC') is None
    assert calls == []


@pytest.mark.parametrize('bad', [response([1., 0.]), response([0., 0., 0.]), response([True, 0., 0.]),
    response([float('nan'), 0., 0.]), response([float('inf'), 0., 0.]), response([1e100, 0., 0.]),
    response(model='text-embedding-3-large'), response(index=1), {'data': []}, [], {'data': [None]}])
def test_malformed_responses_fail_closed(openai, monkeypatch, bad):
    transport(monkeypatch, bad)
    assert engine.embed_text('SYNTHETIC') is None


@pytest.mark.parametrize('code', [401, 403, 429, 500])
def test_api_failures_keep_backlog_and_keyword_search(openai, monkeypatch, capsys, code):
    calls = transport(monkeypatch, error=urllib.error.HTTPError('https://api.openai.com', code, 'SYNTHETIC secret body', {}, None))
    nid = engine.store_entry('Synthetic garden', 'Synthetic garden rule', tags=['synthetic'])
    assert engine.process_pending_embeddings() == 0
    assert engine.pending_embedding_count() == 1
    assert engine.search('synthetic garden')[0]['id'] == nid
    assert engine.LAST_QUERY_EMBED_OK is False
    assert calls
    output = capsys.readouterr()
    assert 'synthetic-runtime-key' not in output.out + output.err
    assert 'secret body' not in output.out + output.err


def test_no_redirect_can_forward_bearer(openai):
    assert embeddings._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid') is None


def test_cache_records_exact_space_input_and_query_budget(openai, monkeypatch):
    calls = transport(monkeypatch, response())
    nid = engine.store_entry('Synthetic garden', 'Undecided synthetic garden.', tags=['synthetic'])
    assert engine.process_pending_embeddings() == 1
    assert calls[0][1] == engine.EMBED_TIMEOUT
    conn = engine._connect()
    row = conn.execute('SELECT provider, model, dimensions, text_hash, text_version FROM embeddings WHERE node_id = ?', (nid,)).fetchone()
    conn.close()
    assert row[:3] == ('openai', 'text-embedding-3-small', 3)
    assert row[3] == hashlib.sha256(json.loads(calls[0][0].data)['input'].encode()).hexdigest()
    assert row[4] == embeddings.TEXT_VERSION
    assert semantic()[0][0] == nid
    assert calls[-1][1] == engine.QUERY_EMBED_TIMEOUT
    assert len(calls) == 2  # read embeds only the query


def test_raw_input_cache_refreshes_once_after_explicit_reindex(openai, monkeypatch):
    calls = transport(monkeypatch, response())
    title, content = 'Synthetic garden', 'Synthetic garden note'
    nid = engine.store_entry(title, content)
    node_path = engine._node_path(nid)
    node_bytes = node_path.read_bytes()
    raw_input = f'{title} {content}'
    canonical_input = engine._embedding_text(node_bytes.decode())
    assert canonical_input != raw_input
    assert embeddings.TEXT_VERSION == 'title-body-500-reserved-headings-v2'

    # A derived index from a raw-content writer has matching raw cache identity.
    conn = engine._connect()
    try:
        conn.execute('UPDATE nodes SET embedding_input_hash = ? WHERE id = ?',
                     (hashlib.sha256(raw_input.encode()).hexdigest(), nid))
        conn.execute('UPDATE nodes_fts SET content = ? WHERE rowid = '
                     '(SELECT rowid FROM nodes WHERE id = ?)', (content, nid))
        assert engine._save_embedding(conn, nid, raw_input, [1., 0., 0.],
                                      embeddings.space(), '2026-01-01')
        conn.commit()
    finally:
        conn.close()
    assert engine.process_pending_embeddings() == 0
    assert engine.embedding_coverage() == {'total': 1, 'current': 1, 'pending': 0}
    assert calls == []

    engine.reindex()
    assert engine.embedding_coverage() == {'total': 1, 'current': 0, 'pending': 1}
    assert calls == []
    assert engine.process_pending_embeddings() == 1
    assert json.loads(calls[0][0].data)['input'] == canonical_input
    assert engine.embedding_coverage() == {'total': 1, 'current': 1, 'pending': 0}
    engine.reindex()
    assert engine.process_pending_embeddings() == 0
    assert len(calls) == 1
    assert node_path.read_bytes() == node_bytes


def test_pending_raw_index_aligns_hash_without_rewriting_fts(openai, monkeypatch):
    calls = transport(monkeypatch, response())
    title, content = 'Synthetic garden', 'Synthetic garden note'
    nid = engine.store_entry(title, content)
    raw_hash = hashlib.sha256(f'{title} {content}'.encode()).hexdigest()
    expected_input = engine._embedding_text(engine._node_path(nid).read_text())
    conn = engine._connect()
    try:
        conn.execute('UPDATE nodes SET embedding_input_hash = ? WHERE id = ?', (raw_hash, nid))
        conn.commit()
        assert engine.process_pending_embeddings() == 1
        assert engine.embedding_coverage() == {'total': 1, 'current': 1, 'pending': 0}
        assert conn.execute('SELECT content FROM nodes_fts WHERE rowid = '
                            '(SELECT rowid FROM nodes WHERE id = ?)', (nid,)).fetchone()[0] == content
        assert json.loads(calls[0][0].data)['input'] == expected_input
        engine.reindex()
        assert engine.process_pending_embeddings() == 0
        assert len(calls) == 1
    finally:
        conn.close()


@pytest.mark.parametrize('change', ['append', 'reindex', 'append-and-reindex'])
def test_source_change_during_request_cannot_bless_stale_vector(openai, monkeypatch, change):
    nid = engine.store_entry('Synthetic garden', 'Original synthetic content')
    calls = []

    def embed(text, **kwargs):
        calls.append(text)
        if len(calls) == 1:
            if 'append' in change:
                engine.append_entry(nid, 'Edge Cases', 'New substantive synthetic content')
            if 'reindex' in change:
                engine.reindex()
        return [1., 0., 0.]

    monkeypatch.setattr(engine, 'embed_text', embed)
    changed = 'append' in change
    assert engine.process_pending_embeddings() == (0 if changed else 1)
    assert engine.embedding_coverage() == {'total': 1, 'current': 0 if changed else 1,
                                          'pending': 1 if changed else 0}
    current_input = engine._embedding_text(engine._node_path(nid).read_text())
    conn = engine._connect()
    try:
        assert conn.execute('SELECT embedding_input_hash FROM nodes WHERE id = ?', (nid,)).fetchone()[0] == hashlib.sha256(current_input.encode()).hexdigest()
        assert conn.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0] == (0 if changed else 1)
    finally:
        conn.close()
    if changed:
        assert calls[0] != current_input
        assert engine.process_pending_embeddings() == 1
        assert calls[-1] == current_input
    engine.reindex()
    assert engine.process_pending_embeddings() == 0
    assert len(calls) == (2 if changed else 1)
    assert engine.embedding_coverage() == {'total': 1, 'current': 1, 'pending': 0}


def test_legacy_hash_alignment_defers_to_a_concurrent_append(openai, monkeypatch):
    nid = engine.store_entry('Synthetic garden', 'Original synthetic content')
    raw_hash = hashlib.sha256(b'Synthetic garden Original synthetic content').hexdigest()
    conn = engine._connect()
    conn.execute('UPDATE nodes SET embedding_input_hash = ? WHERE id = ?', (raw_hash, nid))
    conn.commit()
    conn.close()
    canonical = engine._embedding_text
    changed = False

    def append_after_read(text):
        nonlocal changed
        result = canonical(text)
        if not changed:
            changed = True
            engine.append_entry(nid, 'Edge Cases', 'New substantive synthetic content')
        return result

    monkeypatch.setattr(engine, '_embedding_text', append_after_read)
    calls = transport(monkeypatch, response())
    assert engine.process_pending_embeddings() == 0
    assert calls == []
    assert engine.embedding_coverage() == {'total': 1, 'current': 0, 'pending': 1}
    assert engine.process_pending_embeddings() == 1
    assert len(calls) == 1
    assert 'New substantive synthetic content' in json.loads(calls[0][0].data)['input']
    assert engine.embedding_coverage() == {'total': 1, 'current': 1, 'pending': 0}


def test_second_request_does_not_hold_the_previous_vector_write_lock(openai, monkeypatch):
    nodes = {title: engine.store_entry(title, title + ' original content')
             for title in ('Synthetic first', 'Synthetic second')}
    calls = []

    def embed(text, **kwargs):
        calls.append(text)
        if len(calls) == 2:
            title = next(title for title in nodes if text.startswith(title + ' '))
            engine.append_entry(nodes[title], 'Edge Cases', 'Concurrent second-request update')
        return [1., 0., 0.]

    monkeypatch.setattr(engine, 'embed_text', embed)
    assert engine.process_pending_embeddings() == 1
    assert len(calls) == 2
    assert engine.embedding_coverage() == {'total': 2, 'current': 1, 'pending': 1}
    assert engine.process_pending_embeddings() == 1
    assert 'Concurrent second-request update' in calls[-1]
    engine.reindex()
    assert engine.process_pending_embeddings() == 0
    assert len(calls) == 3
    assert engine.embedding_coverage() == {'total': 2, 'current': 2, 'pending': 0}


@pytest.mark.parametrize('stale_request', [1, 2])
def test_stale_response_does_not_stop_the_remaining_backlog(openai, monkeypatch, stale_request):
    nodes = {title: engine.store_entry(title, title + ' original content')
             for title in ('Synthetic first', 'Synthetic second', 'Synthetic third')}
    calls = []

    def embed(text, **kwargs):
        calls.append(text)
        if len(calls) == stale_request:
            title = next(title for title in nodes if text.startswith(title + ' '))
            engine.append_entry(nodes[title], 'Edge Cases', 'Changed during the request')
        return [1., 0., 0.]

    monkeypatch.setattr(engine, 'embed_text', embed)
    assert engine.process_pending_embeddings() == 2
    assert len(calls) == 3
    assert engine.embedding_coverage() == {'total': 3, 'current': 2, 'pending': 1}
    assert engine.process_pending_embeddings() == 1
    assert len(calls) == 4
    assert 'Changed during the request' in calls[-1]
    engine.reindex()
    assert engine.process_pending_embeddings() == 0
    assert len(calls) == 4
    assert engine.embedding_coverage() == {'total': 3, 'current': 3, 'pending': 0}


@pytest.mark.parametrize('failure', ['unavailable', 'empty', 'nan', 'zero', 'underflow', 'model-change'])
def test_unusable_provider_result_still_stops_the_backlog(openai, monkeypatch, failure):
    for title in ('Synthetic first', 'Synthetic second', 'Synthetic third'):
        engine.store_entry(title, title + ' original content')
    calls = []

    def embed(text, **kwargs):
        calls.append(text)
        if len(calls) != 1:
            return [1., 0., 0.]
        if failure == 'model-change':
            monkeypatch.setenv('EMBED_MODEL', 'text-embedding-3-large')
            return [1., 0., 0.]
        return {'unavailable': None, 'empty': [], 'nan': [float('nan'), 0., 0.],
                'zero': [0., 0., 0.], 'underflow': [1e-50, 0., 0.]}[failure]

    monkeypatch.setattr(engine, 'embed_text', embed)
    assert engine.process_pending_embeddings() == 0
    assert len(calls) == 1
    assert engine.embedding_coverage() == {'total': 3, 'current': 0, 'pending': 3}


@pytest.mark.parametrize('replacement_time', ['before-read', 'after-read'])
def test_archive_replacement_never_changes_the_classified_input_snapshot(
    openai, monkeypatch, replacement_time,
):
    nid = engine.store_entry('Synthetic current', 'Synthetic operational statement')
    path = engine._node_path(nid)
    original = path.read_text()
    archive_marker = 'Synthetic archive-only statement'
    archive = original.replace('status: active', 'status: active\nkind: observation')
    archive = archive.replace('Synthetic operational statement', archive_marker)
    read_text = type(path).read_text
    reads = []

    def replace_during_read(p, *args, **kwargs):
        if p != path:
            return read_text(p, *args, **kwargs)
        reads.append(p)
        if replacement_time == 'before-read':
            engine.atomic_write_text(path, archive)
        snapshot = read_text(p, *args, **kwargs)
        if replacement_time == 'after-read':
            engine.atomic_write_text(path, archive)
        return snapshot

    monkeypatch.setattr(type(path), 'read_text', replace_during_read)
    calls = transport(monkeypatch, response())
    assert engine.process_pending_embeddings() == (0 if replacement_time == 'before-read' else 1)
    assert len(reads) == 1
    assert len(calls) == (0 if replacement_time == 'before-read' else 1)
    for request, _ in calls:
        assert json.loads(request.data)['input'] == engine._embedding_text(original)
        assert archive_marker not in json.loads(request.data)['input']


@pytest.mark.parametrize('status', ['superseded', 'compiled'])
def test_explicit_rebuild_retains_untyped_historical_vectors(openai, monkeypatch, status):
    calls = transport(monkeypatch, response())
    historical = engine.store_entry('Synthetic historical', 'An earlier synthetic procedure')
    current = engine.store_entry('Synthetic current', 'The current synthetic procedure')
    archived = engine.store_entry('Synthetic archive', 'Archive-only synthetic statement', kind='observation')
    path = engine._node_path(historical)
    path.write_text(path.read_text().replace('status: active', f'status: {status}'))
    rendered = {nid: engine._node_path(nid).read_bytes() for nid in (historical, current, archived)}
    engine.reindex(embeddings=True)
    assert len(calls) == 2
    assert all('Archive-only' not in json.loads(request.data)['input'] for request, _ in calls)
    conn = engine._connect()
    try:
        assert {row[0] for row in conn.execute('SELECT node_id FROM embeddings')} == {historical, current}
        assert {row[0] for row in engine._search_embeddings(conn, 'Synthetic query', 10,
                                                          include_superseded=True)} == {historical, current}
        assert {row[0] for row in engine._search_embeddings(conn, 'Synthetic query', 10)} == {current}
    finally:
        conn.close()
    assert all(engine._node_path(nid).read_bytes() == text for nid, text in rendered.items())


@pytest.mark.parametrize('column,value', [('provider', 'ollama'), ('model', 'another-model'),
    ('dimensions', 2), ('text_version', 'future-format'), ('text_hash', 'stale')])
def test_same_length_incompatible_or_stale_cache_is_not_scored(openai, monkeypatch, column, value):
    transport(monkeypatch, response())
    engine.store_entry('Synthetic garden', 'Synthetic note', tags=['synthetic'])
    engine.process_pending_embeddings()
    conn = engine._connect(); conn.execute(f'UPDATE embeddings SET {column} = ?', (value,)); conn.commit(); conn.close()
    assert semantic() == []


def test_edit_invalidates_old_input_hash_until_backlog_refresh(openai, monkeypatch):
    transport(monkeypatch, response())
    nid = engine.store_entry('Synthetic garden', 'Synthetic note', tags=['synthetic'])
    engine.process_pending_embeddings()
    engine.append_entry(nid, 'Synthetic update', 'A different synthetic option')
    assert semantic() == []
    assert engine.process_pending_embeddings() == 1
    assert semantic()[0][0] == nid


def test_provider_switch_is_pending_until_backlog_refresh(openai, monkeypatch):
    transport(monkeypatch, response())
    nid = engine.store_entry('Synthetic garden', 'Synthetic note', tags=['synthetic'])
    engine.process_pending_embeddings()
    monkeypatch.setenv('K7E_EMBEDDINGS', 'ollama')
    monkeypatch.setattr(engine, 'embed_text', lambda *a, **k: [1., 0., 0.])
    assert semantic() == []  # same dimensions do not make a common vector space
    assert engine.pending_embedding_count() == 1
    assert engine.process_pending_embeddings() == 1
    assert semantic()[0][0] == nid


def test_off_switch_also_blocks_explicit_embedding_rebuild(openai, monkeypatch):
    calls = transport(monkeypatch, response())
    engine.store_entry('Synthetic garden', 'Synthetic note', tags=['synthetic'])
    monkeypatch.setenv('K7E_EMBEDDINGS', 'off')
    engine.reindex(embeddings=True)
    assert engine.pending_embedding_count() == 0
    assert calls == []


def test_status_never_probes_paid_api_or_prints_key(openai, monkeypatch):
    calls = transport(monkeypatch, response())
    text = config.status()
    assert 'API access unverified' in text
    assert 'synthetic-runtime-key' not in text
    assert 'ollama pull' not in text
    assert calls == []


def test_cli_refuses_persisting_or_displaying_runtime_key(openai, capsys):
    assert cli.main(['config', 'openai_api_key', 'synthetic-sensitive-value']) == 1
    assert config.load_config() == {}
    assert 'synthetic-sensitive-value' not in ''.join(capsys.readouterr())


@pytest.mark.parametrize('error', [TimeoutError('synthetic timeout'), urllib.error.URLError('synthetic unreachable')])
def test_transport_failure_returns_none_without_output(openai, monkeypatch, capsys, error):
    transport(monkeypatch, error=error)
    assert engine.embed_text('SYNTHETIC') is None
    assert capsys.readouterr() == ('', '')


def test_oversized_and_non_json_responses_are_rejected(openai, monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(b'X' * (embeddings._RESPONSE_BYTES + 1))
    monkeypatch.setattr(embeddings.urllib.request, 'build_opener', lambda *args: Opener())
    assert engine.embed_text('SYNTHETIC') is None
    monkeypatch.setattr(embeddings, '_RESPONSE_BYTES', 100)
    assert engine.embed_text('SYNTHETIC') is None


@pytest.mark.parametrize('key,value', [
    ('K7E_EMBEDDINGS', 'ollama'),
    ('EMBED_MODEL', 'text-embedding-3-large'),
    ('K7E_EMBED_DIMENSIONS', '2'),
])
def test_configuration_change_during_request_cannot_mislabel_cache(openai, monkeypatch, key, value):
    engine.store_entry('Synthetic garden', 'Synthetic note', tags=['synthetic'])
    def change(*args, **kwargs):
        monkeypatch.setenv(key, value)
        return [1., 0., 0.]
    monkeypatch.setattr(engine, 'embed_text', change)
    assert engine.process_pending_embeddings() == 0
    assert engine.pending_embedding_count() == 1
    conn = engine._connect(); assert conn.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0] == 0; conn.close()


def test_archive_records_still_never_enter_embedding_backlog(openai, monkeypatch):
    calls = transport(monkeypatch, response())
    engine.store_entry('Synthetic garden option', 'Synthetic undecided idea', kind='idea', tags=['synthetic'])
    assert engine.pending_embedding_count() == 0
    engine.reindex(embeddings=True)
    assert calls == []


@pytest.mark.parametrize('column,value', [('provider', None), ('model', 'another-model'),
    ('dimensions', 2), ('text_version', 'other-format'), ('text_hash', 'stale')])
def test_incompatible_vectors_are_reported_and_rederived(openai, monkeypatch, column, value):
    transport(monkeypatch, response())
    nid = engine.store_entry('Synthetic garden', 'Synthetic note')
    assert engine.process_pending_embeddings() == 1
    conn = engine._connect()
    conn.execute(f'UPDATE embeddings SET {column} = ?', (value,))
    conn.commit(); conn.close()
    assert engine.embedding_coverage() == {'total': 1, 'current': 0, 'pending': 1}
    assert '0/1 current; 1 pending' in config.status()
    assert engine.pending_embedding_count() == 1
    assert engine.process_pending_embeddings() == 1
    assert engine.embedding_coverage() == {'total': 1, 'current': 1, 'pending': 0}
    assert semantic()[0][0] == nid


def test_new_provider_covers_entries_written_with_embeddings_off(openai, monkeypatch):
    transport(monkeypatch, response())
    monkeypatch.setenv('K7E_EMBEDDINGS', 'off')
    engine.store_entry('Synthetic garden', 'Synthetic note')
    engine.store_entry('Synthetic archive', 'A source speculated', kind='idea')
    monkeypatch.setenv('K7E_EMBEDDINGS', 'openai')
    assert engine.pending_embedding_count() == 1
    assert engine.process_pending_embeddings() == 1
    assert engine.embedding_coverage() == {'total': 1, 'current': 1, 'pending': 0}


def test_query_compares_precomputed_input_hash_in_sql(openai, monkeypatch):
    transport(monkeypatch, response())
    engine.store_entry('Synthetic garden', 'Synthetic note')
    engine.process_pending_embeddings()
    monkeypatch.setattr(engine, '_embedding_text', lambda *a: pytest.fail('Query must not load and hash every body'))
    assert semantic()


@pytest.mark.parametrize('bad', ['nan', 'inf', 'zero', 'text'])
def test_corrupt_current_payload_is_pending_and_rederived(openai, monkeypatch, bad):
    import struct
    transport(monkeypatch, response())
    nid = engine.store_entry('Synthetic garden', 'Synthetic note')
    engine.process_pending_embeddings()
    vector = 'x' * 12 if bad == 'text' else struct.pack('3f', *({'nan': [float('nan'), 0, 0], 'inf': [float('inf'), 0, 0], 'zero': [0, 0, 0]}[bad]))
    conn = engine._connect()
    conn.execute('UPDATE embeddings SET vector = ?', (vector,)); conn.commit(); conn.close()
    assert engine.embedding_coverage() == {'total': 1, 'current': 0, 'pending': 1}
    assert engine.pending_embedding_count() == 1
    assert semantic() == []
    assert engine.process_pending_embeddings() == 1
    assert engine.embedding_coverage()['current'] == 1
    assert semantic()[0][0] == nid



def test_float32_underflow_stays_pending(openai, monkeypatch):
    transport(monkeypatch, response(vector=[1e-50, 0.0, 0.0]))
    engine.store_entry('Synthetic garden', 'Synthetic note')
    assert engine.process_pending_embeddings() == 0
    assert engine.embedding_coverage() == {'total': 1, 'current': 0, 'pending': 1}
