from __future__ import annotations

import tasks
from tasks import (
    close_task,
    ensure_task,
    expire_tasks,
    load_task,
    new_task,
    new_thread_id,
    save_task,
)
from ulid import new as new_ulid

NODE = "acme"


class TestThreadId:
    def test_mints_distinct_ulids(self):
        a, b = new_thread_id(), new_thread_id()
        assert a != b
        assert len(a) == 26 and len(b) == 26


class TestLedger:
    def test_ensure_creates_and_persists(self, r4t_home):
        task_id = new_ulid()
        task = ensure_task(NODE, task_id, "gerry")
        assert task["creator"] == "gerry"
        assert task["status"] == tasks.STATUS_OPEN
        assert not task["answered"]
        again = ensure_task(NODE, task_id, "someone-else")
        assert again["creator"] == "gerry"

    def test_close_marks_answered(self, r4t_home):
        task_id = new_ulid()
        ensure_task(NODE, task_id, "gerry")
        close_task(NODE, task_id)
        task = load_task(NODE, task_id)
        assert task["status"] == tasks.STATUS_CLOSED
        assert task["answered"]

    def test_close_missing_is_noop(self, r4t_home):
        close_task(NODE, new_ulid())  # no exception

    def test_corrupt_ledger_returns_none(self, r4t_home):
        task_id = new_ulid()
        path = tasks.task_path(NODE, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        assert load_task(NODE, task_id) is None


class TestExpiry:
    def test_expires_only_stale_tasks(self, r4t_home):
        stale = new_task(new_ulid(), "gerry")
        stale["updated_at"] = "2020-01-01T00:00:00Z"
        tasks.atomic_write_json(tasks.task_path(NODE, stale["id"]), stale)
        fresh = ensure_task(NODE, new_ulid(), "gerry")

        removed = expire_tasks(NODE, older_than_seconds=86400)
        assert removed == [stale["id"]]
        assert load_task(NODE, stale["id"]) is None
        assert load_task(NODE, fresh["id"]) is not None

    def test_save_refreshes_updated_at(self, r4t_home):
        task = new_task(new_ulid(), "gerry")
        task["updated_at"] = "2020-01-01T00:00:00Z"
        save_task(NODE, task)
        assert task["updated_at"] > "2020-01-01"
