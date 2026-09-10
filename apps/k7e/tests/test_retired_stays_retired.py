"""A retired entry stays retired, whatever a later turn says about it.

The correction pass retires the claim a person contradicted. Then some later
turn — the same member days on, or a peer repeating what it heard — names the
retired id in what it says, and the ordinary pipeline reaches that entry
through the id lookup and grows it. The append re-indexes the node under
today's date, and the claim the store had already replaced ranks again under
its own id, in front of the correction that replaced it.

Three layers stop it and each is tested on its own: distillation never picks a
non-active node to write to, `append_entry` refuses one, and the index carries
the status the file carries so search cannot be told otherwise.
"""
import json

import pytest

import distill
import engine
import hygiene
from conftest import write_path_executable


class TestARetiredEntryIsNeverAnAppendTarget:
    STAMP = "20260909T170000000000Z"
    OPEN_BUG = (
        "The widget renderer crashes on an empty payload. The bug is open and "
        "needs a fix before the next release ships."
    )
    CLOSURE = (
        "The owner closed the widget renderer crash in June. Nothing about an "
        "empty payload is outstanding and no fix remains to be made."
    )

    @pytest.fixture
    def retired(self, store):
        """An open-bug entry and the closure that replaced it, as the
        correction pass leaves them: `(old_id, new_id)`."""
        old = engine.store_entry("Widget renderer crash", self.OPEN_BUG, tags=["bugs"])
        new = engine.store_entry(
            "Widget renderer crash closed", self.CLOSURE, tags=["bugs"]
        )
        engine.supersede(old, new)
        return old, new

    def _llm(self, tmp_path, monkeypatch, old_id):
        """Extraction shaped the way the incident was: one candidate that
        names the retired id and says the retired claim again, close enough to
        that entry that the pipeline would hang it off it as an edge case."""
        candidates = [{
            "title": "Customer screenshot from Tuesday",
            "content": (
                f"{old_id}: a customer screenshot shows the widget renderer "
                "crashing on an empty payload, which is the open bug."
            ),
            "tags": ["bugs"],
        }]
        wrapper = write_path_executable(tmp_path, "fake-llm", (
            "import sys\n"
            "sys.stdin.read()\n"
            f"print({json.dumps(candidates)!r})\n"
        ))
        monkeypatch.setenv("K7E_LLM_COMMAND", str(wrapper))

    def _capture(self, tmp_path, old_id):
        """A later turn capture with no human section — a peer said this, so
        the correction pass is never asked and the file distills the ordinary
        way with the retired id sitting in its prose."""
        path = tmp_path / "later-turn.md"
        path.write_text(
            f"# turn {self.STAMP} (Phil)\n\n"
            f"- stamp: {self.STAMP}\n"
            "- threads: 01X\n"
            "- exit: 0\n"
            "- rig: junior-dev\n"
            f"- knowledge: {old_id}\n\n"
            "## Prompt\n\n"
            "A peer mentioned an old entry.\n\n"
            "## Output\n\n"
            f"A peer brought up {old_id} again this morning.\n",
            encoding="utf-8",
        )
        return path

    def _journal(self, tmp_path, old_id):
        """An ordinary file, no stamp and no output section — distilled with
        no provenance and no correction pass at all."""
        path = tmp_path / "journal.md"
        path.write_text(
            f"Notes from the week, written by hand. Somebody quoted {old_id} "
            "at me again and I wrote it down without checking it.\n",
            encoding="utf-8",
        )
        return path

    def _assert_still_retired(self, old_id, results):
        assert not any(
            r["action"] == "appended" and r.get("id") == old_id for r in results
        ), results
        assert "status: superseded" in engine.get(old_id)
        assert old_id not in {n["id"] for n in engine.list_nodes(status="active")}
        assert old_id not in {
            h["id"] for h in engine.search("widget renderer crash", rerank=False)
        }
        assert hygiene.index_disagreement() is None

    def test_a_later_turn_naming_the_retired_id_does_not_revive_it(
        self, retired, tmp_path, monkeypatch
    ):
        old_id, _ = retired
        self._llm(tmp_path, monkeypatch, old_id)
        results = distill.distill([str(self._capture(tmp_path, old_id))])
        self._assert_still_retired(old_id, results)

    def test_an_ordinary_file_naming_the_retired_id_does_not_revive_it(
        self, retired, tmp_path, monkeypatch
    ):
        old_id, _ = retired
        self._llm(tmp_path, monkeypatch, old_id)
        results = distill.distill([str(self._journal(tmp_path, old_id))])
        self._assert_still_retired(old_id, results)

    def test_the_closure_is_what_the_candidate_is_measured_against(
        self, retired, tmp_path, monkeypatch
    ):
        """Dropping the retired entry does not blind the pipeline: the
        replacement is still in the pool, so a restatement is deduped or
        appended against the entry that is current, never stored beside it as
        a fresh claim that outranks nothing."""
        old_id, new_id = retired
        self._llm(tmp_path, monkeypatch, old_id)
        results = distill.distill([str(self._capture(tmp_path, old_id))])
        touched = {r.get("id") for r in results if r["action"] == "appended"}
        assert old_id not in touched
        assert touched <= {new_id}

    def test_append_entry_refuses_a_retired_entry_and_writes_nothing(self, retired):
        old_id, _ = retired
        before = engine.get(old_id)
        with pytest.raises(ValueError, match="superseded"):
            engine.append_entry(old_id, "Edge Cases", "A screenshot from Tuesday.")
        assert engine.get(old_id) == before
        assert old_id not in {n["id"] for n in engine.list_nodes(status="active")}

    def test_the_cli_reports_the_refusal_and_fails(self, retired, capsys):
        old_id, _ = retired
        import cli
        assert cli.main(["append", old_id, "--content", "A screenshot."]) == 1
        assert old_id in capsys.readouterr().err
        assert "status: superseded" in engine.get(old_id)

    def test_distill_reports_a_refusal_rather_than_stopping_the_sweep(
        self, retired, tmp_path, monkeypatch
    ):
        """The boundary is the last word even when something upstream hands it
        a retired target. A raise here would cost the whole sweep, and
        `dream_sweep` re-runs a failed directory."""
        old_id, _ = retired
        self._llm(tmp_path, monkeypatch, old_id)
        monkeypatch.setattr(
            distill, "diff_against_store",
            lambda candidates: [dict(c, _append_to=old_id) for c in candidates],
        )
        results = distill.distill([str(self._capture(tmp_path, old_id))])
        assert [r["action"] for r in results] == ["refused"], results
        assert "status: superseded" in engine.get(old_id)


