"""#405 daemon wiring: invariant reconciliation + INV-2 on state change."""

from __future__ import annotations

import asyncio

import pytest

from swarm.config import DroneConfig
from swarm.drones.log import SystemAction
from swarm.tasks.task import TaskStatus
from swarm.worker.worker import Worker, WorkerState
from tests.conftest import make_daemon


@pytest.fixture
def daemon(monkeypatch):
    return make_daemon(monkeypatch)


def _worker(name, state):
    w = Worker(name=name, path=f"/tmp/{name}")
    w.state = state
    return w


def _active(daemon, title, worker):
    t = daemon.task_board.create(title=title)
    daemon.task_board.assign(t.id, worker)
    daemon.task_board.activate(t.id)
    return t


def test_working_workers_only_buzzing_or_waiting(daemon):
    daemon.workers = [
        _worker("a", WorkerState.BUZZING),
        _worker("b", WorkerState.WAITING),
        _worker("c", WorkerState.RESTING),
        _worker("d", WorkerState.SLEEPING),
    ]
    assert daemon._working_workers() == {"a", "b"}


def test_reconcile_demotes_active_on_resting_worker_and_buzzes(daemon):
    daemon.workers = [_worker("w1", WorkerState.RESTING)]
    t = _active(daemon, "stuck", "w1")
    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE

    daemon._run_invariant_reconciliation("test")

    assert daemon.task_board.get(t.id).status == TaskStatus.ASSIGNED
    actions = [e.action for e in daemon.drone_log.entries]
    assert SystemAction.TASK_RECONCILED in actions
    # Idempotent — second pass is a no-op (no new repairs / log spam).
    n = len(daemon.drone_log.entries)
    daemon._run_invariant_reconciliation("test")
    assert len(daemon.drone_log.entries) == n


def test_working_worker_keeps_its_active_task(daemon):
    daemon.workers = [_worker("w1", WorkerState.BUZZING)]
    t = _active(daemon, "in flight", "w1")
    daemon._run_invariant_reconciliation("test")
    assert daemon.task_board.get(t.id).status == TaskStatus.ACTIVE


def test_on_state_changed_to_resting_triggers_reconcile(daemon):
    w = _worker("w1", WorkerState.BUZZING)
    daemon.workers = [w]
    t = _active(daemon, "x", "w1")
    # Worker drops to RESTING → hook must demote its ACTIVE task.
    w.state = WorkerState.RESTING
    daemon._on_state_changed(w)
    assert daemon.task_board.get(t.id).status == TaskStatus.ASSIGNED


def test_self_heals_multi_active_resting_worker(daemon):
    # The documented corrupt shape: >1 ACTIVE on a RESTING worker.
    daemon.workers = [_worker("w1", WorkerState.RESTING)]
    a = daemon.task_board.create(title="a")
    b = daemon.task_board.create(title="b")
    for t in (a, b):
        daemon.task_board.assign(t.id, "w1")
        daemon.task_board._tasks[t.id].status = TaskStatus.ACTIVE

    daemon._run_invariant_reconciliation("startup")

    statuses = {daemon.task_board.get(a.id).status, daemon.task_board.get(b.id).status}
    assert statuses == {TaskStatus.ASSIGNED}  # zero ACTIVE — fully healed


def test_complete_task_force_closes_blocked(daemon):
    """#609: complete_task(force=True) closes a wedged BLOCKED task that the
    normal status-gated path refuses — the clean force-close capability that
    replaces the #574 fail→reopen→approve→assign→complete workaround."""
    from swarm.server.daemon import TaskOperationError

    t = daemon.task_board.create(title="wedged")
    daemon.task_board.assign(t.id, "w1")
    daemon.task_board.activate(t.id)
    daemon.task_board.block_for_operator(t.id, "operator hold")
    assert t.status == TaskStatus.BLOCKED

    # Normal completion refuses a BLOCKED task.
    with pytest.raises(TaskOperationError):
        daemon.complete_task(t.id, resolution="x")
    assert daemon.task_board.get(t.id).status == TaskStatus.BLOCKED

    # Force path closes it end-to-end.
    assert daemon.complete_task(t.id, resolution="done e2e", force=True) is True
    closed = daemon.task_board.get(t.id)
    assert closed.status == TaskStatus.DONE
    assert closed.resolution == "done e2e"


