"""Tests for the sync-folder storage service.

The thing under test is not a network client, it is a rendezvous through a
directory some other program is writing into concurrently. So the interesting
cases are all about a file that is present but not yet whole — the state a sync
client puts a folder in for a few seconds, and the state OneDrive's
Files On-Demand leaves a folder in indefinitely.
"""
from __future__ import annotations

import json

import pytest

from conftest import set_home

from services import StorageError
from services.sync_folder import (
    MANIFEST_NAME,
    SyncFolderService,
    marker_for,
    parse_marker,
)

MSG = "01KZAAAAAAAAAAAAAAAAAAAAAA"
OTHER_MSG = "01KZBBBBBBBBBBBBBBBBBBBBBB"


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "OneDrive - Contoso" / "A8S"
    root.mkdir(parents=True)
    return root


def _svc(folder, name="onedrive", **opts):
    return SyncFolderService(name, url=str(folder), **opts)


def _payload(tmp_path, name="hello.txt", body="payload bytes"):
    src = tmp_path / name
    src.write_text(body)
    return src


class TestConfigUrl:
    @pytest.mark.parametrize("url", ["/srv/sync", "~/OneDrive/A8S", "C:\\Sync\\A8S"])
    def test_a_bare_path_is_ours(self, url):
        assert SyncFolderService.supports_config_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "file:///srv/sync",  # file_sync, which publishes a base URL
            "webdav://host/dav",
            "s3://bucket/a8s",
            "https://tempfile.org",
            "rclone://gdrive/A8S",
            "",
        ],
    )
    def test_anything_with_a_scheme_belongs_to_somebody_else(self, url):
        assert SyncFolderService.supports_config_url(url) is False

    def test_a_relative_path_is_rejected_at_config_time(self, tmp_path):
        with pytest.raises(ValueError, match="local folder path"):
            SyncFolderService("x", url="OneDrive/A8S")

    def test_tilde_expands(self, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        svc = SyncFolderService("x", url="~/Sync")
        assert svc.store(_payload(tmp_path), msg_id=MSG)
        assert (tmp_path / "Sync" / MSG / "hello.txt").is_file()

    @pytest.mark.parametrize("bad", ["-1", "later"])
    def test_a_nonsense_retention_fails_now_not_at_daemon_start(self, folder, bad):
        with pytest.raises(ValueError, match="retain_days"):
            _svc(folder, retain_days=bad)

    def test_unknown_options_are_rejected(self, folder):
        with pytest.raises(ValueError, match="base_url"):
            _svc(folder, base_url="https://example.com")


class TestMarker:
    def test_round_trip(self):
        assert parse_marker(marker_for(MSG, "a.txt")) == (MSG, "a.txt")

    def test_a_space_survives(self):
        url = marker_for(MSG, "round trip.m4a")
        assert " " not in url
        assert parse_marker(url) == (MSG, "round trip.m4a")

    def test_the_marker_names_neither_the_service_nor_the_folder(self, folder, tmp_path):
        # The point of the design: nothing about the transport crosses the
        # wire, so the two machines only have to agree on the folder.
        url = _svc(folder, name="work-onedrive").store(_payload(tmp_path), msg_id=MSG)
        assert "onedrive" not in url.lower()
        assert str(folder) not in url

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/a.txt",
            "a8s+sync:not-a-ulid/a.txt",
            f"a8s+sync:{MSG}",
            f"a8s+sync:{MSG}/",
            # A traversal dressed up as a filename.
            f"a8s+sync:{MSG}/..%2F..%2Fetc%2Fpasswd",
        ],
    )
    def test_foreign_or_malformed_urls_are_not_ours(self, url):
        assert parse_marker(url) is None


class TestRoundTrip:
    def test_store_then_retrieve(self, folder, tmp_path):
        svc = _svc(folder)
        url = svc.store(_payload(tmp_path), msg_id=MSG)
        dest = tmp_path / "in" / "hello.txt"
        assert svc.retrieve(url, dest) is True
        assert dest.read_text() == "payload bytes"

    def test_attachments_of_one_message_stay_together(self, folder, tmp_path):
        svc = _svc(folder)
        svc.store(_payload(tmp_path, "a.txt"), msg_id=MSG)
        svc.store(_payload(tmp_path, "b.txt"), msg_id=MSG)
        bundle = folder / MSG
        assert {p.name for p in bundle.iterdir()} == {"a.txt", "b.txt", MANIFEST_NAME}
        # The second attachment must not erase the first one's entry.
        assert set(json.loads((bundle / MANIFEST_NAME).read_text())) == {
            "a.txt", "b.txt",
        }

    def test_a_prefix_nests_below_the_configured_folder(self, folder, tmp_path):
        svc = _svc(folder, prefix="a8s")
        url = svc.store(_payload(tmp_path), msg_id=MSG)
        assert (folder / "a8s" / MSG / "hello.txt").is_file()
        assert svc.retrieve(url, tmp_path / "in" / "hello.txt") is True

    def test_no_prefix_by_default(self, folder, tmp_path):
        # Unlike the published services, the operator has already pointed this
        # at a folder they made for a8s. A second level below it is noise.
        _svc(folder).store(_payload(tmp_path), msg_id=MSG)
        assert (folder / MSG).is_dir()

    def test_health_has_no_envelope_and_still_works(self, folder, tmp_path):
        svc = _svc(folder)
        url = svc.store(_payload(tmp_path, "probe.txt"))
        assert svc.retrieve(url, tmp_path / "in" / "probe.txt") is True
        assert svc.delete(url) is True

    def test_a_foreign_marker_is_declined_not_failed(self, folder, tmp_path):
        # False means "not mine, try the next service" — the whole basis of
        # configuring two sync folders.
        assert _svc(folder).retrieve(marker_for(MSG, "x.txt"), tmp_path / "x") is False


