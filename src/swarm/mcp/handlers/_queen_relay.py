"""Queen auto-relay + Attention-thread upsert helpers.

Extracted from ``mcp/tools.py`` (task #518). These helpers are used by
``swarm_send_message`` and ``swarm_note_to_queen`` to push a short
inbox-relay prompt into the Queen's PTY whenever a worker writes to her,
and to surface the message as an Attention-card thread in the dashboard.

Both functions are best-effort: failures here MUST NOT break the
underlying message-store write. The caller has already persisted the
message — the relay + thread upsert are pure ergonomics.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swarm.server.daemon import SwarmDaemon


def _auto_relay_to_queen(
    d: SwarmDaemon,
    sender: str,
    msg_type: str,
    content: str,
    message_id: int | None = None,
) -> None:
    """Fire-and-forget inject a short inbox relay into the Queen's PTY.

    Keeps the relay prompt small and action-oriented so Claude's next
    turn uses it as a cue to pull the full message via
    ``queen_view_messages``. Skipped silently when the daemon doesn't
    expose ``send_to_worker`` (test fakes) or when there's no running
    event loop.

    Task #277: when ``message_id`` is provided, the queen's inbox row is
    marked read at relay time. The Queen has no ``swarm_check_messages``
    equivalent — ``queen_view_messages`` is a read-only log view — so
    without this the dashboard unread count drifts from functional
    reality: Queen acts on the note, dashboard still shows it UNREAD
    indefinitely. The relay IS the consumption event, per Option A in
    the task write-up.
    """
    from swarm.drones.log import LogCategory, SystemAction
    from swarm.worker.worker import QUEEN_WORKER_NAME

    preview = (content or "")[:200].replace("\n", " ")
    suffix = "..." if len(content) > 200 else ""
    relay = (
        f"[msg to queen] {msg_type} from {sender}: {preview}{suffix}\n"
        "Full thread: `queen_view_messages worker=queen limit=5`"
    )

    send = getattr(d, "send_to_worker", None)
    if send is None:
        return
    try:
        coro = send(QUEEN_WORKER_NAME, relay, _log_operator=False)
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(coro)
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except RuntimeError:
            # No event loop (CLI/test context). Close the coroutine we
            # just created so Python doesn't warn about it.
            try:
                coro.close()
            except Exception:
                pass
    except Exception:
        return

    try:
        d.drone_log.add(
            SystemAction.INBOX_AUTO_RELAY,
            QUEEN_WORKER_NAME,
            f"from {sender}: {preview[:80]}{suffix}",
            category=LogCategory.MESSAGE,
        )
    except Exception:
        pass

    if message_id is not None:
        store = getattr(d, "message_store", None)
        mark_read = getattr(store, "mark_read", None) if store is not None else None
        if mark_read is not None:
            try:
                mark_read(QUEEN_WORKER_NAME, [message_id])
            except Exception:
                # mark_read failure shouldn't break the relay — the worst
                # outcome is the pre-#277 status quo (row stays UNREAD).
                pass

    # Command Center: surface this worker→queen message as an Attention card.
    # Reuses queen_threads/queen_messages so the dashboard renders it via the
    # existing queen.thread / queen.message WS events. One active thread per
    # sender → coalesces a sender's recent messages into one card.
    _upsert_attention_thread(d, sender, msg_type, content)


def _gate_broadcast(
    d: SwarmDaemon,
    sender: str,
    recipient: str,
    msg_type: str,
    content: str,
    reason: str,
    matched: str,
) -> str:
    """Block a gated mass-broadcast and escalate it to the operator (task #647).

    Enforcement is the caller's deterministic gate; this helper handles the
    side effects of a block: a buzz-log entry, an operator Attention card, and
    a fire-and-forget headless-Queen *enrichment* call that summarises
    provenance / blast-radius for the operator. None of it delivers the
    message — that's the whole point. Returns the text shown to the SENDER.

    Best-effort throughout: a failure in any side effect must not raise, so the
    sender always gets a coherent "gated" response.
    """
    from swarm.drones.log import LogCategory, SystemAction

    preview = (content or "")[:80].replace("\n", " ")
    try:
        d.drone_log.add(
            SystemAction.BROADCAST_GATED,
            sender,
            f"→ {recipient} BLOCKED ({reason}, matched '{matched}'): {preview}",
            category=LogCategory.MESSAGE,
        )
    except Exception:
        pass

    # Operator Attention card — the gated directive needs a human (or the
    # Queen) to issue it for real if it is legitimate.
    try:
        _upsert_attention_thread(
            d,
            sender,
            "warning",
            f"[GATED BROADCAST — {reason}] {sender} tried to send to {recipient}: {content}",
        )
    except Exception:
        pass

    _enrich_gated_broadcast_async(d, sender, recipient, reason, matched, content)

    return (
        f"⛔ Broadcast GATED, not delivered. Reason: {reason} "
        f'(matched "{matched}").\n\n'
        "A worker cannot issue a swarm-wide directive or speak for the operator "
        "— this was routed to the operator for confirmation instead. If this is "
        'coordination about YOUR OWN concrete change (e.g. "I changed shared '
        'API X, new shape is Y"), rephrase it that way and resend. If it is a '
        "policy/directive, let the operator or Queen issue it."
    )


def _enrich_gated_broadcast_async(
    d: SwarmDaemon,
    sender: str,
    recipient: str,
    reason: str,
    matched: str,
    content: str,
) -> None:
    """Fire-and-forget headless-Queen analysis of a gated broadcast.

    The deterministic gate already blocked delivery; this adds a provenance /
    blast-radius summary to the operator's Attention thread. Async because the
    MCP handler is synchronous and must not block on an LLM call.
    """
    queen = getattr(d, "queen", None)
    ask = getattr(queen, "ask", None)
    if ask is None:
        return
    prompt = (
        "A worker broadcast was GATED by the deterministic mass-broadcast gate "
        "(task #647) and was NOT delivered. Analyze it for the operator.\n\n"
        f"Sender: {sender}\nRecipient: {recipient}\n"
        f"Gate trigger: {reason} (matched phrase: '{matched}')\n\n"
        f"Message:\n{content}\n\n"
        "Assess: (1) provenance — is this the sender's own verifiable work, or "
        "an unverifiable claim about what the operator or another party said? "
        "(2) blast radius had it been delivered, (3) reversibility, "
        "(4) coordination vs command. Strict JSON only: "
        '{"verdict": "legitimate|hearsay|unclear", '
        '"summary": "one sentence for the operator", '
        '"recommend": "deliver|hold|discard"}'
    )

    async def _run() -> None:
        try:
            result = await ask(prompt, stateless=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        if not isinstance(result, dict):
            return
        summary = result.get("summary") or result.get("result") or ""
        verdict = result.get("verdict", "")
        rec = result.get("recommend", "")
        if not summary:
            return
        try:
            _upsert_attention_thread(
                d,
                sender,
                "warning",
                f"[Queen analysis of gated broadcast — {verdict}/{rec}] {summary}",
            )
        except Exception:
            return

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_run())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except RuntimeError:
        # No event loop (CLI/test) — skip enrichment silently.
        pass


def _upsert_attention_thread(
    d: SwarmDaemon,
    sender: str,
    msg_type: str,
    content: str,
) -> None:
    chat = getattr(d, "queen_chat", None)
    if chat is None:
        return
    try:
        active = chat.list_threads(
            status="active", kind="worker-message", worker_name=sender, limit=1
        )
        if active:
            thread = active[0]
        else:
            title = f"{sender}: {(content or '').splitlines()[0][:80]}"
            thread = chat.create_thread(title=title, kind="worker-message", worker_name=sender)
        msg = chat.add_message(thread.id, role="system", content=f"[{msg_type}] {content}")
    except Exception:
        # Attention surfacing is best-effort — never break the PTY relay path.
        return

    try:
        from swarm.server.routes.queen import _broadcast_message, _broadcast_thread

        _broadcast_thread(d, thread.id, "created" if not active else "updated")
        _broadcast_message(d, thread.id, msg.to_dict())
    except Exception:
        pass
