import json
import os

from gemini_web import store


def test_state_lives_under_the_state_home(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "/data")
    assert store.state_root("gemini") == "/data/ar3/gemini-web/gemini"


def test_without_a_state_home_it_falls_back_to_the_local_share(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert store.state_root("gemini") == os.path.expanduser("~/.local/share/ar3/gemini-web/gemini")


def test_two_seats_on_one_machine_keep_separate_conversations(state_home):
    assert store.SessionStore("gemini").path != store.SessionStore("gemini-two").path


def test_a_conversation_survives_a_restart(state_home):
    store.SessionStore("gemini").remember("example-sender", "https://gemini.google.com/app/abc")
    fresh = store.SessionStore("gemini")
    assert fresh.get("example-sender")["url"] == "https://gemini.google.com/app/abc"
    assert fresh.get("example-sender")["created"]
    assert not fresh.warning


def test_a_correspondent_is_matched_however_the_sender_is_cased(state_home):
    store.SessionStore("gemini").remember("Example-Sender", "https://gemini.google.com/app/abc")
    assert store.SessionStore("gemini").get("example-SENDER")


def test_a_second_message_replaces_the_url_rather_than_adding_a_conversation(state_home):
    first = store.SessionStore("gemini")
    first.remember("example-sender", "https://gemini.google.com/app/one")
    first.remember("example-sender", "https://gemini.google.com/app/two")
    sessions = store.SessionStore("gemini").all()
    assert list(sessions) == ["example-sender"]
    assert sessions["example-sender"]["url"].endswith("two")


def test_forgetting_a_correspondent_reports_whether_there_was_one(state_home):
    keeper = store.SessionStore("gemini")
    keeper.remember("example-sender", "https://gemini.google.com/app/abc")
    assert keeper.forget("example-sender")
    assert not keeper.forget("example-sender")
    assert store.SessionStore("gemini").all() == {}


def test_a_corrupt_store_is_empty_and_says_so_instead_of_dying(state_home):
    broken = store.SessionStore("gemini")
    os.makedirs(broken.root, exist_ok=True)
    with open(broken.path, "w") as handle:
        handle.write("{not json at all")

    assert broken.all() == {}
    assert broken.get("example-sender") is None
    assert broken.path in broken.warning


def test_a_store_of_the_wrong_shape_is_treated_the_same_way(state_home):
    odd = store.SessionStore("gemini")
    os.makedirs(odd.root, exist_ok=True)
    with open(odd.path, "w") as handle:
        json.dump(["example-sender"], handle)

    assert odd.all() == {}
    assert "expected shape" in odd.warning


def test_a_corrupt_store_is_repaired_by_the_next_conversation(state_home):
    broken = store.SessionStore("gemini")
    os.makedirs(broken.root, exist_ok=True)
    with open(broken.path, "w") as handle:
        handle.write("half a file")
    broken.remember("example-sender", "https://gemini.google.com/app/abc")

    assert store.SessionStore("gemini").get("example-sender")
    assert not store.SessionStore("gemini").warning


def test_a_missing_store_is_not_a_warning(state_home):
    empty = store.SessionStore("gemini")
    assert empty.all() == {}
    assert empty.warning == ""


def test_the_write_leaves_nothing_half_finished_behind(state_home):
    keeper = store.SessionStore("gemini")
    keeper.remember("example-sender", "https://gemini.google.com/app/abc")
    # The lock is a permanent fixture beside the store; a temp file is not.
    written = sorted(name for name in os.listdir(keeper.root) if not name.endswith(".lock"))
    assert written == [os.path.basename(keeper.path)]
    with open(keeper.path) as handle:
        assert json.load(handle)["sessions"]["example-sender"]["url"].endswith("abc")
