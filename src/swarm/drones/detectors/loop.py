"""LoopDetector — track native ``/loop`` parked-between-fires windows.

A worker running Claude Code's native ``/loop`` (or any ScheduleWakeup-paced
loop) self-schedules its next tick and parks at an idle prompt. Swarm must not
nudge or speculatively dispatch over it during that dwell — the worker isn't
free, it's waiting to resume its own loop (task #761,
``docs/specs/native-loop-functions.md`` §2).

Unlike a dynamic workflow (a persistent footer indicator → read as BUZZING),
a parked loop is *genuinely idle*; reporting BUZZING would lie to the dashboard
and confuse the stuck-BUZZING safety nets. So the worker stays RESTING and this
detector holds a per-worker no-disturb deadline that the idle-watcher and
speculative dispatch consult — the same shape as a reported blocker.

The signal is the ScheduleWakeup tool result the harness prints when the worker
parks: ``Next wakeup scheduled for <time> (in Ns)``. The captured ``N`` is the
exact dwell, so the deadline is precise rather than a fixed guess. A small grace
is added so a slightly-late wakeup or clock skew doesn't reopen the window early.
"""

from __future__ import annotations

import time

from swarm.worker.worker import Worker


class LoopDetector:
    """Scan PTY output for the loop-wakeup signal and hold a no-disturb window.

    Stateless across providers by construction: only Claude Code emits the
    matched line, so for every other provider :meth:`check` simply never
    matches and :meth:`armed_remaining` always returns ``None``.
    """

    def __init__(self, grace_seconds: float = 30.0) -> None:
        # Deadline (wall-clock) until which each worker is loop-armed.
        self._armed_until: dict[str, float] = {}
        self._grace = max(0.0, grace_seconds)

    def check(self, worker: Worker, content: str) -> None:
        """Refresh the no-disturb window if *content* shows a parked loop.

        Reads the most recent ``(in Ns)`` dwell from the wakeup signal and
        sets the deadline to ``now + N + grace``. Re-reading the same signal
        on a later poll simply re-arms from the latest match, so a worker
        that keeps looping stays protected without manual reset.
        """
        from swarm.providers.claude import _RE_LOOP_WAKEUP

        # Take the LAST match — the freshest scheduled wakeup wins if the
        # tail happens to contain more than one.
        seconds: int | None = None
        for m in _RE_LOOP_WAKEUP.finditer(content):
            seconds = int(m.group(1))
        if seconds is None:
            return
        self._armed_until[worker.name] = time.time() + seconds + self._grace

    def armed_remaining(self, name: str) -> float | None:
        """Seconds until *name*'s loop window expires, or ``None`` if free.

        Returns ``None`` once the deadline has passed (and drops the stale
        entry) so callers get a clean "not loop-armed" answer.
        """
        deadline = self._armed_until.get(name)
        if deadline is None:
            return None
        remaining = deadline - time.time()
        if remaining <= 0:
            self._armed_until.pop(name, None)
            return None
        return remaining

    def is_armed(self, name: str) -> bool:
        """Whether *name* is currently inside a loop no-disturb window."""
        return self.armed_remaining(name) is not None

    def forget(self, name: str) -> None:
        """Drop tracking state for a worker (dead-worker cleanup hook)."""
        self._armed_until.pop(name, None)
