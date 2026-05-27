"""Tests for :class:`swarm.db.task_history.SqliteTaskHistory`.

The SQLite task history store had **45% coverage** before this file
landed — ``append`` was exercised through daemon proxies but
``get_events``, ``search``, and ``prune`` were unexplored.  Filling
those gaps catches regressions in the operator-visible audit-log
surface that the dashboard's task drawer renders.

Coverage gap closed in the 2026-05-27 test-gap fill-in, phase 2.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from swarm.db.core import SwarmDB
from swarm.db.task_history import SqliteTaskHistory
from swarm.tasks.history import TaskAction


@pytest.fixture
def history(tmp_path: Path) -> SqliteTaskHistory:
    db = SwarmDB(Path(tmp_path) / "swarm.db")
    return SqliteTaskHistory(db)


def _seed_task(history: SqliteTaskHistory, task_id: str) -> None:
    """Insert a stub task row so the task_history FK constraint passes.

    The task_history table carries ``REFERENCES tasks(id) ON DELETE
    CASCADE``, so every history insert needs a parent row.  Production
    code path is ``task_board.create()`` → ``task_history.append()`` in
    sequence; these unit tests seed the parent by hand.
    """
    history._db.insert(
        "tasks",
        {
            "id": task_id,
            "title": f"stub-{task_id}",
            "status": "unassigned",
            "priority": "normal",
            "task_type": "chore",
            "created_at": 0.0,
            "updated_at": 0.0,
        },
    )


# ---------------------------------------------------------------------------
# append + get_events round-trip
# ---------------------------------------------------------------------------


class TestAppend:
    def test_append_returns_event(self, history: SqliteTaskHistory) -> None:
        _seed_task(history, "task-1")
        ev = history.append("task-1", TaskAction.CREATED, actor="user", detail="created")
        assert ev.task_id == "task-1"
        assert ev.action == TaskAction.CREATED
        assert ev.actor == "user"
        assert ev.detail == "created"
        assert ev.timestamp > 0

    def test_append_defaults(self, history: SqliteTaskHistory) -> None:
        _seed_task(history, "task-1")
        ev = history.append("task-1", TaskAction.ASSIGNED)
        assert ev.actor == "user"
        assert ev.detail == ""


# ---------------------------------------------------------------------------
# get_events — per-task chronology
# ---------------------------------------------------------------------------


class TestGetEvents:
    def test_returns_chronological_order(self, history: SqliteTaskHistory) -> None:
        _seed_task(history, "t1")
        history.append("t1", TaskAction.CREATED, detail="step1")
        history.append("t1", TaskAction.ASSIGNED, detail="step2")
        history.append("t1", TaskAction.COMPLETED, detail="step3")
        events = history.get_events("t1")
        # get_events DESCs from DB then reverses to chronological
        assert [e.detail for e in events] == ["step1", "step2", "step3"]

    def test_filters_by_task_id(self, history: SqliteTaskHistory) -> None:
        _seed_task(history, "t1")
        _seed_task(history, "t2")
        history.append("t1", TaskAction.CREATED)
        history.append("t2", TaskAction.CREATED)
        history.append("t1", TaskAction.ASSIGNED)
        events = history.get_events("t1")
        assert {e.task_id for e in events} == {"t1"}
        assert len(events) == 2

    def test_respects_limit(self, history: SqliteTaskHistory) -> None:
        _seed_task(history, "t1")
        for _ in range(10):
            history.append("t1", TaskAction.ASSIGNED)
        events = history.get_events("t1", limit=3)
        assert len(events) == 3

    def test_empty_task_returns_empty_list(self, history: SqliteTaskHistory) -> None:
        assert history.get_events("nonexistent") == []

    def test_unknown_action_value_is_skipped(self, history: SqliteTaskHistory) -> None:
        """An action stored under a name no longer in the enum is silently dropped.

        This protects the dashboard from a KeyError when an old daemon
        wrote an action enum that a newer build later removed.  Hit the
        skip branch by inserting a row with a bogus action directly.
        """
        _seed_task(history, "t1")
        history.append("t1", TaskAction.ASSIGNED, detail="ok")
        # Bypass append() to write a malformed action
        history._db.insert(
            "task_history",
            {
                "task_id": "t1",
                "action": "GHOST_ACTION",
                "actor": "system",
                "detail": "stale",
                "created_at": time.time(),
            },
        )
        events = history.get_events("t1")
        # Only the well-formed one survives
        assert len(events) == 1
        assert events[0].detail == "ok"


# ---------------------------------------------------------------------------
# search — cross-task audit log
# ---------------------------------------------------------------------------


class TestSearch:
    def _seed(self, history: SqliteTaskHistory) -> None:
        # Spread timestamps so since/until filters bite
        for tid in ("t1", "t2", "t3"):
            _seed_task(history, tid)
        history._db.insert(
            "task_history",
            {
                "task_id": "t1",
                "action": "CREATED",
                "actor": "user",
                "detail": "fixed login bug",
                "created_at": 100.0,
            },
        )
        history._db.insert(
            "task_history",
            {
                "task_id": "t2",
                "action": "ASSIGNED",
                "actor": "system",
                "detail": "queued for api",
                "created_at": 200.0,
            },
        )
        history._db.insert(
            "task_history",
            {
                "task_id": "t3",
                "action": "COMPLETED",
                "actor": "user",
                "detail": "shipped",
                "created_at": 300.0,
            },
        )

    def test_no_filters_returns_all(self, history: SqliteTaskHistory) -> None:
        self._seed(history)
        events, total = history.search()
        assert total == 3
        assert len(events) == 3

    def test_query_matches_detail(self, history: SqliteTaskHistory) -> None:
        self._seed(history)
        events, total = history.search(query="login")
        assert total == 1
        assert events[0].detail == "fixed login bug"

    def test_query_matches_task_id(self, history: SqliteTaskHistory) -> None:
        self._seed(history)
        events, total = history.search(query="t2")
        assert total == 1
        assert events[0].task_id == "t2"

    def test_filter_by_action(self, history: SqliteTaskHistory) -> None:
        self._seed(history)
        events, total = history.search(action="COMPLETED")
        assert total == 1
        assert events[0].action == TaskAction.COMPLETED

    def test_filter_by_actor(self, history: SqliteTaskHistory) -> None:
        self._seed(history)
        events, total = history.search(actor="system")
        assert total == 1
        assert events[0].actor == "system"

    def test_filter_by_since(self, history: SqliteTaskHistory) -> None:
        self._seed(history)
        events, total = history.search(since=150.0)
        # 200 + 300
        assert total == 2
        assert {e.task_id for e in events} == {"t2", "t3"}

    def test_filter_by_until(self, history: SqliteTaskHistory) -> None:
        self._seed(history)
        events, total = history.search(until=150.0)
        assert total == 1
        assert events[0].task_id == "t1"

    def test_pagination(self, history: SqliteTaskHistory) -> None:
        for i in range(10):
            _seed_task(history, f"t{i}")
            history._db.insert(
                "task_history",
                {
                    "task_id": f"t{i}",
                    "action": "created",
                    "actor": "user",
                    "detail": "",
                    "created_at": float(i),
                },
            )
        page1, total1 = history.search(limit=3, offset=0)
        page2, total2 = history.search(limit=3, offset=3)
        assert total1 == total2 == 10
        # Different pages have different events
        ids1 = {e.task_id for e in page1}
        ids2 = {e.task_id for e in page2}
        assert ids1.isdisjoint(ids2)

    def test_search_skips_malformed_action(self, history: SqliteTaskHistory) -> None:
        """Same skip-on-bad-action protection as ``get_events``."""
        _seed_task(history, "t1")
        _seed_task(history, "t2")
        history._db.insert(
            "task_history",
            {
                "task_id": "t1",
                "action": "ASSIGNED",
                "actor": "user",
                "detail": "ok",
                "created_at": 100.0,
            },
        )
        history._db.insert(
            "task_history",
            {
                "task_id": "t2",
                "action": "PHANTOM_VALUE",
                "actor": "user",
                "detail": "stale",
                "created_at": 200.0,
            },
        )
        events, total = history.search()
        # Total counts ALL rows in DB; events drops the malformed one
        assert total == 2
        assert len(events) == 1


# ---------------------------------------------------------------------------
# prune — TTL-based cleanup
# ---------------------------------------------------------------------------


class TestPrune:
    def test_prune_returns_zero_when_all_fresh(self, history: SqliteTaskHistory) -> None:
        _seed_task(history, "t1")
        history.append("t1", TaskAction.CREATED)
        assert history.prune() == 0

    def test_prune_deletes_old_entries(self, history: SqliteTaskHistory) -> None:
        _seed_task(history, "old")
        _seed_task(history, "new")
        old_ts = time.time() - 100 * 86400  # 100 days
        history._db.insert(
            "task_history",
            {
                "task_id": "old",
                "action": "CREATED",
                "actor": "user",
                "detail": "",
                "created_at": old_ts,
            },
        )
        history.append("new", TaskAction.CREATED)
        # Default prune cutoff is 90 days
        deleted = history.prune()
        assert deleted == 1
        events, total = history.search()
        assert total == 1
        assert events[0].task_id == "new"
