---
title: CLI Ease-of-Use Fixes
type: fix
date: 2026-08-06
origin: docs/brainstorms/2026-08-05-cli-ease-of-use-requirements.md
---

# CLI Ease-of-Use Fixes

## Summary

Fix six confirmed CLI ease-of-use gaps in `src/josu/cli.py` and adjacent config code: internal plan-ID leaks and missing task-type values in `--help` text, an internal-module-path leak in `--config` help wording, an unsurfaced config permission warning, a dead dependency, and a newly-discovered `(R38)` leak that surfacing warnings would otherwise expose for the first time. Adds a one-line contributor convention to prevent the ID-leak pattern recurring.

## Problem Frame

A hands-on walkthrough of josu's documented CLI surface (origin: docs/brainstorms/2026-08-05-cli-ease-of-use-requirements.md) found five confirmed friction points plus a dead dependency, all independent of the separately-tracked `gortex --http-addr` crash. Planning-time research (`ce-spec-flow-analyzer`) surfaced two more issues once the original scope was checked against the actual code: the "good" `--config` help example R3 pointed at (`daemon start`) still names an internal module path (`config/__init__.py`), which would leave a same-PR contradiction with the new no-internal-references convention this plan also adds; and surfacing `config.warnings` (R4) will make a pre-existing `(R38)` leak in `config/orchestrator.py`'s validation error visible for the first time, turning a currently-invisible bug into a shipped regression unless fixed alongside R4.

## Key Technical Decisions