class TestPartialArrival:
    """Presence is not arrival."""

    def test_a_file_still_being_written_is_not_delivered(self, folder, tmp_path):
        svc = _svc(folder)
        url = svc.store(_payload(tmp_path, body="the whole thing"), msg_id=MSG)
        # The sync client has published the name with only some of the bytes.
        (folder / MSG / "hello.txt").write_text("the wh")
        assert svc.retrieve(url, tmp_path / "in" / "hello.txt") is False

    def test_a_zero_byte_placeholder_is_not_delivered(self, folder, tmp_path):
        svc = _svc(folder)
        url = svc.store(_payload(tmp_path), msg_id=MSG)
        (folder / MSG / "hello.txt").write_bytes(b"")
        assert svc.retrieve(url, tmp_path / "in" / "hello.txt") is False

    def test_a_file_with_no_manifest_entry_is_not_delivered(self, folder, tmp_path):
        # The bytes arrived before the manifest did. Nothing yet says how big
        # this file is supposed to be, so nothing here can vouch for it.
        svc = _svc(folder)
        (folder / MSG).mkdir()
        (folder / MSG / "hello.txt").write_text("payload bytes")
        assert svc.retrieve(marker_for(MSG, "hello.txt"), tmp_path / "in") is False

    def test_a_corrupt_manifest_withholds_rather_than_crashes(self, folder, tmp_path):
        svc = _svc(folder)
        url = svc.store(_payload(tmp_path), msg_id=MSG)
        (folder / MSG / MANIFEST_NAME).write_text("{not json")
        assert svc.retrieve(url, tmp_path / "in" / "hello.txt") is False

    def test_the_staging_name_is_never_the_published_name(self, folder, tmp_path):
        # A receiver looks for the final name only, so a `.part` file in the
        # folder can never be mistaken for a complete one.
        svc = _svc(folder)
        svc.store(_payload(tmp_path), msg_id=MSG)
        assert not any(p.name.endswith(".part") for p in (folder / MSG).iterdir())

    def test_a_later_complete_copy_is_delivered(self, folder, tmp_path):
        # What the receiver's poll loop is for: False now, True once the sync
        # client finishes.
        svc = _svc(folder)
        url = svc.store(_payload(tmp_path, body="the whole thing"), msg_id=MSG)
        target = folder / MSG / "hello.txt"
        target.write_text("the wh")
        assert svc.retrieve(url, tmp_path / "in" / "hello.txt") is False
        target.write_text("the whole thing")
        assert svc.retrieve(url, tmp_path / "in" / "hello.txt") is True


class TestDelete:
    def test_delete_takes_the_bundle_with_the_last_file(self, folder, tmp_path):
        svc = _svc(folder)
        url = svc.store(_payload(tmp_path), msg_id=MSG)
        assert svc.delete(url) is True
        assert not (folder / MSG).exists()

    def test_a_sibling_attachment_keeps_the_bundle(self, folder, tmp_path):
        svc = _svc(folder)
        url = svc.store(_payload(tmp_path, "a.txt"), msg_id=MSG)
        svc.store(_payload(tmp_path, "b.txt"), msg_id=MSG)
        assert svc.delete(url) is True
        assert (folder / MSG / "b.txt").is_file()
        assert json.loads((folder / MSG / MANIFEST_NAME).read_text()) .keys() == {
            "b.txt"
        }

    def test_deleting_a_foreign_url_is_a_quiet_no(self, folder):
        assert _svc(folder).delete("https://example.com/x") is False


class TestRetention:
    def test_off_by_default(self, folder, tmp_path):
        # A sync folder is shared, so a delete here is a delete on every
        # machine. Nobody should get that by accident.
        svc = _svc(folder)
        stale = folder / OTHER_MSG
        stale.mkdir()
        (stale / "old.txt").write_text("x")
        import os
        old = 1
        os.utime(stale, (old, old))
        svc.store(_payload(tmp_path), msg_id=MSG)
        assert stale.exists()

    def test_a_sweep_drops_bundles_past_the_window(self, folder, tmp_path):
        import os

        svc = _svc(folder, retain_days=30)
        stale = folder / OTHER_MSG
        stale.mkdir()
        (stale / "old.txt").write_text("x")
        os.utime(stale, (1, 1))
        svc.store(_payload(tmp_path), msg_id=MSG)
        assert not stale.exists()
        assert (folder / MSG).is_dir()  # the one just written survives

    def test_the_sweep_leaves_things_it_did_not_write(self, folder, tmp_path):
        import os

        svc = _svc(folder, retain_days=30)
        theirs = folder / "someone-elses-folder"
        theirs.mkdir()
        os.utime(theirs, (1, 1))
        svc.store(_payload(tmp_path), msg_id=MSG)
        assert theirs.exists()


class TestUnreachableFolder:
    def test_an_unwritable_folder_is_a_storage_error(self, tmp_path):
        # Which is what makes it survivable: the send falls through to the
        # other configured services instead of dying.
        svc = SyncFolderService("x", url=str(tmp_path / "nope" / "deep"))
        (tmp_path / "nope").write_text("this is a file, not a directory")
        with pytest.raises(StorageError):
            svc.store(_payload(tmp_path), msg_id=MSG)
