# Cross-worker-machinery audit — task/blocker/active state-machine invariants

**Task:** #610  **Date:** 2026-06-06  **Author:** swarm worker
**Trigger:** 5 same-class machinery incidents this stretch (see §1). Operator
directive: stop patching instances, audit the state machine.

> Method: 3 read-only fan-out agents enumerated mutation sites; every
> load-bearing claim below was re-verified by hand against the code (file:line
> cited). Agent severity inflation was corrected (notably the web create-route,
> downgraded from "CRITICAL multi-ACTIVE" to a scoped RAW gap).

---

## 1. The incident pattern

| # | Incident | Class | Status |
|---|----------|-------|--------|
| 1 | #574 self-referential blocker → uncloseable BLOCKED | invariant not enforced at a write path | guard shipped 2026.6.6 |
| 2 | #529 stale blocker not auto-cleared | auto-clear not reached | patched |
| 3 | #524 stop-hook evaluated against from-worker | cross-project asymmetry | patched 2026.5.27 |
| 4 | #442/#527 auto-handoff routing | send-failure → re-route | patched 2026.5.28 |
| 5 | INV-1: state-tracker promoted **all** assigned tasks to ACTIVE (platform #604/#605 two in-progress) | invariant not enforced at a mutation path | cap shipped 2026.6.6.3 |

**Common root:** task/blocker/active state is mutated through *many* code paths,
and each invariant (≤1 ACTIVE per worker; no self-block; no blocker cycle;
auto-clear) is enforced *by convention at each site* rather than *structurally
at a chokepoint*. When a new path forgets the guard, the invariant breaks
silently. The reconciler is the only safety net, and it has gaps (§4).

---

## 2. Task-status mutation map (path × invariant)

The low-level setters in `tasks/task.py` (`assign/unassign/start/block/complete/
fail/reopen/approve/reject`) just write `self.status` — **no** enforcement. The
guard layer is `tasks/board.py` (lock + `_persist` + `_notify` + status gates).
Anything that calls a `task.*` setter or writes `task.status` **outside**
board.py bypasses the guard.

### GUARDED (board.py — enforce gates, persist, notify)
`assign` (is_available), `complete` (ASSIGNED/ACTIVE), `force_complete`
(non-terminal; #609), `fail`, `reopen` (DONE/FAILED), `unassign`,
`unassign_worker`, `demote_other_active` (INV-1), `activate` (INV-1 — **dead**,
§3), `park`, `block_for_operator`, `approve_task`/`reject_task` (BACKLOG), and
the reconciler repairs `_recon_operator_action` / `_recon_inv1` / `_recon_inv2`
/ `reconcile_active_per_worker`.

### RAW (mutate status outside the guard layer) — the audit's concern

| File:line | Function | Transition | INV-1 risk | Gap |
|-----------|----------|-----------|-----------|-----|
| `server/task_coordinator.py:273` | `start_task` | ASSIGNED→ACTIVE | **none** | Calls `demote_other_active` (line 262) *then* `task.start()` — correct, but hand-rolls what `board.activate()` exists to do. Not really "raw", but it's the parallel implementation that left `activate()` dead. |
| `drones/state_tracker.py:304` | `_promote_one_assigned` | ASSIGNED→ACTIVE | **mitigated** (2026.6.6.3) | Now caps at one (promotes only if 0 active). Residual: no `_persist`, no `STARTED` history → DB lag + invisible-to-history activation (why #604/#605 had no STARTED rows). |
| `web/routes/tasks.py:~102` | `handle_action_create_task` | →arbitrary (incl. ACTIVE/BLOCKED) | **low** | `task.status = TaskStatus(requested_status)` on create, raw + `persist`. Mostly used for Backlog/Done authoring; the raw-flip branch doesn't set `assigned_worker`, so an ACTIVE flip there doesn't break per-worker INV-1 — but it's an unguarded arbitrary-status write with no validation that the transition is legal. |
| `web/routes/tasks.py:~40, ~267` | `_apply_status_change`, `handle_action_promote_task` | BACKLOG→UNASSIGNED | none | `task.approve()` direct instead of `board.approve_task()`; manual `persist`. Cosmetic DRY/bypass, no INV risk (→UNASSIGNED). |
| `drones/verifier.py:~290` | `_apply_reopen_verdict` | →ASSIGNED (verifier reopen) | none | `task.reopen_for_verifier()` + manual `persist`; no INV risk. |

**Takeaway:** the only ACTIVE-creating paths are `start_task` (guarded by hand)
and `state_tracker` (capped). No *current* path can create >1 ACTIVE per worker
after 2026.6.6.3 — but the enforcement is duplicated and convention-based, not
centralized. The web create-route can still author illegal raw transitions.

---

## 3. Dead code: `board.activate()`

`board.activate()` (board.py ~430) enforces INV-1 (demotes other ACTIVE for the
worker, then `task.start()`) and is the *intended* one-active chokepoint — but
it has **zero callers** (verified across the whole tree). `start_task`
reimplements the same demote-then-start inline. So the canonical
invariant-enforcing entry point is dead while two other paths hand-roll it.
**This is the structural smell behind incident #5.**

---

## 4. The reconciler — the safety net has holes

### Triggers (verified — exactly two)
1. **Startup**, once: `daemon.py:761` → `reconcile_active_per_worker()`.
2. **Worker → non-working state**: `daemon.py:1046`, inside `_on_state_changed`,
   guarded by `if worker.state not in (BUZZING, WAITING)`. Fires on a transition
   *into* RESTING/SLEEPING/STUNG/OFFLINE.

**There is no periodic/timer reconcile sweep.**

### Gap A — unhealed-while-BUZZING window (HIGH)
A violation created while a worker is (and stays) BUZZING is **not** reconciled
until it next goes idle or the daemon restarts. This is precisely why platform's
#604/#605 two-ACTIVE persisted: the state-tracker created it on the BUZZING
transition, and platform kept working (BUZZING), so `_on_state_changed`'s
reconcile branch never fired. A busy worker can hold an invariant violation for
hours. **The reconciler heals on the wrong edge** (worker going idle) relative
to when violations are created (worker going busy).

### Gap B — `_recon_inv1` tiebreak demotes the in-flight job (MEDIUM)
`board.py` `_recon_inv1`: `tasks.sort(key=lambda t: t.updated_at, reverse=True)`
then demote `tasks[1:]` — i.e. **keep newest by `updated_at`, demote the rest.**
`updated_at` bumps on any edit/reassign, *not* on "the PTY is working this one."
So a long-running ACTIVE task with an older `updated_at` gets demoted when a
newer ACTIVE appears. Had the reconciler fired on #604/#605 it would have
demoted **#604 — the in-flight 27k-record remediation** — the opposite of what
the operator (correctly) did by parking #605. The auto-heal is itself a hazard
for long jobs.

### Gap C — blocker-row ↔ BLOCKED-status divergence (MEDIUM)
"Has a `worker_blockers` row" and "task.status == BLOCKED" are independent:
- `swarm_report_blocker` writes a row but does **not** set BLOCKED. The task only
  becomes BLOCKED later, via `_recon_inv2`, *and only when the worker is idle*
  (same Gap-A edge). A worker filing a blocker while BUZZING leaves a row with
  the task still ACTIVE indefinitely.
- `block_for_operator` sets BLOCKED with **no** row (by design).
So the dashboard's "blocked" signal and the nudge-suppression store can disagree.

### Reconciler hygiene (good)
Repairs are direct `task.status =` writes, but that's fine — the reconciler *is*
the guard, runs under `_lock`, and logs every repair to buzz + task history
(`invariants.py`). Audit trail is solid.

---

## 5. Blocker writes — clean (post-#609)

Good news, fully verified: every write to `worker_blockers` is in
`tasks/blockers.py` (`report` / `clear` / `clear_for_task`), and:
- The **only** production caller of `report()` is the guarded MCP handler
  `_handle_report_blocker`, which enforces self-block, cycle, and
  terminal-target rejection *before* the write.
- `clear_for_task` is the #609 force-override (operator/Queen intentional).
- Auto-clear (`has_active_blocker` → `_check_target_done` / `_check_message_since`)
  is reached for every row on every `IdleWatcher` sweep — no row escapes.

No bypass paths for blocker writes. The §1 incidents #1/#2 are closed at the
write layer; the residual blocker issue is the *status divergence* in §4 Gap C,
which is a reconciler/coupling problem, not a write-guard problem.

---

## 6. Prioritized fixes

**P1 — Periodic reconcile sweep (closes Gap A, the highest-impact gap).**
Add a low-frequency timer (e.g. 60–120 s, reusing a drone loop) that calls
`reconcile_invariants()` regardless of worker state changes. This is the missing
safety net: any path that slips a violation through is healed within one tick
instead of persisting until the worker idles. Cheap (only repairs+persists on
actual violation).

**P2 — Fix the `_recon_inv1` tiebreak (closes Gap B).**
Stop using `updated_at` to pick the survivor. Prefer a real "this is the PTY's
task" signal. Concretely: (a) add a `started_at` stamp set in `task.start()` and
keep the *earliest-started* ACTIVE task (the in-flight one), or (b) consult the
worker's current-task signal if available. Never silently demote the longest-
running ACTIVE task. At minimum, log the demotion at WARNING with both task ids
so an operator can catch a bad demotion (per the warning-level-for-ops rule).

**P3 — Revive `board.activate()` as the single ACTIVE chokepoint (closes the §3
dead-code smell + hardens INV-1 structurally).**
Make `start_task` and `state_tracker._promote_one_assigned` both call
`board.activate(task_id)` instead of hand-rolling `demote_other_active` +
`task.start()`. One guarded entry that demotes, starts, stamps `started_at`,
persists, and logs `STARTED` history — so every ACTIVE transition is identical,
persisted, and audited (fixes the missing-history gap too).

**P4 — Board-level invariant assertion (defense in depth).**
Add `_assert_no_double_active()` invoked from `_persist()` (or a debug/guarded
mode) that detects >1 ACTIVE per worker and self-heals via the §P2 tiebreak +
loud log. Makes the invariant violation *impossible to persist silently*
regardless of which path mutated status — the structural fix the incident
pattern calls for.

**P5 — Route the web create-route + promote/approve paths through board methods.**
`handle_action_create_task` should validate transitions (reject authoring
straight to ACTIVE/BLOCKED, or route through the guarded setter);
`_apply_status_change`/`handle_action_promote_task` should call
`board.approve_task()` not `task.approve()`. Removes the last raw status writes.

**P6 — Decide blocker↔status coupling (Gap C).**
Either set `task.status = BLOCKED` at `swarm_report_blocker` time (when the
worker isn't actively on it), or surface "has blocker row" in the dashboard
independently of status so the two can't silently disagree. Product call —
flag for the operator, don't auto-change semantics.

> **DECISION (2026-06-06, operator): leave as-is.** P1's 90s periodic reconcile
> now bounds the divergence for an idle worker to ≤90s (`_recon_inv2` sets
> BLOCKED once a worker with a blocker binding goes idle), and a BUZZING worker
> shouldn't be BLOCKED anyway — so the residual divergence is benign. No code
> change. Revisit only if operators report confusion from the UI showing a
> blocker row on a non-BLOCKED task.

---

## Implementation status (#611)

- **P1** periodic reconcile sweep — shipped `2026.6.6.4`
- **P2** `started_at` + earliest-started `_recon_inv1` tiebreak — shipped `2026.6.6.5`
- **P3** `board.activate()` single ACTIVE chokepoint (+ removed dead
  `demote_other_active`) — shipped `2026.6.6.6`
- **P4** `_persist`-time double-active self-heal — shipped `2026.6.6.7`
- **P5** web routes through guarded board methods — shipped `2026.6.6.8`
- **P6** blocker↔status coupling — operator decision: leave as-is (above)

### Suggested sequencing
P1 (safety net) + P2 (tiebreak) first — together they make the reconciler
*correct* and *timely*, neutralizing the whole incident class even before the
structural work. Then P3 (chokepoint) + P4 (assertion) for structural
prevention. P5/P6 are cleanup/product. Ship each as its own
`release: X.Y.Z` per the phase-independent rule.

---

## 7. Answers to the acceptance criteria

1. **Path × invariant matrix** — §2 (+ dead code §3).
2. **Every blocker write checked** — §5: all guarded; no bypass.
3. **Reconciler triggers + unhealed window + tiebreak re-eval** — §4 (Gap A:
   reactive-only, no periodic sweep; Gap B: `updated_at` tiebreak hazard with
   `started_at` recommendation).
4. **Chokepoint/assertion design** — §6 P3 (revive `activate()` as sole ACTIVE
   entry) + P4 (`_persist`-time assertion). Makes one-active / no-self-block /
   no-cycle unbypassable.
5. **Written to** `docs/audits/state-machine-invariants-2026-06-06.md` — this doc.
6. **`board.activate()` dead vs `start_task` hand-rolled** — §3 + P3: consolidate
   onto `activate()`.

**Bottom line:** the blocker-write layer is now sound; the remaining risk is the
**ACTIVE state machine** — enforcement is convention-based across duplicated
paths, the canonical chokepoint is dead, and the reconciler safety net heals on
the wrong edge (idle, not busy) with a tiebreak that can kill the in-flight job.
P1+P2 are the high-leverage fixes.
