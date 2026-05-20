# Post-Overhaul Cleanup — Follow-up to P1–P6 UX Series

Status: **specified, not yet implemented**
Date: 2026-05-20
Predecessors: P1–P6 release commits `2026.5.20.2`–`.8` (pushed to origin/main)
Audience: implementer (almost certainly the same model that wrote this spec)

---

## Problem

The pipelines + playbooks + mobile UX overhaul shipped clean across seven
release commits, but I named several gaps in the changelogs that I
deliberately deferred rather than bundle. The operator interviewed me
on each one; this spec captures the decisions and orders the work.

The series itself is closed — those commits are on `origin/main`. This
spec covers the **next** batch: cleanup of the gaps named in those
commit messages, plus mobile QA that needs Playwright.

## Interview decisions (2026-05-20)

| Item | Decision |
|---|---|
| Linked-task-by-ID | **Add `GET /api/tasks/{id}` + `showTaskEditorById(id)`** — smallest principled fix; pays back for any future deep-link need (notifications, queen relays). |
| Range validation for PlaybookConfig | **Add `_validate_playbook_ranges` mirroring `_validate_drone_ranges`** — REST endpoint is publicly addressable; the dashboard sliders cover the common case but the API can still POST bad floats. |
| Retry-on-COMPLETED steps | **Add now, behind a confirmation modal.** Operator clicks Retry on a COMPLETED step → confirm-with-checkbox modal "I understand this may have side effects." |
| Step event log / audit trail | **Defer.** No demand yet. Re-evaluate when a real "why did step X retry yesterday" question lands. |
| Force-complete / force-fail pipeline buttons | **Defer.** Pause + per-step Skip already gets there. Footgun surface > convenience win until a use case proves otherwise. |
| Mobile visual QA | **Playwright on the server, drive the dashboard myself.** Real touch interactions / device-specific rendering can't be screenshotted out of the operator. Need to check if Playwright is already wired into this repo or if it lives elsewhere. |
| `test_ws_auth` flake | **Diagnose + fix now.** Order-sensitive — shared module-scope state. ~30 min diagnose, ~10 LOC fix. Worth doing while context is loaded. |
| Older memory-flagged work | **Out of scope for this spec.** Each needs its own `/interview`: Bug B (groups disappear on restart), Bug D (groups sort_order), managed browser capability, no-AI-slop content system. |

---

## Implementation order

1. **Cleanup batch (single follow-up commit):**
   - `_validate_playbook_ranges` + tests
   - `GET /api/tasks/{id}` + dashboard ID-addressable opener + wire P3's chip handler to it
   - Retry-on-COMPLETED with confirmation modal + engine guard relaxed for COMPLETED + tests
   - `test_ws_auth` flake diagnosed + fixed
2. **Mobile QA pass (separate work, may produce a second cleanup commit):**
   - Verify Playwright availability on this server
   - Drive the dashboard via Playwright at 360px / 390px / 414px viewports
   - Capture screenshots, log specific issues
   - Fix what's actually broken
3. **Bug B / Bug D diagnostic (separate commit, separate interview if needed)**

The four older memory-flagged items (Bug B, Bug D, browser capability,
content system) get their own `/interview` runs before any code lands.
Each is big enough to warrant proper scoping.

---

## Implementation detail

### 1. Linked-task-by-ID

**Backend:**

- New `GET /api/tasks/{task_id}` returning the full task row (title,
  description, priority, task_type, tags, deps, status, resolution,
  worker, attachments, etc.). 404 if not found.
- Lives in `src/swarm/server/routes/tasks.py`. Mirror the existing
  `handle_remove_task` / `handle_edit_task` route shape.

**Frontend:**

- New `showTaskEditorById(taskId)` helper in `dashboard.js`:
  - Fetches `/api/tasks/{id}`
  - Builds the data dict `openTaskModal('edit', ...)` expects
  - Switches to the Tasks tab + opens the editor pre-filled
- Replaces the existing scroll-and-flash fallback in the P3 detail
  view's `openLinkedTask`. The scroll-and-flash code stays as a
  secondary fallback if the fetch 404s.

**Tests:**

- Route: 200 returns expected JSON shape; 404 on unknown ID.

### 2. Range validation for PlaybookConfig

**Backend:**

- New `_validate_playbook_ranges(pb: dict)` method in `ConfigManager`,
  called from `_apply_playbooks` before the generic dispatcher. Mirrors
  `_validate_drone_ranges` exactly.
