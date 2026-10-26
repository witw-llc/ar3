import json
import os
import stat
import threading
from pathlib import Path

import pytest

from browser import secretstore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "secrets-v1.json"
POSIX_MODES = pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
ORIGINS = ["https://app.example.com"]


def _entry(value, origins=ORIGINS):
    return {"value": value, "origins": list(origins)}


def _set(name, value, origins=ORIGINS, seat="seat"):
    secretstore.set_secret(seat, name, value, origins)


def _write(seat, text, mode=0o600):
    target = secretstore.path(seat)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(text if isinstance(text, bytes) else text.encode("utf-8"))
    os.chmod(target, mode)
    return target


def _store_text(secrets):
    return json.dumps({"version": 1, "secrets": secrets})


def test_the_store_is_one_file_in_the_seat_directory():
    from browser import session

    assert secretstore.path("garmin") == os.path.join(session.seat_home("garmin"), "secrets.json")
    assert secretstore.FILE == "secrets.json"


def test_a_missing_store_is_an_empty_store():
    assert secretstore.read("seat") == {}


def test_a_secret_set_is_read_back_with_its_origins():
    _set("PASSWORD", "value-one")
    _set("API_TOKEN", "value-two", ["https://api.example.com", "http://127.0.0.1:8080"])
    assert secretstore.read("seat") == {
        "PASSWORD": _entry("value-one"),
        "API_TOKEN": _entry("value-two", ["https://api.example.com", "http://127.0.0.1:8080"]),
    }
    _set("PASSWORD", "value-three", ["https://other.example.com"])
    assert secretstore.read("seat")["PASSWORD"] == _entry("value-three", ["https://other.example.com"])


def test_a_value_keeps_its_spaces_quotes_and_unicode():
    value = ' pa"ss\\ word\u00e9\u2603 '
    _set("PASSWORD", value)
    assert secretstore.read("seat") == {"PASSWORD": _entry(value)}


@POSIX_MODES
def test_the_store_is_created_with_mode_0600():
    _set("PASSWORD", "value-one")
    assert stat.S_IMODE(os.stat(secretstore.path("seat")).st_mode) == 0o600
    _set("OTHER", "value-two")
    assert stat.S_IMODE(os.stat(secretstore.path("seat")).st_mode) == 0o600


@POSIX_MODES
def test_the_store_is_made_aside_with_mode_0600_and_renamed(monkeypatch):
    opened, replaced = [], []
    real_open, real_replace = os.open, os.replace
    monkeypatch.setattr(
        secretstore.os, "open",
        lambda target, flags, mode=0o777: (opened.append((target, flags, mode)), real_open(target, flags, mode))[1],
    )
    monkeypatch.setattr(
        secretstore.os, "replace", lambda a, b: (replaced.append((a, b)), real_replace(a, b))[1]
    )
    _set("PASSWORD", "value-one")
    (source, target), = replaced
    assert os.path.basename(source) == f".secrets.json.{os.getpid()}.{threading.get_ident()}.tmp"
    assert target == secretstore.path("seat")
    (aside, flags, mode), = [call for call in opened if call[0].endswith(".tmp")]
    assert aside == source
    assert flags & os.O_EXCL and flags & os.O_CREAT
    assert mode == 0o600
    assert os.listdir(os.path.dirname(target)) == ["secrets.json"]


def test_a_failed_write_leaves_no_file_and_the_old_store(monkeypatch):
    _set("PASSWORD", "value-one")

    def refuse(a, b):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(secretstore.os, "replace", refuse)
        with pytest.raises(OSError):
            _set("PASSWORD", "value-two")
    assert secretstore.read("seat") == {"PASSWORD": _entry("value-one")}
    assert os.listdir(os.path.dirname(secretstore.path("seat"))) == ["secrets.json"]


