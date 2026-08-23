---
date: 2026-08-20
topic: task-type-bucketed-delegate-cooldown
---

# Task-Type-Bucketed Delegate Cooldown

## Summary

Re-key josu's existing per-candidate delegate cooldown to track failures per `(task_type, candidate)` instead of per candidate alone, so a candidate's trouble on one task_type no longer cools it down for unrelated ones. The same per-`(task_type, candidate)` data is also specified here for a later, separate phase that breaks ties in candidate order by recent reliability — captured now so planning doesn't have to invent that behavior when it's built.

---

## Problem Frame

`CandidateCooldownStore` (`src/josu/delegate/cooldown.py`) tracks consecutive failures per candidate name, globally across every task_type a candidate can be attempted for. A candidate that fails repeatedly on one narrow task_type — say, code generation it's genuinely weak at — cools down for every other task_type too, including ones it handles fine. This mirrors a documented failure mode in a comparable system (ruflo's model-routing bandit, ADR-142: "8 Haiku failures on one hard task suppress Haiku for all tasks, including trivial ones it handles well"), but hasn't yet been observed in josu's own usage — this is a preemptive correctness fix, not a response to an incident.

Separately, `resolve_chain()` (`src/josu/config/chains.py`) orders candidates purely from static `josu.toml` config (free-local-first, then TOML-authored order) with no memory of which candidates have actually been reliable for a given task_type. A candidate listed first but chronically failing still gets tried first on every call, wasting an attempt before the chain falls through to one that was going to work anyway.

---

## Key Decisions

