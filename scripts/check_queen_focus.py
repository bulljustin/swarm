#!/usr/bin/env python3
"""Acceptance probe for task #551 — Queen-view auto-focus.

Drives the live dashboard and asserts all four acceptance criteria:

  1. Opening the Queen view places keyboard focus in the Queen PTY's xterm
     input (the .xterm-helper-textarea inside #cc-queen-term-holder) with no
     extra click.
  2. Keystrokes after opening reach that terminal — the global Alt+N shortcut
     is suppressed and focus stays in the Queen terminal (xterm owns the
     keyboard), proving input is routed to the PTY rather than the dashboard.
  3. Clicking a worker still focuses that worker's terminal (no regression to
     worker PTY focus).
  4. Dashboard global shortcuts still work: Alt+N opens the create-task modal
     when nothing is focused, and is suppressed while the Queen terminal is
     focused (shortcut parity — no regression).

Run against the dev server (default http://localhost:9090). Exits non-zero if
any criterion fails. Same harness pattern as scripts/check_pb_modal.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mobile_qa import _get_session_cookie, _load_dotenv  # type: ignore

BASE_URL = os.environ.get("SWARM_BASE_URL", "http://localhost:9090")

# JS expression: is keyboard focus inside the Queen embed's xterm input?
QUEEN_FOCUSED = """() => {
    const ae = document.activeElement;
    const holder = document.getElementById('cc-queen-term-holder');
    return !!(ae && holder && holder.contains(ae)
        && ae.tagName === 'TEXTAREA'
        && /xterm-helper-textarea/.test(ae.className));
}"""

# JS expression: is keyboard focus inside a worker terminal in #detail-body?
WORKER_FOCUSED = """() => {
    const ae = document.activeElement;
    const detail = document.getElementById('detail-body');
    return !!(ae && detail && detail.contains(ae)
        && ae.tagName === 'TEXTAREA'
        && /xterm-helper-textarea/.test(ae.className));
}"""

TASK_MODAL_OPEN = "() => document.getElementById('task-modal').style.display !== 'none'"


def _pick_worker(page) -> str | None:
    """First non-queen worker name from the rendered worker list."""
    return page.evaluate(
        """() => {
            const el = document.querySelector('.worker-item[data-worker]');
            return el ? el.dataset.worker : null;
        }"""
    )


def main() -> int:
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    password = os.environ["SWARM_API_PASSWORD"]
    session = _get_session_cookie(BASE_URL, password)

    results: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        results.append((label, bool(ok), detail))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        ctx.add_cookies(
            [
                {
                    "name": "swarm_session",
                    "value": session,
                    "domain": "localhost",
                    "path": "/",
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )
        page = ctx.new_page()
        page.goto(BASE_URL + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)

        # --- Criterion 1: Queen view auto-focuses the PTY (no extra click) ---
        # window.selectWorker('queen') is exactly what the queen-card click
        # handler (dashboard.js) invokes — faithful to a real operator click.
        page.evaluate("() => window.selectWorker && window.selectWorker('queen')")
        # Wait past the staged re-focus ladder (80ms + 250ms) plus margin.
        page.wait_for_timeout(700)
        c1 = page.evaluate(QUEEN_FOCUSED)
        active_cls = page.evaluate(
            "() => (document.activeElement && document.activeElement.className) || '(none)'"
        )
        check(
            "1. Queen view open focuses Queen PTY (no click)",
            c1,
            f"activeElement.className={active_cls!r}",
        )

        # --- Criterion 2: typing reaches the Queen terminal ---
        # Ensure clean modal state, type into the focused terminal, then assert
        # focus stayed in the Queen PTY and the Alt+N dashboard shortcut was
        # suppressed (keystrokes owned by xterm, not the dashboard).
        page.evaluate("() => { document.getElementById('task-modal').style.display = 'none'; }")
        page.keyboard.type("echo hello-queen-551")
        still_focused = page.evaluate(QUEEN_FOCUSED)
        page.keyboard.press("Alt+n")
        page.wait_for_timeout(150)
        modal_after_type = page.evaluate(TASK_MODAL_OPEN)
        check(
            "2. Typing reaches Queen PTY (focus retained, Alt+N suppressed)",
            still_focused and not modal_after_type,
            f"queen_focused={still_focused} task_modal_open={modal_after_type}",
        )

        # --- Criterion 4a: Alt+N opens create-task modal when nothing focused ---
        page.evaluate(
            """() => {
                document.getElementById('task-modal').style.display = 'none';
                if (document.activeElement && document.activeElement.blur) {
                    document.activeElement.blur();
                }
                document.body.focus();
            }"""
        )
        page.wait_for_timeout(100)
        page.keyboard.press("Alt+n")
        page.wait_for_timeout(200)
        modal_unfocused = page.evaluate(TASK_MODAL_OPEN)
        check(
            "4a. Alt+N opens create-task modal when nothing focused",
            modal_unfocused,
            f"task_modal_open={modal_unfocused}",
        )
        # Reset modal state for subsequent checks.
        page.evaluate("() => { document.getElementById('task-modal').style.display = 'none'; }")

        # --- Criterion 4b: Alt+N suppressed while Queen terminal focused ---
        page.evaluate("() => window.selectWorker && window.selectWorker('queen')")
        page.wait_for_timeout(700)
        queen_refocused = page.evaluate(QUEEN_FOCUSED)
        page.keyboard.press("Alt+n")
        page.wait_for_timeout(200)
        modal_focused = page.evaluate(TASK_MODAL_OPEN)
        check(
            "4b. Alt+N suppressed while Queen terminal focused",
            queen_refocused and not modal_focused,
            f"queen_focused={queen_refocused} task_modal_open={modal_focused}",
        )

        # --- Criterion 3: clicking a worker focuses that worker's terminal ---
        worker = _pick_worker(page)
        if worker:
            page.evaluate("(name) => window.selectWorker && window.selectWorker(name)", worker)
            page.wait_for_timeout(900)
            c3 = page.evaluate(WORKER_FOCUSED)
            check(
                f"3. Worker view ({worker}) focuses worker PTY (no regression)",
                c3,
                f"worker_focused={c3}",
            )
        else:
            check(
                "3. Worker view focuses worker PTY (no regression)",
                False,
                "no worker available in the worker list to click",
            )

        browser.close()

    print("\n=== Task #551 — Queen-view auto-focus acceptance ===")
    all_ok = True
    for label, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        all_ok = all_ok and ok
        line = f"  [{mark}] {label}"
        if detail:
            line += f"  ({detail})"
        print(line)
    print("=== %s ===" % ("ALL PASS" if all_ok else "FAILURES PRESENT"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
