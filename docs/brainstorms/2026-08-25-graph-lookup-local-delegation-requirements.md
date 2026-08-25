---
date: 2026-08-25
topic: graph-lookup-local-delegation
---

# Local-First Delegation for Context-Graph Lookups

## Summary

Documents a proposed architecture change — routing `context-graph` MCP lookups through the existing local-first delegate chain, falling back to the hosted model when that chain is exhausted — as a candidate future direction, not approved scope. The premise (that this saves meaningful hosted-LLM tokens) is unvalidated. This doc preserves the idea and the risks identified against it for a later decision, once usage data exists.

---

## Problem Frame

Today, `context-graph`'s `search`/`execute` tools are called directly by the hosted (cloud) LLM and return raw graph-engine results straight into its context window, with no dependency on any local model. The idea explored here started from an assumption — that this raw-JSON-into-cloud-context pattern costs meaningfully more hosted tokens than necessary, and that a local model answering the lookup first would save tokens.

That assumption was never measured. Dialogue exploring the idea surfaced several reasons to doubt it holds as stated: individual lookup payloads are likely bounded by the tools' own small default limits, any real savings are more likely concentrated in a minority of large or broad lookups than in the typical case, and the highest-volume use case raised during discussion — understanding a multi-repo codebase during planning — doesn't map onto the current single-engine, single-repo-scoped architecture at all. No measurement exists to confirm or refute any of this either way.

---

## Key Decisions

- **Document, don't implement.** This idea is captured for future reference and possible planning, not scoped for `/ce-plan` or `/ce-work` now. Nothing here is approved to build.
- **Local-first, hosted-fallback shape.** If ever built, the mechanism reuses the existing free-local-first delegate-chain pattern already governing `delegate_to_local` task types, rather than inventing new routing: a lookup is attempted via the local delegate chain first, falling back to the cloud LLM's own direct `context-graph` call when that chain is exhausted.
- **Default-on, config-reversible, if built.** The design assumes the behavior would ship default-on for all projects, with a `stays_hosted`-style per-task-type override in `josu.toml` to opt back to today's always-hosted lookups.
- **Deterministic payload tuning is a separate, smaller effort.** Tighter default limits and response truncation were considered in the same discussion and intentionally kept out of this doc — that direction doesn't depend on any of this idea's open questions and can be pursued independently at any time.
- **Multi-repo / cross-engine search is out of scope for this idea entirely.** The current architecture supports exactly one active graph-engine target and one `scope_root` per daemon (see Sources below); nothing here proposes changing that.

---

## Requirements

These describe what the documented proposal specifies, not approved build scope — see Key Decisions.

**Proposed mechanism**

- R1. A lookup-flavored task type resolves to the project's configured delegate chain the same way other `delegate_to_local` task types do, trying local/free candidates before any opted-in remote one.
- R2. When the delegate chain is exhausted for a lookup, the cloud LLM falls back to calling `context-graph.search`/`execute` directly, mirroring the existing chain-exhausted-fallback convention.
- R3. A local delegate's digested answer for a lookup preserves exact file paths and symbol identifiers verbatim rather than paraphrasing them, so the cloud LLM can act on the result directly.
- R4. A project can opt a lookup task type back to always-hosted via the existing `stays_hosted`-style config override, without affecting other task types.

**Prerequisites before this can be planned**

- R5. Actual `context-graph` response sizes and per-session call volume are measured before any implementation work starts.
- R6. The measurement covers at least one long, lookup-heavy planning-style session, not only isolated single lookups — aggregate call volume was a distinct concern raised in dialogue, separate from per-call payload size.

---

## Risks (identified in dialogue, unresolved)