def test_a_leftover_aside_of_a_dead_run_does_not_stop_a_write():
    _set("PASSWORD", "value-one")
    directory = os.path.dirname(secretstore.path("seat"))
    leftover = os.path.join(directory, f".secrets.json.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(leftover, "w") as handle:
        handle.write("half a store")
    _set("PASSWORD", "value-two")
    assert secretstore.read("seat") == {"PASSWORD": _entry("value-two")}
    assert os.listdir(directory) == ["secrets.json"]


def test_a_reader_never_sees_a_partial_store():
    _set("PASSWORD", "value-0000")
    done = threading.Event()
    bad = []

    def read():
        while not done.is_set():
            try:
                store = secretstore.read("seat")
            except secretstore.SecretStoreError as exc:
                bad.append(str(exc))
                continue
            if list(store) != ["PASSWORD"]:
                bad.append(store)

    def write(number):
        for count in range(60):
            _set("PASSWORD", f"value-{number}-{count}")

    reader = threading.Thread(target=read)
    writers = [threading.Thread(target=write, args=(number,)) for number in range(3)]
    reader.start()
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()
    done.set()
    reader.join()
    assert bad == []


def test_a_secret_is_removed_and_the_others_stay():
    _set("PASSWORD", "value-one")
    _set("API_TOKEN", "value-two")
    assert secretstore.remove("seat", "PASSWORD") is True
    assert secretstore.read("seat") == {"API_TOKEN": _entry("value-two")}
    assert secretstore.remove("seat", "PASSWORD") is False
    assert secretstore.read("seat") == {"API_TOKEN": _entry("value-two")}


def test_each_seat_has_its_own_store():
    _set("PASSWORD", "value-one", seat="one")
    assert secretstore.read("two") == {}


def test_the_store_as_a_version_1_file_reads_the_same_in_every_later_version():
    """Frozen. `fixtures/secrets-v1.json` is a version 1 store as it is written
    today. A stored secret survives an update, so no later version may fail to
    read it, and none may change this file or this expected result."""
    _write("seat", FIXTURE.read_bytes())
    assert json.loads(FIXTURE.read_text()) == {
        "version": 1,
        "secrets": {
            "API_TOKEN": {
                "value": "example-token-value-0001",
                "origins": ["https://api.example.com", "http://127.0.0.1:8080"],
            },
            "PASSWORD": {
                "value": "example password: 2 words",
                "origins": ["https://app.example.com", "https://*.example.org"],
            },
        },
    }
    assert secretstore.read("seat") == {
        "API_TOKEN": _entry("example-token-value-0001", ["https://api.example.com", "http://127.0.0.1:8080"]),
        "PASSWORD": _entry("example password: 2 words", ["https://app.example.com", "https://*.example.org"]),
    }


def test_a_version_1_file_may_carry_keys_this_version_does_not_know():
    _write("seat", _store_text({"PASSWORD": {"value": "value-one", "origins": ORIGINS, "note": "later"}}).replace(
        '"secrets"', '"note": "later", "secrets"'
    ))
    assert secretstore.read("seat") == {"PASSWORD": _entry("value-one")}


def test_a_store_written_now_has_the_version_1_shape():
    _set("PASSWORD", "value-one", ["https://app.example.com", "https://*.example.org"])
    assert json.loads(Path(secretstore.path("seat")).read_text()) == {
        "version": 1,
        "secrets": {
            "PASSWORD": {"value": "value-one", "origins": ["https://app.example.com", "https://*.example.org"]}
        },
    }


def test_the_store_written_now_has_the_shape_of_the_version_1_fixture():
    _set("PASSWORD", "example password: 2 words", ["https://app.example.com", "https://*.example.org"])
    _set("API_TOKEN", "example-token-value-0001", ["https://api.example.com", "http://127.0.0.1:8080"])
    written = json.loads(Path(secretstore.path("seat")).read_text())
    assert written == json.loads(FIXTURE.read_text())


@pytest.mark.parametrize("name", ["A", "PASSWORD", "A1_B2", "X" * 64, "API_TOKEN_2"])
def test_a_good_name_is_kept(name):
    _set(name, "value-one")
    assert list(secretstore.read("seat")) == [name]


@pytest.mark.parametrize(
    "name",
    ["", "lower", "Mixed", "1ABC", "_ABC", "A-B", "A B", "A.B", "X" * 65, "ABC\n", "\u00c4BC", "ABC ", None, 7],
)
def test_a_bad_name_is_refused_and_nothing_is_written(name):
    with pytest.raises(secretstore.SecretStoreError, match="secret name"):
        _set(name, "value-one")
    assert not os.path.exists(secretstore.path("seat"))


@pytest.mark.parametrize("value", ["", "a", "abc", "   ", None, 1234])
def test_a_value_under_four_characters_is_refused(value):
    with pytest.raises(secretstore.SecretStoreError, match="at least 4 characters"):
        _set("PASSWORD", value)
    assert not os.path.exists(secretstore.path("seat"))


def test_a_value_of_four_characters_is_kept():
    _set("PASSWORD", "abcd")
    assert secretstore.read("seat") == {"PASSWORD": _entry("abcd")}


def test_a_value_with_a_nul_is_refused():
    with pytest.raises(secretstore.SecretStoreError, match="NUL"):
        _set("PASSWORD", "abc\0def")


def test_a_refusal_never_holds_the_value():
    with pytest.raises(secretstore.SecretStoreError) as short:
        _set("PASSWORD", "abc")
    with pytest.raises(secretstore.SecretStoreError) as nul:
        _set("PASSWORD", "xy\0z-value")
    assert "abc" not in str(short.value)
    assert "xy" not in str(nul.value)


# --- origins ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "written, normalized",
    [
        ("https://app.example.com", "https://app.example.com"),
        ("HTTPS://App.Example.COM", "https://app.example.com"),
        ("https://app.example.com:443", "https://app.example.com"),
        ("http://app.example.com:80", "http://app.example.com"),
        ("https://app.example.com:8443", "https://app.example.com:8443"),
        ("http://app.example.com:443", "http://app.example.com:443"),
        ("https://app.example.com:08443", "https://app.example.com:8443"),
        ("http://127.0.0.1:18931", "http://127.0.0.1:18931"),
        ("http://localhost", "http://localhost"),
        ("http://[::1]:3000", "http://[::1]:3000"),
        ("http://[0:0:0:0:0:0:0:1]", "http://[::1]"),
        ("https://*.example.org", "https://*.example.org"),
        ("HTTPS://*.Example.ORG:443", "https://*.example.org"),
        ("http://*.localhost:3000", "http://*.localhost:3000"),
        ("HTTP://*.LOCALHOST", "http://*.localhost"),
        ("http://*.sub.localhost", "http://*.sub.localhost"),
        ("https://*.co.uk", "https://*.co.uk"),
        ("https://*.example.com", "https://*.example.com"),
        ("https://*.a.b.c.example", "https://*.a.b.c.example"),
        ("https://xn--bcher-kva.example", "https://xn--bcher-kva.example"),
    ],
)
def test_an_origin_is_scheme_host_and_port_and_is_written_in_one_normal_form(written, normalized):
    assert secretstore.parse_origin(written) == normalized
    assert secretstore.parse_origin(normalized) == normalized


BAD_ORIGINS = [
    "", "app.example.com", "ftp://app.example.com", "file:///tmp/x", "javascript://x", "https:/app.example.com",
    "https://", "https:///", "https://app.example.com/", "https://app.example.com/path",
    "https://app.example.com?x=1", "https://app.example.com#top", "https://user@app.example.com",
    "https://user:pass@app.example.com", "https://app.example.com:0", "https://app.example.com:65536",
    "https://app.example.com:80x", "https://app.example.com:", "https://app.example.com:-1",
    "https://app.example.com:\u0663", "https://*", "https://*.", "https://*example.org", "https://a.*.example.org",
    "https://*.*.example.org", "https://**.example.org", "*.example.org", "https://*.127.0.0.1", "https://*.[::1]",
    "https://exa mple.com", "https://exa\tmple.com", "https://b\u00fccher.example", "https://-bad.example",
    "https://bad-.example", "https://example..com", "https://.example.com", "https://example.com.",
    "https://exa_mple.com", "https://[::1", "https://[zz]", "https://[]", "https://[::1]x", "https://" + "a" * 64 + ".example",
    "https://" + ".".join(["abcdefghi"] * 30), None, 7, ["https://app.example.com"],
]


@pytest.mark.parametrize("origin", BAD_ORIGINS)
def test_anything_but_scheme_host_and_port_is_refused(origin):
    with pytest.raises(secretstore.SecretStoreError) as bad:
        secretstore.parse_origin(origin)
    assert str(bad.value)