- **Reword all three `--config` help strings, not just the two originally flagged.** R3's origin wording said to match `run`/`delegate`'s `--config` help to `daemon start`'s existing wording, but `daemon start`'s own text still contains `"config/__init__.py resolves"` — an internal module reference that R6's new convention would ban. All three now describe behavior only (resolves to the XDG config location, with the concrete path), naming no internal file.
- **Build R2's task-type list from `DELEGABLE_TASK_TYPES` directly, not a hardcoded copy.** `_cmd_delegate` already imports from `josu.config.chains`; building the help string at parser-construction time (`f"...({', '.join(sorted(DELEGABLE_TASK_TYPES))})"`) means a future sixth task type updates the help text automatically instead of silently drifting out of sync.
- **Fix `config/orchestrator.py`'s `(R38)` leak alongside R4** (a planning-time discovery, not in the origin doc — see Problem Frame): shipping R4 without this turns an invisible bug into a visible, shipped regression the moment `config.warnings` becomes printed. Same one-line fix pattern as R1 — strip the ID from the `ValueError` message, no behavior change.
- **Print config warnings from a fixed, simple location, not a `create_app()`/`run()` signature refactor.** `daemon.py` has no `print()` calls anywhere today (printing is `cli.py`'s own `_cmd_*` convention); routing warnings through that convention would require `run()`/`create_app()` to hand the loaded config back out to `_cmd_daemon_start` before blocking in `uvicorn.run()`. Printing directly inside `create_app()` (called by production startup and ~5 test fixtures alike) is the smaller, bounded change and keeps this plan a fix set rather than a refactor. `josu run`'s `_cmd_run` already loads config in-process (`cli.py:430`) and already prints via the file's standard `print(f"josu run: ...")` convention — same pattern applies there, reprinting on every invocation if the warning condition persists (accepted: the alternative, de-duplication/caching across runs, is unrequested complexity for a config file the user can simply fix).
- **`josu delegate` gets no in-process warning print.** Its CLI entry (`_cmd_delegate`) never calls `load_config()` itself — only `resolve_daemon_token(config_path)` needs the path, not the loaded config — so the daemon-side print (via `create_app()`) is the only surfacing point for that command, consistent with R4's origin scope.
- **The pre-existing `(R19/R22)` leak in `orchestrator/adapter.py`'s error text stays deferred** (see Scope Boundaries) — not caused by this plan, not in `--help` text, and the origin brainstorm already deferred a broader error-message audit on the same grounds.

## Requirements

(carried from origin: docs/brainstorms/2026-08-05-cli-ease-of-use-requirements.md, with two planning-time additions noted below)

**CLI messaging clarity**

- R1. `josu --help` and every subcommand's `--help` text contain no internal plan/requirement IDs.
- R2. `josu delegate --help`'s `task_type` argument description lists the actual valid task-type values.
- R3. `josu run`, `josu delegate`, and `josu daemon start`'s `--config` flag descriptions state the actual resolved default path with no internal module-path reference (broadened during planning — see Key Technical Decisions).

**Config / dependency hygiene**

- R4. The group/world-readable `josu.toml` permission warning reaches the user via console output at daemon startup and at `josu run`'s CLI entry.
- R4b. *(planning-time addition)* `config/orchestrator.py`'s `(R38)` internal-ID leak, newly exposed by R4, is fixed alongside it.
- R5. The unused `graphifyy` runtime dependency is removed from `pyproject.toml`, and `uv.lock` is regenerated to match.

**Contributor convention**

- R6. A one-line convention in `CONTRIBUTING.md`'s "Code conventions" section states that user-facing CLI help/error text must not reference internal plan-doc IDs or internal file/module paths.

## Implementation Units

### U1. Strip internal plan-IDs from `--help` text

**Goal:** Remove every `(U\d...)`/`(R\d...)` parenthetical from argparse `help=` strings in `src/josu/cli.py`.

**Requirements:** R1

**Dependencies:** none

**Files:**
- `src/josu/cli.py` (modify)
- `tests/test_cli.py` (modify or create — see Test scenarios)

**Approach:** Edit the `help=` text at the five confirmed locations, rewording each to describe behavior in plain language with the ID parenthetical simply removed (not replaced with equivalent jargon):
- `cli.py:568` — `init` subparser help (currently references `(U9, R15)`)
- `cli.py:598` — `delegate` subparser help (currently references `(U7, R28/R29)`)
- `cli.py:616` — `delegate --config` argument help (currently references `(U14)`)
- `cli.py:638` — `run` subparser help (currently references `(U13)`)
- `cli.py:680` — `cleanup` subparser help (currently references `(U8, R27)`)

Docstring/comment mentions of plan IDs elsewhere in `cli.py` (not part of any `help=` string, so never shown by `--help`) are out of scope — R1 and R6 target user-facing text only.

**Patterns to follow:** `cli.py:566-570`, `595-599`, `636-641`, `677-681` for the surrounding help-block style being edited.

**Test scenarios:**
- Happy path: `build_parser().format_help()` (and each subcommand's own help text via `parser.parse_args(["<cmd>", "--help"])`-style invocation, or direct inspection of the constructed subparsers) contains no substring matching a `(U\d` or `(R\d` pattern.
- Regression guard: a single test asserts this across the whole constructed parser tree (all subcommands), not just the five listed lines, so a future help-string addition reintroducing this pattern is caught automatically.

**Verification:** Running `uv run josu --help` and every subcommand's `--help` (`daemon`, `daemon start`, `init`, `log`, `delegate`, `run`, `cleanup`) shows no `(U#)`/`(R#)`-style reference anywhere in the output.

---

### U2. List actual task-type values in `delegate --help`

**Goal:** Replace the `task_type` argument's source-file reference with the real, current list of valid values.

**Requirements:** R2

**Dependencies:** none

**Files:**
- `src/josu/cli.py` (modify)

**Approach:** At `cli.py:601-608`, change the `task_type` positional argument's `help=` text from pointing at `config/chains.py DELEGABLE_TASK_TYPES` to inlining the actual values, built from the constant itself rather than a hardcoded literal — e.g. constructing the help string with `', '.join(sorted(DELEGABLE_TASK_TYPES))` at parser-build time (in `build_parser()`) so the five current values (`file_summarization`, `directory_summarization`, `boilerplate_scaffolding`, `simple_search_extraction`, `pattern_matched_test_generation`) render automatically and stay correct if the set changes later. `DELEGABLE_TASK_TYPES` is currently imported function-locally inside `_cmd_delegate` (`cli.py:337`), not at module scope — `build_parser()` needs its own import of `josu.config.chains.DELEGABLE_TASK_TYPES` (module-level or function-local to `build_parser()`, implementer's call) since it's a separate function.

**Patterns to follow:** `src/josu/config/chains.py:56-64` (`DELEGABLE_TASK_TYPES` definition); `cli.py:337`'s existing import of this module for `_cmd_delegate`.

**Test scenarios:**
- Happy path: the constructed `delegate` subparser's `task_type` help string contains every current `DELEGABLE_TASK_TYPES` value.
- Regression guard: assert the help string is built from the constant (not a separately-maintained literal) — e.g. by monkeypatching `DELEGABLE_TASK_TYPES` to a different set and confirming the constructed help text reflects it, proving no hardcoded duplicate exists.

**Verification:** `uv run josu delegate --help` lists all five current task-type values by name, with no reference to `config/chains.py` or `DELEGABLE_TASK_TYPES` as a symbol name.

---

### U3. Fix `--config` help wording across all three commands

**Goal:** Make `run`, `delegate`, and `daemon start`'s `--config` help consistently state the real default path without naming an internal module.

**Requirements:** R3

**Dependencies:** none (independent of U1/U2, though all three touch `cli.py` — see Risks for sequencing note)

**Files:**
- `src/josu/cli.py` (modify)

**Approach:** Reword all three `--config` argument `help=` strings (`cli.py:553-561` for `daemon start`, `610-620` for `delegate`, `649-656` for `run`) to a single consistent phrasing that states default behavior and the concrete path — e.g. "Path to `josu.toml`... Defaults to `~/.config/josu/josu.toml`, or `$XDG_CONFIG_HOME/josu/josu.toml` if set." — with no reference to `config/__init__.py` or any other internal module path. This unit fully resolves `delegate --config`'s R1 finding (the `(U14)` parenthetical at `cli.py:616`) as a side effect of the reword — if U1 lands first, coordinate to avoid a duplicate edit to the same lines; if U3 lands first, its rewritten text already contains no ID reference, so U1 has nothing further to strip there.

**Patterns to follow:** Existing `--config` help block shapes at the three cited line ranges — reword content, keep the same argparse structure.

**Test scenarios:**
- Happy path: each of the three `--config` help strings contains the literal resolved default path text and does not contain the substring `config/__init__.py` or `config/`.
- Consistency check: all three strings describe the default-resolution behavior in matching wording (not necessarily byte-identical, but no longer divergent — two omitting the path, one including it).

**Verification:** `uv run josu daemon start --help`, `uv run josu run --help`, and `uv run josu delegate --help` all show the same concrete default path for `--config`, and none references an internal file or module.

---

### U4. Surface `config.warnings` at daemon startup and in `josu run`

**Goal:** Print any config warnings (including permission warnings) to the console instead of silently discarding them.

**Requirements:** R4

**Dependencies:** none

**Files:**
- `src/josu/daemon.py` (modify)
- `src/josu/cli.py` (modify — `_cmd_run`)
- `tests/test_daemon.py` (modify or extend)
- `tests/test_cli.py` (modify or create)

**Approach:** In `daemon.py`'s `create_app()`, immediately after `config = load_config(config_path)` (`daemon.py:112`), print each entry in `config.warnings` if the list is non-empty (one line per warning, prefixed to identify it as a config warning). In `cli.py`'s `_cmd_run`, immediately after `config = load_config(config_path)` (`cli.py:430`), print each `config.warnings` entry the same way, using the file's existing `print(f"josu run: ...")` convention. `_cmd_delegate` is intentionally untouched — it never loads config in-process (see Key Technical Decisions), so the daemon-side print is its only surfacing point.

**Patterns to follow:** `cli.py`'s existing `print(f"josu run: {exc}")` error-output convention (`cli.py:425, 443, 446, 456`) for the `_cmd_run` side; no existing `print()` precedent in `daemon.py` — this unit introduces the first one, accepted per Key Technical Decisions rather than routing through a `create_app()`/`run()` signature refactor.

**Test scenarios:**
- Happy path: a `josu.toml` with correct (`0o600`) permissions and no warnings produces no warning output from either `create_app()` or `_cmd_run`.
- Edge case: a group/world-readable (`0o644`) `josu.toml` produces the permission warning in `create_app()`'s output.
- Edge case: the same misconfigured `josu.toml` produces the warning in `_cmd_run`'s output too, independently of the daemon-side print (two separate processes).
- Integration: an end-to-end daemon-start test (matching this repo's existing "real subprocess/real HTTP" convention — see `tests/test_daemon.py`'s existing daemon-lifecycle tests) confirms the warning text actually reaches stdout when the daemon starts with a misconfigured file, not just that `create_app()`'s return value is correct.

**Verification:** Starting `josu daemon start` against a `josu.toml` with `0o644` permissions shows the permission warning in the daemon's console output; running `josu run <task>` against the same file shows the same warning before the run proceeds.

---

### U5. Fix the `(R38)` leak in `config/orchestrator.py`

**Goal:** Remove the internal ID from the `structured_output_mode` validator's error message before U4 makes it user-visible.

**Requirements:** R4b

**Dependencies:** U4 (logically — this fix matters only once warnings are surfaced; no code dependency, safe to implement in either order)

**Files:**
- `src/josu/config/orchestrator.py` (modify)
- `tests/config/test_orchestrator.py` (modify — locate or create the relevant test file for this validator)

**Approach:** At `config/orchestrator.py:119-126`, remove the `"(R38) "` fragment from the `ValueError` message raised by `_output_mode_must_be_declarable`, keeping the rest of the message (which explains the rejection reason without needing the ID) unchanged.

**Patterns to follow:** `config/orchestrator.py:105-115`'s sibling validator (`_command_must_be_allowlisted`) already has no internal-ID reference in its message — matches the target shape directly.

**Test scenarios:**
- Happy path: an `[[orchestrator.adapters]]` entry with an undeclarable `structured_output_mode` still gets rejected at load time (existing behavior unchanged) with a warning that names the invalid value and the allowed set, but contains no `(R38)` substring.

**Verification:** Triggering this validator (an adapter config with an invalid `structured_output_mode`) produces a `config.warnings` entry with no internal ID reference, once that entry is printed per U4.

---

### U6. Remove the dead `graphifyy` dependency

**Goal:** Drop the unused runtime dependency and keep the lockfile in sync.

**Requirements:** R5

**Dependencies:** none

**Files:**
- `pyproject.toml` (modify)
- `uv.lock` (modify — regenerated, not hand-edited)

**Approach:** Remove the `"graphifyy"` line from `pyproject.toml`'s `dependencies` list (`pyproject.toml:14`), then regenerate `uv.lock` so it no longer resolves or pins `graphifyy`.

**Patterns to follow:** none needed — mechanical removal plus lockfile regeneration.

**Test scenarios:** Test expectation: none -- pure dependency removal with zero source imports (confirmed via repo-wide grep during planning research), no behavior to test.

**Verification:** `pyproject.toml` no longer lists `graphifyy`; `uv.lock` no longer contains a `graphifyy` package entry; `uv sync` succeeds cleanly afterward.

---

### U7. Add the no-internal-IDs convention to CONTRIBUTING.md

**Goal:** Document the convention this plan's other units establish, so it doesn't regress.

**Requirements:** R6

**Dependencies:** none (best done last, once the pattern it documents is fresh, but no code dependency)

**Files:**
- `CONTRIBUTING.md` (modify)

**Approach:** Add one bullet to the "Code conventions" section (`CONTRIBUTING.md:31-38`), following the existing bold-lead-in-plus-concrete-example style (matching e.g. the credentials-handling and docstring-content bullets already in that section): state that user-facing CLI help/error text must not reference internal plan-doc IDs (`U#`/`R#`) or internal file/module paths, pointing at `cli.py`'s `help=` strings as the example surface to check. Optionally note the existing sibling distinction already drawn by the docstring-content bullet (docstrings may reference IDs for maintainers; anything end-user-facing may not).

**Patterns to follow:** `CONTRIBUTING.md:33, 35, 37` — three existing bullets showing the target phrasing style exactly.

**Test scenarios:** Test expectation: none -- documentation-only addition, no executable behavior.

**Verification:** `CONTRIBUTING.md`'s "Code conventions" section contains the new bullet, matching the surrounding bullets' style and referencing `cli.py` as the concrete example.

## Scope Boundaries

**Deferred for later** (from origin)
- The `gortex --http-addr` crash and any other flag mismatches against the real `gortex` CLI — separate, higher-priority fix track. Origin's versioning-risk note carries forward: the installed `gortex` binary was built the same day as the originating walkthrough, so a hardcoded-flag fix alone may not be durable without some compatibility check.
- A broader audit of error messages beyond `--help` text, the "Run it" flow's first errors, and the `(R38)` leak this plan fixes — not exercised in the origin walkthrough.

**Deferred to Follow-Up Work** (plan-local)
- The pre-existing `(R19/R22)` ID leak in `orchestrator/adapter.py`'s `ForbiddenFlagError` message, which reaches `josu run`'s terminal output today whenever an adapter's args trip the forbidden-flag check. Not caused by this plan and not in `--help` text, so it falls outside R1's stated scope — but it's the same class of issue R1/R6 target, worth a follow-up.
- README's "or refuses, in strict mode" promise is currently unreachable — no CLI flag anywhere in `cli.py` ever passes `strict=True` to `load_config()`. Adjacent to R4 (both concern `load_config()`'s warning/strict behavior) but out of R4's stated scope (R4 is about the warnings path, not adding a new strict-mode flag).

