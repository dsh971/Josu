---
date: 2026-08-07
topic: gortex-integration-rework
---

# Gortex Integration Rework

## Summary

Fix josu's broken gortex integration by making the context engine a config-declared connection target, not a process josu installs, spawns, or authenticates itself. josu is a pure client everywhere — for the hosted agent's proxied lookups and the delegate model's direct ones alike — degrading to its existing file-exploration fallback when nothing is configured or reachable. Gortex is the default, capability-leading target; graphify is added as a narrow, lazily-checked complement for Excel/Word/Google-Workspace formats, installed by the user rather than by josu.

## Problem Frame

`josu daemon start` — and therefore `josu run`, `josu delegate`, and the post-commit proactive-check hook — are fully blocked today. Josu's `gortex_process.py` hardcodes a `--http-addr` flag the real, currently-installed gortex CLI (v0.62.0) doesn't have, and assumes a disposable "spawn one embedded process per josu-daemon lifecycle" model that no longer matches how gortex actually works. Live testing against the real binary (this session) confirmed the mismatch goes deeper than a flag rename: `--index`/`--no-daemon` are now silently-ignored no-ops, and `/healthz` (the endpoint josu polls for startup readiness) doesn't respond. No version-compatibility check exists anywhere in the codebase, and the existing test suite gives zero signal on any of this — both `tests/graph/test_gortex_process.py` and `tests/graph/test_gortex.py` test against fake HTTP servers standing in for gortex, so they passed the entire time the real integration was completely broken.

Gortex's own reference guide (`gortex guide`, checked live this session) also revealed josu is using a tiny fraction of gortex's actual capability — a narrow `/v1/tools/search` REST-style call — while gortex has native support for doc-comments, TODOs, doc-staleness analysis, PDFs, images, infra-as-code files, and arbitrary "artifacts" (ADRs, API specs, DB schemas, configs) declared via `.gortex.yaml`. This reframes what originally looked like a need for a second "unstructured data" engine: most of that ground is already covered by gortex itself, once integrated properly.

## Key Decisions

