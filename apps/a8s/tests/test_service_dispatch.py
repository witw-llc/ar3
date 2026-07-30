"""Tests for the storage-service dispatcher (#90) — `_build_service`,
`load_services`, `configured_service_ids`, `detect_service_kind`. The
TempFile.org-specific HTTP behavior lives in test_service_tempfile_org.py."""
from __future__ import annotations

from network import (
    configured_service_ids,
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
