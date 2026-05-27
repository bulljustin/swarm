"""Tests for :class:`swarm.db.buzz_store.BuzzStore`.

The buzz log store had **0% direct coverage** before this file
landed — every production write goes through ``DroneLog`` (which is
mocked in most test paths) and reads go through the dashboard's
``/api/system_log`` route (also mocked at the daemon level).  This
file fills the gap by exercising the store directly against a
temp-path SQLite DB.

Coverage gap closed in the 2026-05-27 test-gap fill-in, phase 2
(storage layer).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from swarm.db.buzz_store import BuzzStore
from swarm.db.core import SwarmDB


@pytest.fixture
def store(tmp_path: Path) -> BuzzStore:
    db = SwarmDB(Path(tmp_path) / "swarm.db")
    return BuzzStore(db)


# ---------------------------------------------------------------------------
# insert + round-trip
# ---------------------------------------------------------------------------


class TestInsert:
    def test_minimal_insert_returns_rowid(self, store: BuzzStore) -> None:
        rowid = store.insert(
            timestamp=time.time(),
            action="STATE_TRANSITION",
            worker_name="api",
        )
        assert isinstance(rowid, int)
        assert rowid > 0

    def test_insert_round_trips_via_query(self, store: BuzzStore) -> None:
        ts = time.time()
        store.insert(
            timestamp=ts,
            action="TASK_ASSIGNED",
            worker_name="api",
            detail="queued: T#123",
            category="task",
            is_notification=True,
            metadata={"task_id": "abc-123"},
            repeat_count=2,
        )
        rows = store.query()
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "TASK_ASSIGNED"
        assert row["worker_name"] == "api"
        assert row["detail"] == "queued: T#123"
        assert row["category"] == "task"
        assert row["is_notification"] is True
        assert row["metadata"] == {"task_id": "abc-123"}
        assert row["repeat_count"] == 2
        assert row["timestamp"] == pytest.approx(ts, abs=0.001)

    def test_insert_defaults(self, store: BuzzStore) -> None:
        """Default category 'drone', is_notification False, empty metadata."""
        store.insert(timestamp=time.time(), action="X", worker_name="w")
        row = store.query()[0]
        assert row["category"] == "drone"
        assert row["is_notification"] is False
        assert row["metadata"] == {}
        assert row["repeat_count"] == 1

    def test_metadata_none_persists_as_empty_dict(self, store: BuzzStore) -> None:
        """``metadata=None`` round-trips as ``{}`` — not None."""
        store.insert(timestamp=time.time(), action="X", worker_name="w", metadata=None)
        assert store.query()[0]["metadata"] == {}


# ---------------------------------------------------------------------------
# load_recent — startup hydration in chronological order
# ---------------------------------------------------------------------------


class TestLoadRecent:
    def test_returns_chronological_order(self, store: BuzzStore) -> None:
        """load_recent reverses DESC results into chronological (ASC) order."""
        store.insert(timestamp=100.0, action="A", worker_name="w")
        store.insert(timestamp=200.0, action="B", worker_name="w")
        store.insert(timestamp=300.0, action="C", worker_name="w")
        rows = store.load_recent()
        assert [r["action"] for r in rows] == ["A", "B", "C"]

    def test_respects_limit(self, store: BuzzStore) -> None:
        for i in range(5):
            store.insert(timestamp=float(i), action=f"A{i}", worker_name="w")
        rows = store.load_recent(limit=3)
        # 3 most-recent (by timestamp DESC), reversed back to chronological:
        # A2, A3, A4
        assert [r["action"] for r in rows] == ["A2", "A3", "A4"]


# ---------------------------------------------------------------------------
# query — filter combinations
# ---------------------------------------------------------------------------


class TestQuery:
    def _seed_mixed(self, store: BuzzStore) -> None:
        store.insert(timestamp=100.0, action="A", worker_name="api", category="drone")
        store.insert(timestamp=200.0, action="B", worker_name="api", category="task")
        store.insert(timestamp=300.0, action="A", worker_name="web", category="drone")
        store.insert(timestamp=400.0, action="C", worker_name="web", category="task")

    def test_no_filters_returns_all_desc(self, store: BuzzStore) -> None:
        self._seed_mixed(store)
        rows = store.query()
        assert [r["timestamp"] for r in rows] == [400.0, 300.0, 200.0, 100.0]

    def test_filter_by_worker_name(self, store: BuzzStore) -> None:
        self._seed_mixed(store)
        rows = store.query(worker_name="api")
        assert {r["worker_name"] for r in rows} == {"api"}
        assert len(rows) == 2

    def test_filter_by_action(self, store: BuzzStore) -> None:
        self._seed_mixed(store)
        rows = store.query(action="A")
        assert {r["action"] for r in rows} == {"A"}
        assert len(rows) == 2

    def test_filter_by_category(self, store: BuzzStore) -> None:
        self._seed_mixed(store)
        rows = store.query(category="task")
        assert {r["category"] for r in rows} == {"task"}

    def test_filter_by_since(self, store: BuzzStore) -> None:
        self._seed_mixed(store)
        rows = store.query(since=250.0)
        # Only timestamps >= 250: 300 and 400
        assert {r["timestamp"] for r in rows} == {300.0, 400.0}

    def test_filter_by_until(self, store: BuzzStore) -> None:
        self._seed_mixed(store)
        rows = store.query(until=250.0)
        # Only timestamps <= 250: 100 and 200
        assert {r["timestamp"] for r in rows} == {100.0, 200.0}

    def test_combined_filters_AND(self, store: BuzzStore) -> None:
        self._seed_mixed(store)
        rows = store.query(worker_name="api", action="A")
        assert len(rows) == 1
        assert rows[0]["timestamp"] == 100.0

    def test_limit_and_offset(self, store: BuzzStore) -> None:
        for i in range(10):
            store.insert(timestamp=float(i), action="X", worker_name="w")
        page1 = store.query(limit=3, offset=0)
        page2 = store.query(limit=3, offset=3)
        # No overlap between pages
        ids1 = {r["id"] for r in page1}
        ids2 = {r["id"] for r in page2}
        assert ids1.isdisjoint(ids2)
        assert len(page1) == 3
        assert len(page2) == 3


# ---------------------------------------------------------------------------
# search — free-text LIKE across detail + worker_name
# ---------------------------------------------------------------------------


class TestSearch:
    def test_matches_detail_substring(self, store: BuzzStore) -> None:
        store.insert(timestamp=100.0, action="X", worker_name="api", detail="failed to send")
        store.insert(timestamp=200.0, action="X", worker_name="web", detail="ok")
        rows = store.search("failed")
        assert len(rows) == 1
        assert rows[0]["detail"] == "failed to send"

    def test_matches_worker_name_substring(self, store: BuzzStore) -> None:
        store.insert(timestamp=100.0, action="X", worker_name="api-worker")
        store.insert(timestamp=200.0, action="X", worker_name="web-worker")
        rows = store.search("api")
        assert {r["worker_name"] for r in rows} == {"api-worker"}

    def test_search_respects_limit(self, store: BuzzStore) -> None:
        for i in range(20):
            store.insert(timestamp=float(i), action="X", worker_name="w", detail="match-me")
        rows = store.search("match-me", limit=5)
        assert len(rows) == 5

    def test_no_match_returns_empty(self, store: BuzzStore) -> None:
        store.insert(timestamp=100.0, action="X", worker_name="w", detail="hello")
        assert store.search("nonexistent") == []


# ---------------------------------------------------------------------------
# count — filtered totals
# ---------------------------------------------------------------------------


class TestCount:
    def test_no_filter_returns_total(self, store: BuzzStore) -> None:
        for i in range(5):
            store.insert(timestamp=float(i), action="X", worker_name="w")
        assert store.count() == 5

    def test_filter_by_worker(self, store: BuzzStore) -> None:
        store.insert(timestamp=100.0, action="X", worker_name="api")
        store.insert(timestamp=200.0, action="X", worker_name="web")
        store.insert(timestamp=300.0, action="X", worker_name="api")
        assert store.count(worker_name="api") == 2
        assert store.count(worker_name="web") == 1
        assert store.count(worker_name="missing") == 0

    def test_filter_by_action(self, store: BuzzStore) -> None:
        store.insert(timestamp=100.0, action="A", worker_name="w")
        store.insert(timestamp=200.0, action="B", worker_name="w")
        assert store.count(action="A") == 1
        assert store.count(action="B") == 1

    def test_filter_by_since(self, store: BuzzStore) -> None:
        store.insert(timestamp=100.0, action="X", worker_name="w")
        store.insert(timestamp=200.0, action="X", worker_name="w")
        store.insert(timestamp=300.0, action="X", worker_name="w")
        assert store.count(since=150.0) == 2  # 200 + 300


# ---------------------------------------------------------------------------
# rule_analytics — per-rule firing aggregates
# ---------------------------------------------------------------------------


class TestRuleAnalytics:
    def test_aggregates_continued_and_escalated(self, store: BuzzStore) -> None:
        for _ in range(3):
            store.insert(
                timestamp=time.time(), action="CONTINUED", worker_name="api", detail="rule:foo"
            )
        store.insert(
            timestamp=time.time(), action="ESCALATED", worker_name="api", detail="rule:bar"
        )
        # Ignored — not CONTINUED/ESCALATED
        store.insert(timestamp=time.time(), action="STATE_TRANSITION", worker_name="api")

        rows = store.rule_analytics()
        # Two groups: (CONTINUED, "rule:foo") with count 3, (ESCALATED, "rule:bar") with count 1
        assert len(rows) == 2
        # Sorted by count DESC
        assert rows[0]["count"] == 3
        assert rows[0]["action"] == "CONTINUED"
        assert rows[0]["detail"] == "rule:foo"

    def test_respects_since_filter(self, store: BuzzStore) -> None:
        store.insert(timestamp=100.0, action="CONTINUED", worker_name="w", detail="r1")
        store.insert(timestamp=200.0, action="CONTINUED", worker_name="w", detail="r2")
        rows = store.rule_analytics(since=150.0)
        # Only the timestamp 200 entry
        assert len(rows) == 1
        assert rows[0]["detail"] == "r2"


# ---------------------------------------------------------------------------
# Misc lifecycle methods
# ---------------------------------------------------------------------------


class TestMisc:
    def test_mark_overridden_is_truthful_noop(self, store: BuzzStore) -> None:
        """``mark_overridden`` is a schema-stub; always returns True."""
        rowid = store.insert(timestamp=time.time(), action="X", worker_name="w")
        assert store.mark_overridden(rowid, "approve") is True

    def test_mark_recent_overridden_returns_none(self, store: BuzzStore) -> None:
        """Schema lacks the column — method returns None per contract."""
        store.insert(timestamp=time.time(), action="X", worker_name="api")
        assert store.mark_recent_overridden("api", "approve") is None

    def test_close_is_noop(self, store: BuzzStore) -> None:
        """``close()`` is a no-op (SwarmDB owns lifecycle)."""
        store.close()  # Should not raise


# ---------------------------------------------------------------------------
# prune — TTL-based deletion
# ---------------------------------------------------------------------------


class TestPrune:
    def test_prune_with_default_returns_zero_when_all_fresh(self, store: BuzzStore) -> None:
        store.insert(timestamp=time.time(), action="X", worker_name="w")
        assert store.prune() == 0
        assert store.count() == 1

    def test_prune_deletes_old_entries(self, store: BuzzStore) -> None:
        # An entry from ~60 days ago
        old_ts = time.time() - 60 * 86400
        store.insert(timestamp=old_ts, action="OLD", worker_name="w")
        # A recent entry
        store.insert(timestamp=time.time(), action="NEW", worker_name="w")
        deleted = store.prune(max_age_days=30)
        assert deleted == 1
        assert store.count() == 1
        assert store.query()[0]["action"] == "NEW"
