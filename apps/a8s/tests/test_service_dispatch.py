"""Tests for the storage-service dispatcher (#90) — `_build_service`,
`load_services`, `configured_service_ids`, `detect_service_kind`. The
TempFile.org-specific HTTP behavior lives in test_service_tempfile_org.py."""
from __future__ import annotations

import inspect
import sys

from network import (
    configured_service_ids,
    deps_group_for,
    detect_service_kind,
    load_network_config,
    load_services,
    save_network_config,
)


class TestNetworkConfigServices:
    def test_absent_file_includes_services(self, fake_home):
        cfg = load_network_config()
        assert cfg == {"remotes": {}, "services": {}}

    def test_round_trip(self, fake_home):
        save_network_config({
            "remotes": {},
            "services": {
                "tempfile": {"service": "tempfile_org", "url": "https://tempfile.org"},
            },
        })
        cfg = load_network_config()
        assert cfg["services"]["tempfile"]["service"] == "tempfile_org"

    def test_configured_service_ids_order_preserved(self, fake_home):
        save_network_config({"remotes": {}, "services": {"a": {}, "z": {}, "m": {}}})
        assert configured_service_ids() == ["a", "z", "m"]

    def test_non_dict_services_value_resets(self, fake_home):
        # A bad services value (string, list, etc.) gets treated as empty
        # rather than crashing the config loader.
        from core import network_config_path

        p = network_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"remotes": {}, "services": "not-a-dict"}')
        cfg = load_network_config()
        assert cfg["services"] == {}


class TestLoadServices:
    def test_unknown_kind_skipped(self, fake_home):
        save_network_config({
            "remotes": {},
            "services": {"weird": {"service": "telepathy", "url": "https://x"}},
        })
        # Should not raise; just skip the bad entry.
        assert load_services() == []

    def test_missing_url_skipped(self, fake_home):
        save_network_config({
            "remotes": {},
            "services": {"tempfile": {"service": "tempfile_org"}},
        })
        assert load_services() == []

    def test_unknown_option_skips_service(self, fake_home):
        # `_build_service` forwards unknown opts to the service constructor,
        # which raises ValueError — load_services catches and skips. Same
        # backstop pattern the remote dispatcher uses.
        save_network_config({
            "remotes": {},
            "services": {
                "tempfile": {
                    "service": "tempfile_org",
                    "url": "https://tempfile.org",
                    "boguskey": "x",
                }
            },
        })
        assert load_services() == []

    def test_valid_entry_loads(self, fake_home):
        save_network_config({
            "remotes": {},
            "services": {
                "tempfile": {"service": "tempfile_org", "url": "https://tempfile.org"},
            },
        })
        services = load_services()
        assert len(services) == 1
        assert services[0].id == "tempfile"

    def test_non_dict_entry_skipped(self, fake_home):
        save_network_config({
            "remotes": {},
            "services": {"bad": "not-a-dict"},
        })
        assert load_services() == []


class TestDetectServiceKind:
    def test_tempfile_org_url_matches(self):
        assert detect_service_kind("https://tempfile.org") == "tempfile_org"

    def test_tempfile_org_with_path_matches(self):
        assert detect_service_kind("https://tempfile.org/api") == "tempfile_org"

    def test_www_subdomain_matches(self):
        assert detect_service_kind("https://www.tempfile.org") == "tempfile_org"

    def test_unrelated_host_returns_none(self):
        assert detect_service_kind("https://example.com") is None

    def test_bad_scheme_returns_none(self):
        assert detect_service_kind("ftp://tempfile.org") is None

    def test_s3_url(self):
        assert detect_service_kind("s3://bucket") == "s3"

    def test_file_sync_url(self):
        assert detect_service_kind("file:///tmp/sync") == "file_sync"

    def test_webdav_url(self):
        assert detect_service_kind("webdav://webdav.example/dav") == "webdav"

    def test_a_bare_folder_path_is_a_sync_folder(self):
        assert detect_service_kind("/srv/sync/a8s") == "sync_folder"
        assert detect_service_kind("~/OneDrive/A8S") == "sync_folder"

    def test_a_file_url_stays_file_sync(self):
        # sync_folder claims bare paths only; `file://` still means the
        # publish-a-base-URL service.
        assert detect_service_kind("file:///srv/sync") == "file_sync"


class TestServicesFollowTheConfig:
    """A daemon runs for days. One that captured its services at startup
    ignored everything configured afterwards, failed every attachment, and
    said nothing — while `a8s storage` listed the service it was skipping."""

    def test_a_service_added_later_is_picked_up(self, fake_home, tmp_path):
        save_network_config({"remotes": {}, "services": {}})
        assert load_services() == []
        save_network_config({
            "remotes": {},
            "services": {"drop": {"service": "sync_folder", "url": str(tmp_path)}},
        })
        assert [s.id for s in load_services()] == ["drop"]

    def test_a_service_removed_later_stops_being_used(self, fake_home, tmp_path):
        save_network_config({
            "remotes": {},
            "services": {"drop": {"service": "sync_folder", "url": str(tmp_path)}},
        })
        assert [s.id for s in load_services()] == ["drop"]
        save_network_config({"remotes": {}, "services": {}})
        assert load_services() == []

    def test_an_unchanged_config_is_not_rebuilt(self, fake_home, tmp_path):
        # Repeated calls are on the routing path, and a constructor can read
        # secrets and touch the filesystem.
        save_network_config({
            "remotes": {},
            "services": {"drop": {"service": "sync_folder", "url": str(tmp_path)}},
        })
        assert load_services()[0] is load_services()[0]


class TestDepsGroupFor:
    """#242: `deps_group_for` is the one place a kind names the `ar3 deps`
    group it needs, so `a8s storage` can install it instead of a service
    failing at first real use with a WARN pointing at a second command."""

    #: kind -> its service module, by the same names `_build_service` and
    #: `detect_service_kind` dispatch on.
    _KIND_MODULES = {
        "tempfile_org": "services.tempfile_org",
        "s3": "services.s3",
        "file_sync": "services.file_sync",
        "webdav": "services.webdav",
        "rclone": "services.rclone",
        "sync_folder": "services.sync_folder",
    }

    def test_s3_needs_a8s_s3(self):
        assert deps_group_for("s3") == "a8s-s3"

    def test_unknown_kind_needs_nothing(self):
        assert deps_group_for("telepathy") is None

    def test_every_kind_that_imports_ar3_deps_has_a_group_mapped(self):
        # A kind whose module reaches into `ar3.deps` (`require_group` /
        # `use_group`) carries a heavy on-demand import; one that does not
        # needs nothing installed. This fails the moment a new kind adds a
        # tier-2 import without naming its group here.
        for kind, module_name in self._KIND_MODULES.items():
            __import__(module_name)
            src = inspect.getsource(sys.modules[module_name])
            uses_deps = "ar3.deps" in src
            group = deps_group_for(kind)
            if uses_deps:
                assert group is not None, (
                    f"{kind} ({module_name}) imports ar3.deps but names no group"
                )
            else:
                assert group is None, (
                    f"{kind} ({module_name}) names group {group!r} but never imports ar3.deps"
                )
