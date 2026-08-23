---
title: Task-Type-Bucketed Delegate Cooldown
type: feat
date: 2026-08-22
origin: docs/brainstorms/2026-08-20-task-type-bucketed-delegate-cooldown-requirements.md
---

# Task-Type-Bucketed Delegate Cooldown

## Summary

Rekey `CandidateCooldownStore` (`src/josu/delegate/cooldown.py`) from tracking failures per candidate name to tracking them per `(task_type, candidate)` pair, so a candidate's trouble on one task_type no longer cools it down for unrelated ones. This plan covers the bucketing rekey only — empirical reordering (origin R5-R8) is a separate future plan; see Scope Boundaries.

---

## Problem Frame

`CandidateCooldownStore` keys its internal `_health` dict by candidate name alone, globally across every task_type a candidate can be attempted for. A candidate that trips cooldown on one narrow task_type — say, code generation it's genuinely weak at — cools down for every other task_type too, including ones it handles fine, until the shared cooldown window elapses. This mirrors a documented failure mode in a comparable system (see origin doc's Sources / Research), but hasn't been observed in josu's own usage yet — a preemptive correctness fix, not a response to an incident.

The only place `CandidateCooldownStore`'s methods are actually called is `chain.py`'s `_attempt()` closure (confirmed via a repo-wide search: `daemon.py`, `delegate/server.py`, `delegate/internal_api.py`, `fallback/quota.py`, and `proactive/watchers.py` all thread the store instance through as a constructor/parameter but never call its methods directly) — so the fix is contained to two files.

---

## Key Technical Decisions

- **KTD1 — Rekey the existing store in place; don't introduce a second store type.** `_health: dict[str, _CandidateHealth]` becomes `dict[tuple[str, str], _CandidateHealth]`, keyed `(task_type, candidate_name)`. `cooldown.py`'s existing invariants (injectable clock, no-`await` synchronous safety, absent-key-means-healthy default, expiry-reset-on-read) carry over unchanged — broadening the key is a minimal diff, not a rewrite chain.py would need to reconcile against a parallel structure.
- **KTD2 — Public methods gain a leading `task_type: str` parameter, not a combined string key.** `record_failure(task_type, name)`, `record_success(task_type, name)`, `is_in_cooldown(task_type, name)` — matches the tuple-key internal representation directly. A composite string key (e.g. `f"{task_type}:{name}"`) was considered and rejected: it would need its own escaping/collision reasoning if a task_type or candidate name ever contained the delimiter, for no benefit over two explicit parameters.
- **KTD3 — `chain.py`'s three call sites source `task_type` from `execute_chain()`'s own parameter, already in scope via `_make_attempt()`'s closure.** No signature change to `execute_chain()` itself, `server.py`, `internal_api.py`, or `daemon.py` — `task_type` was always a required `execute_chain()` argument; this plan only threads it one level deeper, into calls that previously dropped it.
- **KTD4 — Reordering (origin R5-R8) is out of scope here.** The rekeyed `(task_type, candidate)` structure is what a future reordering plan would extend with a success/attempt counter, but no reordering behavior ships in this plan — see Scope Boundaries.

---

## Requirements

Carried from origin (`docs/brainstorms/2026-08-20-task-type-bucketed-delegate-cooldown-requirements.md`), scoped to the bucketing subset this plan implements:

- R1. A candidate's cooldown state is tracked per `(task_type, candidate)` pair — consecutive failures on one task_type do not affect eligibility for a different task_type.
- R2. Within one `(task_type, candidate)` bucket, existing cooldown behavior is unchanged: N consecutive qualifying failures trip cooldown, the cooldown expires automatically, and any success resets the failure count.
- R3. Bucketing applies uniformly wherever a candidate can be attempted for a given task_type — both task-type delegation chains and the proactive-check chain.
- R4. Bucketing ships enabled by default with no new configuration required.
- R9. All new state stays in-memory and per-daemon-process, exactly like the existing store — no persisted file, nothing surviving a daemon restart.
- R10. Bucketing cannot override `stays_hosted = true` or the free-local-first / `allow_remote` gating a chain already resolves — this plan touches only cooldown-skip logic inside an already-resolved candidate list, never `resolve_chain()`'s ordering or filtering itself.

**Origin R5-R8 (empirical reordering) are explicitly deferred** — see Scope Boundaries.

R4 and R10 are cross-cutting constraints rather than unit-specific deliverables: R4 (no new config) is satisfied by U1/U2 adding no `josu.toml` surface at all, and R10 (cannot override `stays_hosted`/`allow_remote` gating) is satisfied by construction — this plan only touches cooldown-skip logic inside a chain `resolve_chain()` has already resolved and filtered, never that resolution itself. Both are verified by the existing regression test scenarios in U2 rather than a dedicated test of their own.

---

## Implementation Units

### U1. Rekey `CandidateCooldownStore` to `(task_type, candidate)`

**Goal:** Track cooldown state per `(task_type, candidate)` pair instead of per candidate name alone.

