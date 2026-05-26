"""Tests for :class:`swarm.drones.detectors.context_pressure_check.ContextPressureCheck`.

Migrated from ``tests/test_state_tracker.py::TestContextPressure`` as
part of Phase 3 of ``docs/specs/state-tracker-refactor.md`` — the
synchronous BUZZING-only context-pressure guard now lives in its own
detector instead of inline on the tracker.

Note: this detector overlaps with
:class:`swarm.drones.context_pressure.ContextPressureWatcher` (the
periodic sweep). The duplication is acknowledged in the detector's
module-level ``# DUPLICATION`` comment and tracked as a follow-up
audit task; the tests here cover only the inline-per-poll path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from swarm.config import DroneConfig
from swarm.drones.detectors import ContextPressureCheck
from swarm.drones.log import DroneLog
from swarm.worker.worker import Worker, WorkerState


def _make_worker(name: str = "w1", state: WorkerState = WorkerState.BUZZING) -> Worker:
    w = Worker(name=name, path=f"/tmp/{name}")
    w.state = state
    return w


def _make_detector(
    *, drone_config: DroneConfig | None = None
) -> tuple[ContextPressureCheck, MagicMock]:
    decision_executor = MagicMock()
    decision_executor._deferred_actions = []
    detector = ContextPressureCheck(
        log=DroneLog(),
        decision_executor=decision_executor,
        drone_config=drone_config or DroneConfig(),
    )
    return detector, decision_executor


class TestContextPressureCheck:
    def test_below_threshold_no_action(self) -> None:
        cfg = DroneConfig(context_warning_threshold=0.7, context_critical_threshold=0.9)
        detector, de = _make_detector(drone_config=cfg)
        worker = _make_worker("w1", state=WorkerState.BUZZING)
        worker.context_pct = 0.5
        detector.check(worker)
        assert de._deferred_actions == []
        assert worker._context_warned is False

    def test_warning_threshold_logs_once(self) -> None:
        cfg = DroneConfig(context_warning_threshold=0.7, context_critical_threshold=0.95)
        detector, de = _make_detector(drone_config=cfg)
        worker = _make_worker("w1", state=WorkerState.BUZZING)
        worker.context_pct = 0.75
        detector.check(worker)
        assert worker._context_warned is True
        # Second call doesn't re-fire because the flag is set.
        detector.check(worker)
        # No /compact yet — only warning.
        compacts = [a for a in de._deferred_actions if a[0] == "compact"]
        assert compacts == []

    def test_critical_threshold_queues_compact(self) -> None:
        cfg = DroneConfig(context_warning_threshold=0.7, context_critical_threshold=0.9)
        detector, de = _make_detector(drone_config=cfg)
        worker = _make_worker("w1", state=WorkerState.BUZZING)
        worker.context_pct = 0.95
        detector.check(worker)
        assert worker.compacting is True
        compacts = [a for a in de._deferred_actions if a[0] == "compact"]
        assert len(compacts) == 1

    def test_already_compacting_is_skipped(self) -> None:
        cfg = DroneConfig(context_critical_threshold=0.9)
        detector, de = _make_detector(drone_config=cfg)
        worker = _make_worker("w1", state=WorkerState.BUZZING)
        worker.context_pct = 0.95
        worker.compacting = True
        detector.check(worker)
        assert de._deferred_actions == []

    def test_non_buzzing_worker_is_skipped(self) -> None:
        """The synchronous check is BUZZING-only; the periodic watcher
        handles RESTING / SLEEPING / WAITING workers."""
        cfg = DroneConfig(context_critical_threshold=0.9)
        detector, de = _make_detector(drone_config=cfg)
        worker = _make_worker("w1", state=WorkerState.RESTING)
        worker.context_pct = 0.95
        detector.check(worker)
        assert de._deferred_actions == []
        assert worker.compacting is False

    def test_zero_pct_no_action(self) -> None:
        """``context_pct == 0`` indicates the worker has never reported
        usage — never fire a warning or compact off a missing signal."""
        cfg = DroneConfig(context_warning_threshold=0.7, context_critical_threshold=0.9)
        detector, de = _make_detector(drone_config=cfg)
        worker = _make_worker("w1", state=WorkerState.BUZZING)
        worker.context_pct = 0.0
        detector.check(worker)
        assert de._deferred_actions == []
        assert worker._context_warned is False
