# josu

A hosted-primary coding agent that offloads bounded, low-complexity work to a cheaper delegate model, so you're not paying frontier-model prices for file summaries and boilerplate.

## Why this exists

AI coding tools are becoming central to how developers work, but cost is a real constraint for anyone without an enterprise budget. Hosted CLI agents like Claude Code are reliable at sustained, multi-step reasoning — but every token of that reasoning is billed, including the large share of a coding session that isn't actually hard: summarizing a file, scaffolding boilerplate against an established pattern, running a simple search, writing a pattern-matched test. That work doesn't need a frontier model. It needs a model that's cheap enough not to think twice about, running right there on your machine (or a cheap remote endpoint) when you already have one.

An earlier version of this project tried the opposite shape — a local model as the primary driver, escalating to a hosted agent when stuck. The evidence didn't support it: local models in the hardware-realistic range are strong at bounded, well-specified tasks but collapse specifically on sustained multi-turn tool-calling, which is exactly what "primary orchestrator" requires. So josu flips that: the hosted agent stays in the driver's seat, doing what it's already good at, and hands off the bounded, low-complexity pieces to a delegate worker — local-first, with remote open-weight models as a fallback once they clear a real capability bar.

Neither model re-derives context from scratch to do this. Both query a shared, incrementally-maintained code-relationship graph, so delegating a summary doesn't cost the hosted agent tokens just to explain what needs summarizing.

## What it does

- **Claude Code drives.** Tasks run in an isolated git worktree, unattended but never with a full permission bypass — work lands as a diff for review before merging.
- **Bounded sub-tasks get delegated.** A static, developer-overridable guide decides what's safe to hand off; a ranked fallback chain (free/local candidates first) picks who actually does it.
- **Both sides share one context graph.** Backed by [gortex](https://github.com/zzet/gortex), queried through a fixed two-tool MCP surface so the schema footprint never grows with the graph.
- **Nothing runs unattended without a safety net.** Wall-clock timeouts, per-call timeouts, a local run log, and a hard rule against `--dangerously-skip-permissions`-style bypasses.

## Project status

Early and under active development (v0.1.0) — architecture and scope are still settling. `docs/brainstorms/` holds the product-level reasoning (problem framing, alternatives considered, what's explicitly out of scope); `docs/plans/` holds the corresponding implementation plans. Both are living documents that get revised as the project's own assumptions get tested against reality — worth reading before assuming any given design decision is final.
