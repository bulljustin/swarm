"""Direct tests for :class:`swarm.server.task_coordinator.TaskCoordinator`.

The lifecycle methods (assign / start / complete / handoff / …)
moved out of the daemon in 2026.5.27.2.  The existing daemon-proxy
tests cover the happy path through the public surface; this file
exercises the branches that the proxy tests don't reach:
:meth:`check_ownership` (file-ownership gate), :meth:`spawn_handoff_task`
(#442), and the various validation / error branches in start_task /
retry_draft_reply.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.server.daemon import SwarmOperationError, TaskOperationError
from swarm.tasks.task import TaskStatus
from tests.conftest import make_daemon

# ---------------------------------------------------------------------------
# check_ownership (#442 file-ownership gate)
# ---------------------------------------------------------------------------


class TestCheckOwnership:
    """File ownership conflicts gate task assignment."""

    def test_off_mode_is_noop(self) -> None:
        from swarm.coordination.ownership import FileOwnershipMap, OwnershipMode

        d = make_daemon()
        d.file_ownership = FileOwnershipMap(mode=OwnershipMode.OFF)
        # Should not raise even with overlapping files registered
        d.tasks_coord.check_ownership("api")

    def test_no_file_ownership_attr_is_noop(self) -> None:
        """Missing ``file_ownership`` on the daemon must not crash."""
        d = make_daemon()
        # Override the attr to None to simulate a daemon without ownership wiring
        d.file_ownership = None
        d.tasks_coord.check_ownership("api")

    def test_warning_mode_logs_but_does_not_raise(self) -> None:
        from swarm.coordination.ownership import FileOwnershipMap, OwnershipMode

        d = make_daemon()
        d.file_ownership = FileOwnershipMap(mode=OwnershipMode.WARNING)
        # Simulate two workers each having claimed foo.py — claim() only
        # records the first owner, so seed the second worker's
        # _worker_files set directly to model the real-world race where
        # both workers' dirty git state overlaps.
        d.file_ownership.claim("api", {"src/foo.py"})
        d.file_ownership._worker_files.setdefault("web", set()).add("src/foo.py")
        # Warning mode logs and returns — no raise
        d.tasks_coord.check_ownership("web")

    def test_hard_block_mode_raises_swarm_operation_error(self) -> None:
        from swarm.coordination.ownership import FileOwnershipMap, OwnershipMode

        d = make_daemon()
        d.file_ownership = FileOwnershipMap(mode=OwnershipMode.HARD_BLOCK)
        d.file_ownership.claim("api", {"src/foo.py"})
        d.file_ownership._worker_files.setdefault("web", set()).add("src/foo.py")
        with pytest.raises(SwarmOperationError, match="File ownership conflict"):
            d.tasks_coord.check_ownership("web")

    def test_no_worker_files_is_noop(self) -> None:
        from swarm.coordination.ownership import FileOwnershipMap, OwnershipMode

        d = make_daemon()
        d.file_ownership = FileOwnershipMap(mode=OwnershipMode.HARD_BLOCK)
        # Worker has no registered files — no overlap possible
        d.tasks_coord.check_ownership("api")

    def test_no_overlap_is_noop(self) -> None:
        """File registered but no other worker claims it — no warning fires."""
        from swarm.coordination.ownership import FileOwnershipMap, OwnershipMode

        d = make_daemon()
        d.file_ownership = FileOwnershipMap(mode=OwnershipMode.HARD_BLOCK)
        d.file_ownership.claim("api", {"src/foo.py"})
        d.tasks_coord.check_ownership("api")


# ---------------------------------------------------------------------------
# start_task — validation branches
# ---------------------------------------------------------------------------


class TestStartTaskValidation:
    """The pre-dispatch validation gate raises the right errors."""

    @pytest.mark.asyncio
    async def test_missing_task_raises(self) -> None:
        d = make_daemon()
        with pytest.raises(TaskOperationError, match="not found"):
            await d.tasks_coord.start_task("nonexistent")

    @pytest.mark.asyncio
    async def test_wrong_status_raises(self) -> None:
        d = make_daemon()
        # Create + leave UNASSIGNED — start_task only fires from ASSIGNED
        task = d.task_board.create(title="T")
        with pytest.raises(TaskOperationError, match="must be ASSIGNED to start"):
            await d.tasks_coord.start_task(task.id)

    @pytest.mark.asyncio
    async def test_no_assigned_worker_raises(self) -> None:
        """ASSIGNED status without an ``assigned_worker`` is the corrupt-row case."""
        d = make_daemon()
        task = d.task_board.create(title="T")
        # Force into ASSIGNED status with empty assigned_worker — corrupt row
        # invariant; task.assign() refuses empty strings, so set fields directly.
        task.status = TaskStatus.ASSIGNED
        task.assigned_worker = ""
        with pytest.raises(TaskOperationError, match="has no assigned worker"):
            await d.tasks_coord.start_task(task.id)


# ---------------------------------------------------------------------------
# assign_task — validation branches
# ---------------------------------------------------------------------------


class TestAssignTaskValidation:
    @pytest.mark.asyncio
    async def test_missing_task_raises(self) -> None:
        d = make_daemon()
        with pytest.raises(TaskOperationError, match="not found"):
            await d.tasks_coord.assign_task("nonexistent", "api")

    @pytest.mark.asyncio
    async def test_unavailable_task_raises_409(self) -> None:
        """A task already ACTIVE is not ``is_available`` — 409 to the caller."""
        d = make_daemon()
        task = d.task_board.create(title="T")
        d.task_board.assign(task.id, "api")
        d.task_board.activate(task.id)
        with pytest.raises(TaskOperationError) as ex:
            await d.tasks_coord.assign_task(task.id, "web")
        assert ex.value.status_code == 409


# ---------------------------------------------------------------------------
# spawn_handoff_task (#442 — promote a message into a tracked task)
# ---------------------------------------------------------------------------


class TestSpawnHandoffTask:
    """Auto-promote inter-worker messages into tracked tasks."""

    def _make_message(self, sender: str = "api", content: str = "fix this") -> SimpleNamespace:
        return SimpleNamespace(
            sender=sender,
            msg_type="dependency",
            id=42,
            content=content,
        )

    @pytest.mark.asyncio
    async def test_creates_task_assigns_to_recipient(self, monkeypatch) -> None:
        d = make_daemon()
        # Stub assign_and_start so the test isn't gated on PTY sending
        async_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(d.tasks_coord, "assign_and_start_task", async_mock)
        monkeypatch.setattr(d, "edit_task", MagicMock())

        result = await d.tasks_coord.spawn_handoff_task(
            "web", self._make_message(sender="api", content="please fix the foo bug")
        )

        assert result is True
        # A task was created and the title carries the sender + first content line
        tasks = list(d.task_board.all_tasks)
        assert len(tasks) == 1
        assert tasks[0].title.startswith("Handoff from api:")
        assert "please fix the foo bug" in tasks[0].title
        assert "auto-handoff" in tasks[0].tags
        # Source worker tag landed via edit_task (bypasses plan-mode gate)
        d.edit_task.assert_called_once()
        args, kwargs = d.edit_task.call_args
        assert kwargs["source_worker"] == "api"
        # And the recipient got the auto-assign
        async_mock.assert_awaited_once_with(tasks[0].id, "web", actor="drone:inter-worker-handoff")

    @pytest.mark.asyncio
    async def test_no_task_board_returns_false(self) -> None:
        d = make_daemon()
        d.task_board = None
        result = await d.tasks_coord.spawn_handoff_task("web", self._make_message())
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_content_uses_placeholder(self, monkeypatch) -> None:
        d = make_daemon()
        monkeypatch.setattr(d.tasks_coord, "assign_and_start_task", AsyncMock(return_value=True))
        monkeypatch.setattr(d, "edit_task", MagicMock())
        msg = SimpleNamespace(sender="api", msg_type="finding", id=1, content="")
        await d.tasks_coord.spawn_handoff_task("web", msg)
        tasks = list(d.task_board.all_tasks)
        assert "(no content)" in tasks[0].title

    @pytest.mark.asyncio
    async def test_unknown_sender_skips_source_worker_tag(self, monkeypatch) -> None:
        """A sender of ``"?"`` (unknown) shouldn't set source_worker."""
        d = make_daemon()
        monkeypatch.setattr(d.tasks_coord, "assign_and_start_task", AsyncMock(return_value=True))
        edit_mock = MagicMock()
        monkeypatch.setattr(d, "edit_task", edit_mock)
        msg = SimpleNamespace(sender="", msg_type="finding", id=2, content="text")
        await d.tasks_coord.spawn_handoff_task("web", msg)
        # edit_task NOT called — sender resolved to "?" which skips the tag
        edit_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_assign_failure_returns_false(self, monkeypatch) -> None:
        """If assign_and_start raises, return False (best-effort handoff)."""
        d = make_daemon()
        monkeypatch.setattr(
            d.tasks_coord,
            "assign_and_start_task",
            AsyncMock(side_effect=RuntimeError("worker dead")),
        )
        monkeypatch.setattr(d, "edit_task", MagicMock())
        result = await d.tasks_coord.spawn_handoff_task("web", self._make_message())
        assert result is False

    @pytest.mark.asyncio
    async def test_create_failure_returns_false(self, monkeypatch) -> None:
        """task_board.create raising bails out cleanly."""
        d = make_daemon()
        monkeypatch.setattr(d.task_board, "create", MagicMock(side_effect=RuntimeError("DB down")))
        result = await d.tasks_coord.spawn_handoff_task("web", self._make_message())
        assert result is False