- **Bucketing and reordering are separate phases, not one feature.** Bucketing is a bounded correctness fix with value independent of reordering — it ships first, on its own. Reordering is real product-behavior change (candidate selection changes without a config edit) and depends on data bucketing produces, so it's specified here but deferred to a later phase.
- **Reordering ships default-on with a config opt-out, not opt-in.** This trades against `chains.py`'s own framing as "a static, predefined guide, not a dynamic router" — reordering is a real, if small, step away from that. The opt-out preserves an escape hatch without requiring discovery to get the benefit.
- **In-memory and ephemeral, resetting on daemon restart — no persisted state.** Matches the existing cooldown store's design exactly. Both resilience4j's circuit breaker and Envoy's outlier detection treat this kind of state as disposable by design, not a gap to patch with persistence; a stale on-disk verdict about candidate health can be wrong by the time it's read back.
- **Reordering is a reliability signal, never a quality signal.** The only data josu records today (`observability/runlog.py`'s `DelegationEvent`) is whether a candidate responded successfully, not whether its output was correct. Labeling this as picking "the best" candidate would overstate what it measures — a real Goodhart's-law risk if left implicit.

---

## Requirements

**Cooldown bucketing (v1)**

- R1. A candidate's cooldown state is tracked per `(task_type, candidate)` pair, not per candidate alone — consecutive failures on one task_type do not affect the candidate's eligibility for a different task_type.
- R2. Within one `(task_type, candidate)` bucket, existing cooldown behavior is unchanged: N consecutive qualifying failures trip cooldown, the cooldown expires automatically, and any success resets the failure count.
- R3. Bucketing applies uniformly wherever a candidate can be attempted for a given task_type — both task-type delegation chains and the proactive-check chain.
- R4. Bucketing ships enabled by default with no new configuration required — it refines the existing mechanism's granularity rather than adding new opt-in behavior.

**Reordering (later phase, specified now)**

- R5. The same per-`(task_type, candidate)` structure also tracks a success/attempt count, used to reorder candidates by recent reliability within a task_type's already-resolved local-first / remote-opt-in group — never across that grouping.
- R6. Reordering only activates for a `(task_type, candidate)` pair once it has accumulated a minimum number of recorded attempts; below that minimum, candidate order stays exactly as `josu.toml` and free-local-first ordering produce today.
- R7. Reordering ships enabled by default once a pair crosses the minimum-attempts threshold, with a `josu.toml` opt-out to fully disable it.
- R8. Reordering is documented as a reliability/availability signal — does this candidate reliably respond for this task_type — never as a quality or correctness ranking.

**Constraints on both**

- R9. All new state is in-memory and per-daemon-process, exactly like the existing cooldown store — no new persisted file, nothing that survives a daemon restart.
- R10. Neither bucketing nor reordering can override `stays_hosted = true` or the free-local-first / `allow_remote` gating a chain already resolves — both operate strictly within a chain's already-resolved, already-filtered candidate list.

---

## Key Flows

- F1. Candidate trips cooldown for one task_type only
  - **Trigger:** a candidate accumulates N consecutive qualifying failures on a specific task_type.
  - **Steps:** the `(task_type, candidate)` bucket trips into cooldown; chain resolution for that task_type skips the candidate until the cooldown expires; chain resolution for any other task_type is unaffected.
  - **Outcome:** the candidate stays eligible for every task_type it hasn't failed on.
  - **Covers:** R1, R2, R3

- F2. Reordering activates once enough data exists (later phase)
  - **Trigger:** a `(task_type, candidate)` pair crosses the configured minimum-attempts threshold.
  - **Steps:** `resolve_chain()`'s local-first / remote-opt-in grouping resolves exactly as today; within each group, candidates are then ordered by recent success rate instead of TOML order.
  - **Outcome:** a chain that would otherwise attempt an unreliable candidate first tries a more reliable one first, without changing which candidates are eligible at all.
  - **Covers:** R5, R6, R7

---

## Acceptance Examples

- AE1. Given a candidate has tripped cooldown for task_type A only, When a task of task_type B resolves a chain containing that candidate, Then the candidate is attempted normally. **Covers R1.**
- AE2. Given the `josu.toml` reordering opt-out is set, When a `(task_type, candidate)` pair crosses the minimum-attempts threshold, Then candidate order still follows TOML-authored order exactly as today. **Covers R7.**
- AE3. Given a `(task_type, candidate)` pair has fewer attempts than the minimum threshold, When that task_type's chain resolves, Then candidate order is unaffected by any recorded outcomes. **Covers R6.**
- AE4. Given reordering is active for a task_type with both local and remote candidates, When the chain resolves, Then no remote candidate is ever ordered ahead of a local one regardless of recorded reliability. **Covers R10.**

---

## Scope Boundaries

**Deferred for later**

- Empirical reordering itself (R5-R8) — specified here so planning doesn't have to invent the behavior later, but its implementation is a separate phase from the v1 bucketing work.
- Hierarchical / partial-pooling statistical shrinkage across task_type buckets — a real refinement for the small-sample-per-bucket problem, worth revisiting only if bucket isolation proves costly in practice (e.g., seeding a new bucket from a candidate's cross-task_type history).
- A quality/correctness signal for candidate output — no mechanism exists today to score whether a candidate's output was actually right, only whether it responded successfully.

**Outside this product's identity**

- ML/embedding-based routing, a learned query-to-model router, or any bandit/neural-network-backed candidate selection (the ruflo/claude-flow approach researched as a comparison point) — josu's delegate chain stays a bounded, developer-configured static guide per `chains.py`'s existing design commitment. This feature only lets that guide's default ordering be broken by real reliability data; it doesn't replace the guide with a learned one.
- Persisted cooldown/reliability state or a runlog-derived warm start on daemon restart — in-memory, reset-on-restart stays the model, consistent with the existing cooldown feature.

---

## Dependencies / Assumptions

- Depends on the existing `CandidateCooldownStore` (`docs/plans/2026-08-04-001-feat-delegate-candidate-circuit-breaker-plan.md`) as the mechanism this extends, not replaces.
- Assumes `execute_chain()`'s existing `_ADVANCE_ON` failure classification stays the definition of a qualifying failure — the same dependency the original cooldown feature already carries.
- Assumes the reliability signal available today (served vs. skipped, via `observability/runlog.py`'s `DelegationEvent`) is sufficient for reordering's purposes — no new instrumentation is required to build R5-R8.
- No cross-task_type cooldown interference has been observed in production usage yet — this is a preemptive correctness fix and forward-looking capability, not a response to an incident.

---

## Outstanding Questions

**Deferred to Planning**

- Exact minimum-attempts threshold before reordering activates (R6) — should be informed by the existing `DEFAULT_CANDIDATE_FAILURE_THRESHOLD` (3) / `DEFAULT_CANDIDATE_COOLDOWN_SECONDS` (60.0) defaults' scale, not a generic microservice-breaker default. resilience4j's 100-call `minimumNumberOfCalls` default was researched and found not to transfer — it's for a rate-based sliding-window breaker, a different mechanism at a different call volume than josu's streak-based one.
- Exact `josu.toml` config key name/shape for the reordering opt-out (R7) — implementation detail, not a product decision.
- Whether bucketing and reordering land as one implementation unit or two sequenced ones — a planning-time sequencing choice. The product requirement is only that bucketing is valuable and shippable independent of reordering (R4 does not depend on R5-R8).

---

## Sources / Research

- `src/josu/delegate/cooldown.py`'s `CandidateCooldownStore` — the existing in-memory, per-candidate mechanism this feature re-keys and extends; today keyed by candidate name alone (`dict[str, _CandidateHealth]`).
- `src/josu/config/chains.py`'s `resolve_chain()`, `default_chain_order()`, and module docstring ("a static, predefined guide, not a dynamic router") — the free-local-first / `allow_remote` grouping any reordering must operate strictly within.
- `src/josu/observability/runlog.py`'s `DelegationEvent` — confirms the only signal available today is served-vs-skipped (reliability), never a correctness/quality score.
- `src/josu/config/__init__.py` — confirms current defaults (`DEFAULT_CANDIDATE_FAILURE_THRESHOLD = 3`, `DEFAULT_CANDIDATE_COOLDOWN_SECONDS = 60.0`), informing why external breaker defaults like resilience4j's 100-call minimum don't transfer to this codebase's scale.
- `docs/plans/2026-08-04-001-feat-delegate-candidate-circuit-breaker-plan.md` and its origin `docs/brainstorms/2026-08-04-delegate-candidate-circuit-breaker-requirements.md` — the existing cooldown feature this one extends; the in-memory-only, no-persisted-state design commitment carries forward unchanged.
- ruvnet/ruflo (github.com/ruvnet/ruflo) — researched as a comparison point. Its per-task-complexity-bucket Thompson-sampling bandit (ADR-142) motivated task_type bucketing; its per-model cost-optimal router (ADR-149) and its own admitted early mistake (shipping a router validated against hand-coded, not measured, quality scores) motivated labeling this a reliability signal rather than a quality one. Its ML/embedding-router machinery was scoped out as disproportionate to josu's candidate-roster scale.
- resilience4j (`CircuitBreaker` sliding-window `minimumNumberOfCalls`) and Envoy outlier detection (ejection-state ephemerality, escalating ejection duration) — researched for state-storage and minimum-sample-size best practices; both confirmed circuit-breaker-style state should stay in-memory/ephemeral rather than persisted, informing R9.