@pytest.mark.parametrize(
    "origin",
    [
        "LEAK-4410", "ftp://LEAK-4410.example", "https://LEAK-4410.example/path", "https://user@LEAK-4410.example",
        "https://LEAK-4410.example:99999", "https://LEAK-4410.example:80x", "https://*LEAK-4410.example",
        "https://LEAK-4410 .example", "https://LEAK_4410.example", "https://[LEAK-4410]", "https://*.LEAK-4410:\u0663",
    ],
)
def test_a_refused_origin_is_not_repeated_in_the_message(origin):
    """The text could be a value typed in the wrong place."""
    with pytest.raises(secretstore.SecretStoreError) as bad:
        secretstore.parse_origin(origin)
    assert "LEAK-4410" not in str(bad.value) and "4410" not in str(bad.value)


@pytest.mark.parametrize(
    "origin",
    ["https://*.com", "http://*.org:8080", "https://*.io", "https://*.uk", "https://*.test", "https://*.local",
     "https://*.internal", "https://*.localhost2", "https://*.xn--p1ai", "HTTPS://*.COM"],
)
def test_a_wildcard_on_one_label_is_refused_and_the_message_says_why(origin):
    with pytest.raises(secretstore.SecretStoreError, match="at least two labels") as bad:
        secretstore.parse_origin(origin)
    message = str(bad.value)
    assert "https://*.example.com" in message and "https://*.co.uk" in message
    assert "would match every site under it" in message
    assert "*.localhost is the one exception" in message


@pytest.mark.parametrize(
    "origin",
    ["https://*.example.com", "https://*.co.uk", "https://*.example.com:8443", "http://*.sub.localhost",
     "https://*.a.b.c", "http://*.example.test", "https://*.xn--bcher-kva.example"],
)
def test_a_wildcard_on_two_labels_or_more_passes(origin):
    assert secretstore.parse_origin(origin).startswith(origin.split("*")[0].lower() + "*.")


@pytest.mark.parametrize("origin", ["http://*.localhost", "http://*.localhost:3000", "https://*.LocalHost", "HTTP://*.LOCALHOST:8080"])
def test_localhost_is_the_one_wildcard_on_a_single_label(origin):
    normalized = secretstore.parse_origin(origin)
    assert "*.localhost" in normalized
    assert secretstore.origin_parts(normalized)["host"] == "localhost"
    assert secretstore.origin_parts(normalized)["wildcard"] is True


def test_a_name_that_only_starts_like_localhost_is_not_the_exception():
    for origin in ("http://*.localhost2", "http://*.localhost.", "http://*.xlocalhost", "http://*.local"):
        with pytest.raises(secretstore.SecretStoreError):
            secretstore.parse_origin(origin)


def test_a_one_label_wildcard_stops_a_set_and_nothing_is_written():
    with pytest.raises(secretstore.SecretStoreError, match="at least two labels"):
        _set("PASSWORD", "value-one", ["https://app.example.com", "https://*.com"])
    assert not os.path.exists(secretstore.path("seat"))


def test_a_store_with_a_one_label_wildcard_is_not_a_valid_store():
    target = _write("seat", _store_text({"PASSWORD": _entry("LEAK-9137", ["https://*.com"])}))
    with pytest.raises(secretstore.SecretStoreError) as bad:
        secretstore.read("seat")
    assert target in str(bad.value) and "LEAK-9137" not in str(bad.value)


def test_an_origin_refusal_says_what_an_origin_is():
    for origin, hint in [
        ("app.example.com", "http:// or https://"),
        ("https://app.example.com/login", "no path"),
        ("https://*example.org", "wildcard"),
        ("https://app.example.com:99999", "1 to 65535"),
    ]:
        with pytest.raises(secretstore.SecretStoreError, match=hint):
            secretstore.parse_origin(origin)


def test_the_parts_of_an_origin_are_what_the_page_check_compares():
    assert secretstore.origin_parts("https://app.example.com") == {
        "scheme": "https", "host": "app.example.com", "port": "", "wildcard": False,
    }
    assert secretstore.origin_parts("http://127.0.0.1:18931") == {
        "scheme": "http", "host": "127.0.0.1", "port": "18931", "wildcard": False,
    }
    assert secretstore.origin_parts("https://*.example.org:8443") == {
        "scheme": "https", "host": "example.org", "port": "8443", "wildcard": True,
    }


def test_a_secret_needs_an_origin():
    for origins in ([], None, (), "https://app.example.com"):
        with pytest.raises(secretstore.SecretStoreError, match="at least one origin"):
            _set("PASSWORD", "value-one", origins)
    assert not os.path.exists(secretstore.path("seat"))


def test_a_bad_origin_stops_a_set_and_nothing_is_written():
    with pytest.raises(secretstore.SecretStoreError, match="no path"):
        _set("PASSWORD", "value-one", ["https://app.example.com", "https://app.example.com/login"])
    assert not os.path.exists(secretstore.path("seat"))


def test_origins_are_stored_normalized_and_once_in_the_order_given():
    _set("PASSWORD", "value-one", [
        "https://B.example.com", "https://a.example.com:443", "https://b.example.com", "http://a.example.com",
    ])
    assert secretstore.read("seat")["PASSWORD"]["origins"] == [
        "https://b.example.com", "https://a.example.com", "http://a.example.com",
    ]


def test_a_set_replaces_the_origins_with_the_value():
    _set("PASSWORD", "value-one", ["https://a.example.com", "https://b.example.com"])
    _set("PASSWORD", "value-two", ["https://c.example.com"])
    assert secretstore.read("seat") == {"PASSWORD": _entry("value-two", ["https://c.example.com"])}


@POSIX_MODES
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o606, 0o666, 0o610])
def test_a_store_open_to_group_or_others_is_refused_with_the_fix(mode):
    target = _write("seat", _store_text({"PASSWORD": _entry("value-one")}), mode)
    with pytest.raises(secretstore.SecretStoreError) as refused:
        secretstore.read("seat")
    message = str(refused.value)
    assert f"chmod 600 {target}" in message
    assert target in message
    assert "value-one" not in message


@POSIX_MODES
@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_a_store_closed_to_group_and_others_is_read(mode):
    _write("seat", _store_text({"PASSWORD": _entry("value-one")}), mode)
    assert secretstore.read("seat") == {"PASSWORD": _entry("value-one")}


@POSIX_MODES
def test_a_loose_store_cannot_be_written_over_unnoticed():
    _write("seat", _store_text({"PASSWORD": _entry("value-one")}), 0o644)
    with pytest.raises(secretstore.SecretStoreError, match="chmod 600"):
        _set("OTHER", "value-two")
    with pytest.raises(secretstore.SecretStoreError, match="chmod 600"):
        secretstore.remove("seat", "PASSWORD")