# ---------------------------------------------------------------------------
# auto_resolve_attention_for_task — completion-time Attention sweep
# ---------------------------------------------------------------------------


class TestAutoResolveAttentionForTask:
    def test_no_chat_attr_is_noop(self) -> None:
        d = make_daemon()
        # daemon fixture doesn't bind queen_chat → exercise the early return
        # branch.  Use a delattr-then-call dance because make_daemon might
        # add it.
        if hasattr(d, "queen_chat"):
            delattr(d, "queen_chat")
        d.tasks_coord.auto_resolve_attention_for_task("task-id")

    def test_empty_task_id_is_noop(self) -> None:
        d = make_daemon()
        d.queen_chat = MagicMock()
        d.tasks_coord.auto_resolve_attention_for_task("")
        d.queen_chat.list_threads.assert_not_called()

    def test_resolves_matching_thread(self) -> None:
        d = make_daemon()
        d.queen_chat = MagicMock()
        thread = SimpleNamespace(id="thread-1", task_id="task-X")
        d.queen_chat.list_threads.return_value = [thread]
        d.queen_chat.resolve_thread.return_value = True
        d.tasks_coord.auto_resolve_attention_for_task("task-X")
        d.queen_chat.resolve_thread.assert_called_once_with(
            "thread-1", resolved_by="queen", reason="upstream task DONE"
        )

    def test_skips_thread_with_different_task_id(self) -> None:
        d = make_daemon()
        d.queen_chat = MagicMock()
        thread = SimpleNamespace(id="t-1", task_id="other-task")
        d.queen_chat.list_threads.return_value = [thread]
        d.tasks_coord.auto_resolve_attention_for_task("target-task")
        d.queen_chat.resolve_thread.assert_not_called()

    def test_list_threads_exception_is_swallowed(self) -> None:
        """Best-effort: an exception from list_threads must not propagate."""
        d = make_daemon()
        d.queen_chat = MagicMock()
        d.queen_chat.list_threads.side_effect = RuntimeError("DB locked")
        # No raise — error is logged and method returns
        d.tasks_coord.auto_resolve_attention_for_task("task-X")


