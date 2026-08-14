"""Tests for the reserved-env contract — The Ark's foundation layer.

Trivial by design: this module is just names. The behavior that matters
(a8s injecting these on wake, r4t refusing a rig that names one) is tested
in each app's own suite.
"""
from __future__ import annotations

from ark import envseam


class TestContractShape:
    def test_outbox_dir_env_name(self):
        assert envseam.TELL_OUTBOX_DIR_ENV == "TELL_OUTBOX_DIR"

    def test_file_max_env_name(self):
        assert envseam.TELL_FILE_MAX_ENV == "TELL_FILE_MAX"

    def test_routing_owned_is_a_tuple_of_both_names(self):
        assert envseam.ROUTING_OWNED == (
            envseam.TELL_OUTBOX_DIR_ENV,
            envseam.TELL_FILE_MAX_ENV,
        )

    def test_routing_owned_entries_are_strings(self):
        assert all(isinstance(name, str) and name for name in envseam.ROUTING_OWNED)