**Requirements:** R1, R2, R9

**Dependencies:** none

**Files:**
- Modify: `src/josu/delegate/cooldown.py`
- Test: `tests/delegate/test_cooldown.py`

**Approach:** Rekey `_health: dict[str, _CandidateHealth]` to `dict[tuple[str, str], _CandidateHealth]`. `_CandidateHealth` itself is unchanged. Each public method (`record_failure`, `record_success`, `is_in_cooldown`) gains a leading `task_type: str` parameter and looks up/writes `(task_type, name)` instead of `name`. Update the module and class docstrings to describe per-`(task_type, candidate)` tracking in place of the current per-candidate framing. Every existing behavior — injectable clock, no-`await` synchronous updates, implicit-healthy-when-absent, expiry clearing the bucket's state on read — carries over exactly, just scoped to a narrower key.

**Patterns to follow:** The existing methods' own shape and docstrings — this is a minimal-diff rekey, not a restructure. `tests/delegate/test_cooldown.py`'s existing `_FakeClock` convention.

**Test scenarios:**
- Happy path: N consecutive failures for `(task_type_A, candidate)` trips `is_in_cooldown(task_type_A, candidate)` to `True`.
- Edge case (the core new behavior): failures recorded for `(task_type_A, candidate)` do not trip or affect `is_in_cooldown(task_type_B, candidate)` for a different task_type — a fresh bucket starts healthy regardless of another bucket's state on the same candidate. **Covers AE1.**
- Happy path: `record_success(task_type, candidate)` resets only that bucket's failure count; a different task_type's bucket for the same candidate is unaffected.
- Happy path: advancing the injected fake clock past a bucket's cooldown expiry clears only that bucket; a different task_type's bucket for the same candidate keeps its own independent state.
- Edge case: a `(task_type, candidate)` pair never seen returns `is_in_cooldown()` `False` — implicit healthy-by-default, same as today's per-candidate default.
- Edge case: within one fixed task_type, every existing single-bucket behavior (trip at threshold, auto-expire, reset-on-success, absent-means-healthy) is unchanged — a direct translation of the current `test_cooldown.py` scenarios onto one bucket.