- Range contracts:
  - `auto_promote_winrate ∈ [0.0, 1.0]`
  - `prune_max_winrate ∈ [0.0, 1.0]`
  - `dedupe_similarity_threshold ∈ [0.0, 1.0]`
  - `min_resolution_chars >= 0`
  - `max_synth_per_hour >= 0`
  - `auto_promote_uses >= 1`
  - `prune_min_uses >= 1`
  - `consolidation_interval_seconds >= 300` (matches the engine's floor)
- Errors raise `ValueError("playbooks.X must be …")` consumed by the
  existing `handle_errors` middleware → 400.

**Tests:**

- One test per rejected value: each invalid input raises `ValueError`
  with the expected message; the in-memory config is unchanged.

### 3. Retry-on-COMPLETED with confirmation modal

**Backend:**

- Engine `retry_step` currently rejects non-FAILED. Relax to also
  accept COMPLETED **only when the caller passes `confirmed=True`**;
  add a new `confirmed: bool = False` kwarg. Non-FAILED + not confirmed
  → raise `ValueError` like today. Non-FAILED + confirmed + status
  ∈ {FAILED, COMPLETED} → proceed.
- The cascade-reset rule is unchanged: only FAILED downstream descendants
  reset; SKIPPED + COMPLETED downstream still stay sticky. Unless the
  operator wants cascade-reset for completed downstream too — flagging
  this as an open question; current spec says no.
- Route accepts a JSON body `{"confirmed": true}` and threads it to the
  engine. 409 if the step is non-FAILED + non-COMPLETED, or if
  COMPLETED but `confirmed=false`.

**Frontend:**

- P3 detail view's Retry button appears on COMPLETED steps **with a
  warning icon** ("⚠ Retry").
- Click → opens a confirmation modal:
  - Title: "Retry this completed step?"
  - Body: explains that re-running may have side effects (re-execute
    shell commands, re-post webhooks, re-create downstream tasks).
  - Checkbox: "I understand this step may have side effects" (required
    to enable the confirm button).
  - Confirm → POST to retry endpoint with `confirmed: true`.
- FAILED step retries still skip the modal (current behaviour preserved).

**Tests:**

- Engine: COMPLETED + `confirmed=True` resets; COMPLETED + default
  raises; FAILED unchanged (current tests still pass).
- Route: 200 with `{confirmed: true}` body on COMPLETED step;
  409 without the confirmed flag.

### 4. `test_ws_auth` flake

**Diagnostic plan:**

1. Reproduce: run the full suite, observe the error in
   `TestRateLimitLogic::test_mixed_old_and_new`.
2. The test passes in 0.09s standalone — points at shared mutable
   state. Most likely candidates:
   - A module-level dict / counter that other tests populate.
   - A `time.time()`-based rate-limit window that other tests have
     advanced past.
   - A frozen-time fixture that other tests forget to restore.
3. `pytest --randomly` may give us a smaller failing subset to bisect.
4. Fix is almost certainly either:
   - Reset the shared state in a fixture (`@pytest.fixture(autouse=True)`).
   - Inject the time source instead of reading `time.time()` directly.

**Done criteria:**

- `uv run pytest -q --timeout=60` → 4409+ passing, 0 errors.
- Test still passes standalone.

### 5. Mobile QA via Playwright

**Pre-flight checks:**

- Confirm Playwright is installed somewhere on this server. The
  operator noted "we have playwright on this server, but maybe not just
  on this repo." Check `which playwright` / `pip list | grep playwright`
  and any sibling project that might have it (e.g. `rcg-*`).
- If absent: install in this repo's `.venv` (`uv add --dev playwright`
  + `uv run playwright install chromium`).

**QA script:**

- Launch a Playwright-controlled Chromium with viewport 390×844
  (iPhone 14 portrait).
- Log into the dashboard (auth approach depends on what's already
  in `.swarm/` — likely an API password env var).
- Navigate through each touch point:
  1. Command Center mobile focus toggle (Attention / Queen panes flip).
  2. Queen panel — action button grid wraps correctly; status strip
     wraps at 0.75rem; resize handle hidden.
  3. Worker pill names flow without 140px truncation.
  4. Buzz log filter dropdown appears in place of chip strip.
  5. Task editor modal at 360px — tm-meta-row fields stack vertically.
  6. Pipeline editor at 390px — step cards readable, dep chips usable.
  7. Pipeline detail modal — wave grouping renders; result blobs scroll.
  8. Playbooks tab — analytics panels render; event timeline modal
     readable.
  9. Filter chip rows horizontal-scroll with sticky "All" chip.
- Capture screenshots of each touch point.
- Surface a punch list of anything that looks visibly broken.

**Done criteria:**

- Screenshots in `docs/qa-mobile-{date}/` (or wherever the operator
  prefers).
- Punch list filed (commit message, GitHub issue, or follow-up spec).
- Any blocking visual breakage fixed in this same pass.

---

## Out of scope

These each need their own `/interview` before any code:

- **Bug B — groups disappear on restart.** Per
  `project_328_silent_drop_fix` memory. Almost certainly another
  save/load asymmetry in the groups table. Half-day diagnostic + fix.
- **Bug D — groups sort_order.** Same memory. Probably same root cause
  as Bug B; worth investigating together but still merits a focused
  interview pass.
- **Managed browser capability** (per `project_browser_idea`). Big
  feature. Unlocks several downstream ideas.
- **No-AI-slop content system** (per `project_no_ai_slop_content_system`).
  Even bigger. Likely months of work, needs proper spec + interview.

## Open questions

- **Cascade-reset for COMPLETED downstream on retry?** Current decision
  is "no, only FAILED cascades." If retrying a COMPLETED step is meant
  to imply rerunning the whole subtree, that's a separate design
  decision. Flagging here so the implementer asks before assuming.

## Risks

- **Range validation breaks existing configs.** Anyone with a YAML
  override outside the documented ranges will hit a 400 on next save.
  Unlikely (defaults are conservative) but worth a one-line warning in
  the changelog.
- **`/api/tasks/{id}` enumerable.** The endpoint exposes one task per
  ID — same auth as `/api/tasks` already does, so no new surface; just
  noting it.
- **Retry-on-COMPLETED side effects.** Even with the confirmation
  modal, an operator could still trigger a destructive re-run. The
  spec mitigates with the explicit warning but doesn't eliminate the
  footgun. Idempotency hints on service handlers would be a
  follow-up.
- **Playwright auth.** If the dashboard requires API password / session
  cookie auth, the QA script needs to handle login. Worth time-boxing
  the auth setup to 30 min before falling back to a no-auth dev profile.
