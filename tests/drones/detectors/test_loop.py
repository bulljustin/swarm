"""Tests for :class:`swarm.drones.detectors.loop.LoopDetector` (task #761).

The detector reads the ScheduleWakeup tool result a worker parked between
native ``/loop`` fires emits, and holds a precise no-disturb window so the
idle-watcher and speculative dispatch leave it alone until its next tick.
See ``docs/specs/native-loop-functions.md`` §2.
"""

from __future__ import annotations

import time

from swarm.drones.detectors import LoopDetector
from swarm.providers.claude import _RE_LOOP_WAKEUP
from swarm.worker.worker import Worker

# The exact line the harness prints when a loop self-schedules its next tick.
_WAKEUP = (
    "Next wakeup scheduled for 2026-06-22T14:05:00 (in 270s). "
    "Nothing more to do this turn — the harness re-invokes you when the "
    "wakeup fires or a task-notification arrives."
)


def _make_worker(name: str = "w1") -> Worker:
    return Worker(name=name, path=f"/tmp/{name}")


class TestSignalRegex:
    """The provider regex captures the dwell from the real wakeup line."""

    def test_matches_and_captures_seconds(self) -> None:
        m = _RE_LOOP_WAKEUP.search(_WAKEUP)
        assert m is not None
        assert m.group(1) == "270"

    def test_no_match_on_ordinary_output(self) -> None:
        assert _RE_LOOP_WAKEUP.search("Done. All tests pass.") is None

    def test_no_match_on_gate_off_message(self) -> None:
        # When the loop has ended the harness prints a *different* line that
        # must NOT arm the window.
        ended = (
            "Wakeup not scheduled. Either the /loop dynamic runtime gate is "
            "off or the loop reached its maximum duration — the loop has ended."
        )
        assert _RE_LOOP_WAKEUP.search(ended) is None


class TestArming:
    def test_no_signal_leaves_worker_free(self) -> None:
        det = LoopDetector()
        det.check(_make_worker("w1"), "normal output, no loop")
        assert det.armed_remaining("w1") is None
        assert det.is_armed("w1") is False

    def test_signal_arms_with_dwell_plus_grace(self) -> None:
        det = LoopDetector(grace_seconds=30.0)
        det.check(_make_worker("w1"), _WAKEUP)
        remaining = det.armed_remaining("w1")
        assert remaining is not None
        # 270s dwell + 30s grace, minus the sliver of wall time since check().
        assert 295.0 < remaining <= 300.0
        assert det.is_armed("w1") is True

    def test_latest_match_wins(self) -> None:
        det = LoopDetector(grace_seconds=0.0)
        two = _WAKEUP + "\n...later...\nNext wakeup scheduled for X (in 5s)."
        det.check(_make_worker("w1"), two)
        remaining = det.armed_remaining("w1")
        assert remaining is not None
        assert remaining <= 5.0

    def test_unknown_worker_is_not_armed(self) -> None:
        det = LoopDetector()
        assert det.armed_remaining("never-seen") is None


class TestExpiry:
    def test_expired_window_returns_none_and_drops_entry(self) -> None:
        det = LoopDetector(grace_seconds=0.0)
        # Arm a window already in the past.
        det._armed_until["w1"] = time.time() - 1.0
        assert det.armed_remaining("w1") is None
        # Entry is dropped so the dict doesn't grow unbounded.
        assert "w1" not in det._armed_until


class TestForgetCleanup:
    def test_forget_clears_window(self) -> None:
        det = LoopDetector()
        det.check(_make_worker("w1"), _WAKEUP)
        assert det.is_armed("w1")
        det.forget("w1")
        assert det.armed_remaining("w1") is None
