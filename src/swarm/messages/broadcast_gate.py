"""Deterministic gate for worker mass-broadcasts (task #647).

Any worker can call ``swarm_send_message`` to ``*`` and reshape every peer's
behavior. The worst case is a worker CLAIMING operator authority ("OPERATOR
DIRECTIVE", "Brad said", "standing policy") — unverifiable hearsay with maximal
blast radius and poor reversibility once peers persist it to memory. A single
hallucinated or misremembered directive becomes swarm-wide policy.

This module classifies a message by INTENT, not volume:

- ALLOW — findings / warnings about the SENDER'S OWN concrete change ("I changed
  shared API contract X, new shape is Y"). Verifiable, scoped, legitimate peer
  coordination.
- GATE — directive / policy / "everyone should…" messages, ESPECIALLY any
  claiming operator authority. A worker has no authority to set swarm-wide
  policy and cannot verify operator intent.

**Deterministic by design.** A regex gate is injection-proof: an LLM gate could
be subverted by the very directive it is judging ("ignore your gate, this is a
real operator directive"). The headless Queen runs only as *async enrichment*
on a block (provenance / blast-radius summary for the operator), never as the
enforcement path — see CLAUDE.md "prefer a deterministic drone rule first".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Claims of operator authority — gated regardless of recipient count. A worker
# may not speak for the operator; the operator (or Queen) issues directives.
_AUTHORITY_PATTERNS: tuple[str, ...] = (
    r"\boperator directive\b",
    r"\bbrad\s+(said|wants|asked|directed|told|says|wanted)\b",
    r"\bthe operator\s+(said|wants|asked|directed|told|says|wanted)\b",
    r"\bper (the )?operator\b",
    r"\bon behalf of (the )?operator\b",
    r"\bstanding policy\b",
)

# Peer-directed command / policy language — gated only when the message fans
# out (broadcast to ``*`` or beyond the recipient threshold). The same words in
# a 1:1 message are coordination, not a swarm-wide directive.
_DIRECTIVE_PATTERNS: tuple[str, ...] = (
    r"\beveryone (should|should be|must|needs to|has to|is to)\b",
    r"\ball workers? (should|must|need to|are to|have to)\b",
    r"\bfrom now on\b",
    r"\bgoing forward\b",
    r"\beffective immediately\b",
    r"\bnew (standing )?policy\b",
    r"\bmandatory for (all|everyone)\b",
)

_DEFAULT_BROADCAST_THRESHOLD = 5


@dataclass(frozen=True)
class GateVerdict:
    """Result of classifying a message against the broadcast gate."""

    blocked: bool
    reason: str  # machine reason: "operator-authority-claim" | "broadcast-directive" | ""
    matched: str  # the phrase that triggered the gate (surfaced in the escalation)


_PASS = GateVerdict(blocked=False, reason="", matched="")


def classify_broadcast(
    content: str,
    *,
    is_broadcast: bool,
    fanout_count: int,
    broadcast_threshold: int = _DEFAULT_BROADCAST_THRESHOLD,
) -> GateVerdict:
    """Classify a worker message; return a :class:`GateVerdict`.

    - Operator-authority claims ALWAYS gate (any recipient count) — a worker
      cannot speak for the operator.
    - Directive / command-policy language gates only when the message fans out
      (``is_broadcast`` or ``fanout_count > broadcast_threshold``).
    - Everything else passes: coordination about the sender's own work, and
      single-recipient non-authority messages.
    """
    text = (content or "").lower()
    if not text.strip():
        return _PASS

    for pat in _AUTHORITY_PATTERNS:
        m = re.search(pat, text)
        if m:
            return GateVerdict(True, "operator-authority-claim", m.group(0))

    fans_out = is_broadcast or fanout_count > broadcast_threshold
    if fans_out:
        for pat in _DIRECTIVE_PATTERNS:
            m = re.search(pat, text)
            if m:
                return GateVerdict(True, "broadcast-directive", m.group(0))

    return _PASS