class TestTheIndexKeepsTheFilesStatus:
    def test_an_append_cannot_index_a_node_as_active_against_its_file(self, store):
        """The write path is closed, so the index is proved on the function
        that writes the row: a node whose file says superseded indexes as
        superseded, whatever the caller was doing."""
        node_id = engine.store_entry("Widget renderer crash", "The bug is open.")
        engine.supersede(node_id, "K7E-000-09999")
        text = engine.get(node_id)

        engine._index_node(
            node_id, "Widget renderer crash", [], [], engine._extract_body(text),
            "2026-09-09", status=engine._parse_frontmatter(text)["status"],
        )

        assert [n["status"] for n in engine.list_nodes() if n["id"] == node_id] == [
            "superseded"
        ]
        assert node_id not in {
            h["id"] for h in engine.search("widget renderer crash", rerank=False)
        }
        assert hygiene.index_disagreement() is None

    def test_reindex_agrees_with_the_index_the_writes_left(self, store):
        node_id = engine.store_entry("Widget renderer crash", "The bug is open.")
        engine.store_entry("Widget renderer closed", "The owner closed it in June.")
        engine.supersede(node_id, "K7E-000-09999")
        before = engine.list_nodes()

        engine.reindex()

        assert engine.list_nodes() == before
        assert hygiene.index_disagreement() is None
