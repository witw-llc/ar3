"""A backend failure cannot turn its attachment URL into an HTML download."""
from io import BytesIO
from urllib.error import HTTPError

import pytest

from core import Participant
from mailbox import _attempt_download_file
from services.file_sync import FileSyncService
from services.tempfile_org import TempFileOrgService
from services.webdav import WebdavService


def test_tempfile_api_failure_never_downloads_the_viewer(fake_home, tmp_path, monkeypatch):
    calls = []
    url = 'https://tempfile.org/opaque/'

    def request(req, **kwargs):
        calls.append(req.full_url)
        if req.full_url.endswith('/download'):
            raise HTTPError(req.full_url, 503, 'Unavailable', {}, None)
        return BytesIO(b'<html>viewer</html>')

    monkeypatch.setattr('urllib.request.urlopen', request)
    service = TempFileOrgService('temp', url='https://tempfile.org')
    dest = tmp_path / 'attachment.bin'
    assert not _attempt_download_file(
        Participant('receiver', tmp_path), dest, dest.name, [url], [service], 1000,
        announce=False,
    )
    assert calls == [url + 'download']
    assert not dest.exists()


@pytest.mark.parametrize('kind', ['file_sync', 'webdav'])
def test_claimed_missing_file_never_falls_back(fake_home, tmp_path, monkeypatch, kind):
    base = 'https://files.example/attachments'
    if kind == 'file_sync':
        service = FileSyncService('sync', url=tmp_path.as_uri(), base_url=base)
    else:
        service = WebdavService('dav', url='webdav://private.example/dav', base_url=base)

    def missing(req, **kwargs):
        raise HTTPError(req.full_url, 404, 'Missing', {}, None)

    def forbidden(*args, **kwargs):
        pytest.fail('generic GET used for a claimed URL')

    monkeypatch.setattr('urllib.request.urlopen', missing)
    monkeypatch.setattr('services.http_get.http_get_url_to_path', forbidden)
    dest = tmp_path / 'attachment.bin'
    assert not _attempt_download_file(
        Participant('receiver', tmp_path), dest, dest.name,
        [base + '/bundle/file.bin'], [service], 1000, announce=False,
    )


@pytest.mark.parametrize('alternative', ['service', 'url'])
def test_claimed_failure_allows_other_copies(fake_home, tmp_path, monkeypatch, alternative):
    from services import StorageError

    class Failed:
        id = 'failed'

        def retrieve(self, url, dest):
            if url == 'https://unclaimed.example/file':
                return False
            raise StorageError('claimed but unavailable')

    class Working:
        id = 'working'

        def retrieve(self, url, dest):
            dest.write_bytes(b'actual file')
            return True

    fetched = []

    def fetch(url, dest, **kwargs):
        fetched.append(url)
        assert url == 'https://unclaimed.example/file'
        dest.write_bytes(b'actual file')
        return True

    monkeypatch.setattr('services.http_get.http_get_url_to_path', fetch)
    urls = ['https://claimed.example/file']
    services = [Failed()]
    if alternative == 'service':
        services.append(Working())
    else:
        urls.append('https://unclaimed.example/file')
    dest = tmp_path / 'attachment.bin'
    assert _attempt_download_file(
        Participant('receiver', tmp_path), dest, dest.name, urls, services, 1000,
        announce=False,
    )
    assert dest.read_bytes() == b'actual file'
    assert fetched == ([] if alternative == 'service' else [urls[-1]])