**Verification:** All test scenarios pass; per-bucket isolation is directly asserted (a failure sequence in one bucket provably leaves another bucket's state untouched), not just inferred from the rekey.

---

### U2. Thread `task_type` through `chain.py`'s cooldown call sites

**Goal:** `execute_chain()`'s `_attempt()` closure passes `task_type` to all three cooldown-store calls, so the rekeyed store actually gates per-`(task_type, candidate)` in production.

**Requirements:** R1, R3

**Dependencies:** U1

**Files:**
- Modify: `src/josu/delegate/chain.py`
- Test: `tests/delegate/test_chain.py`

**Execution note:** Test-first for the cooldown-check-and-skip path specifically — this is the layer where a wrong threading (e.g. a stale `task_type` captured incorrectly, or a call site accidentally left on the old single-argument form) would silently reproduce today's global-cooldown behavior without any test failing to say so.

**Approach:** Update the three existing call sites inside `_attempt()` — the `is_in_cooldown` check before attempting a candidate, `record_failure` in the exception handler, and `record_success` on a successful result — to pass `task_type` (already in scope via `_make_attempt()`'s closure over `execute_chain()`'s own parameter) as the new leading argument. No signature change to `execute_chain()` itself, or to any of its callers (`server.py`, `internal_api.py`, `daemon.py`) — `task_type` was always a required argument there; this only threads it one level deeper into calls that previously dropped it.

`tests/delegate/test_chain.py` has 18 direct `cooldown_store`/`store` method calls that bypass `execute_chain()` as test setup — every one needs the new leading `task_type` argument just to keep compiling against U1's new signature, independent of any behavioral changes below.

One existing test, `test_cooldown_state_shared_across_different_chains_for_same_candidate_name`, explicitly documents and asserts the pre-rekey behavior this plan reverses — its docstring says "Covers R6: cooldown state is a property of the candidate name, not the chain/task_type that resolved it," and its body trips cooldown via one task_type and asserts it's skipped via a different one. That assertion becomes false under R1. Rewrite it (not delete — the underlying regression it guards, "does cooldown state leak across task_types," is exactly what this plan must not reintroduce) to assert the opposite: a candidate tripped via one task_type is attempted normally via a different one, using the same shared store instance.

**Patterns to follow:** The existing `_attempt()` closure's control flow and exception handling — this is a call-site update, not a structural change.

**Test scenarios:**
- Happy path: a candidate cooled down for task_type A is skipped (`delegate()` never invoked) when `execute_chain()` resolves a chain for task_type A.
- Edge case (the core regression this unit prevents): a candidate cooled down for task_type A via one `execute_chain()` call is attempted normally — not skipped — on a subsequent `execute_chain()` call for task_type B, using the same shared `cooldown_store` instance. **Covers AE1.** This scenario is the rewritten form of `test_cooldown_state_shared_across_different_chains_for_same_candidate_name` (see Approach above) — its assertion direction flips, its name and docstring should too.
- Integration: `internal_api.py`'s pre-resolved-candidates branch (the `payload.candidates` path that buckets under `_PRE_RESOLVED_CHAIN_KEY`, the actual production trigger for commit-hook-driven proactive checks — not `run_proactive_check()`/`resolve_proactive_check_chain()`, which have zero in-repo production callers today) and a `resolve_chain()`-sourced call for a real task_type, both referencing the same candidate, trip and check cooldown independently. **Covers R3.**
- Happy path: a successful call resets only that `(task_type, candidate)` bucket's failure count, verified by a near-threshold failure streak on one task_type not tripping cooldown after an intervening success.
- Regression: existing single-task_type `test_chain.py` scenarios (cooldown trip, expiry, success-reset, chain-exhausted-when-every-candidate-cooled-down) continue to pass, and every direct `cooldown_store`/`store` method call in the file compiles against the new signature — confirms this unit widens the mechanism without changing behavior within one task_type.

**Verification:** All test scenarios pass; the rewritten cross-task_type isolation test explicitly proves R1 at the `execute_chain()` level, not just at the store level (U1 already proves it in isolation — this unit proves it end-to-end); no test in the file still asserts the pre-rekey sharing behavior.

---

## Scope Boundaries

**Deferred for later** (from origin)

- Empirical reordering (origin R5-R8) — a separate future plan. The `(task_type, candidate)` structure this plan produces is what that plan would extend with a success/attempt counter; no reordering behavior ships here.
- Hierarchical / partial-pooling statistical shrinkage across task_type buckets — unaffected by this plan's scope.
- A quality/correctness signal for candidate output — unaffected; this plan's bucketing is purely about reachability/reliability, same as the existing cooldown mechanism it extends.

**Outside this product's identity** (from origin, reaffirmed)

- ML/embedding-based routing or any bandit/neural-network-backed candidate selection — josu's delegate chain stays a bounded, developer-configured static guide; this plan only makes the existing cooldown gate finer-grained, it doesn't add a routing algorithm.
- Persisted cooldown state or a runlog-derived warm start on daemon restart — in-memory, reset-on-restart stays the model (R9).

**Deferred to Follow-Up Work** (plan-local)

- `fallback/quota.py`'s `route_bounded_request()` and `proactive/watchers.py`'s `run_proactive_check()` both accept a `cooldown_store` parameter but have zero in-repo callers today (confirmed via the same repo-wide search that scoped this plan to two files) — unchanged and untouched by this plan; if either becomes a live call path later, it inherits the rekeyed signatures automatically since neither calls the store's methods directly today.

---

## Risks & Dependencies

- Depends on `execute_chain()`'s `task_type` argument always being the caller-supplied, correct category — the same trust boundary the system already has today (`task_type` is a required field in the `delegate_to_local` MCP tool schema); this plan doesn't add or change that validation.
- Depends on `cooldown_store` remaining the SAME shared instance across both the MCP tool path and the internal HTTP route path (`daemon.py`'s `create_app()`), as already established by the original circuit-breaker feature — unchanged by this plan, but U2's cross-task_type isolation test relies on it.

---

## Sources / Research

- Origin brainstorm: `docs/brainstorms/2026-08-20-task-type-bucketed-delegate-cooldown-requirements.md` — full requirements, key decisions, and the comparison research (ruflo, resilience4j, Envoy) that scoped this down to an in-memory, task_type-bucketed rekey.
- `src/josu/delegate/cooldown.py`'s `CandidateCooldownStore` — the store this plan rekeys in place.
- `src/josu/delegate/chain.py`'s `_attempt()` closure — confirmed via repo-wide search to be the only caller of the store's `record_failure`/`record_success`/`is_in_cooldown` methods anywhere in the codebase, which is why this plan touches exactly two files.
- `docs/plans/2026-08-04-001-feat-delegate-candidate-circuit-breaker-plan.md` — the original feature this plan extends; U2's test-first execution note mirrors that plan's own U3 caution about this exact closure.
- `tests/delegate/test_cooldown.py`, `tests/delegate/test_chain.py` — existing test shape and `_FakeClock` convention this plan's test scenarios extend rather than replace. `test_chain.py`'s `test_cooldown_state_shared_across_different_chains_for_same_candidate_name` was confirmed (via a headless `ce-doc-review` feasibility pass) to assert the exact cross-task_type sharing this plan reverses, and 18 direct `cooldown_store`/`store` method calls in the same file were confirmed to need the new `task_type` argument — both now folded into U2 above.
- `src/josu/delegate/internal_api.py` — confirmed the actual production proactive-check trigger is the pre-resolved-candidates branch (`_PRE_RESOLVED_CHAIN_KEY = "__josu_internal_pre_resolved__"`), not `run_proactive_check()`/`resolve_proactive_check_chain()` (zero in-repo production callers today) — U2's R3 test scenario targets the real path.