CORRUPT = [
    b"not json at all LEAK-9137",
    b"",
    b"[]",
    b'"LEAK-9137"',
    b"null",
    b'{"version": 1}',
    b'{"secrets": {"PASSWORD": {"value": "LEAK-9137", "origins": ["https://a.example.com"]}}}',
    b'{"version": 1, "secrets": ["LEAK-9137"]}',
    b'{"version": 1, "secrets": "LEAK-9137"}',
    b'{"version": "1", "secrets": {"PASSWORD": {"value": "LEAK-9137", "origins": ["https://a.example.com"]}}}',
    b'{"version": true, "secrets": {"PASSWORD": {"value": "LEAK-9137", "origins": ["https://a.example.com"]}}}',
    b'{"version": 0, "secrets": {"PASSWORD": {"value": "LEAK-9137", "origins": ["https://a.example.com"]}}}',
    b'{"version": 1, "secrets": {"password": {"value": "LEAK-9137", "origins": ["https://a.example.com"]}}}',
    b'{"version": 1, "secrets": {"PASSWORD": {"value": "abc", "origins": ["https://a.example.com"]}}}',
    b'{"version": 1, "secrets": {"PASSWORD": {"value": 12345678, "origins": ["https://a.example.com"]}}}',
    b'{"version": 1, "secrets": {"PASSWORD": "LEAK-9137"}}',
    b'{"version": 1, "secrets": {"PASSWORD": ["LEAK-9137"]}}',
    b'{"version": 1, "secrets": {"PASSWORD": {"origins": ["https://a.example.com"]}}}',
    b'{"version": 1, "secrets": {"PASSWORD": {"value": "LEAK-9137"}}}',
    b'{"version": 1, "secrets": {"PASSWORD": {"value": "LEAK-9137", "origins": []}}}',
    b'{"version": 1, "secrets": {"PASSWORD": {"value": "LEAK-9137", "origins": "https://a.example.com"}}}',
    b'{"version": 1, "secrets": {"PASSWORD": {"value": "LEAK-9137", "origins": ["https://a.example.com/path"]}}}',
    b'{"version": 1, "secrets": {"PASSWORD": {"value": "LEAK-9137", "origins": ["a.example.com"]}}}',
    b'{"version": 1, "secrets": {"PASSWORD": {"value": "LEAK-9137", "origins": [7]}}}',
    b'{"version": 1, "secrets": {"PASSWORD": {"value": "LEAK-9137", "origins": ["https://a.example.com"]}, "BAD NAME": {"value": "LEAK-9137", "origins": ["https://a.example.com"]}}}',
    b'{"version": 1, "secrets": {"PASSWORD": {"value": "LEAK-9137", "origins": ["https://a.example.com"]}',
    b"\xff\xfe LEAK-9137 \x00\x01",
]


@pytest.mark.parametrize("content", CORRUPT)
def test_a_corrupt_store_names_the_file_and_never_prints_its_content(content):
    target = _write("seat", content)
    with pytest.raises(secretstore.SecretStoreError) as bad:
        secretstore.read("seat")
    message = str(bad.value)
    assert target in message
    assert "LEAK-9137" not in message
    assert "LEAK-9137" not in repr(bad.value)
    assert bad.value.__cause__ is None
    assert bad.value.__context__ is None or bad.value.__suppress_context__


def test_a_store_from_a_later_version_names_the_version_and_the_file():
    target = _write("seat", json.dumps({"version": 2, "secrets": {"PASSWORD": _entry("LEAK-9137")}}))
    with pytest.raises(secretstore.SecretStoreError) as later:
        secretstore.read("seat")
    assert "version 2" in str(later.value)
    assert target in str(later.value)
    assert "LEAK-9137" not in str(later.value)


def test_a_store_that_is_a_directory_is_an_error_that_names_the_file():
    target = secretstore.path("seat")
    os.makedirs(target)
    with pytest.raises(secretstore.SecretStoreError, match="cannot be read") as bad:
        secretstore.read("seat")
    assert target in str(bad.value)


def test_a_seat_directory_that_is_a_file_is_an_empty_store(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker))
    assert secretstore.read("seat") == {}


# --- the scrub ------------------------------------------------------------------


def _scrubber(**secrets):
    return secretstore.Scrubber(
        {name: {"value": value, "origins": ORIGINS} for name, value in secrets.items()}
    )


def test_a_scrubber_with_no_secret_is_false_and_changes_nothing():
    scrubber = secretstore.Scrubber({})
    assert not scrubber
    assert scrubber.scrub("anything at all") == "anything at all"
    assert scrubber.secrets == {}


def test_a_value_becomes_its_marker():
    scrubber = _scrubber(PASSWORD="hunter2-value")
    assert scrubber
    assert scrubber.scrub("pw=hunter2-value; again hunter2-value") == "pw=[secret:PASSWORD]; again [secret:PASSWORD]"


def test_the_longest_value_wins():
    scrubber = _scrubber(SHORT="abcd1234", LONG="abcd1234-extra")
    assert scrubber.scrub("abcd1234-extra and abcd1234") == "[secret:LONG] and [secret:SHORT]"


def test_a_marker_is_never_scanned_again():
    """A value that appears inside another secret's marker must not nest markers."""
    scrubber = _scrubber(ALPHA="secret", BETA="beta-value-1")
    assert scrubber.scrub("beta-value-1 and secret") == "[secret:BETA] and [secret:ALPHA]"


def test_a_scrub_is_idempotent_on_text_with_no_value_inside_a_marker():
    scrubber = _scrubber(PASSWORD="hunter2-value")
    once = scrubber.scrub("pw=hunter2-value")
    assert scrubber.scrub(once) == once


def test_a_text_without_a_value_is_returned_as_it_is():
    scrubber = _scrubber(PASSWORD="hunter2-value")
    text = "nothing to hide\nin several lines"
    assert scrubber.scrub(text) == text
    assert scrubber.scrub("") == ""
    assert scrubber.scrub(None) is None


def test_a_value_with_a_newline_is_scrubbed_before_a_text_is_split():
    scrubber = _scrubber(KEY="line one\nline two")
    assert scrubber.scrub("a line one\nline two b") == "a [secret:KEY] b"


