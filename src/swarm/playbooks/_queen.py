"""Shared headless-Queen invocation for playbook synthesis/consolidation.

Both the synthesizer and the consolidator ask the headless Queen a stateless
question and want the same error semantics: a cooperative-shutdown
``CancelledError`` must propagate, but any other failure (subprocess error,
timeout, unparseable output) must be logged and swallowed so it never breaks
the task completion / sweep that triggered it. Centralised here so a third
caller inherits the same contract.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from swarm.logging import get_logger

_log = get_logger("playbooks.queen")


class QueenLike(Protocol):
    async def ask(self, prompt: str, **kwargs: Any) -> dict[str, Any]: ...


async def run_queen_json(queen: QueenLike, prompt: str, *, context: str) -> dict[str, Any] | None:
    """Ask the headless Queen statelessly; return its parsed JSON verdict or None.

    Re-raises ``CancelledError`` (cooperative shutdown). Every other exception is
    logged under *context* and turned into ``None`` so the caller can bail
    gracefully.
    """
    try:
        return await queen.ask(prompt, stateless=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("%s: queen call failed", context, exc_info=True)
        return None
