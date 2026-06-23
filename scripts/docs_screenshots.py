#!/usr/bin/env python3
"""Generate README/docs screenshots from a throwaway, SEEDED demo dashboard.

The public docs screenshots must NEVER come from the live daemon (it holds
real, private project data). This harness instead spins up an **isolated**
in-process dashboard:

* ``HOME`` is redirected to a temp dir BEFORE any swarm import, so the demo
  ``SwarmDB`` opens ``<tmp>/.swarm/swarm.db`` — the real ``~/.swarm`` and the
  live ``:9090`` daemon are never touched.
* No ``api_password`` is set, so the session-auth middleware is skipped (no
  login dance) — see ``server/api.py`` ``_session_auth_middleware``.
* The daemon is CONSTRUCTED but never ``start()``-ed, so no worker PTYs spawn.
  Fake ``Worker`` rows + generic FAKE store data are seeded directly.

Captures the bottom-panel tabs that are hard to screenshot otherwise. Extend
``TABS`` to add more. Output overwrites ``docs/screenshots/<name>.png``.

    uv run python scripts/docs_screenshots.py

Run after adding a dashboard tab so the launch images stay current.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

# --- Isolation: redirect HOME before importing anything from swarm. --------- #
# Pin Playwright's browser cache to the REAL home first — otherwise the HOME
# redirect below sends it looking under the empty temp dir.
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path(os.path.expanduser("~")) / ".cache" / "ms-playwright"),
)
_TMP_HOME = tempfile.mkdtemp(prefix="swarm-demo-shots-")
os.environ["HOME"] = _TMP_HOME
os.environ.pop("SWARM_API_PASSWORD", None)  # ensure no-auth mode
(Path(_TMP_HOME) / ".swarm").mkdir(parents=True, exist_ok=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "docs" / "screenshots"

# (filename, tab data-tab value, settle-ms) — the bottom-panel tabs to capture.
TABS = [
    ("loops-tab", "loops", 1400),
    ("harness-tab", "harness", 1400),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed(daemon) -> None:
    """Populate the isolated daemon with generic FAKE demo data.

    Tuned so the Harness digest shows a realistic mix: 3 actionable items
    (approval rule + playbook promote + playbook retire) and 2 display-only
    items (error-prone tool + dreamer pattern). Notes on the tuning:
      * MCP buzz entries must be UNIQUE — the buzz log collapses consecutive
        identical entries via repeat_count, which would undercount tool calls.
      * Playbooks need DISTINCT bodies — identical bodies hash to the same
        content_hash and the second create() dedups into the first.
      * One command-bearing decision entry (``docker run``) drives a clean
        approval-rule suggestion: suggest_rule prefers an extracted command
        over the common-token fallback, so it ignores the MCP noise.
    """
    from swarm.drones.log import DroneAction, LogCategory
    from swarm.playbooks.models import Playbook, PlaybookStatus
    from swarm.worker.worker import Worker, WorkerState

    # A few resting workers so the chrome looks alive (no real processes).
    for name, path in [
        ("api-service", "/demo/api-service"),
        ("web-frontend", "/demo/web-frontend"),
        ("data-pipeline", "/demo/data-pipeline"),
    ]:
        daemon.workers.append(Worker(name=name, path=path, state=WorkerState.RESTING))

    # --- Standing loops (Loops tab) -------------------------------------- #
    daemon.standing_loop.start("api-service")
    daemon.standing_loop.record_burn("api-service", 84_000)  # mid-window burn
    daemon.standing_loop.start("web-frontend")
    daemon.standing_loop.pause("web-frontend")

    # --- Harness digest signals ----------------------------------------- #
    # Error-prone MCP tool (display-only): 14 calls, 5 distinct errors = 36%.
    tool_errors = [
        "error: missing required field 'target_worker'",
        "error: unknown priority 'critical'",
        "error: task #4120 not found",
        "error: invalid acceptance_criteria (expected list)",
        "error: worker 'billing' is not registered",
    ]
    for i in range(14):
        snippet = tool_errors[i] if i < len(tool_errors) else f"task #{4100 + i} created"
        daemon.drone_log.add(
            DroneAction.CONTINUED,
            "api-service",
            f"mcp:swarm_create_task → {snippet}",
            category=LogCategory.MESSAGE,
        )
    # A healthy tool for contrast (all unique).
    for i in range(12):
        daemon.drone_log.add(
            DroneAction.CONTINUED,
            "web-frontend",
            f"mcp:swarm_check_messages → {i} unread from peers",
            category=LogCategory.MESSAGE,
        )
    # Operator-reviewed decisions for a clean ``escalate docker run`` rule.
    for i in range(4):
        daemon.drone_log.add(
            DroneAction.ESCALATED,
            "data-pipeline",
            f"Bash · docker run --rm -v /data:/data etl-image stage-{i}",
            category=LogCategory.DRONE,
        )

    # Playbooks (DISTINCT bodies): a strong candidate to promote + a
    # low-win-rate active one to retire.
    daemon.playbook_store.create(
        Playbook(
            name="grep-before-edit",
            title="Grep all call sites before changing a signature",
            body="Before changing any function signature, grep for every call site "
            "(hooks, tests, components) and update them in a single pass.",
            status=PlaybookStatus.CANDIDATE,
            uses=6,
            wins=5,
            losses=1,
        )
    )
    daemon.playbook_store.create(
        Playbook(
            name="retry-flaky-tests",
            title="Retry flaky tests up to 3x",
            body="When a test fails, re-run it up to three times before reporting "
            "the failure to the operator.",
            status=PlaybookStatus.ACTIVE,
            uses=13,
            wins=2,
            losses=10,
        )
    )

    # Dreamer-mined pattern (display-only).
    daemon.queen_chat.add_learning(
        context="Tasks touching the migration runner failed verification twice",
        correction="Auto-discovered by the dreamer: run `alembic check` before completing.",
        applied_to="discovered_by_dreamer:VERIFIER_TIER1_REOPENED:9f2a",
    )


def _run_server(app, port: int, ready: threading.Event) -> None:
    import asyncio

    from aiohttp import web

    async def _serve() -> None:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        ready.set()
        while True:  # serve until the process exits
            await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_serve())


def _capture(base_url: str) -> int:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, device_scale_factor=2.0
        )
        page = context.new_page()
        page.goto(base_url, wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(1500)  # WS init + first paint
        # Open the bottom panel once.
        page.evaluate("document.querySelector('[data-action=\"toggleBottomPanel\"]')?.click();")
        page.wait_for_timeout(400)
        for name, tab, settle in TABS:
            page.evaluate(f"document.querySelector('[data-tab=\"{tab}\"]')?.click();")
            page.wait_for_timeout(settle)  # tab switch fires fetch + render
            shot = OUT_DIR / f"{name}.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"  ✓ {shot.relative_to(REPO_ROOT)}")
        browser.close()
    return 0


def main() -> int:
    from swarm.config.models import HiveConfig
    from swarm.server.api import create_app
    from swarm.server.daemon import SwarmDaemon

    port = _free_port()
    config = HiveConfig(api_password="", port=port)
    daemon = SwarmDaemon(config)
    _seed(daemon)

    app = create_app(daemon)
    ready = threading.Event()
    threading.Thread(target=_run_server, args=(app, port, ready), daemon=True).start()
    if not ready.wait(timeout=20):
        print("ERROR: demo server did not start", file=sys.stderr)
        return 1
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{port}"
    print(f"[docs-shots] isolated demo dashboard at {base_url} (HOME={_TMP_HOME})")
    rc = _capture(base_url)
    print(f"[docs-shots] done — {len(TABS)} screenshots in {OUT_DIR.relative_to(REPO_ROOT)}/")
    return rc


if __name__ == "__main__":
    sys.exit(main())