- **josu never installs, spawns, or authenticates a context-engine process — for either consumer.** The hosted agent's proxied lookups and the delegate model's direct ones both go through josu's daemon acting purely as a client. This mirrors how josu already treats the hosted CLI agent itself: a documented prerequisite the user installs, never something josu installs on their behalf.
- **The context engine is a config-declared connection target, not hardcoded to gortex.** A declarative entry — the same shape as josu's existing `[[orchestrator.adapters]]` (command/args, no install ownership) and delegate candidates' `api_key_env` (josu reads a user-configured credential, never generates or stores one) — points josu's daemon at a reachable engine. Gortex is the default/reference target given its current capability lead; any engine speaking the expected `GraphEngine` surface can serve the same role.
- **The hosted agent keeps seeing only josu's own narrow MCP surface.** Claude Code connects to josu's `context-graph`/`delegate-to-local` servers, never to the underlying context engine directly — the engine swap happens behind that boundary. Considered and rejected: letting the hosted agent manage gortex itself via its own MCP config (matching Claude Code's native `.mcp.json` lifecycle model) — it would expose gortex's full native tool surface to the hosted agent instead of josu's curated one, and does nothing for the delegate side, which isn't an MCP client the hosted agent manages.
- **No engine configured or reachable is the default degraded posture, not an explicit decline.** Both consumers fall back to josu's existing direct-file-exploration mode transparently. There's no separate install-consent flow to decline in the first place.
- **Gortex's watcher becomes the sole reindex trigger once a target is configured; josu retires its own.** Gortex's daemon-tracked mode watches continuously via fsnotify with no repo-wide off switch (`gortex config exclude` only manages sub-path excludes) — confirmed live. Running josu's own commit/merge-triggered reindex calls alongside it would double-trigger on the same changes.
- **The post-commit hook's reindex call and its proactive-check call are separable — only the reindex call is retired.** `install_commit_hook()`'s hook currently calls `_run_reindex_on_commit()` then runs the proactive check, in one script invocation (`watchers.py:638-656`). Retiring josu's reindex trigger means removing that one call, not the hook — proactive checks stay exactly as they are today.
- **A version-compatibility guard checks whatever's connected, not just corrected flags.** The gortex binary tested this session was built the same week as the walkthrough that found it broken — gortex's CLI surface moves fast, and josu currently has zero defense against that. A check against a known-compatible version range, failing with a clear message instead of the engine's raw usage dump, is part of "fixed" — not a follow-up.
- **Verify against gortex's actual current capability, not just restore prior narrow usage.** The original plan named gortex's own docs as the source of truth but flagged several behaviors "unverified at time of writing" and never checked them before shipping — a mistake worth not repeating. The fix should be grounded in gortex's real, current documented surface (`gortex guide`, its docs pages) plus live testing against the real binary.
- **Graphify is scoped narrowly to Excel/Word/Google-Workspace formats, not general "unstructured data."** Gortex already covers PDFs, images, and generic docs/artifacts. Graphify's genuinely non-redundant value is Excel (`.xlsx`), Word (`.docx`), and Google Workspace formats (`.gdoc`/`.gsheet`/`.gslides`), which gortex doesn't ingest at all.
- **Graphify's presence is checked lazily; josu instructs, never installs.** Most josu usage is pure code work where gortex alone suffices. On first encountering a file format graphify handles, josu checks whether the package is present and points the user at installing it themselves if not — the same posture as gortex, applied consistently, not a separate exception.
- **Routing between engines is a simple file-extension check, not content sniffing.** Recognized Excel/Word/Google-Workspace extensions route to graphify; everything else routes to gortex. A more sophisticated, content-aware routing scheme is explicitly deferred (see Scope Boundaries) — noted as worth revisiting later, not resolved now.
- **The old `GraphifyEngine` implementation is a restore, not a rebuild.** It's fully recoverable from git history (`git show cc1df8d^:src/josu/graph/build.py`, ~152 lines) — it only ever did AST/structural code extraction, the same job gortex now does. Its non-code role (Excel/Word/Google-Workspace ingestion) is new integration work, not something to resurrect as-is.

## Requirements

**Gortex integration correctness**

- R1. `josu daemon start` succeeds and stays functional whether or not a context-engine target is configured or reachable — no engine is a degraded-mode condition, not a startup failure.
- R2. Josu's daemon connects to a configured context-engine target via gortex's real daemon-tracked mode; it never installs, spawns, or manages that target's process lifecycle itself.
- R3. A version-compatibility check runs against whatever context engine is connected; an incompatible or absent engine fails or degrades with a clear, actionable josu-owned message, not the engine's raw CLI usage dump.

**Reindex ownership**

- R4. Gortex's own continuous watcher is the sole reindex trigger once a target is configured and tracking the repo; josu's own commit-hook and merge-triggered reindex calls are removed.
- R5. The post-commit hook's proactive-check behavior is unchanged — only its reindex call is removed, not the hook itself.

**Context-engine configuration**

- R6. One or more context-engine connection targets are declared in josu's config, following the same declarative, non-installing pattern as `[[orchestrator.adapters]]` and delegate candidates' `api_key_env`; gortex is the default/reference target, not the only option the schema supports.
- R7. When no context-engine target is configured or reachable, josu falls back to its existing direct-file-exploration degraded mode (`_fallback_file_context()`) — for both the hosted agent's and the delegate model's lookups.

**Graphify (secondary engine)**

- R8. Graphify is added as a second context engine, scoped to Excel, Word, and Google Workspace file formats (`.xlsx`, `.docx`, `.gdoc`, `.gsheet`, `.gslides`) specifically.
- R9. Graphify's presence is checked lazily, only on first encountering a file format it's needed for; if not installed, josu instructs the user to install it themselves rather than installing it automatically.
- R10. Routing between the configured graph engine and graphify is a file-extension check: recognized Excel/Word/Google-Workspace extensions route to graphify, everything else routes to the configured engine (gortex by default).

## Scope Boundaries

- Multi-engine selection UI (letting users choose among several graph/context engines beyond gortex/graphify at runtime) — a real future idea, explicitly not scoped now.
- Content-based routing beyond file-extension matching — noted as worth thinking through more later, not resolved in this brainstorm.
- Slack, ticket systems, PR-discussion threads, and other external non-file data sources — confirmed uncovered by both gortex and graphify. A real, acknowledged gap, not silently dropped from the original "unstructured data" framing.
- Letting the hosted agent manage the context engine directly via its own MCP config — considered (see Key Decisions) and rejected: breaks the narrow-surface guarantee and doesn't cover the delegate side.
- Automating a persistent, OS-level context-engine service (e.g. a launchd/systemd unit) — out of scope; josu connects to whatever's reachable, it doesn't set up long-lived infrastructure.

## Dependencies / Assumptions

- **Dependency:** graphify's `[office]`/`[google]` extras actually working as PyPI's description states — confirmed only via the package's own description page, not by running the code. Real verification is planning-level work.
- **Dependency:** this whole effort still rests on gortex remaining a going concern josu can integrate against as the default target — pluggability (Key Decisions) reduces this risk by not hardcoding to gortex specifically, and the version-compatibility guard makes drift visible and fail clearly, but neither eliminates the underlying dependency risk.
- **Assumption:** users configuring a non-default context-engine target will implement the same `GraphEngine`-equivalent surface gortex does (`search`, the tool names josu's client calls) — no compatibility-testing story for arbitrary third-party engines exists yet.

## Outstanding Questions

**Deferred to Planning**

- The exact shape of the config schema for declaring context-engine connection targets — single target vs. an ordered/prioritized list (mirroring delegate candidate chains), and how a per-target credential (mirroring `api_key_env`) is named.
- Whether `.gortex.yaml`'s `artifacts:` declaration (ADRs, specs, configs) needs to be auto-generated/maintained by josu on the user's behalf, or whether gortex auto-discovers common documentation patterns without explicit per-file declaration.
- The exact protocol/endpoint shape for querying a connected gortex daemon — the "MCP 2026 Streamable HTTP transport" gortex's daemon mode exposes, versus the ad-hoc `/v1/tools/{name}` REST-style calls josu's current `GortexEngine` makes.
- How strongly josu should verify a configured target is authentic before trusting it — with no install-time trust establishment at all, this is a pure "point at a URL" trust model; whether that needs strengthening is a planning-level judgment call.

## Sources / Research

- `src/josu/graph/gortex_process.py:161-170` — the hardcoded, broken spawn invocation (`--http-addr`, `--index`, `--no-daemon`).
- `src/josu/graph/gortex_process.py:88-136` — `check_gortex_reachable()`'s `/healthz` dependency, confirmed unresponsive against the real binary.
- `src/josu/graph/gortex.py:108-138` — `GortexEngine._call_tool()`'s `/v1/tools/{name}` REST-style calls, the narrow slice of gortex's capability josu currently uses.
- `src/josu/daemon.py:64-90` — the daemon's current probe-then-spawn gortex lifecycle sequence.
- `src/josu/proactive/watchers.py:592-656` — the fused reindex-then-proactive-check post-commit hook.
- `tests/graph/test_gortex_process.py:1-6`, `tests/graph/test_gortex.py:1-5` — confirmed both test against fake HTTP servers, giving zero signal on real-gortex compatibility.
- Live `gortex --help`, `gortex mcp --help`, `gortex daemon --help`, `gortex daemon start --help`, `gortex track --help`, `gortex config --help`, `gortex guide`, `gortex guide capabilities`, `gortex guide resources` — run directly against the real, installed gortex v0.62.0 binary this session.
- [github.com/zzet/gortex/docs/cli.md](https://github.com/zzet/gortex/blob/main/docs/cli.md), [docs/multi-repo.md](https://github.com/zzet/gortex/blob/main/docs/multi-repo.md) — gortex's own documentation, fetched and cross-checked against live CLI behavior (found some inconsistency between the docs and observed behavior — treat both as partial evidence, not a single source of truth).
- [pypi.org/project/graphifyy](https://pypi.org/project/graphifyy/) — graphify's documented capabilities (AST code extraction, LLM-based semantic extraction, `[office]`/`[google]` extras), fetched this session.
- `git show cc1df8d^:src/josu/graph/build.py` — the prior `GraphifyEngine` implementation, confirmed recoverable from git history.
- `git log -1 cc1df8d` — the original graphify-to-gortex migration commit message and rationale.
- `docs/brainstorms/2026-08-05-cli-ease-of-use-requirements.md` — where this gortex gap was first found and deliberately split into its own track.
- `README.md`'s own positioning ("both sides share one context graph... queried through a fixed two-tool MCP surface so the schema footprint never grows with the graph") and Prerequisites section (the hosted CLI agent and gortex are both listed as user-installed, not josu-installed) — the internal precedent this revision's client-only posture extends rather than invents.
- `src/josu/orchestrator/adapter.py` / the `[[orchestrator.adapters]]` config pattern, and delegate candidates' `api_key_env` field — existing declarative, non-installing config shapes a context-engine target declaration should mirror.
- `src/josu/orchestrator/mcp_manifest.py` — confirms Claude Code today connects only to josu's own `context-graph`/`delegate-to-local` MCP servers, never to gortex directly; the reason letting the hosted agent manage gortex itself was rejected (see Scope Boundaries).
- External research on how comparable tools integrate external context/indexing engines: Claude Code's own MCP lifecycle model (client owns spawn/auth via `.mcp.json`, https://code.claude.com/docs/en/mcp-quickstart), Continue.dev's pluggable `ContextProvider` interface (interface-only, never installs a backend, https://docs.continue.dev/customize/deep-dives/custom-providers), Aider's retreat from an external `ctags` binary to a bundled dependency (https://aider.chat/2023/10/22/repomap.html), VS Code's build-time-vendored, hash-pinned `ripgrep` (https://github.com/microsoft/vscode-ripgrep), and LSP's protocol-first stance against vendor lock-in (https://langserver.org/).
