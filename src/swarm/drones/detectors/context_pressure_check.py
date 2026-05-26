"""ContextPressureCheck — synchronous, BUZZING-only context-pressure guard.

Extracted from :class:`~swarm.drones.state_tracker.WorkerStateTracker`
(Phase 3 of ``docs/specs/state-tracker-refactor.md``).  Per poll, when
a worker is mid-turn (BUZZING) and its ``context_pct`` crosses the
configured thresholds, this detector emits a one-shot warning and/or
queues a deferred ``/compact`` action on the shared
:class:`~swarm.drones.decision_executor.DecisionExecutor`.

The trigger is the worker's own ``context_pct`` (refreshed every 15 s
by :meth:`SwarmDaemon._usage_refresh_loop`); this detector itself is
stateless across workers.  The single piece of per-worker state it
reads/writes is ``Worker._context_warned`` — a flag on the worker that
prevents repeated warning-tier log spam.

# DUPLICATION: ContextPressureWatcher (``swarm/drones/context_pressure.py``)
# also injects ``/compact`` based on the same thresholds, but runs as a
# periodic sweep across *all* worker states (RESTING / SLEEPING / BUZZING
# / WAITING / STUNG) with hysteresis and state-aware fallbacks
# (Ctrl-C-then-compact for BUZZING, defer for WAITING). This detector
# fires synchronously per poll for **BUZZING workers only** — the
# overlap is intentional today (the sync check catches the rare in-poll
# critical excursion before the watcher's next sweep) but should be
# untangled in a follow-up. See ``docs/specs/state-tracker-refactor.md``
# §6 "Out of scope" and the audit task that follows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.drones.log import LogCategory, SystemAction

if TYPE_CHECKING:
    from swarm.config import DroneConfig
    from swarm.drones.decision_executor import DecisionExecutor
    from swarm.drones.log import DroneLog
    from swarm.worker.worker import Worker


class ContextPressureCheck:
    """Inline pressure check: queue ``/compact`` when BUZZING + pct ≥ critical."""

    def __init__(
        self,
        log: DroneLog,
        decision_executor: DecisionExecutor,
        drone_config: DroneConfig,
    ) -> None:
        self._log = log
        self._decision_executor = decision_executor
        self._drone_config = drone_config

    def check(self, worker: Worker) -> None:
        """Warn or inject ``/compact`` when context fill exceeds thresholds."""
        from swarm.worker.worker import WorkerState

        if worker.state != WorkerState.BUZZING or worker.compacting:
            return
        pct = worker.context_pct
        if pct <= 0:
            return

        cfg = self._drone_config
        critical = cfg.context_critical_threshold
        warning = cfg.context_warning_threshold

        if pct >= critical:
            # Inject /compact via deferred action
            self._log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                f"context critical ({pct:.0%}) — injecting /compact",
                category=LogCategory.DRONE,
            )
            worker.compacting = True
            self._decision_executor._deferred_actions.append(
                ("compact", worker, None, worker.state, worker.process)
            )
        elif pct >= warning and not worker._context_warned:
            self._log.add(
                SystemAction.QUEEN_BLOCKED,
                worker.name,
                f"context warning ({pct:.0%}) — approaching limit",
                category=LogCategory.DRONE,
            )
            worker._context_warned = True
