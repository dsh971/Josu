---
date: 2026-08-05
topic: headroom-docs-recommendation
---

# Headroom Docs Recommendation

## Summary

Add a short docs note (README / getting-started) recommending [Headroom](https://github.com/headroomlabs-ai/headroom) as an optional external tool users can point their own setup at for context-size reduction — a Headroom proxy in front of a local delegate candidate for context-window reliability, and `headroom wrap claude` for hosted-Claude cost as a secondary mention. Josu itself ships no compression code, dependency, or protocol for this.

## Problem Frame

Josu's `delegate()` (`src/josu/delegate/local_model.py:155-161`) builds `{task, scope, context}` and serializes it raw into the message sent to a candidate model — no truncation, summarization, or compression. `internal_api.py` enforces `MAX_SCOPE_BYTES = 200_000` (`src/josu/delegate/internal_api.py:99-100`), but that's a hard rejection wall, not a size reduction: a payload under the cap still goes through completely unreduced. Local delegate candidates (e.g. `qwen2.5-coder:7b` via Ollama) typically run with much smaller effective context windows than hosted frontier models, so a large-but-under-cap payload could still overflow or degrade a local candidate's response quality.

No such incident has actually happened yet — this is anticipatory, not a response to an observed failure or cost problem. Josu's own stated identity (`README.md`) is to stay lean and config-driven rather than hard-coding vendor integrations ("not hardcoded to one vendor," adapters as "a declarative config entry ... not a hand-written integration").

## Key Decisions

- **Docs-only, not an owned integration.** Three code-based approaches were considered — depending on Headroom as a Python library behind a new `ContextCompressor` protocol, running Headroom as an MCP-server subprocess (mirroring how `gortex` is integrated), and vendoring/porting Headroom's compressor code directly into josu. All three were rejected in favor of a documentation-only recommendation: the underlying problem is unconfirmed (preemptive, no incident), and each code-based option adds real carrying cost — a new dependency's import weight is unverified, a subprocess adds a third external tool to install, and vendored code is harder to swap out later than something behind a dependency or process boundary, which contradicts the explicit goal of being able to switch to a better solution later.
- **Local-model reliability is the framing, not hosted-Claude cost.** Josu's own delegate calls to local candidates are direct HTTP calls made from josu's daemon process (`local_model.py`) — they never go through the `claude` CLI subprocess (`src/josu/orchestrator/adapter.py`). `headroom wrap claude` only compresses what the wrapped `claude` process sends to Claude's own API; it does not touch josu's local-candidate payloads at all. The recommendation must lead with pointing a local candidate's `endpoint` at a Headroom proxy sitting in front of the local model server, since that's the piece that actually addresses local-model context-window reliability. Mentioning `headroom wrap claude` for hosted-Claude cost is a secondary, bonus note, not the primary point.
- **"Live-zone" compression is not the relevant Headroom mechanism.** Live-zone/cache-alignment exists to avoid busting a *hosted provider's* prompt cache. Josu doesn't proxy Claude's own API calls (it shells out to the `claude` CLI and lets Claude Code manage its own caching), so this problem doesn't apply to josu. The Headroom capability actually relevant here is its content-aware compression pipeline (SmartCrusher for JSON, CodeCompressor for AST-aware code), reached through Headroom's proxy mode.

## Requirements

**Scope**

- R1. The change is documentation only — no new dependency, protocol, or subprocess integration is added to josu's own code or config schema.

**Recommendation content**

- R2. The doc recommends pointing a local delegate candidate's `endpoint` (in `josu.toml`) at a Headroom proxy in front of the candidate's own local model server, framed as the primary use case (local-model context-window reliability).
- R3. The doc mentions `headroom wrap claude` for reducing hosted-Claude Code token cost as a secondary, clearly subordinate use case.
- R4. The doc does not claim or imply that Headroom's compatibility with local models or with proxying a local OpenAI-compatible endpoint has been verified by josu's maintainers — it's presented as a suggestion to try, not an endorsed, tested integration.

## Scope Boundaries

**Deferred for later**
- Depending on Headroom (or a similar tool) as a Python library behind a josu-owned `ContextCompressor`-style protocol.
- Running Headroom as an MCP-server subprocess integrated into josu's daemon, mirroring the `gortex` integration pattern.
- Both are worth revisiting if a real local-model failure or cost problem is actually observed — this brainstorm's conclusion is specific to the current preemptive, unconfirmed state.

**Outside this product's identity**
- Vendoring or porting Headroom's compression algorithms into josu's own codebase — rejected on principle (harder to swap out later, not easier), not just as premature.

## Dependencies / Assumptions

- **Unverified:** whether Headroom's proxy mode can actually target an arbitrary local OpenAI-compatible backend (e.g. Ollama's `/v1` endpoint) rather than only real cloud providers.
- **Unverified:** whether Headroom's compression quality holds up when the downstream consumer is a small local model. Its published benchmarks are presumably measured against larger hosted models; it's unconfirmed whether compressed context still reads well to something like a 7B local candidate.

## Outstanding Questions

**Deferred to Implementation**
- Whether to actually test the Ollama-proxy configuration before publishing the note, or ship it with the unverified caveat as-is — a judgment call on how much verification effort a preemptive doc change warrants.

## Sources / Research

- `src/josu/delegate/local_model.py:84-90, 119-125, 155-161` — `delegate()`'s message construction; scope/context serialized raw, no reduction.
- `src/josu/delegate/internal_api.py:99-100, 154` — `MAX_TASK_CHARS` / `MAX_SCOPE_BYTES`, a hard rejection wall, not a size-reduction mechanism.
- `src/josu/delegate/client.py:90-101` and `src/josu/delegate/chain.py:95` — `DelegateClient` Protocol and `ClientFactory`, josu's existing pattern for swappable transport implementations.
- `src/josu/graph/engine.py:24-49` — `GraphEngine` Protocol, the second existing swappable-implementation precedent.
- `src/josu/graph/gortex.py:203-220` — `gortex` integrated as an external subprocess/binary, not vendored — the precedent behind the rejected MCP-subprocess approach.
- `src/josu/orchestrator/adapter.py` — josu invokes the `claude` CLI as a subprocess; josu does not construct or proxy Claude's own API calls itself, which is why `headroom wrap claude` doesn't reach josu's local-delegate payloads.
- `README.md` — josu's stated "not hardcoded to one vendor" / declarative-config philosophy, and its core "don't pay frontier-model prices" cost framing.
- Headroom (github.com/headroomlabs-ai/headroom) — Apache 2.0 licensed; ships library, HTTP-proxy, and MCP-server deployment modes; content-aware compression pipeline (SmartCrusher, CodeCompressor, Kompress-v2-base); live-zone/cache-alignment compression for hosted-provider prompt caches.