- **Unvalidated premise.** No measurement shows `context-graph` lookups cost meaningfully more hosted tokens than they should; the idea could be solving a problem that doesn't exist at meaningful scale.
- **Cost/benefit asymmetry.** Local-first pays latency and reliability cost on every lookup, but likely only saves meaningful tokens on a minority of large ones.
- **Fidelity risk on a load-bearing path.** A local model paraphrasing a lookup answer risks corrupting exact file paths or symbol names the cloud LLM then acts on directly — a categorically different risk than typical delegated tasks like summarization or boilerplate generation.
- **Hot-path latency.** Graph lookup is likely one of the most frequent operations in a session; replacing a fast direct call with local inference (default delegate timeout: 60s) risks a default-on latency regression for every user.
- **Default-on risk management.** Shipping an unvalidated mechanism as the default, rather than opt-in or canaried, means a wrong bet degrades every project's experience until someone finds the override.
- **Weak fallback economics.** The existing chain-exhausted-fallback convention is a prompt-level contract, not a guarantee; even a successful fallback costs two round trips (a failed delegate attempt plus the cloud LLM's own direct call) instead of one.
- **Queue contention.** `delegate_to_local`'s chain execution holds a single lock across the whole candidate sequence, designed for occasional, effort-tolerant sub-tasks; routing high-frequency lookups through the same queue risks contention with genuine delegated tasks.
- **New integration surface.** `context-graph` lookups have no `task_type` concept today; giving them one to reuse `stays_hosted` is new design work, not a free reuse of an existing mechanism.

---

## Scope Boundaries

**Deferred for later**

- Deterministic payload-size tuning (tighter limits, truncation) — a separate, smaller, independently valuable effort.
- Escalating to size-triggered digestion (compressing only large results) if instrumentation shows savings are concentrated in a tail of large lookups rather than spread evenly.
- Session-level caching or deduplication of repeated lookups, raised as a candidate fix for the call-volume concern that this idea's mechanism does not itself address.

**Outside this idea's identity**

- A unified MCP surface merging Gortex and Graphify into one interface — a distinct, separate project.
- True multi-repo or multi-engine simultaneous search — not supported by the current architecture (one active `[[graph.engines]]` target, one `scope_root` per daemon) and not something this idea proposes building.

---

## Dependencies / Assumptions

- Assumes the project's existing delegate-chain infrastructure (`chains.py`, `chain.py`, the cooldown store) is reused rather than rebuilt, if this is ever planned.
- Assumes at least one local delegate candidate is configured for the local-first path to have any effect; a project with none configured falls back immediately under today's `NoCandidatesError` semantics (see Outstanding Questions).

---

## Outstanding Questions

Nothing here blocks writing this idea down; these are open forks a future planning pass would need to resolve, not blockers on this doc.

**Deferred to planning**

- Whether `NoCandidatesError` (no delegate candidates configured at all) needs the same chain-exhausted-fallback guidance as `ChainExhaustedError`. Default-on rollout would make the no-candidates case common for projects with no local model set up, and today's guide only names the latter explicitly.
- Whether `search` and `execute` share one lookup task type and override, or `execute`'s broader operation surface warrants its own.
- Exact task-type naming and config schema for a lookup-flavored delegate chain entry.
- Whether local-first should apply uniformly or only above a size threshold, once instrumentation data exists to inform that call.

---

## Success Criteria

Because this doc documents an idea rather than approved scope, success is not "shipped behavior." Success is: instrumentation data on `context-graph` payload size and per-session call volume exists, and a build / no-build / revise decision on this idea can be made from that data rather than from assumption.

---

## Sources / Research

- `src/josu/graph/server.py` — `context-graph` MCP tool description and the two-tool minimal-surface rationale this idea would need to reconcile with.
- `src/josu/delegate/local_model.py` — the existing `_graph_context()` step already querying the graph on behalf of a delegated task (`limit=5`); default 60s delegate timeout.
- `src/josu/delegate/chain.py`, `src/josu/delegate/queue.py` — existing free-local-first fallback-chain machinery and its single-lock queue design.
- `src/josu/CLAUDE.md.template` — the existing Delegation Guide, including the `stays_hosted` override and the chain-exhausted-fallback convention this idea would extend.
- `src/josu/config/graph_engines.py` — confirms only one `[[graph.engines]]` target is ever active; "real multi-engine selection is out of scope for now."
- `docs/brainstorms/2026-08-05-headroom-docs-recommendation-requirements.md` — prior brainstorm that rejected owned context-compression machinery for a similarly unconfirmed problem; directly informs this doc's "measure first" framing.
