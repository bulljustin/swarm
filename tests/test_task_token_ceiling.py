"""Tests for the per-task token-budget governor (task #762).

``SwarmDaemon._enforce_task_token_ceiling`` charges each worker's output-
token delta to its ACTIVE task and, when a task crosses
``DroneConfig.task_token_ceiling``, escalates (a TASK_OVER_TOKEN_BUDGET
notification) and parks the task (ACTIVE → BLOCKED) so it stops burning.
See ``docs/specs/native-loop-functions.md`` (#762).

The method only touches a handful of ``self`` attributes, so it is unit-
tested with a ``SimpleNamespace`` stand-in for the daemon plus a real
``TaskBoard`` (so the park is a genuine state transition).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from swarm.config.models import DroneConfig
from swarm.drones.log import SystemAction
from swarm.server.daemon import SwarmDaemon
from swarm.tasks.board import TaskBoard
from swarm.tasks.task import SwarmTask, TaskStatus
from swarm.worker.worker import TokenUsage, Worker


def _active_task(board: TaskBoard, worker: str = "w1", number_title: str = "t") -> SwarmTask:
    task = board.add(SwarmTask(title=number_title, assigned_worker=worker))
    task.assigned_worker = worker
    task.status = TaskStatus.ACTIVE
    return task


def _worker(name: str = "w1", output_tokens: int = 0) -> Worker:
    w = Worker(name=name, path=f"/tmp/{name}")
    w.usage = TokenUsage(output_tokens=output_tokens)
    return w


def _run_governor(
    *,
    board: TaskBoard,
    workers: list[Worker],
    ceiling: int,
    prev: dict[str, int] | None = None,
) -> tuple[SimpleNamespace, MagicMock]:
    drone_log = MagicMock()
    fake = SimpleNamespace(
        task_board=board,
        config=SimpleNamespace(drones=DroneConfig(task_token_ceiling=ceiling)),
        workers=workers,
        _prev_worker_output_tokens={} if prev is None else prev,
        drone_log=drone_log,
    )
    SwarmDaemon._enforce_task_token_ceiling(fake)
    return fake, drone_log


def _ceiling_log(drone_log: MagicMock):
    for call in drone_log.add.call_args_list:
        if call.args and call.args[0] is SystemAction.TASK_OVER_TOKEN_BUDGET:
            return call
    return None


class TestBaselineSeeding:
    def test_first_sighting_seeds_baseline_without_charging(self) -> None:
        board = TaskBoard()
        task = _active_task(board)
        worker = _worker(output_tokens=5000)
        fake, log = _run_governor(board=board, workers=[worker], ceiling=1000)
        # First sighting must NOT retro-charge the 5000 cumulative tokens.
        assert task.tokens_spent == 0
        assert task.status is TaskStatus.ACTIVE
        assert fake._prev_worker_output_tokens["w1"] == 5000
        assert _ceiling_log(log) is None


class TestAccrual:
    def test_delta_under_ceiling_accrues_no_park(self) -> None:
        board = TaskBoard()
        task = _active_task(board)
        worker = _worker(output_tokens=500)
        _run_governor(board=board, workers=[worker], ceiling=1000, prev={"w1": 0})
        assert task.tokens_spent == 500
        assert task.status is TaskStatus.ACTIVE

    def test_no_active_task_is_ignored(self) -> None:
        board = TaskBoard()
        # Task exists but is ASSIGNED, not ACTIVE → governor skips it.
        task = board.add(SwarmTask(title="t", assigned_worker="w1"))
        task.status = TaskStatus.ASSIGNED
        worker = _worker(output_tokens=99999)
        _run_governor(board=board, workers=[worker], ceiling=1000, prev={"w1": 0})
        assert task.tokens_spent == 0


class TestBreach:
    def test_crossing_ceiling_escalates_and_parks(self) -> None:
        board = TaskBoard()
        task = _active_task(board)
        worker = _worker(output_tokens=1500)
        _, log = _run_governor(board=board, workers=[worker], ceiling=1000, prev={"w1": 0})
        assert task.tokens_spent == 1500
        # Parked: ACTIVE → BLOCKED (block_for_operator), not killed.
        assert task.status is TaskStatus.BLOCKED
        assert task._token_ceiling_breached is True
        call = _ceiling_log(log)
        assert call is not None
        assert call.args[1] == "w1"
        assert call.kwargs.get("is_notification") is True

    def test_breach_is_one_shot(self) -> None:
        board = TaskBoard()
        task = _active_task(board)
        worker = _worker(output_tokens=1500)
        prev = {"w1": 0}
        _run_governor(board=board, workers=[worker], ceiling=1000, prev=prev)
        assert task.status is TaskStatus.BLOCKED
        # A later refresh with more burn must not re-log or crash — the task
        # is no longer ACTIVE, so it drops out of the governor's index.
        worker.usage = TokenUsage(output_tokens=9000)
        _, log2 = _run_governor(board=board, workers=[worker], ceiling=1000, prev=prev)
        assert _ceiling_log(log2) is None


class TestDisabled:
    def test_zero_ceiling_tracks_but_never_parks(self) -> None:
        board = TaskBoard()
        task = _active_task(board)
        worker = _worker(output_tokens=999_999)
        fake, log = _run_governor(board=board, workers=[worker], ceiling=0, prev={"w1": 0})
        # Disabled: deltas still accrue (so enabling mid-run is clean) but
        # nothing is parked.
        assert task.tokens_spent == 999_999
        assert task.status is TaskStatus.ACTIVE
        assert _ceiling_log(log) is None
        # Baseline advanced so a future enable won't see a huge first delta.
        assert fake._prev_worker_output_tokens["w1"] == 999_999
