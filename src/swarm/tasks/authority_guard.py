"""Deterministic guard against auto-generated tasks that FABRICATE operator
authority (task #894).

The 2026-06-26 incident: an auto-synthesized program filed tasks (@types/node
24→26, #890/#891) whose DESCRIPTIONS invented operator authorization —
"operator opted IN to @types/node 26 fleet-wide (amendment in flight)" — for
an amendment that never happened, to justify undoing a deliberate fleet hold.
Worst failure mode: an auto-generated task citing an operator decision/policy
that has no verifiable source.

A task arriving through ``swarm_create_task`` is AUTO-GENERATED (a worker/drone
filed it) — it cannot speak for the operator, exactly like the message
broadcast gate (:mod:`swarm.messages.broadcast_gate`, task #647). So if such a
task's text CITES operator authority / a policy amendment, the citation is
unverifiable hearsay by construction; we park it for operator review instead of
dispatching it. Deterministic on purpose (regex) — injection-proof, no model in
the enforcement path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Phrases asserting operator authority or a policy decision/amendment. A worker
# may not speak for the operator; an auto-generated task that invokes this
# language is claiming authorization it cannot prove. Superset of the message
# broadcast gate's authority patterns, plus the "opted in / amendment / policy
# change" framing that the #894 fabrication used to undo a hold.
_AUTHORITY_PATTERNS: tuple[str, ...] = (
    r"\boperator\s+(directive|opted[- ]?in|opt[- ]?in|approv\w+|authoriz\w+|"
    r"want\w*|ask\w*|direct\w*|told|say\w*|decided|mandat\w*)\b",
    r"\bbrad\s+(said|want\w*|ask\w*|direct\w*|told|say\w*|wanted|approv\w*|opted|decided)\b",
    r"\bthe operator\s+(said|want\w*|ask\w*|direct\w*|told|say\w*|approv\w*|opted|decided)\b",
    r"\bper (the )?operator\b",
    r"\bon behalf of (the )?operator\b",
    r"\bstanding policy\b",
    r"\bpolicy (amendment|change|update|reversal)\b",
    r"\bamendment in flight\b",
    r"\b(operator|policy|fleet)[- ]?(wide )?(opt(ed)?[- ]?in|approval|sign[- ]?off)\b",
)

# Tokens that, when present near the authority claim, indicate the citer is
# pointing at a CONCRETE, checkable source — these are NOT flagged, because the
# operator told the worker to reference where the decision lives. The guard is
# meant to catch BARE assertions ("operator opted in"), not legitimate "see the
# operator's message in thread #N / approval <url>" provenance.
_VERIFIABLE_SOURCE_PATTERNS: tuple[str, ...] = (
    r"https?://\S+",  # a link to the decision/approval
    r"\bthread\s*#?\d+\b",  # an operator thread reference
    r"\bmessage\s*#?\d+\b",
    r"\bapproval\s*(id|#)\s*\S+",
    r"\b(see|per|ref|cf\.?)\s+[A-Z]+-\d+\b",  # a ticket reference (PROJ-123)
)


@dataclass(frozen=True)
class AuthorityVerdict:
    """Result of screening a task's text for fabricated operator authority."""

    flagged: bool
    matched: str  # the authority phrase that triggered the guard ("" if none)


_PASS = AuthorityVerdict(flagged=False, matched="")


def screen_task_authority(title: str, description: str) -> AuthorityVerdict:
    """Screen an auto-generated task's text for an UNVERIFIABLE operator-
    authority / policy-amendment claim.

    Flags when the text asserts operator authority but carries no concrete,
    checkable source (a link, thread/message/approval id, or ticket ref). A
    bare "operator opted in to X (amendment in flight)" is flagged; a cited
    "operator approved in thread #42" is not.
    """
    text = f"{title or ''}\n{description or ''}".lower()
    if not text.strip():
        return _PASS
    matched = ""
    for pat in _AUTHORITY_PATTERNS:
        m = re.search(pat, text)
        if m:
            matched = m.group(0)
            break
    if not matched:
        return _PASS
    # An authority claim WITH a concrete source is legitimate provenance.
    for src in _VERIFIABLE_SOURCE_PATTERNS:
        if re.search(src, text):
            return _PASS
    return AuthorityVerdict(flagged=True, matched=matched)
