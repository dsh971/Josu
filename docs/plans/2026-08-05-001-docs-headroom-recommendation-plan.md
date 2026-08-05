---
title: Headroom Docs Recommendation
type: docs
date: 2026-08-05
origin: docs/brainstorms/2026-08-05-headroom-docs-recommendation-requirements.md
---

# Headroom Docs Recommendation

## Summary

Add one short paragraph to `README.md` recommending [Headroom](https://github.com/headroomlabs-ai/headroom) as an optional, external context-compression tool — no new josu dependency, protocol, or subprocess integration.

## Problem Frame

`delegate()` (`src/josu/delegate/local_model.py:155-161`) serializes `{task, scope, context}` raw into the message sent to a candidate model; `internal_api.py`'s `MAX_SCOPE_BYTES = 200_000` (`src/josu/delegate/internal_api.py:99-100`) is a hard rejection wall, not a size reduction. Local delegate candidates typically run smaller effective context windows than hosted frontier models, so a large-but-under-cap payload could still degrade a local candidate's response — though no such incident has actually been observed yet (see origin: docs/brainstorms/2026-08-05-headroom-docs-recommendation-requirements.md). Three code-based ways to address this were considered and rejected in favor of documentation only (see Key Technical Decisions).

## Key Technical Decisions

- **Documentation only, no code integration** (see origin): a library+protocol dependency, an MCP-subprocess integration mirroring `gortex`, and vendoring Headroom's compressors were all considered and rejected — the problem is unconfirmed (preemptive), and each code-based option adds carrying cost disproportionate to that. This plan adds prose only.
- **Inline paragraph, not a new heading.** README has no existing "optional extras" section; it consistently introduces optional/secondary tools inline via link + one functional clause + explicit optional-hedge (e.g. `local model server, README.md:26-27` for `gortex`/Ollama) and subordinates secondary asides via parenthetical/em-dash rather than new headings (`README.md:91`). A new heading for one optional tool would break that convention.
- **Local-candidate-proxy example leads; `headroom wrap claude` is fully subordinate.** Josu's own delegate calls to local candidates are direct HTTP calls made from josu's daemon (`local_model.py`); they never route through the `claude` CLI subprocess (`src/josu/orchestrator/adapter.py`). `headroom wrap claude` only affects what the wrapped `claude` process sends to Claude's own API — it doesn't touch josu's local-candidate payloads. The note must lead with pointing a candidate's `endpoint` at a Headroom proxy in front of its own model server, and mention `wrap claude` as a one-sentence aside.
- **Publish now with an explicit unverified caveat, rather than testing first.** Resolves the origin doc's one deferred question. Testing Headroom's proxy against Ollama before publishing is disproportionate effort for a one-paragraph doc edit; the note states plainly that local-model compatibility hasn't been verified (R4) instead.

## Requirements

(carried from origin: docs/brainstorms/2026-08-05-headroom-docs-recommendation-requirements.md)

- R1. The change is documentation only — no new dependency, protocol, or subprocess integration is added to josu's own code or config schema.
- R2. The doc recommends pointing a local delegate candidate's `endpoint` (in `josu.toml`) at a Headroom proxy in front of the candidate's own local model server, framed as the primary use case (local-model context-window reliability).
- R3. The doc mentions `headroom wrap claude` for reducing hosted-Claude Code token cost as a secondary, clearly subordinate use case.
- R4. The doc does not claim or imply that Headroom's compatibility with local models or with proxying a local OpenAI-compatible endpoint has been verified by josu's maintainers — it's presented as a suggestion to try, not an endorsed, tested integration.

## Implementation Units

### U1. Add the Headroom paragraph to README.md

**Goal:** Insert the recommendation note in `README.md`'s existing Configure subsection.

**Requirements:** R1, R2, R3, R4

**Dependencies:** none

**Files:**
- `README.md` (modify)

**Approach:** Insert a new paragraph immediately after the closing ` ``` ` of the existing `[[delegate.candidates]]` example block and before the sentence pointing readers to the full schema (currently `README.md:66-68`). The paragraph:
1. Names Headroom with a link, one clause on why it's relevant here (compresses context sent to a delegate model), explicitly flagged as optional/external/not-a-josu-dependency — mirroring the hedge pattern used for `gortex`/Ollama (`README.md:26-27`).
2. States the concrete mechanism: point a local candidate's `endpoint` at a Headroom proxy sitting in front of that candidate's own model server. A minimal `[[delegate.candidates]]` variant illustrating only the changed `endpoint` value (mirroring the existing example at `README.md:46-50`; schema confirmed in `src/josu/config/delegate.py:30-37`) may accompany the prose if it reads better than prose alone — implementer's call.
3. Adds one subordinate sentence (parenthetical or em-dash aside, matching `README.md:91`'s existing pattern) mentioning `headroom wrap claude` for hosted-Claude cost, not given its own paragraph.
4. States plainly that this hasn't been verified against Ollama or small local models specifically (R4) — a suggestion to try, not a tested recommendation.

**Patterns to follow:**
- `README.md:26-27` — how `gortex` and Ollama are introduced (link + functional clause + optional-tool hedge).
- `README.md:91` — existing parenthetical-aside pattern for subordinating a secondary point.
- `README.md:46-50` — existing `[[delegate.candidates]]` example block format.

**Test scenarios:** Test expectation: none -- pure documentation content with no executable behavior.

**Verification:** `README.md` contains the new paragraph in the Configure subsection, and it (1) names Headroom as optional/external/not-a-dependency, (2) leads with the local-candidate-endpoint-proxy use case, (3) subordinates `headroom wrap claude` as a secondary aside rather than its own paragraph, (4) does not assert or imply verified compatibility with local models or Ollama proxying, (5) matches the surrounding prose's tone (no marketing language, dense/technical, hedged consistently with the rest of the doc).

## Scope Boundaries

**Deferred for later** (from origin)
- Depending on Headroom as a Python library behind a josu-owned `ContextCompressor`-style protocol.
- Running Headroom as an MCP-server subprocess integrated into josu's daemon, mirroring the `gortex` pattern.
- Revisit both if a real local-model failure or cost problem is actually observed.

**Outside this product's identity** (from origin)
- Vendoring or porting Headroom's compression algorithms into josu's own codebase.

## Risks & Dependencies

- **Partially addressed:** Headroom's own docs describe pointing its proxy at a local OpenAI-compatible backend, including Ollama's endpoint specifically (confirmed during doc review) — so the mechanism in R2 isn't resting on a nonexistent capability. Josu's maintainers still haven't independently verified this end-to-end (R4's caveat stands for that reason).
- **Unverified:** whether Headroom's compression quality holds up when the downstream consumer is a small local model — its published benchmarks are presumably measured against larger hosted models.
- Both are stated as explicit caveats in the note itself (R4), not resolved by this plan.

## Sources / Research

- `src/josu/delegate/local_model.py:84-90, 119-125, 155-161` — `delegate()`'s raw message construction.
- `src/josu/delegate/internal_api.py:99-100, 154` — `MAX_TASK_CHARS` / `MAX_SCOPE_BYTES`, a rejection wall, not size reduction.
- `src/josu/config/delegate.py:30-37` — `DelegateCandidate` schema (`name`, `endpoint`, `api_key_env`, `local`, `model`).
- `README.md:26-27, 46-50, 66-68, 91` — insertion point, existing example format, and existing voice/hedging conventions for introducing optional external tools.
- `src/josu/orchestrator/adapter.py` — josu invokes `claude` as a subprocess; local-delegate payloads never route through it, which is why `headroom wrap claude` doesn't reach them.
- `docs/brainstorms/2026-08-05-headroom-docs-recommendation-requirements.md` — origin requirements and full decision rationale.