def test_a_value_with_regex_characters_matches_literally():
    scrubber = _scrubber(PASSWORD="a.b*c(d)[e]+f?g|h$^")
    assert scrubber.scrub("x a.b*c(d)[e]+f?g|h$^ y axb") == "x [secret:PASSWORD] y axb"


def test_every_secret_of_the_store_is_scrubbed():
    scrubber = _scrubber(PASSWORD="hunter2-value", API_TOKEN="tok-abc-123")
    assert scrubber.scrub("hunter2-value tok-abc-123") == "[secret:PASSWORD] [secret:API_TOKEN]"


TRICKY = '@pa"ss\\wo{rd}: #x\t\u00e9\u2603'


def test_the_value_is_scrubbed_as_a_failed_fill_prints_it():
    # Captured from playwright-cli 0.1.22 on Chrome 155: the call log holds the raw value.
    log = '\x1b[2m    - fill("@pa"ss\\wo{rd}: #x\t\u00e9\u2603")\x1b[22m'
    assert _scrubber(PASSWORD=TRICKY).scrub(log) == '\x1b[2m    - fill("[secret:PASSWORD]")\x1b[22m'


def test_the_value_is_scrubbed_as_a_snapshot_quotes_and_folds_it():
    # Captured from playwright-cli 0.1.22 on Chrome 155: a value that needs
    # quotes prints as a double-quoted YAML scalar, escaped, with its tab folded to a space.
    snapshot = '    - textbox "Password" [active] [ref=f6e4]: "@pa\\"ss\\\\wo{rd}: #x \u00e9\u2603"'
    assert _scrubber(PASSWORD=TRICKY).scrub(snapshot) == (
        '    - textbox "Password" [active] [ref=f6e4]: "[secret:PASSWORD]"'
    )


def test_the_value_is_scrubbed_as_the_driver_prints_a_string_result():
    printed = json.dumps(f"typed {TRICKY}")
    assert _scrubber(PASSWORD=TRICKY).scrub(printed) == '"typed [secret:PASSWORD]"'
    printed = json.dumps(f"typed {TRICKY}", ensure_ascii=False)
    assert _scrubber(PASSWORD=TRICKY).scrub(printed) == '"typed [secret:PASSWORD]"'


def test_the_value_is_scrubbed_in_a_url():
    from urllib.parse import quote, quote_plus

    scrubber = _scrubber(PASSWORD=TRICKY)
    assert scrubber.scrub(f"https://example.com/?pw={quote(TRICKY, safe='')}&x=1") == (
        "https://example.com/?pw=[secret:PASSWORD]&x=1"
    )
    assert scrubber.scrub(f"https://example.com/?pw={quote_plus(TRICKY)}&x=1") == (
        "https://example.com/?pw=[secret:PASSWORD]&x=1"
    )


def test_the_value_is_scrubbed_in_markup():
    import html

    assert _scrubber(PASSWORD=TRICKY).scrub(f"<p>{html.escape(TRICKY)}</p>") == "<p>[secret:PASSWORD]</p>"


# What `CSS.escape` returns in Chrome 155, for the strings that matter: a page that makes an id from a value
# prints it so, and so does the selector a tool builds from that id. Taken from the browser, not from the spec.
CSS_ESCAPES = [
    ("SyntheticUserA7492", "SyntheticUserA7492"),
    ("user@example.com", "user\\@example\\.com"),
    ("1234abcd", "\\31 234abcd"),
    ("-5x", "-\\35 x"),
    ("-", "\\-"),
    ("--x", "--x"),
    ("a b", "a\\ b"),
    ("a\tb", "a\\9 b"),
    ("x.y#z", "x\\.y\\#z"),
    ("caf\u00e9", "caf\u00e9"),
    ("ab\x01cd", "ab\\1 cd"),
    ("ab\x7fcd", "ab\\7f cd"),
    ("\x00ab", "\ufffdab"),
    ("a\\b", "a\\\\b"),
    ("a'b\"c", "a\\'b\\\"c"),
    ("a:b", "a\\:b"),
    ("a/b", "a\\/b"),
    ("0", "\\30 "),
    ("_x_", "_x_"),
]


@pytest.mark.parametrize("raw, escaped", CSS_ESCAPES)
def test_the_css_form_is_what_the_browser_makes_of_the_value(raw, escaped):
    assert secretstore._css_escaped(raw) == escaped


def test_a_value_is_scrubbed_in_the_selector_a_page_made_from_it():
    """The id of a button is the user name typed a moment before; a tool names the button by `#id`."""
    value = "user@example.com"
    scrubber = _scrubber(USER=value)
    assert scrubber.scrub("#user\\@example\\.com") == "#[secret:USER]"
    assert scrubber.scrub("button.user\\@example\\.com") == "button.[secret:USER]"
    assert scrubber.scrub("[data-testid=\"user@example.com\"]") == "[data-testid=\"[secret:USER]\"]"
    assert scrubber.scrub("#1234abcd") == "#1234abcd"
    assert _scrubber(PIN="1234abcd").scrub("#\\31 234abcd") == "#[secret:PIN]"


def test_a_scrubber_holds_a_text_that_has_a_value_in_any_of_its_forms():
    scrubber = _scrubber(PASSWORD=TRICKY)
    from urllib.parse import quote, quote_plus
    import html

    forms = [
        TRICKY, json.dumps(TRICKY)[1:-1], secretstore._yaml_quoted(TRICKY), secretstore._css_escaped(TRICKY),
        quote(TRICKY, safe=""), quote_plus(TRICKY), html.escape(TRICKY),
    ]
    for form in forms:
        assert scrubber.holds(f"before {form} after"), form
    assert not scrubber.holds("nothing of it")
    assert not scrubber.holds("")


def test_a_scrubber_with_no_value_holds_nothing_and_a_hidden_value_is_held_at_once():
    scrubber = secretstore.Scrubber({})
    assert not scrubber.holds("482916")
    scrubber.hide("PASSCODE", "482916")
    assert scrubber.holds("x=482916") and not scrubber.holds("x=48291")


def test_a_text_that_is_not_text_holds_no_value():
    scrubber = _scrubber(PASSWORD="hunter2-value")
    assert not scrubber.holds(None) and not scrubber.holds(b"hunter2-value") and not scrubber.holds(7)


