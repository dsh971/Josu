---
date: 2026-08-05
topic: cli-ease-of-use
---

# CLI Ease-of-Use Findings

## Summary

Fix five confirmed ease-of-use gaps in josu's CLI, found via a hands-on walkthrough of the full documented lifecycle: internal plan-ID leaks and missing default-path info in `--help` text, a config permission warning that's computed but never shown, and a dead runtime dependency. Add a one-line contributor convention to stop the ID-leak pattern recurring.

## Problem Frame

A periodic gut-check on josu's usability, done as a hands-on walkthrough (fresh clone of `main`, isolated `XDG_CONFIG_HOME`, following `README.md`'s documented steps exactly) rather than a desk review, since desk review only catches wording issues, not real runtime friction. The walkthrough covered `uv sync`, every command and subcommand's `--help` output, and the "Run it" flow's first error paths.

That walkthrough surfaced a hard blocker: `josu daemon start` crashes immediately against the currently-installed `gortex` binary (`gortex_process.py:168` hardcodes a `--http-addr` flag the real CLI doesn't have — confirmed via `gortex mcp --help`'s own flag list). Every new user hits this on their first command, with zero config needed to reproduce it. This is being tracked as its own fix, separate from this doc (see Scope Boundaries) — it's a blocking bug, not ease-of-use polish, and may hide more than one flag mismatch rather than just this one.

The five items below are what remained once that blocker was set aside: real, but non-blocking, friction confirmed by actually running the commands.

## Requirements

**CLI messaging clarity**

- R1. `josu --help` and every subcommand's `--help` text contain no internal plan/requirement IDs (e.g. `(U13)`, `(R28/R29)`, `(U14)`) — describe behavior in plain language instead.
- R2. `josu delegate --help`'s `task_type` argument description lists the actual valid task-type values (or points to a live discovery mechanism), not an internal source file/constant name.
- R3. `josu run --help` and `josu delegate --help`'s `--config` flag descriptions state the actual resolved default path, matching what `josu daemon start --help`'s `--config` description already does.

**Config / dependency hygiene**

- R4. The group/world-readable `josu.toml` permission warning the README promises ("josu warns... if it's group/world-readable") reaches the user — currently `load_config()` computes `JosuConfig.warnings` but no code path in `src/josu/` reads or prints it.
- R5. The unused `graphifyy` runtime dependency is removed from `pyproject.toml` — zero imports anywhere in `src/`, fully superseded by gortex.

**Contributor convention**

- R6. A one-line convention (e.g. in `CONTRIBUTING.md`'s existing conventions section) states that user-facing CLI help/error text must not reference internal plan-doc IDs or internal file/module paths.

## Scope Boundaries

**Deferred for later**
- The `gortex --http-addr` crash and any other flag mismatches against the real `gortex` CLI — a separate, higher-priority fix track. Risk to note for whoever picks it up: the installed `gortex` binary (v0.62.0) was built the same day as this walkthrough, suggesting its CLI surface may be actively moving — a hardcoded flag fix alone may not be durable without some compatibility check.
- A broader audit of error messages beyond `--help` text and the "Run it" flow's first errors (e.g. malformed `josu.toml`, remote-candidate auth failures) — not exercised in this walkthrough.

**Outside this product's identity**
- Redesigning the CLI's command or flag structure — findings here are about clarity of the existing structure, not restructuring it.

## Sources / Research

- `src/josu/graph/gortex_process.py:168` — hardcoded `--http-addr` flag; confirmed absent from the real `gortex mcp --help` flag list (v0.62.0, `--bind`/`--port` instead).
- `src/josu/daemon.py:112` and grep across `src/josu/` — `load_config()` is called and its `.warnings` field is never read or printed anywhere in production code.
- `pyproject.toml:14` (`graphifyy` dependency) vs. `grep -rn "graphify" src/` — all remaining references are historical comments/docstrings from the graphify-to-gortex migration, no actual imports.
- Live `--help` output for every command (`josu`, `daemon`, `daemon start`, `init`, `log`, `delegate`, `run`, `cleanup`) — captured during the walkthrough, confirms exact wording cited above.
- `README.md`'s "Getting started" section — the documented flow the walkthrough followed verbatim.
