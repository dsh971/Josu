---
title: Gortex Integration Rework
type: feat
date: 2026-08-07
origin: docs/brainstorms/2026-08-07-gortex-integration-rework-requirements.md
---

# Gortex Integration Rework

## Summary

Fix josu's broken gortex integration by making the graph engine a config-declared connection target, not a process josu installs, spawns, or authenticates itself. A new `[[graph.engines]]` config section — mirroring josu's existing `[[orchestrator.adapters]]` and `api_key_env` patterns — points josu's daemon at a reachable engine; josu's daemon acts as a pure client everywhere, degrading to its existing file-exploration fallback when nothing is configured or reachable. Gortex is the default/reference target; graphify is added as a narrow, lazily-checked complement for Excel/Word/Google-Workspace formats, installed by the user rather than by josu.

## Problem Frame

(carried from origin: docs/brainstorms/2026-08-07-gortex-integration-rework-requirements.md, with this session's course-correction folded in)

`josu daemon start` — and therefore `josu run`, `josu delegate`, and the post-commit proactive-check hook — are fully blocked today. `gortex_process.py:161-170` hardcodes `gortex mcp --index <path> --no-daemon --server --http-addr <host:port>`, an invocation whose `--http-addr` flag does not exist on `gortex mcp`, and whose `--index`/`--no-daemon` flags are now no-ops. No version-compatibility check exists anywhere, and both existing gortex test files test exclusively against fake HTTP servers, so they gave zero signal while the real integration was completely broken.

**Live research against the real gortex v0.62.0+99d745c binary (an earlier planning pass this session) resolved the mechanical facts, which still hold:** `gortex daemon start --http-addr <host:port> --tools facade-v1 [--http-auth-token <token>]` is a real, working invocation whose `/healthz` responds `{"status":"ok","transport":"streamable-http","spec":"mcp-2026-03-26"}`; `gortex track <path> --wait` synchronously indexes a repo; both the MCP Streamable HTTP transport (`/mcp`) and the REST-ish `/v1/tools/{name}` route `GortexEngine._call_tool()` already uses coexist on the same address, so **no client-layer protocol rewrite is needed** — but `index_repository`/`reindex_repository` return HTTP 404 against the real, current tool surface (indexing/reindexing is owned by `gortex track`/the daemon's watcher, not a callable tool), and `search` is gated behind a `--tools` preset (`core` by default, `facade-v1` unblocks it).

**A follow-up review of the resulting plan (this session) found the ownership model itself didn't fit josu.** README.md's own positioning — "both sides share one context graph... queried through a fixed two-tool MCP surface so the schema footprint never grows" — is a thin-client framing; a plan where josu installs gortex via a consent-gated `curl | sh` script, spawns and owns its daemon process, and generates its own bearer tokens is materially heavier ownership than that framing describes, and hardcodes the integration to one vendor. External research (Claude Code's own MCP lifecycle model, Continue.dev's pluggable context providers, Aider's retreat from an owned `ctags` binary, LSP's protocol-first stance — see origin doc Sources) plus one internal check (`src/josu/orchestrator/mcp_manifest.py` confirms Claude Code today only ever sees josu's own narrow `context-graph`/`delegate-to-local` MCP surface, never gortex directly) converged on a different shape: josu declares a connection target and connects, the same way it already treats the hosted CLI agent and delegate credentials — never installs, spawns, or authenticates the engine itself. This plan implements that corrected shape, not the original spawn-owning one.

## Key Technical Decisions

- **The config section is named `graph.engines`, not `context.engines`.** The codebase's own vocabulary is already "graph" (`GraphEngine` Protocol, the `graph/` package, the `context-graph` MCP server name) — matching it keeps one vocabulary instead of introducing a second term the brainstorm dialogue used colloquially.
- **`[[graph.engines]]` is a flat list, not a priority-ordered fallback chain.** The codebase has exactly that machinery available (`[[delegation.chains]]`: schema/resolution/execution split across three modules, ordered-list resolution, typed exhaustion error) — deliberately not reused here. Nothing in scope asks for multiple simultaneous alternative engines racing for the same role; v1 expects zero or one active entries. Additional entries beyond the first validate individually (consistent with every other config section's degrade-on-bad-entry behavior) but only the first is used as the active target, with a warning noting the rest are ignored — documented behavior, not silent data loss. Real multi-engine selection stays a named Scope Boundary.
- **Non-loopback graph-engine targets require an explicit acknowledgment that credentials travel in cleartext.** Doc-review's security-lens persona flagged (confidence 75) that removing josu's own spawn-owned, always-loopback gortex process in favor of a user-declared `host`/`port` target — combined with U4's bearer-token support — opens a real gap: `check_gortex_reachable()` and `GortexEngine`'s `base_url` both hardcode `http://` with no HTTPS path, so a target pointed at a non-loopback host (a "shared team gortex," which this plan's own pluggability decision makes newly plausible) would send the bearer token and all query content over the network unencrypted. V1 does not add TLS support. Instead: `load_graph_engines_config()` (U1) warns loudly — the same `config.warnings` mechanism already used for other risky-but-not-blocking conditions — whenever a configured target's host is not a loopback address (`127.0.0.1`/`localhost`) AND `api_key_env` is set, naming the cleartext-credential risk explicitly. This doesn't block the configuration (some users may have their own network-level protection), but it stops the risk from being silent.
- **The connection-target schema mirrors `OrchestratorAdapterConfig`; the optional credential field mirrors `api_key_env`.** `GraphEngineTargetConfig` gets independent per-entry validation and degrade-on-bad-entry loading (matching `orchestrator.py`'s and `delegate.py`'s loaders exactly), and an optional `api_key_env: str | None = None` field resolved lazily at connect time via `os.environ.get(...)` — never a value read/stored/logged at config-load time, matching `delegate/chain.py:141-152`'s `_default_client_factory()` pattern. `gortex_process.py`'s own existing docstring already names this exact pattern ("R31") as the convention gortex intentionally didn't get before; this plan gives it that convention.
- **josu never installs, spawns, or authenticates a graph-engine process.** `spawn_gortex()`/`terminate_gortex()` and the whole probe-then-spawn/owns-process lifecycle in `daemon.py` are removed, not patched. The daemon reads the configured target (if any), checks reachability, and either connects or proceeds with no engine — the same posture josu already has toward the hosted CLI agent itself (a documented prerequisite the user installs, never something josu installs).
- **Getting `--tools facade-v1` (and any auth token) onto the target daemon is now the user's own setup step, not josu's.** Since josu never spawns gortex, it can no longer pass that flag itself. `docs/USAGE.md` documents the exact invocation the user runs themselves; the version-and-capability guard (below) is josu's only remaining lever — it can *detect* a misconfigured target and report exactly what to fix, but can't fix it for them.
- **Version, capability, and repo-tracking checks all query the configured target's own HTTP/tool surface — never a local `gortex` CLI shell-out.** An earlier draft's guard ran `gortex version` as a local subprocess; doc-review's adversarial persona correctly flagged this as inconsistent with the plan's own premise — a configured target may not be on the same machine as josu's daemon, and even when it is, the `gortex` binary isn't guaranteed to be on *josu's* `PATH` just because the daemon it's talking to is running somewhere. All three checks (version, tool-surface capability, and repo-identity — see below) go through the target's own MCP/HTTP tool surface (e.g. a `capabilities`-style tool call, exact shape to verify live during implementation), never a local process spawn.
- **The compatibility guard also verifies the connected target is tracking *this* repo, not just that it's reachable and capable.** Doc-review's adversarial persona flagged (confidence 75) the worst failure mode this plan's three-layer guard didn't originally cover: a reachable, version-compatible, correctly-preset target that happens to be tracking a *different* repo (plausible given gortex's own multi-repo support) degrades silently to confidently wrong context — results from the wrong repo, or an empty index that reads as "no matches" rather than "not tracking this repo" — which is worse than R7's clean "no engine" fallback, and none of the reachability/version/tool-surface checks can detect it. If the target's tool surface can report its tracked repos (verify during U3's implementation), cross-check the daemon's own target root against that list before treating the connection as usable. If no such capability exists on the current tool surface, this is named explicitly as a residual risk (Dependencies/Risks) rather than silently unaddressed.
- **A target absent at `daemon start` is retried lazily on next use, not treated as permanently absent for the daemon's lifetime.** Doc-review's adversarial persona noted `daemon.py` is a long-running process, and this plan explicitly decouples "start gortex" from "start josu's daemon" as separate, user-driven steps — nothing enforces gortex-before-josu. A single startup-time check would wrongly treat a target that comes online a minute later as unusable for the rest of the session. U2/U3's connect-and-check logic re-runs on next engine use after a prior "no engine" outcome, rather than caching that result forever. Detecting a *working* target regressing mid-session (e.g., restarted without `--tools facade-v1`) is not solved by this — that gap is named explicitly in Dependencies/Risks rather than built as live health-monitoring, which is disproportionate scope for this fix.
- **`GortexEngine.build()` and `update()` become documented no-ops.** `index_repository`/`reindex_repository` don't exist in the current tool surface, and since josu no longer runs `gortex track` itself either (that's now the user's own setup step, alongside starting the daemon), there is nothing left for either method to do — tracking and reindexing are entirely the user's gortex's own business now.
- **Gortex's watcher is the sole reindex trigger once a target is configured and tracking; josu retires its own.** Unaffected by the ownership-model change — still true that running josu's own commit/merge-triggered reindex calls alongside a continuously-watching gortex would double-trigger. If anything, more clearly correct now: josu never even told gortex what to track, so it has no basis to also drive reindexing.
- **The post-commit hook's reindex call and its proactive-check call are separable — only the reindex call is retired.** `install_commit_hook()`'s hook currently calls `_run_reindex_on_commit()` then runs the proactive check, in one script invocation (`watchers.py:638-656`). Retiring josu's reindex trigger means removing that one call, not the hook.
- **Graphify's install step is a pure instruction, not a josu-run subprocess.** On first encountering an eligible file with the package absent, josu prints the exact install command and degrades that call — it never shells out to `pip install` itself. This replaces an earlier design (isolated-subprocess install) with something simpler: since josu takes no installing action at all, there's no subprocess-isolation concern to design around.
- **Graphify is scoped narrowly to Excel/Word/Google-Workspace formats, not general "unstructured data."** Gortex already covers PDFs, images, and generic docs/artifacts. Graphify's non-redundant value is `.xlsx`/`.docx`/`.gdoc`/`.gsheet`/`.gslides`, which gortex doesn't ingest.
- **Routing between engines is a simple file-extension check, not content sniffing.** Recognized Excel/Word/Google-Workspace extensions route to graphify; everything else routes to the configured graph engine. More sophisticated routing is explicitly deferred (Scope Boundaries).
- **`GraphifyEngine` has no dependency on the routing layer; `RoutingEngine` depends on `GraphifyEngine`, not the reverse.** `GraphifyEngine` builds and tests entirely against the standalone `GraphEngine` Protocol; `RoutingEngine` is the consumer that needs a concrete instance to wire in.
- **U5's (daemon-side degrade) engine slot is always a `RoutingEngine`, never a bare `None`.** `graph/server.py`'s `build_server(engine: GraphEngine, ...)` and `graph/internal_api.py`'s `build_graph_internal_route(*, engine: GraphEngine, ...)` both require a non-optional `GraphEngine` and call its methods directly with no `None`-check — a bare `None` on "no engine configured" would crash the graph-MCP surface instead of degrading. `RoutingEngine` is what turns "no target configured/reachable" into the `GraphEngineUnavailableError` those call sites already handle.
- **"No engine configured or reachable" scopes to the primary graph engine specifically, not graphify.** Graphify's own lazy-install-and-instruct flow is independent — a user with no `[[graph.engines]]` configured at all can still get graphify-backed context for eligible files once they install it.
- **Routing (R10) applies to `execute()`'s `path`-bearing calls and to `build`/`update`, not to `search()`.** Doc-review's feasibility persona found (confidence 100) that the two agent/delegate-facing tools — `search(query, limit)` and `execute(operation, params)` — are the *only* calls that actually reach `RoutingEngine`, and `search()` carries no file path at all to extension-check against; treating R10 as applying to `search()` left graphify's content structurally unreachable through the one tool most likely to be called. Resolution: `search()` always routes to the primary engine (free-text search has no path to route on, and graphify's ingested content was never part of a unified free-text index). `execute()` inspects `params.get("path")` when the caller supplies one — a graphify-eligible extension there routes to graphify, everything else (including no `path` key at all) routes to the primary engine. `build`/`update` keep routing by their existing explicit `root`/`changed_files` arguments. This makes `execute()` the actual mechanism a caller that already knows it's looking at a specific spreadsheet/document uses to reach graphify's content.
- **The internal `/graph/internal/reindex` route is retired alongside U5's other dead-code removal, not left inert.** Once U4 makes `update()` a no-op and U5 removes `reindex_on_commit()`/`reindex_on_merge()` (the route's only production callers besides the already-dead `reindex_on_save()`), the route would accept POSTs and silently do nothing — an authenticated but functionally inert endpoint. Consistent with how this plan already retires genuinely dead code elsewhere, U5 removes it rather than leaving it as vestigial infrastructure.

## Requirements

(carried from origin: docs/brainstorms/2026-08-07-gortex-integration-rework-requirements.md)

**Gortex integration correctness**

- R1. `josu daemon start` succeeds and stays functional whether or not a graph-engine target is configured or reachable — no engine is a degraded-mode condition, not a startup failure.
- R2. Josu's daemon connects to a configured graph-engine target via gortex's real daemon-tracked mode; it never installs, spawns, or manages that target's process lifecycle itself.
- R3. A version-compatibility check runs against whatever graph engine is connected; an incompatible or absent engine fails or degrades with a clear, actionable josu-owned message, not the engine's raw CLI usage dump.

**Reindex ownership**

- R4. Gortex's own continuous watcher is the sole reindex trigger once a target is configured and tracking the repo; josu's own commit-hook and merge-triggered reindex calls are removed.
- R5. The post-commit hook's proactive-check behavior is unchanged — only its reindex call is removed, not the hook itself.

**Graph-engine configuration**

- R6. One or more graph-engine connection targets are declared in josu's config, following the same declarative, non-installing pattern as `[[orchestrator.adapters]]` and delegate candidates' `api_key_env`; gortex is the default/reference target, not the only option the schema supports.
- R7. When no graph-engine target is configured or reachable, josu falls back to its existing direct-file-exploration degraded mode (`_fallback_file_context()`) — for both the hosted agent's and the delegate model's lookups.

**Graphify (secondary engine)**

- R8. Graphify is added as a second context engine, scoped to Excel, Word, and Google Workspace file formats (`.xlsx`, `.docx`, `.gdoc`, `.gsheet`, `.gslides`) specifically.
- R9. Graphify's presence is checked lazily, only on first encountering a file format it's needed for; if not installed, josu instructs the user to install it themselves rather than installing it automatically.
- R10. Routing between gortex and graphify is a file-extension check: recognized Excel/Word/Google-Workspace extensions route to graphify, everything else routes to the configured graph engine.

## Implementation Units

### U1. Config schema for graph-engine connection targets

**Goal:** Add a declarative `[[graph.engines]]` config section for connection targets, mirroring `[[orchestrator.adapters]]`'s per-entry validation and delegate candidates' `api_key_env` lazy-credential pattern.

**Requirements:** R6

**Dependencies:** none

**Files:**
- `src/josu/config/graph_engines.py` (new)
- `src/josu/config/__init__.py` (modify — wire into `JosuConfig`/`load_config()`)
- `tests/config/test_graph_engines.py` (new)

**Approach:**
- New `GraphEngineTargetConfig` pydantic `BaseModel`, mirroring `OrchestratorAdapterConfig`'s shape (`orchestrator.py:87-128`): required `name: str`, required connection fields (`host: str`, `port: int` — the exact field shape, e.g. whether to instead accept a single `url` string, is an implementation-time call; see Deferred to Implementation), optional `api_key_env: str | None = None` (mirroring `delegate.py:32-39` exactly, field-for-field).
- `GraphEnginesConfig` container: `engines: list[GraphEngineTargetConfig] = []` (mirroring `OrchestratorConfig`, `orchestrator.py:131-136`).
- `load_graph_engines_config(data: dict) -> tuple[GraphEnginesConfig, list[str]]` following the exact per-entry independent-validate/degrade-on-bad-entry shape all three existing loaders (`delegate.py`, `orchestrator.py`, `chains.py`) already use, rendering validation failures via `safe_validation_error_detail()` (`config/_validation.py`).
- Existence-only check for `api_key_env` at load time (warn if the named env var isn't set; never read/log its value) — mirroring `delegate.py:84-89` line-for-line.
- Wire into `config/__init__.py`: new `graph_engines: GraphEnginesConfig = field(default_factory=GraphEnginesConfig)` field on `JosuConfig` (`__init__.py:100-109`), a `load_graph_engines_config(data)` call added alongside the three existing section-loader calls in `load_config()` (`__init__.py:185-192`), and `graph_engines=graph_engines_config` added to the final `JosuConfig(...)` construction (`__init__.py:197-204`).
- If more than one `[[graph.engines]]` entry is present, only the first validated entry is used as the active target; a warning names the ignored extras (see Key Technical Decisions — deliberately not a fallback chain).

**Patterns to follow:** `orchestrator.py:87-128` (`OrchestratorAdapterConfig` shape), `delegate.py:32-39` + `:84-89` (`api_key_env` field + existence-check), all three existing loaders' per-entry degrade-on-bad-entry shape.

**Test scenarios:**
- Valid single entry parses with all fields populated correctly.
- An entry missing a required field is dropped with a warning; the rest of config load proceeds unaffected.
- `api_key_env` referencing an unset env var: warns, doesn't block load.
- `api_key_env` omitted: no warning, field is `None`.
- Multiple valid entries: only the first becomes the active target; a warning names the ignored extras.
- No `[[graph.engines]]` section at all: `GraphEnginesConfig.engines == []`, no warning — this is the valid, expected "no engine configured" state (R7).
- A non-loopback `host` with `api_key_env` set: warns naming the cleartext-credential risk. A non-loopback `host` with no `api_key_env`: no warning (no credential at risk). A loopback `host` with `api_key_env` set: no warning.

**Verification:** A `josu.toml` with a `[[graph.engines]]` entry loads cleanly; malformed entries or missing env vars surface through `config.warnings` the same way the delegate and orchestrator sections already do.

---

### U2. Make the daemon's graph-engine connection a pure client

**Goal:** Remove all install/spawn/process-ownership code from josu's daemon startup path; the daemon reads the configured graph-engine target (if any) and connects to it as a client, or proceeds with no engine configured.

**Requirements:** R1, R2

**Dependencies:** U1

**Files:**
- `src/josu/graph/gortex_process.py` (modify — remove `spawn_gortex()`/`terminate_gortex()`, simplify `check_gortex_reachable()` and `GortexProcess`)
- `src/josu/daemon.py` (modify — replace probe-then-spawn with connect-or-degrade)
- `tests/graph/test_gortex_process.py` (modify)
- `tests/test_daemon.py` (modify)

**Approach:**
- Remove `spawn_gortex()` (`gortex_process.py:139-221`) and `terminate_gortex()` (`gortex_process.py:224-238`) entirely — there is nothing left to spawn or terminate.
- Keep `check_gortex_reachable()` (`gortex_process.py:88-136`) as the liveness probe, but it now checks whatever host/port comes from the *configured* target (U1), not a daemon-owned `DEFAULT_GORTEX_HOST`/`DEFAULT_GORTEX_PORT` pair tied to a spawn contract. Update the module's docstring (`gortex_process.py:1-25`) — its current "standalone/embedded mode... deliberately NOT gortex's daemon-tracked mode" framing inverts: the module's whole premise becomes "connect to gortex's daemon-tracked mode, never spawn it."
- `GortexProcess`'s `popen`/ownership-tracking fields (`gortex_process.py:60-73`) become unnecessary — replace with a simpler value carrying just the resolved connection info (host/port/base_url), since there's no subprocess handle to carry anymore.
- Replace `daemon.py`'s `_ensure_gortex_running()` (`:64-74`) with a new function that reads the configured target from `GraphEnginesConfig` (U1) — if none configured, returns "no engine"; if configured, calls `check_gortex_reachable()` and returns either a connected engine or "no engine" (unreachable degrades the same as unconfigured, never blocks).
- `create_app()` (`daemon.py:77-...`) no longer wraps engine construction in a `try/except: terminate_gortex(); raise` block — there's nothing to terminate on a later construction failure. The connect-or-none result this unit produces (a `GortexEngine` instance or `None`) stays a local value in `create_app()` for now; **U6 is the sole unit that edits `daemon.py:154`'s construction call**, swapping it from raw `GortexEngine(...)` to `RoutingEngine(...)` wrapping this unit's result — this unit does not reference `RoutingEngine` at all, since it doesn't exist yet at this point in the dependency order.
- `lifespan()`'s shutdown hook (`daemon.py:212-222`, `if owns_gortex_process: terminate_gortex(...)`) is removed — there's no owned process to terminate on shutdown.

**Patterns to follow:** N/A — this unit is primarily deletion and simplification of the existing spawn/ownership code; no new pattern is being introduced.

**Test scenarios:**
- Configured, reachable target: daemon constructs a working engine.
- Configured, unreachable target: daemon starts successfully anyway; engine construction degrades to "no engine."
- No target configured at all: daemon starts with no engine, same degraded path.
- Existing tests referencing `_ensure_gortex_running()`'s survivor-reuse behavior are removed or rewritten — "reuse" was only ever a special case of spawn-avoidance, which no longer exists as a concept; a reachable configured target is just the normal connect path now.
- Spawn-failure tests (missing binary) are removed — there is no spawn path left to fail.

**Verification:** `josu daemon start` succeeds immediately whether or not any `[[graph.engines]]` entry is configured, and whether or not a configured target is currently reachable. Process inspection confirms josu never launches a `gortex` subprocess under any `daemon start` condition.

---

### U3. Graph-engine version-and-capability compatibility guard

**Goal:** If a graph-engine target is configured and reachable, check its version against a known-compatible range and confirm the required tool surface actually responds; degrade to "no engine" with a clear, actionable message — naming the exact fix for the user's own gortex invocation — when either check fails.

**Requirements:** R3

**Dependencies:** U1, U2

**Files:**
- `src/josu/graph/gortex_process.py` (modify — add version/capability-check functions)
- `src/josu/daemon.py` (modify — call the checks after a successful connect)
- `tests/graph/test_gortex_process.py` (modify)

**Approach:**
- Add a function that determines the connected target's version **via its own HTTP/tool surface** (e.g. a `capabilities`-style tool call — exact shape to verify live during implementation; gortex's real tool list includes a `capabilities` entry), never by shelling out to a local `gortex version` CLI command — the configured target may not be on the same machine as josu's daemon, and josu's own `PATH` has no guaranteed relationship to it. Parse the reported semver, checked against a `MIN_COMPATIBLE`..`MAX_KNOWN` range — below the floor fails, above the ceiling warns and proceeds (gortex's CLI surface moves fast; a bare-pin check would break on the next compatible release).
- Also probe the tool surface directly (e.g. `tools/list`, or a harmless `search` call) using whatever `api_key_env` token is configured. On a `tool_blocked_by_mode`-shaped failure, the resulting message is explicit and user-actionable: since josu can no longer fix this by re-spawning with the right flag, the message must name the exact fix for the user to apply to their own gortex invocation (e.g. "restart gortex with `--tools facade-v1`").
- **Also verify the target is tracking this daemon's own repo, if the tool surface supports asking.** Cross-check the daemon's own target root path against whatever tracked-repo listing the connected target's tool surface can report (verify during implementation whether the current tool surface — `capabilities`, or another tool — exposes this at all). A reachable, version-compatible target tracking a *different* repo is a worse failure mode than "unreachable": it degrades silently to confidently wrong context rather than a clean fallback. If no such capability exists on the current tool surface, note that explicitly rather than silently skipping the check (see Dependencies/Risks).
- All checks run after U2's connect step succeeds, before the engine is handed to `RoutingEngine`.
- On any check failing: does **not** block `josu daemon start` (R1) — the target is treated as unusable for this session, the daemon proceeds with no engine for that slot, and the clear guidance is printed via the existing `config.warnings`/`print(f"josu daemon: warning: ...")` convention this session already established for non-fatal startup conditions.
- **A target that fails these checks (or was simply absent) at `daemon start` is re-checked lazily on next engine use, not cached as permanently unusable** — `daemon.py` is a long-running process, and this plan explicitly decouples starting gortex from starting josu's daemon as separate, user-driven steps.

**Patterns to follow:** `gortex_process.py:55-57`'s exception style; the existing `config.warnings` print convention for non-fatal, user-actionable startup conditions.

**Test scenarios:**
- Compatible version + correct tool preset: passes silently, engine used normally.
- Incompatible version: engine unused, clear warning names the version mismatch, daemon still starts.
- Capability probe returns a `tool_blocked_by_mode`-shaped failure: engine unused, clear warning names the exact `--tools facade-v1` fix, daemon still starts.
- Unparseable version output: treated as incompatible, same degrade-and-warn behavior, not a crash on the parse step.
- Version above the verified ceiling: warns, does not degrade — confirms a working newer gortex isn't blocked outright.
- Target tracking a different repo than the daemon's own root (when the tool surface can report this): degrades to "no engine" with a message naming the mismatch, not silently returning wrong-repo results.
- Target absent at daemon startup, then reachable on a later call: the later call succeeds without requiring a daemon restart.

**Verification:** Configuring josu to point at a real gortex daemon started with the default `--tools core` preset: `josu daemon start` succeeds, prints a warning naming the exact fix, and graph-engine-backed lookups degrade to file exploration until the user restarts their gortex correctly.

---

### U4. Fix `GortexEngine`'s tool-call layer

**Goal:** Remove calls to the now-nonexistent `index_repository`/`reindex_repository` tools; make `build()`/`update()` documented no-ops; send a user-configured bearer token (if any) on every request, resolved lazily the same way delegate candidates resolve `api_key_env`.

**Requirements:** R1, R2

**Dependencies:** U1, U2

**Files:**
- `src/josu/graph/gortex.py` (modify)
- `tests/graph/test_gortex.py` (modify)

**Approach:**
- `build()` (`gortex.py:189-191`) and `update()` (`gortex.py:193-201`) become no-ops: `index_repository`/`reindex_repository` return HTTP 404 against the real tool surface, and since josu no longer runs `gortex track` itself (that's now the user's own setup step, alongside starting the daemon), there is nothing left for either method to do — document why in each docstring.
- `search()` (`gortex.py:203-214`) and `execute()` (`gortex.py:216-220`) keep their existing dispatch logic unchanged.
- `GortexEngine.__init__()` (`gortex.py:97-106`) gains an *optional* `auth_token: str | None = None` parameter (not required — a user's gortex may not require auth at all). When present, sent as `Authorization: Bearer <token>` on every `_call_tool()` request; when absent, no auth header is sent. The token itself is resolved by U2's connect step from the configured target's `api_key_env` (if set) via `os.environ.get(...)`, lazily — mirroring `delegate/chain.py:141-152`'s `_default_client_factory()` pattern exactly, not a josu-generated credential.

**Patterns to follow:** `gortex.py:108-187`'s `_call_tool()` (unchanged); `delegate/chain.py:141-152`'s lazy `api_key_env`-to-token resolution.

**Test scenarios:**
- `build()`/`update()` make zero HTTP calls and return without raising.
- `search()`/`execute()` against the real response shape (`query_class`, `results[]`, `total`, `truncated`).
- Configured `api_key_env` present and set: requests carry the bearer header.
- `api_key_env` absent or unset: requests carry no `Authorization` header and still proceed — mirrors an unauthenticated-localhost gortex working fine with no token configured.
- Existing timeout/5xx/oversized/warming/null-results/unsafe-operation-name test cases continue to pass unmodified.

**Verification:** A user-run gortex with `--http-auth-token` set, and josu's config pointing `api_key_env` at the matching variable, authenticates successfully; a user-run gortex with no auth configured on either side also works, unauthenticated, with no josu-side token generation anywhere.

---

### U5. Retire josu's own reindex triggers

**Goal:** Remove josu's commit-hook-triggered and merge-triggered reindex calls now that a configured gortex's own watcher is the sole reindex mechanism, while leaving the post-commit hook's proactive-check behavior untouched.

**Requirements:** R4, R5

**Dependencies:** U2, U4

**Files:**
- `src/josu/proactive/watchers.py` (modify)
- `src/josu/orchestrator/run.py` (modify)
- `src/josu/graph/index.py` (modify — remove now-dead trigger functions)
- `src/josu/cli.py` (modify — `reindex_result` display)
- `src/josu/observability/runlog.py` (modify — `reindexed_files`/`pruned_files` fields)
- `src/josu/graph/internal_api.py` (modify — remove the now-dead internal reindex route)
- `src/josu/daemon.py` (modify — remove the internal reindex route's wiring)
- `tests/proactive/test_watchers.py` (modify, if exists)
- `tests/orchestrator/test_run.py` (modify)
- `tests/graph/test_index.py` (modify — remove commit/merge-trigger tests, keep `reindex_on_save` coverage)

**Approach:**
- In `watchers.py`, remove the `_run_reindex_on_commit(base_url, token)` call (`watchers.py:656`) and the function itself (`watchers.py:592-636`), plus its now-unused import. Leave the rest of `_run_commit_hook_from_cli()` unchanged — the proactive-check dispatch stays exactly as-is.
- In `orchestrator/run.py`, remove the merge-triggered reindex call (`run.py:377`) and its `try/except ReindexError` wrapper (`run.py:375-389`). `RunTaskResult.reindex_result` (`run.py:227`, `:152`, populated at `:410`, read at `:430-431` into the run record) is removed entirely rather than left as a permanently-empty field — `cli.py:464-465`'s display and `observability/runlog.py`'s corresponding `RunRecord.reindexed_files`/`.pruned_files` fields (shown in `josu log`) are removed alongside it, since a configured gortex's own watcher now owns reindexing invisibly to josu and there is nothing meaningful left to report per-run.
- In `graph/index.py`, remove the now-fully-dead `reindex_on_commit()` (`index.py:226-248`) and `reindex_on_merge()` (`index.py:291-315`) functions. Leave `reindex_on_save()` (`index.py:254-268`) in place — it already has zero production callers, a separate pre-existing gap out of scope here.
- **`tests/graph/test_index.py` imports and directly exercises both removed functions** (`from josu.graph.index import reindex_on_commit, reindex_on_merge, reindex_on_save`, with roughly eight test functions built around commit/merge reindex scoping and pruning) — left off an earlier draft's Files list, which would have left this file with a module-level `ImportError` the moment U5 landed. Remove or rewrite the commit- and merge-trigger tests and their supporting fixtures, keeping only `reindex_on_save`-related coverage.
- **`graph/internal_api.py`'s `/graph/internal/reindex` route (`build_graph_internal_route`, wired into `daemon.py`'s routes) loses its only production callers once U4 and the above land** — it currently calls `engine.update(...)` on every POST, which is now a no-op, making the authenticated route functionally inert rather than genuinely retired. Remove the route and its wiring in `daemon.py`, consistent with how this plan retires other dead code rather than leaving vestigial infrastructure. Verify no other caller depends on this route before removing.
- Check whether `ReindexError`/`ReindexResult` types are still referenced elsewhere before removing them.

**Patterns to follow:** Existing `_run_commit_hook_from_cli()` structure for how much of the function survives untouched.

**Test scenarios:**
- Post-commit hook still runs the proactive-check dispatch but no longer calls into reindex-on-commit.
- `run_task()`'s Step 6 completes without the merge-reindex call; test `RunTaskResult` construction sites updated to drop the removed field.
- `reindex_on_save()` remains present, its own tests unaffected.
- `josu log`'s output no longer references reindexed/pruned file counts.

**Verification:** `git grep` for reindex-on-commit/merge/result/reindexed_files/pruned_files across `src/josu/` returns no hits outside historical comments. `tests/graph/test_index.py` collects and passes with only `reindex_on_save` coverage remaining. A real commit against a repo with the hook installed and a correctly-configured, tracking gortex target shows the proactive-check still firing and no josu-initiated reindex call anywhere. `POST /graph/internal/reindex` no longer exists on the daemon.

---

### U6. Engine-routing composing layer

**Goal:** Add a new `GraphEngine` implementation that composes the configured graph engine and (lazily-constructed) graphify behind the existing Protocol, dispatching each call by file-extension, without changing any of `daemon.py`'s existing call sites.

**Requirements:** R7, R10

**Dependencies:** U7 (needs a concrete `GraphifyEngine` to wire in — see Key Technical Decisions on the corrected dependency direction)

**Files:**
- `src/josu/graph/router.py` (new)
- `tests/graph/test_router.py` (new)
- `src/josu/daemon.py` (modify — construct the router engine instead of `GortexEngine` directly, at `daemon.py:154`)

**Approach:**
- New `RoutingEngine` class implementing `GraphEngine` (`engine.py:24-48`): holds an optional primary engine (unset when U2/U3 find nothing configured or reachable) and an optional, lazily-constructed graphify engine.
- **`search(query, limit)` always routes to the primary engine.** It carries no path argument to extension-check against, and graphify's content was never part of a unified free-text index — see Key Technical Decisions.
- **`execute(operation, params)` routes on `params.get("path")` when the caller supplies one.** A graphify-eligible extension there dispatches to graphify; everything else (including calls with no `path` key) dispatches to the primary engine. This is the actual mechanism a caller that already knows it's looking at a specific spreadsheet/document uses to reach graphify.
- **`build(root)`/`update(root, changed_files)` route by their existing explicit path arguments** against the recognized extension set (`.xlsx`, `.docx`, `.gdoc`, `.gsheet`, `.gslides`).
- With the primary engine unset, route everything not graphify-eligible through to `_fallback_file_context()`'s mechanism (raise the same `GraphEngineUnavailableError` an unreachable engine would raise) rather than silently no-op'ing.

**Patterns to follow:** `engine.py:1-16`'s own architectural note ("nothing outside this module should import gortex directly").

**Test scenarios:**
- `search()` with any query routes to the (fake) primary engine, regardless of what the query text mentions.
- `execute()` with `params["path"]` set to a `.py`/generic path, or with no `path` key at all, routes to the (fake) primary engine.
- `execute()` with `params["path"]` set to a recognized Excel/Word/Google-Workspace extension routes to the (fake) graphify engine.
- `build()`/`update()` with a recognized graphify extension route to graphify; otherwise to the primary engine.
- Primary engine unset + a call that would otherwise route to the primary engine: raises the same unavailable-error shape the fallback expects.
- Primary engine unset + a graphify-eligible call: still routes to graphify — the two consent/availability axes are independent.

**Verification:** With both engines available, editing a tracked `.xlsx` file surfaces graphify-backed context; editing a `.py` file surfaces the primary engine's context; no change to `daemon.py`'s server-construction call sites beyond the single engine-construction line.

---

### U7. `GraphifyEngine` for Excel, Word, and Google-Workspace formats

**Goal:** Implement a new, narrowly-scoped `GraphEngine` around the `graphifyy` package's `[office]`/`[google]` extras, covering exactly the formats R8 names.

**Requirements:** R8

**Dependencies:** none (builds and tests against the standalone `GraphEngine` Protocol directly)

**Files:**
- `src/josu/graph/graphify.py` (new)
- `tests/graph/test_graphify.py` (new)
- `pyproject.toml` (modify — new `graphify` optional-dependency group)

**Approach:**
- Add `[project.optional-dependencies] graphify = ["graphifyy[office,google]>=X,<Y"]` to `pyproject.toml` with an explicit upper bound, following the existing `dev` group's pattern.
- Implement `build()`/`update()`/`search()`/`execute()` against graphify's actual extras API surface — verify this against the real installed package during implementation, not just the PyPI description.
- Reference the old `GraphifyEngine`'s wiring shape (`git show cc1df8d^:src/josu/graph/build.py`) for how it previously satisfied the Protocol structurally — its extraction logic is not reused, only the class-shape pattern.
- No `_call_tool`-style HTTP layer is needed — graphify is an in-process library; confirm whether its operations need `asyncio.to_thread`-style wrapping to satisfy the Protocol's async signatures without blocking the daemon's event loop.

**Patterns to follow:** `engine.py:24-48`'s Protocol signatures; `gortex.py:66-78`'s `GortexUnavailableError(GraphEngineUnavailableError)` pattern for a graphify-specific error subclass.

**Test scenarios:**
- Happy path: `build()`/`search()` against a real small fixture file for each of the five recognized extensions returns real extracted content.
- Missing/unsupported format: raises the graphify-specific unavailable/error type, not an unhandled exception.
- Package genuinely not installed: construction itself is deferred to U8 — this unit's tests assume the package is present.

**Verification:** A real `.xlsx` file, queried via the routing layer, returns graphify-extracted content distinct from what the primary engine would return (gortex doesn't ingest spreadsheet formats at all).

---

### U8. Lazy graphify presence check — instruct, don't install

**Goal:** Defer constructing `GraphifyEngine` until the routing layer first encounters an eligible file; if the package isn't installed, instruct the user to install it themselves — josu never runs the install itself.

**Requirements:** R9

**Dependencies:** U6, U7

**Files:**
- `src/josu/graph/router.py` (modify — add lazy-construction + instruction message)
- `tests/graph/test_router.py` (modify)

**Approach:**
- `RoutingEngine` holds graphify as `Optional[GraphifyEngine]`, starting `None`. On first encountering a graphify-eligible path, check whether the package is importable (`try: import graphify except ImportError` — **the importable module name is `graphify`, not `graphifyy`**; `graphifyy` is only the PyPI distribution name, confirmed via the old `GraphifyEngine`'s own imports at `git show cc1df8d^:src/josu/graph/build.py`, e.g. `from graphify.build import ...`. Re-verify this against the currently-published package before finalizing — package-name/import-name splits occasionally change). If present, construct `GraphifyEngine` and proceed.
- If absent, print a one-time message naming the exact install command (e.g. `uv sync --extra graphify`) and degrade that specific call to `_fallback_file_context()` treatment. **josu takes no installing action itself** — no subprocess is spawned, nothing is written to the environment.
- No consent prompt is needed since josu never acts — "consent" collapses to the user reading the instruction and deciding whether to run it themselves.
- On a later call after the user has installed it, the importability check succeeds and `GraphifyEngine` constructs normally — no stale "absent" state is cached.

**Patterns to follow:** `RoutingEngine`'s existing extension-dispatch logic from U6.

**Test scenarios:**
- First eligible-file encounter, package present: `GraphifyEngine` constructs and the call proceeds.
- First eligible-file encounter, package absent: instruction message printed once, call degrades to fallback, no subprocess spawned (assert no `subprocess`/`pip` invocation occurs).
- Second eligible-file encounter after the package becomes available: `GraphifyEngine` constructs normally.

**Verification:** A fresh environment with a graph engine configured but `graphifyy` not installed: editing a `.py` file works normally with no graphify-related message; the first Excel/Word/Google-Workspace file touched prints the install instruction and degrades gracefully, with zero subprocess activity from josu.

---

### U9. Update documentation

**Goal:** Bring `docs/USAGE.md` and `README.md` in line with the config-declared, connect-only architecture.

**Requirements:** (documentation follow-through for R1-R10, no new requirement ID)

**Dependencies:** U1-U8

**Files:**
- `docs/USAGE.md` (modify)
- `README.md` (modify, if it references the gortex blocker or an install flow)

**Approach:**
- Remove `docs/USAGE.md`'s "Known limitation: the daemon doesn't start today" section now that R1 is fixed.
- Add a `[[graph.engines]]` config example to the Configure section, matching the existing `[[orchestrator.adapters]]`/`[[delegate.candidates]]` examples' style.
- Document, as a Prerequisite (the same way the hosted CLI agent already is), that the user runs and manages their own gortex daemon — including the exact invocation (`gortex daemon start --http-addr <host:port> --tools facade-v1 [--http-auth-token <token>]`) — not something josu installs or starts for them.
- Document graphify's install-instruction flow: what the message looks like, what command it names, and that josu never runs it automatically. Document, alongside it, that graphify's own Google Workspace authentication (for `.gdoc`/`.gsheet`/`.gslides`) is entirely the user's own setup — not something josu's config or `api_key_env` mechanism covers.
- Check README.md's "both sides share one context graph" framing still reads accurately given the config-declared, connect-only shape; adjust only if it now overstates or understates josu's role.

**Patterns to follow:** `docs/USAGE.md`'s existing structure and its convention of showing real captured command output, not invented examples.

**Test scenarios:** N/A — documentation-only unit.

**Verification:** A fresh read-through shows a new user exactly what to run themselves (gortex, with the right flags) and how to point josu's config at it, with no reference to a josu-driven install or consent flow anywhere.

## Scope Boundaries

(carried from origin, unchanged, plus additions surfaced during planning)

- Multi-engine selection UI (letting users choose among several graph/context engines beyond gortex/graphify at runtime) — a real future idea, explicitly not scoped now.
- Content-based routing beyond file-extension matching — worth thinking through more later, not resolved here.
- Slack, ticket systems, PR-discussion threads, and other external non-file data sources — confirmed uncovered by both gortex and graphify.
- Letting the hosted agent manage the graph engine directly via its own MCP config — considered and rejected: breaks the narrow-surface guarantee `mcp_manifest.py` already enforces and does nothing for the delegate side.
- Automating a persistent, OS-level context-engine service (e.g. a launchd/systemd unit) — out of scope; josu connects to whatever's reachable, it doesn't set up long-lived infrastructure.
- A priority-ordered fallback chain across multiple simultaneous `[[graph.engines]]` entries (mirroring delegation chains) — deliberately not built; v1 uses only the first configured entry (see Key Technical Decisions).
- `.gortex.yaml`'s `artifacts:` auto-generation/maintenance — confirmed no auto-discovery exists in gortex itself; out of scope for this plan's core fix.

## Dependencies / Risks

- **Graphify runs in-process with unrestricted access to the daemon's resolved credentials — an accepted trade-off, not an oversight.** Doc-review's security-lens persona flagged (confidence 100) a real asymmetry: `gortex_process.py`'s own docstring documents why the (now-removed) gortex subprocess got a deliberately minimal environment — "an external, unaudited third-party binary" shouldn't get "a live credential-exfiltration path" to resolved API credentials. Graphify (U7) is exactly that same category of unaudited third-party PyPI code, but runs in-process inside the daemon rather than as an isolated subprocess, with no equivalent restriction — a supply-chain compromise or vulnerability in `graphifyy` would have unrestricted read access to every credential the daemon process holds (delegate candidates' resolved API keys, the graph-engine bearer token). Sandboxing an in-process Python library to the same degree as a subprocess is a substantially larger engineering lift than this plan's scope — noted here explicitly, matching how the `check_gortex_reachable()` MITM gap below is also named rather than silently carried. Revisit if graphify's install rate or the package's own security posture changes this calculus.
- **Graphify's Google Workspace auth (`.gdoc`/`.gsheet`/`.gslides`) is entirely the user's own setup, outside josu's config.** R8 names these formats but neither the package's extras nor this plan establish how `graphifyy` authenticates to Google's API. Consistent with this plan's "declare, don't own" posture, that credential is the user's own graphify-side configuration (however `graphifyy` itself expects it — OAuth flow, service account file, etc.) — josu's `api_key_env` mechanism is specific to the graph-engine bearer token (U1/U4) and does not extend to graphify's Google auth. U9 documents this explicitly alongside the gortex prerequisite so it isn't silently discovered at implementation time.
- **A working target that regresses mid-session (e.g. restarted without `--tools facade-v1`) is not detected until its next failing call.** U3's checks run once, when the target is first connected, and lazily re-run only after a prior "no engine" outcome (see U2/U3). A target that was working and then gets misconfigured mid-session isn't proactively re-checked — `gortex.py`'s `_call_tool()` collapses the resulting HTTP 4xx into a generic `reason="http-error"`, losing U3's specific "restart with `--tools facade-v1`" guidance at the moment it would matter most. Building proactive health-monitoring (periodic re-probes, a manual reconnect command) is disproportionate scope for this fix; named here as a known limitation rather than solved.
- **`check_gortex_reachable()`'s liveness probe accepts any local listener returning HTTP 200 with a JSON object as an authentic survivor.** Documented in `gortex_process.py`'s own existing docstring as a known MITM-adjacent gap. Since josu no longer generates any token itself, the only mitigation available is whatever `api_key_env` the user configures on their own target — unlike the earlier spawn-owning design, there is no josu-side narrowing of this window at all now. Flagging explicitly since this plan removes a mitigation path (josu-generated tokens) that an earlier draft had.
- **Gortex's CLI surface has moved meaningfully within days during this planning session alone** — the version-guard's entire reason for existing (U3). Since josu can no longer control the exact invocation a user runs, drift is now something josu can only detect and report, not prevent — implementers should re-verify `gortex daemon start --help`/the tool-preset behavior live before finalizing U2/U3, not trust this plan's citations blindly.
- **Graphify's `[office]`/`[google]` extras working as PyPI describes remains genuinely unverified** — U7 explicitly calls out verifying this against the real installed package before finalizing method bodies.
- **This whole effort still depends on gortex remaining a going concern josu can integrate against as the default target.** Pluggability (Key Technical Decisions) meaningfully reduces this risk now — since there's no code-level dependency on gortex's spawn contract, a user could point `[[graph.engines]]` at a different compatible engine without a josu code change — but the version-and-capability guard's specific checks (tool names, `--tools` preset behavior) are still gortex-shaped.
- **No compatibility-testing story exists for a non-default (non-gortex) graph-engine target.** A user configuring a different engine must independently ensure it speaks the same tool surface (`search`, the specific tool names josu's client calls) — carried from origin as an explicit assumption.
- **Gortex's current tool surface may have no way to report which repos it's tracking, in which case the repo-identity check (U3) cannot be built as designed.** If implementation confirms no such capability exists, a reachable, version-compatible, correctly-preset target tracking the wrong repo remains an undetectable, confidently-wrong-context failure mode — named explicitly here rather than silently unaddressed, per doc-review's adversarial persona.

## Deferred to Implementation

- Exact `GraphEngineTargetConfig` connection-field shape — `host`/`port` fields vs. a single `url` string; a real design detail, not a planning-time product decision.
- `MIN_COMPATIBLE`/`MAX_KNOWN` exact version bounds for U3's guard — this plan names the verified version (`0.62.0+99d745c`) as a known-good point; the exact floor/ceiling values are an implementation-time judgment call.
- Exact `graphifyy[office,google]` version bounds for U7's `pyproject.toml` entry.
- `search()`'s applicability to extension-based routing at all (U6) — the Protocol's `search(query, limit)` signature has no path argument; resolve during U6's implementation.
- Exact graphify install-command wording surfaced to the user (`uv sync --extra graphify` vs. `pip install josu[graphify]`) — matches whichever install mechanism the user's own environment is set up with.

## Sources

- `docs/brainstorms/2026-08-07-gortex-integration-rework-requirements.md` — origin document for this plan (revised this session to the config-declared, connect-only direction).
- Live testing against the real installed `gortex v0.62.0+99d745c` binary (earlier planning pass this session): confirmed `gortex daemon start --http-addr ... --tools facade-v1` starts cleanly with a working `/healthz`, `gortex track --wait` synchronously indexes, both the MCP Streamable HTTP transport and the REST-ish `/v1/tools/{name}` route coexist and work, and `index_repository`/`reindex_repository` return HTTP 404 against the real, current tool surface.
- Repo research (this planning session, dispatched to `ce-repo-research-analyst`, focused on config-declaration patterns): full current contents and line citations for `src/josu/config/orchestrator.py` (`OrchestratorAdapterConfig`, per-entry validation), `src/josu/config/delegate.py` (`api_key_env` field + existence-check + lazy resolution in `delegate/chain.py`), `src/josu/config/__init__.py` (top-level `JosuConfig` assembly), `src/josu/config/chains.py` (the rejected fallback-chain precedent, schema/resolution/execution split), and re-confirmation that `gortex_process.py`/`gortex.py`/`daemon.py`/`engine.py`/`watchers.py`/`run.py` are unchanged from the earlier planning pass (no source files edited this session, only docs).
- External research on context-engine integration patterns (gathered during the brainstorm course-correction): Claude Code's own MCP lifecycle model (client owns spawn/auth via `.mcp.json`), Continue.dev's pluggable `ContextProvider` interface, Aider's retreat from an owned `ctags` binary to a bundled dependency, VS Code's build-time-vendored `ripgrep`, and LSP's protocol-first stance — see origin doc's Sources for full citations.
- `README.md`'s own positioning and Prerequisites section, and `src/josu/orchestrator/mcp_manifest.py` (confirms Claude Code only ever sees josu's own narrow MCP surface, never gortex directly) — the internal precedents this plan's client-only posture extends.
