---
date: 2026-08-04
topic: delegate-candidate-circuit-breaker
---

# Delegate Candidate Circuit Breaker

## Summary

Give josu's delegate fallback chain per-candidate failure memory: a candidate that fails repeatedly gets skipped for a cooldown window instead of eating its full timeout on every subsequent task, then becomes eligible again automatically once the cooldown elapses.

## Problem Frame

`execute_chain()` walks the resolved candidate chain fresh on every call, with no memory of past outcomes. A candidate that has been unreachable for the last several tasks still gets attempted — and times out — on every new task before the chain advances to the next candidate. Because every delegate call is serialized behind one global lock (`delegate/queue.py`'s `DelegateQueue`), that wasted timeout isn't just latency for the task that hit it; it's lock-hold time every other queued caller waits behind too. The cost is pure waste: identical outcome to a fast skip, just slower.

This surfaced while researching OmniRoute — a multi-provider AI gateway — as a possible design reference for josu's broader delegation story. That broader comparison didn't hold: OmniRoute solves a materially different, larger-scale problem (500+ models across 268+ vendors) than josu's static 2-5-candidate delegate tier, and its actual maturity and security track record turned out weaker than its marketing suggests (see Sources / Research). Its per-candidate circuit-breaker pattern, though, is the one piece that transfers cleanly regardless of scale.

## Key Decisions

- **Time-boxed cooldown, not probe-based Half-Open recovery.** OmniRoute's own three-state (Closed/Open/Half-Open) design has real, reported edge-case bugs in failure classification — network blips incorrectly tripping the breaker, recovery-state races. A simple failure-count-then-cooldown-timer delivers the same practical benefit (stop hammering a dead candidate) without that state-machine surface.
- **In-memory, per-daemon-process state, not persisted.** Cooldown state resets when `josu daemon start` restarts. This is the smallest form that still delivers the value, consistent with other state the daemon already holds only for its own process lifetime.
- **Reuses the existing failure classification, not a new taxonomy.** `execute_chain()` already classifies which exceptions advance the chain to the next candidate (`_ADVANCE_ON`: `DelegateError`, `TimeoutError`). A qualifying failure for cooldown purposes is the same set — no second, parallel definition of "this candidate is unhealthy."
- **Scoped to this one mechanism, not OmniRoute's broader design.** The rest of OmniRoute's sophistication — load balancing, semantic caching, multi-vendor OAuth, a provider catalog — solves a scale problem josu doesn't have, and research surfaced real security issues (a hardcoded JWT secret, an SSRF vulnerability) in that codebase that make wholesale adoption a bad trade even before scale is considered.

## Requirements

- R1. A candidate that fails N consecutive qualifying times during chain execution is excluded from chain attempts for a cooldown window, rather than retried (and timed out) on every subsequent task.
- R2. Once the cooldown window elapses, the candidate becomes eligible for chain attempts again automatically — no manual reset required.
- R3. A successful call to a candidate resets its consecutive-failure count.
- R4. A qualifying failure is the same exception classification `execute_chain()` already uses to advance to the next candidate — no new failure taxonomy.
- R5. The failure threshold and cooldown duration are configurable in `josu.toml`, following the existing developer-override pattern (`explicit_order`, `allow_remote`, `wall_clock_timeout_seconds`), with sensible built-in defaults when unset.
- R6. Cooldown state applies uniformly to every chain a candidate can appear in — both task-type delegation chains and the proactive-check chain — since it belongs to the candidate, not the task type.
- R7. A cooldown-skip (the chain never attempted a tripped candidate) is recorded in the run log as a reason distinguishable from a same-request attempt-and-fail.

## Key Flows

- F1. Candidate trips into cooldown
  - **Trigger:** A candidate accumulates N consecutive qualifying failures (R4) across chain attempts.
  - **Steps:** `execute_chain()` records the Nth failure; the candidate is marked in cooldown with an expiry; the chain advances to the next candidate exactly as it does today.
  - **Outcome:** every chain resolution — any task, any chain — skips this candidate without attempting it until the cooldown expires.
  - **Covers:** R1, R2, R4

- F2. Candidate recovers
  - **Trigger:** A chain resolution runs after the cooldown window has elapsed.
  - **Steps:** the candidate is eligible again; a success resets its failure count (R3); a fresh failure starts a new count toward another cooldown.
  - **Outcome:** no manual intervention brings the candidate back.
  - **Covers:** R2, R3

## Acceptance Examples

- AE1. Given a candidate has failed the configured threshold of consecutive times, When a new task resolves a chain containing it, Then it is skipped without an attempt, and the run log records the reason as cooldown, not attempt-failure. **Covers R1, R7.**
- AE2. Given every candidate in a resolved chain is currently in cooldown, When the chain executes, Then the existing chain-exhausted signal fires exactly as it does for any exhausted chain today — cooldown is a possible cause, not a new outcome type. **Covers R1, R6.**
- AE3. Given a candidate was in cooldown, When the cooldown elapses and the next chain resolution attempts it and it succeeds, Then its consecutive-failure count resets to zero. **Covers R2, R3.**

## Scope Boundaries

**Deferred for later**

- Probe-based Half-Open recovery (a trial request before fully reopening) — the cooldown-timer approach is the v1 mechanism; revisit only if blind auto-retry-after-cooldown proves worse in practice than a probe would be.
- Persisting cooldown state across daemon restarts — in-memory-only is v1 scope.
- Populating the run log's existing `DelegationEvent.cost` field — a related, independent, pre-existing gap noticed during this research, not part of this feature.

**Outside this product's identity** (carried from origin, reaffirmed)

- OmniRoute's broader gateway design — load-balancing strategies, semantic caching, multi-vendor OAuth/PKCE, a provider catalog, routing telemetry. Confirmed during this brainstorm to be solving a problem at OmniRoute's scale that josu's static delegate tier doesn't have.
- Dynamic quality/cost-based delegate routing (RouteLLM/Martian-style) — already deferred in the origin plan; this brainstorm found no new evidence to revisit that.
- A general-purpose LLM router/gateway — already outside this product's identity per the origin plan; unaffected here.
- OmniRoute's skill/MCP-server packaging pattern (scoped `SKILL.md` files decomposed by capability, fronting a separate MCP server) — raised as an interesting distribution pattern during this brainstorm, explicitly set aside from this doc's scope.

## Dependencies / Assumptions

- Depends on `execute_chain()`'s existing `_ADVANCE_ON` failure classification staying the definition of a qualifying failure (R4) — if that classification changes, this mechanism's trip criteria changes with it, by design.
- Assumes the new config knobs belong in `josu.toml` alongside the existing developer-override fields, not a separate config surface.
- Assumes cooldown state can live wherever the daemon already holds other in-memory, per-process state — no new persistence layer.

## Outstanding Questions

**Deferred to Planning**

- Exact default values for the failure threshold (N) and cooldown duration — left to planning, informed by existing delegate-call timeout norms already in the codebase.
- Exactly where cooldown state lives structurally (new module vs. alongside `DelegateQueue`) — an implementation choice, not a product decision.

## Sources / Research

- OmniRoute (github.com/diegosouzapw/OmniRoute, forked from github.com/decolua/9router) — researched as a potential design reference. Both repos are approximately 6-7 months old with signs of inflated adoption metrics (a roughly 175:1 star-to-subscriber ratio against an organic norm closer to 20:1). A hardcoded fallback JWT secret (CVE-2026-49352) and an SSRF vulnerability were both found and patched in the underlying `9router` project. Even OmniRoute's own circuit breaker has had reported edge-case bugs (network errors incorrectly tripping it, Half-Open recovery races) — informs the Key Decision to use a simpler cooldown-timer mechanism instead of replicating its state machine.
- `src/josu/delegate/chain.py`'s `execute_chain()`, `SkipRecord`, and `ChainExhaustedError` — the existing chain-walk and failure-reporting mechanisms this feature extends.
- `src/josu/config/chains.py`'s `resolve_chain()` and `resolve_proactive_check_chain()` — confirms both chain types draw from the same candidate registry, supporting R6.
- `src/josu/orchestrator/circuit_breaker.py` — confirms the existing `CircuitBreaker` is a whole-run wall-clock budget only, with no per-candidate concept, which is the gap this feature closes.
- `docs/plans/2026-07-21-001-feat-hybrid-local-hosted-coding-agent-plan.md`'s Scope Boundaries — origin for the "outside this product's identity" and "dynamic quality-based routing deferred" positions this brainstorm reaffirms.
