# docs: Update README/CONTRIBUTING/USAGE to reflect current app state

## Summary

The gortex integration rework (merged PR #4) replaced josu's spawn-and-own gortex lifecycle with a pure-client, config-declared model. `docs/USAGE.md` still describes the *old* design as broken -- an entire "Known limitation" section claims `josu daemon start`/`run`/`delegate` fail against gortex's CLI flags, which is no longer true since josu never invokes those flags at all anymore (independently confirmed end-to-end during planning: a real gortex v0.62.0 daemon started with README's exact documented command connects successfully via josu's real `_resolve_graph_engine_target()`). `CONTRIBUTING.md` has two stale citations (a dead test function name, a dead module symbol) and a subprocess-lifecycle description that no longer matches current code. `docs/USAGE.md` is also missing coverage of features that shipped during the rework: the `GraphifyEngine` secondary engine (Excel/Word/Google-Workspace file support), `--target`'s changed meaning, and lazy engine re-probing. `README.md` was updated during the rework itself and was confirmed accurate by research, aside from one small clarifying addition to its `--target` example; `CONTRIBUTING.md` separately gains a one-line mention that the `graphify` extra is needed to work on that engine.

## Problem Frame

A developer reading `docs/USAGE.md` today is told the daemon doesn't start and the three most important commands (`daemon start`, `run`, `delegate`) don't work -- which was true of the pre-rework design but is false today. A contributor reading `CONTRIBUTING.md` is pointed at a test function and a config symbol that no longer exist. Neither doc mentions a real, already-shipped feature (graphify file support). This plan brings all three docs back in line with the current codebase.

**Scope:** limited to `README.md`, `CONTRIBUTING.md`, and `docs/USAGE.md` (confirmed with user) -- code-level docstrings and CLI `--help` text are out of scope for this pass; a prior pass already brought `--help` text in line with current behavior (see `docs/plans/2026-08-06-001-fix-cli-ease-of-use-plan.md`).

---

## Requirements

- R1. `docs/USAGE.md` no longer frames `daemon start`/`run`/`delegate` as broken -- the "Known limitation" section (and every passage that assumes it) described the pre-rework spawn-based design and does not match current behavior.
- R2. `docs/USAGE.md` documents three shipped-but-undocumented behaviors: the `GraphifyEngine` secondary engine (Excel/Word/Google-Workspace file support via the optional `graphify` extra), `--target`'s current meaning (scopes graphify reads and crash-orphan scanning, not "which graph engine"), and the lazy re-probe behavior (an unavailable graph engine is rechecked automatically, not just at startup). `README.md`'s existing `--target` example gets a one-line clarification for the same reason.
- R3. `CONTRIBUTING.md`'s testing-conventions and code-conventions sections cite only code that currently exists, describe the `gortex version` subprocess call accurately as a one-shot compatibility check rather than a long-lived spawned service, and its Development-setup section mentions the `graphify` extra as a prerequisite for working on that engine.

---

## Key Technical Decisions

- **U1 and U2 both touch `docs/USAGE.md` but stay separate units.** U1 is subtractive (delete the obsolete section and its downstream references); U2 is additive (document real gaps). Keeping them separate makes each independently reviewable and gives each a clean commit message, even though they land in the same file.
- **`README.md` gets no dedicated unit.** Research (`ce-repo-research-analyst`, full-file read cross-checked against `daemon.py`, `gortex_process.py`, `config/graph_engines.py`) found it already accurate -- it was updated during the rework itself. It gets exactly one clarifying line, folded into U2, so the `--target` comment in its "Run it" example can't be misread as controlling which graph engine gets used.
- **Gortex's own current CLI flag surface was independently verified live, not just inferred from josu's own code.** An adversarial review round raised the concern that README's `gortex daemon start --http-addr 127.0.0.1:7411 --tools facade-v1 --detach` example might be stale the same way `gortex mcp`'s `--http-addr` flag was found stale in the cli-ease-of-use plan's research (`docs/brainstorms/2026-08-05-cli-ease-of-use-requirements.md`) -- that finding was about a *different* gortex subcommand (`mcp`, not `daemon start`), so it doesn't transfer automatically. Checked directly against the real local `gortex` v0.62.0 binary (matching `MAX_KNOWN_GORTEX_VERSION`): `gortex daemon start --help` confirms `--http-addr` is a real, current flag on that subcommand, and actually running `gortex daemon start --http-addr 127.0.0.1:17411 --tools facade-v1 --detach` followed by josu's own `_resolve_graph_engine_target()` against it returns a fully-resolved target (`GortexProcess(...)`, `reason=None`) -- README's existing setup example works end-to-end today, not just plausibly. No hedge is needed in the doc text; U1/U2 can state plainly that the documented setup works.

---

## Implementation Units

### U1. Remove the obsolete "Known limitation" framing from `docs/USAGE.md`

**Goal:** Delete the entire premise that `josu daemon start`/`run`/`delegate` are broken, and every passage downstream of that premise, so the doc matches current (working, gracefully-degrading) behavior.

**Requirements:** R1

**Dependencies:** none

**Files:**
- `docs/USAGE.md` (modify)

**Approach:**
- Delete the `## Known limitation: the daemon doesn't start today` section in full (current lines 5-9) -- this single deletion removes its `--http-addr`, `--index`, and `--no-daemon` flag claims and its `/healthz` non-response claim together, plus the `Error: unknown flag: --http-addr` troubleshooting-adjacent claim below it.
- In Prerequisites, drop the `--target ... only actually reachable once the known limitation ... is resolved` qualifier on the gortex bullet -- state plainly that the daemon connects to a configured target if reachable, degrading gracefully otherwise (matching README.md's existing framing of the same point).
- Drop the `only needed once you configure at least one local delegate candidate` -style "once the daemon works" qualifiers wherever they appear (the "Orchestrator adapter" heading, the run-log section's "before any runs exist yet" note).
- In the command-reference table, replace the `daemon start`/`run`/`delegate` entry's "Blocked today -- see Known limitation" status with an accurate one-line description of what each command actually does.
- In Troubleshooting, delete the `gortex exited with code 1 during startup: Error: unknown flag: --http-addr` entry outright (this failure mode can no longer occur -- josu never invokes gortex's CLI to start it). Keep the adjacent `josu daemon not reachable at ...` entry, but drop its "starting that daemon is itself blocked" framing -- the message text itself is still accurate.

**Patterns to follow:** README.md's existing description of graceful degradation ("No target configured or reachable degrades gracefully to direct file exploration, not a hard failure") is the tone/framing to match.

**Test scenarios:** Test expectation: none -- pure documentation edit, no behavioral change.

**Verification:** `docs/USAGE.md` contains no reference to `--http-addr`, `--index`, `--no-daemon`, or any claim that `daemon start`/`run`/`delegate` fail or are blocked. Every remaining passage is consistent with the daemon starting successfully in either an engine-connected or degraded-no-engine state.

---

### U2. Document graphify support, `--target`'s current meaning, and lazy re-probing

**Goal:** Close the three real documentation gaps research surfaced: graphify (office/Google-Workspace) file support has zero mentions anywhere across all three docs; `--target`'s current meaning is undocumented and its README example is easy to misread; the lazy re-probe behavior (an unavailable target becomes usable again without a daemon restart) isn't mentioned anywhere.

**Requirements:** R2

**Dependencies:** U1 (same file -- land the cleanup before adding new content to avoid re-touching the same passages twice)

**Files:**
- `docs/USAGE.md` (modify)
- `README.md` (modify)

**Approach:**
- Add a new `docs/USAGE.md` section (near the existing Configure/config-field-reference material) describing: what graphify is (a secondary engine for `.docx`/`.xlsx`/`.gdoc`/`.gsheet`/`.gslides`, since gortex doesn't ingest those formats), that it's opt-in via `uv sync --extra graphify` (confirm this is the exact extra name against `pyproject.toml` before writing -- it should be, per `[project.optional-dependencies].graphify`), and that `.gdoc`/`.gsheet`/`.gslides` additionally require a user-installed `gws` CLI the user authenticates themselves (josu never touches that credential).
- In `docs/USAGE.md`'s command reference or config-field reference, add a short note on `--target`'s actual current scope: it bounds graphify file reads and crash-orphaned-worktree scanning, and has no effect on which graph engine is used (that's `[[graph.engines]]`'s job).
- Add a short note (Troubleshooting or the daemon-start command reference, whichever reads more naturally) that an unavailable graph engine is automatically rechecked on a later query -- no daemon restart needed once the target becomes reachable.
- In `README.md`'s "Run it" section, adjust the one-line comment on the `--target /path/to/your/repo` example so it can't be read as "this selects the graph engine" -- e.g. clarify it scopes graphify/worktree behavior, distinct from the `[[graph.engines]]` connection target declared in config.

**Patterns to follow:** `docs/USAGE.md`'s existing config-field-reference style (concrete, example-driven) for the new graphify section; `cli.py`'s own `--target` help text (`src/josu/cli.py:544-550`) as the source of truth for what to say about its scope.

**Test scenarios:** Test expectation: none -- pure documentation edit, no behavioral change.

**Verification:** A reader of `docs/USAGE.md` alone can discover that graphify file support exists and how to enable it; can correctly state what `--target` does and does not affect; and knows a degraded graph engine recovers automatically. README's `--target` example no longer implies it controls the graph engine.

---

### U3. Fix stale citations and subprocess-lifecycle description in `CONTRIBUTING.md`

**Goal:** Replace two dead code citations and one outdated behavioral description so `CONTRIBUTING.md` only points contributors at code that actually exists.

**Requirements:** R3

**Dependencies:** none

**Files:**
- `CONTRIBUTING.md` (modify)

**Approach:**
- Line ~28 (testing conventions, "fake executable on `PATH`" bullet): replace the dead citation `tests/graph/test_gortex_process.py`'s `test_terminate_gortex_terminates_a_real_spawned_process` (this function no longer exists -- that file is now reachability/version/capability-probe tests only, with no spawn/terminate lifecycle left to test) with `tests/orchestrator/conftest.py`'s `fake_claude_bin` fixture, the current live example of a real fake executable placed on `PATH` (used by `tests/orchestrator/test_adapter.py` and `test_claude_code.py` -- not `test_run.py`, which deliberately fakes at a higher boundary per its own module docstring, not a real `PATH` executable).
- Line ~34 (code conventions, subprocess bullet, first half): drop the citation to `graph/index.py`'s `_GIT_INDEX_SUBCOMMANDS` -- that symbol doesn't exist anywhere in the codebase (the file has no subprocess call at all today). Keep only the real citation, `orchestrator/worktree.py`'s `_GIT_LIFECYCLE_SUBCOMMANDS`.
- Line ~34 (second half): rewrite "A subprocess spawned for a long-lived service (gortex) gets an explicit, minimal environment..." -- gortex is no longer spawned as a long-lived service at all. Describe the current reality instead: the one remaining gortex-related subprocess call is a short-lived, best-effort `gortex version` compatibility check, and it still gets a minimal `PATH`/`HOME`-only environment (per `graph/gortex_process.py`'s `_minimal_subprocess_env()`) rather than inheriting the daemon's full environment (which carries resolved delegate credentials).
- While touching the Development-setup section, add a one-line mention that the `graphify` extra (`uv sync --extra graphify`) is needed to work on or test `graph/graphify.py`, separate from the plain `dev` extra -- a contributor working on that file today has no way to know it's opt-in.

**Patterns to follow:** The rest of `CONTRIBUTING.md`'s Code conventions section (each bullet: concrete claim, one citation, no editorializing) for both the citation swaps and the rewritten subprocess-lifecycle bullet.

**Test scenarios:** Test expectation: none -- pure documentation edit, no behavioral change.

**Verification:** Every code/test citation in `CONTRIBUTING.md` resolves to a real, currently-existing symbol. No passage describes gortex as a long-lived spawned process. A contributor reading Development-setup knows the `graphify` extra exists and when to install it.

---

## Scope Boundaries

**Deferred to Follow-Up Work**

- A code-level docstring/comment audit across `src/josu/` for drift -- explicitly out of scope; the user chose the 3-doc scope over "everything including code-level docs."
- Verifying gortex's actual current CLI flag surface against the real upstream `gortex` project -- not verifiable from inside this repo; this plan relies only on what josu's own code does (never invokes gortex CLI flags for startup), not on claims about gortex's flags.
- CI configuration documentation -- no `.github/workflows/` or other CI config exists in this repo to document.

---

## Sources & Research

- `ce-repo-research-analyst` diff-style audit: full reads of `docs/USAGE.md`, `CONTRIBUTING.md`, `README.md`, `src/josu/daemon.py`, `src/josu/graph/gortex_process.py`, `src/josu/config/graph_engines.py`; spot-checks with citations against `src/josu/cli.py`, `src/josu/graph/graphify.py`, `src/josu/graph/router.py`, `src/josu/graph/index.py`, `src/josu/orchestrator/worktree.py`, `src/josu/daemon_auth.py`, `src/josu/orchestrator/mcp_manifest.py`, `src/josu/delegate/daemon_client.py`, `pyproject.toml`, `tests/graph/test_gortex_process.py`, `tests/conftest.py`.
- `ce-learnings-researcher`: confirmed `docs/solutions/` remains empty/absent -- no institutional learnings on documentation maintenance to carry forward.
- `ce-doc-review` (headless, 3 personas: coherence, feasibility, adversarial) ran against this plan and surfaced 6 findings, all applied: R2/R3 traceability gaps (README/Development-setup scope wasn't reflected in the requirements they satisfy), an inaccurate Summary claim ("both docs" overstated what CONTRIBUTING.md actually gains), U1's Verification naming flags its Approach didn't explicitly cover, a factually wrong test citation (`fake_claude_bin` isn't used by `test_run.py`), and an adversarial challenge to README's `gortex daemon start` example's continued accuracy.
- The adversarial finding was resolved by direct live verification rather than a doc hedge: a real local `gortex` v0.62.0 binary (`gortex daemon start --http-addr 127.0.0.1:17411 --tools facade-v1 --detach`) was started, and josu's own `_resolve_graph_engine_target()` was called against it directly -- it returned a fully-resolved `GortexProcess` target with no degrade reason, confirming README's existing setup example works end-to-end today, not just plausibly.
- This session's own direct verification of `tests/orchestrator/conftest.py`'s `fake_claude_bin` fixture as the current replacement citation for U3.