@pytest.mark.parametrize(
    "typed, shown",
    [
        ("abc  def", "abc def"),
        ("  lead and trail  ", "lead and trail"),
        ("ab\u00a0cd\u00a0\u00a0ef", "ab cd ef"),
        ("ab\u200bcd1234", "abcd1234"),
        ("one\ttwo\nthree four", "one two three four"),
    ],
)
def test_the_value_is_scrubbed_as_a_snapshot_folds_its_whitespace(typed, shown):
    """A snapshot drops zero-width spaces, trims a value and folds each run of
    whitespace to one space (see tests of playwright-cli 0.1.22)."""
    scrubber = _scrubber(PASSWORD=typed)
    assert scrubber.scrub(f"textbox: {shown}") == "textbox: [secret:PASSWORD]"
    assert scrubber.scrub(f"textbox: {typed}") == "textbox: [secret:PASSWORD]"


def test_a_control_character_is_scrubbed_in_its_yaml_form():
    typed = "pass\x01word\x7f-end"
    assert _scrubber(PASSWORD=typed).scrub('"pass\\x01word\\x7f-end"') == '"[secret:PASSWORD]"'


@POSIX_MODES
def test_a_file_is_scrubbed_in_place_through_a_file_made_beside_it(tmp_path, monkeypatch):
    target = tmp_path / "page.yml"
    target.write_text("- textbox: hunter2-value\n- textbox: other\n")
    replaced = []
    real = os.replace
    monkeypatch.setattr(secretstore.os, "replace", lambda a, b: (replaced.append((a, b)), real(a, b))[1])
    assert _scrubber(PASSWORD="hunter2-value").scrub_file(str(target)) is True
    assert target.read_text() == "- textbox: [secret:PASSWORD]\n- textbox: other\n"
    (source, destination), = replaced
    assert os.path.basename(source) == f"page.yml.{os.getpid()}.{threading.get_ident()}.tmp"
    assert destination == str(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert os.listdir(tmp_path) == ["page.yml"]


def test_a_file_without_a_value_is_not_rewritten(tmp_path):
    target = tmp_path / "page.yml"
    target.write_text("- textbox: other\n")
    before = target.stat()
    assert _scrubber(PASSWORD="hunter2-value").scrub_file(str(target)) is False
    after = target.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_a_file_that_is_not_text_is_left_alone(tmp_path):
    target = tmp_path / "screen.png"
    data = b"\x89PNG\r\n\x1a\n\xff\xfe hunter2-value \x00"
    target.write_bytes(data)
    assert _scrubber(PASSWORD="hunter2-value").scrub_file(str(target)) is False
    assert target.read_bytes() == data


def test_a_file_that_is_gone_is_not_an_error(tmp_path):
    assert _scrubber(PASSWORD="hunter2-value").scrub_file(str(tmp_path / "gone.yml")) is False


def test_a_file_keeps_its_line_endings(tmp_path):
    target = tmp_path / "console.log"
    target.write_bytes(b"[LOG] a hunter2-value\r\n[LOG] b\r\n")
    _scrubber(PASSWORD="hunter2-value").scrub_file(str(target))
    assert target.read_bytes() == b"[LOG] a [secret:PASSWORD]\r\n[LOG] b\r\n"


def test_a_file_that_cannot_be_replaced_is_reported_unchanged_and_leaves_no_aside(tmp_path, monkeypatch):
    target = tmp_path / "page.yml"
    target.write_text("hunter2-value")

    def refuse(a, b):
        raise OSError("read only")

    monkeypatch.setattr(secretstore.os, "replace", refuse)
    assert _scrubber(PASSWORD="hunter2-value").scrub_file(str(target)) is False
    assert os.listdir(tmp_path) == ["page.yml"]


# --- a value a script read at run time ------------------------------------------


def test_a_hidden_value_becomes_its_marker_from_then_on():
    scrubber = _scrubber(PASSWORD="hunter2-value")
    assert scrubber.scrub("code 482916") == "code 482916"
    scrubber.hide("PASSCODE", "482916")
    assert scrubber.scrub("code 482916, pw hunter2-value, again 482916") == (
        "code [secret:PASSCODE], pw [secret:PASSWORD], again [secret:PASSCODE]"
    )


def test_a_scrubber_with_no_secret_scrubs_a_value_hidden_later():
    scrubber = secretstore.Scrubber({})
    assert not scrubber
    scrubber.hide("PASSCODE", "482916")
    assert scrubber
    assert scrubber.scrub("a 482916 b") == "a [secret:PASSCODE] b"


def test_a_hidden_value_is_not_a_stored_secret():
    """No script can type it: `fill-secret` reads `secrets`, and the value was never stored."""
    scrubber = _scrubber(PASSWORD="hunter2-value")
    scrubber.hide("PASSCODE", "482916")
    assert list(scrubber.secrets) == ["PASSWORD"]


def test_a_hidden_value_is_scrubbed_in_every_shape_a_page_prints_it():
    scrubber = secretstore.Scrubber({})
    scrubber.hide("TOKEN", TRICKY)
    log = '\x1b[2m    - fill("@pa"ss\\wo{rd}: #x\té☃")\x1b[22m'
    assert scrubber.scrub(log) == '\x1b[2m    - fill("[secret:TOKEN]")\x1b[22m'
    snapshot = '    - textbox "Code" [ref=e4]: "@pa\\"ss\\\\wo{rd}: #x é☃"'
    assert scrubber.scrub(snapshot) == '    - textbox "Code" [ref=e4]: "[secret:TOKEN]"'


def test_the_longest_value_still_wins_after_a_value_is_hidden():
    scrubber = _scrubber(SHORT="abcd1234")
    scrubber.hide("LONG", "abcd1234-extra")
    assert scrubber.scrub("abcd1234-extra and abcd1234") == "[secret:LONG] and [secret:SHORT]"


def test_a_value_hidden_twice_keeps_its_first_marker():
    scrubber = secretstore.Scrubber({})
    scrubber.hide("FIRST", "482916")
    scrubber.hide("SECOND", "482916")
    assert scrubber.scrub("482916") == "[secret:FIRST]"


@pytest.mark.parametrize("value", ["", "123", "a" * 3, 482916, None, b"482916", "has\0nul"])
def test_a_value_too_short_or_not_text_is_not_hidden_and_changes_nothing(value):
    scrubber = _scrubber(PASSWORD="hunter2-value")
    with pytest.raises(secretstore.SecretStoreError) as refused:
        scrubber.hide("PASSCODE", value)
    assert not isinstance(value, str) or not value or value not in str(refused.value), "the message repeats the value"
    assert scrubber.scrub("482916 123 hunter2-value") == "482916 123 [secret:PASSWORD]"


def test_a_hidden_value_needs_a_name_like_a_secret_name():
    scrubber = secretstore.Scrubber({})
    for name in ("passcode", "1CODE", "", None, "A-B"):
        with pytest.raises(secretstore.SecretStoreError):
            scrubber.hide(name, "482916")
    assert not scrubber


def test_a_file_is_scrubbed_of_a_value_hidden_after_it_was_made(tmp_path):
    target = tmp_path / "page.yml"
    target.write_text("- generic: Your code is 482916\n")
    scrubber = secretstore.Scrubber({})
    assert scrubber.scrub_file(str(target)) is False
    scrubber.hide("PASSCODE", "482916")
    assert scrubber.scrub_file(str(target)) is True
    assert target.read_text() == "- generic: Your code is [secret:PASSCODE]\n"


def test_the_module_does_not_shadow_the_standard_library():
    import secrets

    assert secrets.__name__ == "secrets"
    assert secretstore.__name__ == "browser.secretstore"
    assert not (Path(secretstore.__file__).parent / "secrets.py").exists()


# --- a value is found a character at a time ----------------------------------------------------------------------
#
# A page, a browser and Playwright write a value in contexts that mix escapes: a URL keeps `@` and `!` and escapes a
# space, `CSS.escape` escapes a digit at the start of an identifier and not in the middle, and an id is a prefix and a
# value. The scrub finds a value in any mix, and one that a tool cut and ended with an ellipsis by its start.

KEY = {"USER": {"value": "x", "origins": ["https://app.example.com"]}}


def _holding(value):
    return secretstore.Scrubber({"USER": {"value": value, "origins": ["https://app.example.com"]}})


@pytest.mark.parametrize("value, text", [
    # an id made of a prefix and a value: CSS.escape of the whole, not of the value
    ("2Fast4U!", "#pw-2Fast4U\\!"),
    ("1user@example.com", "#pw-1user\\@example\\.com"),
    ("4ever.young", "#pw-4ever\\.young"),
    ("9 lives here", "#pw-9\\ lives\\ here"),
    ("-5x-group", "#pw--5x-group"),
    ("2Fast4U!", "#\\32 Fast4U\\!"),
    # the URL a browser writes: each context escapes what it must and leaves the rest
    ("my pass!", "https://example.com/p?u=my%20pass!"),
    ("my pass!", "https://example.com/p?u=my+pass%21"),
    ("p@ss w0rd", "https://example.com/p?u=p@ss%20w0rd"),
    ("a b/c:d", "https://example.com/a%20b/c:d"),
    ("Tr0ub4dor&3!", "https://example.com/?u=Tr0ub4dor%263!"),
    ("pass~word1", "https://example.com/?u=pass%7Eword1"),
    ("pass~word1", "https://example.com/?u=pass%7eword1"),
    ("caf\u00e9-latte", "https://example.com/caf%C3%A9-latte"),
    ("caf\u00e9-latte", "https://example.com/caf%c3%a9-latte"),
    # strings and markup
    ("pa'ss\"w\\rd", "'pa\\'ss\"w\\\\rd'"),
    ("pa'ss\"w\\rd", '"pa\'ss\\"w\\\\rd"'),
    ("Tr0ub4dor&3!", "<p>Tr0ub4dor&amp;3!</p>"),
    ("it's a trap", "it&#39;s a trap"),
    ("it's a trap", "it&#x27;s a trap"),
    ("it's a trap", "it&apos;s a trap"),
    ("tab\there", "tab\\there"),
    ("tab\there", "tab\\x09here"),
    ("tab\there", "tab\\u0009here"),
    ("caf\u00e9!", "caf\\u00E9!"),
    ("\U0001F600smile", "\\ud83d\\ude00smile"),
])
def test_a_value_is_found_in_the_text_a_context_makes_of_it(value, text):
    scrubber = _holding(value)
    assert scrubber.holds(text), text
    assert "[secret:USER]" in scrubber.scrub(text), text
    assert value not in scrubber.scrub(text)


def test_a_value_is_found_in_every_mix_of_raw_and_escaped_characters():
    """A URL escapes some characters of a value and not others: any of the 2**n mixes is the value."""
    from urllib.parse import quote

    value = "a b@c!d"
    scrubber = _holding(value)
    plain = list(value)
    escaped = [quote(char, safe="") for char in value]
    for mask in range(2 ** len(value)):
        text = "".join(escaped[i] if mask >> i & 1 else plain[i] for i in range(len(value)))
        assert scrubber.holds(f"https://example.com/?x={text}&y=1"), text


@pytest.mark.parametrize("value", ['@pa"ss\\wo{rd}: #x\t\u00e9\u2603', "correct horse battery staple", "user@example.com", "1234abcd"])
def test_a_value_is_found_in_the_text_each_independent_producer_makes_of_it(value):
    import html as html_module
    from urllib.parse import quote, quote_plus

    scrubber = _holding(value)
    produced = [
        value, json.dumps(value), json.dumps(value, ensure_ascii=False), repr(value), quote(value), quote(value, safe=""),
        quote_plus(value), html_module.escape(value), html_module.escape(value, quote=False), secretstore._yaml_quoted(value),
        secretstore._css_escaped(value), secretstore._css_escaped("pw-" + value), secretstore._css_escaped("a" + value)[1:],
        secretstore._css_escaped("pw-" + value)[3:],
    ]
    for text in produced:
        assert scrubber.holds(text), text


def test_the_start_of_a_value_that_a_tool_cut_and_ended_with_an_ellipsis_is_found():
    value = "alex.tester@example.com"
    scrubber = _holding(value)
    for cut in range(4, len(value)):
        for ellipsis in ("\u2026", "..."):
            text = f"Signed in as {value[:cut]}{ellipsis} (more)"
            assert scrubber.scrub(text) == "Signed in as [secret:USER] (more)", text
    assert scrubber.scrub("<div class=\"banner\">Signed in to the example workspace as alex.teste\u2026</div>") == (
        "<div class=\"banner\">Signed in to the example workspace as [secret:USER]</div>"
    )


def test_a_cut_value_in_an_escaped_form_is_found_too():
    value = "pa'ss word-1234"
    scrubber = _holding(value)
    assert scrubber.scrub("x=pa%27ss%20wo\u2026") == "x=[secret:USER]"
    assert scrubber.scrub("repr: 'pa\\'ss wor...'") == "repr: '[secret:USER]'"


@pytest.mark.parametrize("text", [
    "alex", "ale...", "ale\u2026", "alex.teste", "alex ...", "alex .. .", "alexx...", "tester...", "alex..",
])
def test_a_start_that_is_too_short_or_not_followed_by_an_ellipsis_is_left_alone(text):
    scrubber = _holding("alex.tester@example.com")
    assert scrubber.scrub(text) == text


def test_a_cut_start_is_not_what_a_text_holds():
    """`holds` is for the gate on a command line: a text holds a value when the value is in it, whole."""
    scrubber = _holding("alex.tester@example.com")
    assert not scrubber.holds("alex.teste\u2026") and not scrubber.holds("alex...")
    assert scrubber.holds("alex.tester@example.com")


def test_a_whole_text_is_the_value_in_any_form():
    scrubber = _holding("my pass!")
    for text in ("my pass!", "my%20pass!", "my+pass%21", "my\\ pass\\!"):
        assert scrubber.is_value(text), text
    assert not scrubber.is_value("my pass!!") and not scrubber.is_value("x my pass!")


def test_the_longer_of_two_values_wins_in_every_form():
    scrubber = secretstore.Scrubber({
        "SHORT": {"value": "abcd1234", "origins": ["https://app.example.com"]},
        "LONG": {"value": "abcd1234-extra", "origins": ["https://app.example.com"]},
    })
    assert scrubber.scrub("abcd1234-extra abcd1234%2Dextra abcd1234") == "[secret:LONG] [secret:LONG] [secret:SHORT]"


def test_a_value_too_long_for_a_pattern_of_its_own_is_found_by_its_whole_forms(monkeypatch):
    from urllib.parse import quote

    monkeypatch.setattr(secretstore, "MAX_PATTERN_VALUE", 40)
    value = "long value with 'quotes' and spaces " * 3
    scrubber = _holding(value)
    assert scrubber.scrub(f"a {value} b") == "a [secret:USER] b"
    assert scrubber.scrub(f"a {quote(value, safe='')} b") == "a [secret:USER] b"
    assert scrubber.scrub(f"a {json.dumps(value)[1:-1]} b") == "a [secret:USER] b"
    assert scrubber.holds(value)


def test_a_long_value_does_not_take_long_to_compile():
    import time

    started = time.perf_counter()
    scrubber = _holding("".join(chr(33 + (i * 7) % 90) for i in range(1500)))
    assert time.perf_counter() - started < 2.0 and scrubber


def test_the_scrub_of_a_large_text_is_fast_enough():
    import time

    scrubber = secretstore.Scrubber({
        "A": {"value": "hunter2-value", "origins": ["https://app.example.com"]},
        "B": {"value": "correct horse battery staple", "origins": ["https://app.example.com"]},
        "C": {"value": "4821", "origins": ["https://app.example.com"]},
    })
    text = '<div class="x">some page text here with words 1234 and more</div>\n' * 50_000
    started = time.perf_counter()
    scrubber.scrub(text)
    assert time.perf_counter() - started < 5.0


def test_a_hidden_value_is_found_in_the_same_forms():
    scrubber = secretstore.Scrubber({})
    scrubber.hide("PASSCODE", "48 29-16")
    assert scrubber.scrub("code=48+29-16 and 48%2029-16 and #\\34 8\\ 29-16") == (
        "code=[secret:PASSCODE] and [secret:PASSCODE] and #[secret:PASSCODE]"
    )


def test_a_file_with_bytes_that_are_not_utf8_and_a_value_in_it_is_removed_when_asked(tmp_path):
    scrubber = _holding("hunter2-value")
    target = tmp_path / "console.log"
    target.write_bytes(b"[LOG] typed hunter2-value \xff\xfe \x00\n")
    assert scrubber.scrub_file(str(target)) is False and target.exists(), "by default it is left alone"
    assert scrubber.scrub_file(str(target), remove_on_failure=True) is True
    assert not target.exists()


def test_a_file_with_bytes_that_are_not_utf8_and_no_value_in_it_is_left_alone_even_when_asked(tmp_path):
    scrubber = _holding("hunter2-value")
    target = tmp_path / "page.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe nothing here \x00")
    assert scrubber.scrub_file(str(target), remove_on_failure=True) is False
    assert target.read_bytes() == b"\x89PNG\r\n\x1a\n\xff\xfe nothing here \x00"