# --- P1 (#611): periodic invariant-reconcile loop ---


def test_drone_config_has_reconcile_interval():
    """#611 P1: DroneConfig exposes a periodic-reconcile interval (default 90s)."""
    assert DroneConfig().reconcile_interval_seconds == 90.0


@pytest.mark.asyncio
async def test_invariant_reconcile_loop_ticks(daemon, monkeypatch):
    """#611 P1: the periodic loop calls _run_invariant_reconciliation on each
    tick, independent of any worker state change (closes the unhealed-while-
    BUZZING window that left platform #604/#605 both ACTIVE)."""
    daemon.config.drones.reconcile_interval_seconds = 90.0
    calls: list[str] = []
    monkeypatch.setattr(
        daemon, "_run_invariant_reconciliation", lambda reason: calls.append(reason)
    )

    # First sleep returns; second raises CancelledError to break the loop after
    # exactly one reconcile tick.
    state = {"n": 0}

    async def fake_sleep(_seconds):
        state["n"] += 1
        if state["n"] >= 2:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr("swarm.server.daemon.asyncio.sleep", fake_sleep)
    await daemon._invariant_reconcile_loop()
    assert calls == ["periodic"]


@pytest.mark.asyncio
async def test_invariant_reconcile_loop_disabled_skips(daemon, monkeypatch):
    """interval <= 0 disables the reconcile (loop idles without reconciling)."""
    daemon.config.drones.reconcile_interval_seconds = 0.0
    calls: list[str] = []
    monkeypatch.setattr(
        daemon, "_run_invariant_reconciliation", lambda reason: calls.append(reason)
    )

    state = {"n": 0}

    async def fake_sleep(_seconds):
        state["n"] += 1
        if state["n"] >= 2:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr("swarm.server.daemon.asyncio.sleep", fake_sleep)
    await daemon._invariant_reconcile_loop()
    assert calls == []  # disabled — never reconciles


# --- P5 (#611): web routes go through guarded board methods ---


class _FakeReq:
    """Minimal request: handle_action_create_task only needs app['daemon'] +
    an awaitable post() form."""

    def __init__(self, daemon, data: dict[str, str]):
        self.app = {"daemon": daemon}
        self._data = data

    async def post(self):
        from multidict import MultiDict

        return MultiDict(self._data)


def test_apply_status_change_backlog_to_unassigned(daemon):
    """#611 P5: Backlog→Unassigned routes through board.approve_task (guarded)
    rather than a raw task.approve() + manual persist."""
    from swarm.web.routes.tasks import _apply_status_change

    t = daemon.task_board.create(title="b")
    t.status = TaskStatus.BACKLOG
    _apply_status_change(daemon, t.id, "backlog", "unassigned")
    assert daemon.task_board.get(t.id).status == TaskStatus.UNASSIGNED


@pytest.mark.asyncio
async def test_create_task_refuses_active_status(daemon):
    """#611 P5: the create route refuses to author a task straight into ACTIVE
    (must go through the activate() chokepoint / INV-1); it stays in its
    default lane instead."""
    import json

    from swarm.web.routes.tasks import handle_action_create_task

    resp = await handle_action_create_task(_FakeReq(daemon, {"title": "x", "status": "active"}))
    created = daemon.task_board.get(json.loads(resp.text)["id"])
    assert created.status != TaskStatus.ACTIVE


@pytest.mark.asyncio
async def test_create_task_allows_terminal_authoring(daemon):
    """Lane/terminal authoring (recording historical work) is still allowed."""
    import json

    from swarm.web.routes.tasks import handle_action_create_task

    resp = await handle_action_create_task(_FakeReq(daemon, {"title": "hist", "status": "done"}))
    assert daemon.task_board.get(json.loads(resp.text)["id"]).status == TaskStatus.DONE