**Outside this product's identity** (from origin)
- Redesigning the CLI's command or flag structure — this plan's findings are about clarity of the existing structure, not restructuring it.

## Risks & Dependencies

- U1 and U3 both edit `cli.py:610-620` (`delegate --config`'s help block, which carries both the `(U14)` ID from R1 and the missing-path issue from R3). Implement U3 first (or combine into one edit pass over that block) to avoid two separate diffs touching the same lines — noted in U3's Approach.
- U4 introduces the first `print()` call in `daemon.py`; `create_app()` is called directly by roughly five existing test files (`tests/test_daemon.py`, `tests/graph/test_index.py`, `tests/delegate/test_internal_api.py`, `tests/orchestrator/test_run.py`, and others per planning research). None of them currently assert on stdout content or fail on extra output, so this is expected to be non-breaking, but worth a first-pass test run to confirm no test captures and asserts against clean stdout.
- U6's `uv.lock` regeneration should be reviewed to confirm no other resolved package version shifts unexpectedly as a side effect of removing `graphifyy` (unlikely for an unrelated leaf dependency, but cheap to check).

## Sources / Research

- `src/josu/cli.py` — single-file CLI implementation (719 lines); `build_parser()` at `cli.py:538-712`; all `help=` string locations for R1 (568, 598, 616, 638, 680), R2 (601-608), R3 (553-561, 610-620, 649-656), and R4's `_cmd_run` insertion point (430).
- `src/josu/config/chains.py:56-64` — `DELEGABLE_TASK_TYPES` frozenset definition for U2.
- `src/josu/config/__init__.py:109, 150-204` — `JosuConfig.warnings` field and `load_config()`'s warning-producing paths (missing-file, permission, section-level).
- `src/josu/daemon.py:111-112` — `create_app()`'s `load_config()` call site, the U4 daemon-side insertion point.
- `src/josu/config/orchestrator.py:119-126, 194-207` — `(R38)` leak location (U5) and how it reaches `config.warnings` via `load_orchestrator_config()`'s `ValidationError` string interpolation.
- `pyproject.toml:14`, `uv.lock:316, 333` — `graphifyy`'s declaration and lockfile entries for U6.
- `CONTRIBUTING.md:31-38` — "Code conventions" section and existing bullet style for U7.
- `src/josu/orchestrator/adapter.py:104` — pre-existing `(R19/R22)` leak, deferred per Scope Boundaries.
- `README.md:70` — the unreachable "strict mode" promise, deferred per Scope Boundaries.
- `docs/brainstorms/2026-08-05-cli-ease-of-use-requirements.md` — origin requirements and full walkthrough findings.