def test_a_long_value_cut_in_an_attribute_is_followed_as_far_as_it_shows():
    """Playwright cuts the attributes of an element at 500 characters: a value of 300 shows 250 of them."""
    value = "".join(chr(97 + (i * 7) % 26) for i in range(300))
    scrubber = _holding(value)
    shown = f'<input type="text" id="token" data-x="{value[:250]}\u2026'
    assert scrubber.scrub(shown) == '<input type="text" id="token" data-x="[secret:USER]'


def test_a_value_that_ends_in_dots_is_cut_at_the_last_three_of_a_run():
    scrubber = _holding("wait for it...")
    assert scrubber.scrub("x wait for it... y") == "x [secret:USER] y"
    assert scrubber.scrub("x wait f.... y") == "x [secret:USER]. y", "the last three dots would leave `wait f.`, which is no start of it"
    assert scrubber.scrub("x wait for it....... y") == "x [secret:USER].. y", "the value, and the cut that the dots can be, are one stretch"


def test_two_cut_values_in_one_text_are_both_found():
    scrubber = secretstore.Scrubber({
        "USER": {"value": "alex.tester@example.com", "origins": ["https://app.example.com"]},
        "PASS": {"value": "correct horse battery staple", "origins": ["https://app.example.com"]},
    })
    text = "a alex.teste\u2026 b correct horse bat... c"
    assert scrubber.scrub(text) == "a [secret:USER] b [secret:PASS] c"


def test_many_ellipses_in_a_large_text_cost_little():
    import time

    scrubber = _holding("alex.tester@example.com")
    text = "Loading... please wait\u2026 and alex.t... " * 2_000
    started = time.perf_counter()
    out = scrubber.scrub(text)
    assert time.perf_counter() - started < 10.0
    assert out.count("[secret:USER]") == 2_000 and "alex.t" not in out