# ---------------------------------------------------------------------------
# auto_start_next_assigned — post-ship self-loop (task #225 Phase 3)
# ---------------------------------------------------------------------------


class TestAutoStartNextAssigned:
    def test_empty_worker_is_noop(self) -> None:
        d = make_daemon()
        d.tasks_coord.auto_start_next_assigned("")
        # No exception, no work — exercise the early return

    def test_no_task_board_is_noop(self) -> None:
        d = make_daemon()
        d.task_board = None
        d.tasks_coord.auto_start_next_assigned("api")

    def test_no_assigned_task_is_noop(self) -> None:
        """Worker with no ASSIGNED task in queue — no dispatch."""
        d = make_daemon()
        # No tasks at all
        d.tasks_coord.auto_start_next_assigned("api")

    def test_runtime_error_swallowed_when_no_event_loop(self, monkeypatch) -> None:
        """Sync caller (no loop) shouldn't see an exception."""
        d = make_daemon()
        task = d.task_board.create(title="T")
        d.task_board.assign(task.id, "api")
        # Force asyncio.create_task to raise RuntimeError (no loop)
        monkeypatch.setattr(
            "swarm.server.task_coordinator.asyncio.create_task",
            MagicMock(side_effect=RuntimeError("no loop")),
        )
        d.tasks_coord.auto_start_next_assigned("api")
        # Task stays ASSIGNED — the test passes if no exception leaked


# ---------------------------------------------------------------------------
# retry_draft_reply — email re-draft path
# ---------------------------------------------------------------------------


class TestRetryDraftReply:
    @pytest.mark.asyncio
    async def test_no_source_email_raises_409(self) -> None:
        d = make_daemon()
        task = d.task_board.create(title="T")
        with pytest.raises(TaskOperationError) as ex:
            await d.tasks_coord.retry_draft_reply(task.id)
        assert ex.value.status_code == 409
        assert "no source email" in str(ex.value).lower()

    @pytest.mark.asyncio
    async def test_no_resolution_raises_409(self) -> None:
        d = make_daemon()
        task = d.task_board.create(title="T", source_email_id="msg-123")
        with pytest.raises(TaskOperationError) as ex:
            await d.tasks_coord.retry_draft_reply(task.id)
        assert ex.value.status_code == 409
        assert "no resolution" in str(ex.value).lower()

    @pytest.mark.asyncio
    async def test_no_graph_mgr_raises_409(self) -> None:
        d = make_daemon()
        task = d.task_board.create(title="T", source_email_id="msg-123")
        task.resolution = "shipped"
        d.task_board._persist()
        # graph_mgr left None on the fixture
        with pytest.raises(TaskOperationError) as ex:
            await d.tasks_coord.retry_draft_reply(task.id)
        assert ex.value.status_code == 409
        assert "graph not configured" in str(ex.value).lower()

    @pytest.mark.asyncio
    async def test_happy_path_delegates_to_email_service(self) -> None:
        d = make_daemon()
        task = d.task_board.create(title="T", source_email_id="msg-123")
        task.resolution = "shipped"
        d.task_board._persist()
        d.graph_mgr = MagicMock()
        d.email.send_completion_reply = AsyncMock()
        await d.tasks_coord.retry_draft_reply(task.id)
        d.email.send_completion_reply.assert_awaited_once()
