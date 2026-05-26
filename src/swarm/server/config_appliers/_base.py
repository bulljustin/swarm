"""Section applier protocol + the ApplierDeps bundle.

Each :class:`~swarm.server.config_manager.ConfigManager` section
applier was extracted from a method on the manager into a free
function under ``swarm.server.config_appliers``.  Most appliers are
pure functions of ``(cfg, body) -> FieldOutcome``; a few need to
reach back into the daemon for side effects (provider cache
invalidation when LLM tuning changes, worker service lookups on
rename / path change).  Those handles travel via :class:`ApplierDeps`
so the appliers stay testable as plain free functions without
having to fake the entire daemon.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from swarm.server.worker_service import WorkerService


@dataclass
class ApplierDeps:
    """Side-effect handles passed to section appliers that need them.

    The vast majority of appliers ignore ``deps`` — they mutate
    ``cfg`` and that's it.  Two exceptions:

    * ``apply_llms`` / ``apply_provider_overrides`` flush the pilot's
      cached :class:`~swarm.providers.LLMProvider` instances when
      tuning changes, via :meth:`invalidate_provider_cache`.
    * ``apply_workers`` (rename / path change path) reaches into the
      live worker service to mirror the YAML change into the running
      Worker dataclass, via :meth:`get_worker_svc`.

    Both handles are nullable in tests; the real daemon wires them
    in :meth:`ConfigManager.__init__`.
    """

    invalidate_provider_cache: Callable[[], None]
    get_worker_svc: Callable[[], WorkerService | None]
