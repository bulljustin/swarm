"""Send-path guards for inter-worker messaging (task #873).

Two deterministic guards that harden ``swarm_send_message`` against the
rate-limit-amplifier failure mode observed 2026-06-25, where a single
worker fanned the SAME finding out as ~24 separate direct sends —
addressed to unrelated workers AND nonexistent ones — and the
InterWorkerMessageWatcher then woke the whole fleet:

- :func:`resolve_recipient` — reject a direct send to a name that is not a
  registered worker, instead of silently enqueuing a row no one reads.
- :class:`FanoutGuard` — cap how many DISTINCT recipients one sender may
  reach with an IDENTICAL message inside a short rolling window. Past the
  cap the worker is told to use the explicit ``*`` broadcast path rather
  than hand-enumerating the roster.

Both are deterministic on purpose — same rationale as
:mod:`swarm.messages.broadcast_gate`: a regex/counting guard is
injection-proof, whereas an LLM gate could be talked out of enforcing
itself. These complement (do not replace) the broadcast_gate, which
classifies a message by INTENT (authority claims / directives); this
module bounds VOLUME and validates the recipient namespace.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time

# A burst is "one turn": the #873 incident fired ~24 sends within seconds.
# 60s comfortably brackets a single agent turn without bleeding into the
# next unrelated coordination round.
_DEFAULT_WINDOW_SECONDS = 60.0
# Distinct recipients one sender may reach with identical content before the
# guard insists on an explicit ``*`` broadcast. Mirrors broadcast_gate's
# ``_DEFAULT_BROADCAST_THRESHOLD`` so "more than a handful = broadcast".
_DEFAULT_MAX_RECIPIENTS = 5

_WS_RE = re.compile(r"\s+")


def resolve_recipient(known: set[str], recipient: str) -> str | None:
    """Resolve ``recipient`` to its canonical worker name, or ``None``.

    Matches case-insensitively against the ``known`` roster and returns the
    canonical (config-cased) name so the persisted row's ``recipient``
    column lines up with what ``get_unread`` later queries — a worker that
    addresses ``"Hub"`` when the roster name is ``"hub"`` would otherwise
    write a row no ``get_unread("hub")`` ever returns. ``None`` means the
    name is not a registered worker and the caller should reject the send.
    """
    if recipient in known:
        return recipient
    lowered = recipient.lower()
    for name in known:
        if name.lower() == lowered:
            return name
    return None


def _fingerprint(content: str) -> str:
    """Stable short fingerprint of a message body.

    Whitespace-normalized + lowercased so trivially-reformatted copies of
    the same finding (the #873 case — the same uuid memo pasted 24×) share
    a fingerprint and count toward the same fan-out budget.
    """
    normalized = _WS_RE.sub(" ", (content or "").strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


class FanoutGuard:
    """Per-sender cap on identical-message fan-out within a rolling window.

    Tracks, per ``(sender, content-fingerprint)``, the set of distinct
    recipients reached inside ``window_seconds``. A send to a recipient
    already counted (a retry / dedup) is always allowed — it doesn't widen
    the blast radius. A send to a NEW recipient once the distinct count has
    reached ``max_recipients`` is blocked; the caller surfaces "use ``*``"
    guidance. State is in-memory and self-pruning (the window bounds it),
    so a daemon restart simply resets the budgets.

    Thread-safe: MCP tool calls may dispatch from a worker pool.
    """

    def __init__(
        self,
        *,
        max_recipients: int = _DEFAULT_MAX_RECIPIENTS,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
    ) -> None:
        self.max_recipients = int(max_recipients)
        self.window_seconds = float(window_seconds)
        self._lock = threading.Lock()
        # sender -> fingerprint -> {recipient: last_seen_monotonic}
        self._seen: dict[str, dict[str, dict[str, float]]] = {}

    @property
    def enabled(self) -> bool:
        """A non-positive cap or window disables the guard (allow-all)."""
        return self.max_recipients > 0 and self.window_seconds > 0

    def check(self, sender: str, recipient: str, content: str, *, now: float | None = None) -> bool:
        """Return True if this send is allowed, False if it exceeds the cap.

        Records the recipient against the sender's budget when allowed so
        the next distinct recipient sees the incremented count. A blocked
        send is NOT recorded — it never happened, so it can't push the
        count further.
        """
        if not self.enabled:
            return True
        now = now if now is not None else time.monotonic()
        fp = _fingerprint(content)
        cutoff = now - self.window_seconds
        with self._lock:
            by_fp = self._seen.setdefault(sender, {})
            recipients = by_fp.setdefault(fp, {})
            # Prune expired recipients for this fingerprint so a steady
            # trickle never accretes past the window.
            for r in [r for r, ts in recipients.items() if ts <= cutoff]:
                del recipients[r]
            if recipient in recipients:
                # Re-send to an already-counted recipient: refresh, allow.
                recipients[recipient] = now
                return True
            if len(recipients) >= self.max_recipients:
                # Opportunistic cleanup so a blocked sender's stale buckets
                # don't linger; the window guarantees eventual emptiness.
                self._gc(now)
                return False
            recipients[recipient] = now
            return True

    def _gc(self, now: float) -> None:
        """Drop fully-expired buckets. Caller holds the lock."""
        cutoff = now - self.window_seconds
        for sender in list(self._seen.keys()):
            by_fp = self._seen[sender]
            for fp in list(by_fp.keys()):
                recipients = by_fp[fp]
                for r in [r for r, ts in recipients.items() if ts <= cutoff]:
                    del recipients[r]
                if not recipients:
                    del by_fp[fp]
            if not by_fp:
                del self._seen[sender]
