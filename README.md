# josu

A hosted-primary coding agent that offloads bounded, low-complexity work to a cheaper delegate model, so you're not paying frontier-model prices for file summaries and boilerplate.

## Why this exists

AI coding tools are becoming central to how developers work, but cost is a real constraint for anyone without an enterprise budget. Hosted CLI agents (Claude Code, Codex, and others like them) are reliable at sustained, multi-step reasoning — but every token of that reasoning is billed, including the large share of a coding session that isn't actually hard: summarizing a file, scaffolding boilerplate against an established pattern, running a simple search, writing a pattern-matched test. That work doesn't need a frontier model. It needs a model that's cheap enough not to think twice about, running right there on your machine (or a cheap remote endpoint) when you already have one.

An earlier version of this project tried the opposite shape — a local model as the primary driver, escalating to a hosted agent when stuck. The evidence didn't support it: local models in the hardware-realistic range are strong at bounded, well-specified tasks but collapse specifically on sustained multi-turn tool-calling, which is exactly what "primary orchestrator" requires. So josu flips that: the hosted agent stays in the driver's seat, doing what it's already good at, and hands off the bounded, low-complexity pieces to a delegate worker — local-first, with remote open-weight models as a fallback once they clear a real capability bar.

Neither model re-derives context from scratch to do this. Both query a shared, incrementally-maintained code-relationship graph, so delegating a summary doesn't cost the hosted agent tokens just to explain what needs summarizing.

## What it does

- **A hosted CLI agent drives, via a config-driven adapter — not hardcoded to one vendor.** Tasks run in an isolated git worktree, unattended but never with a full permission bypass — work lands as a diff for review before merging. Claude Code is the only adapter that ships today (its headless mode supports non-interactive MCP tool approval); Codex is the natural next candidate but is blocked on an external, currently-open upstream issue ([openai/codex#24135](https://github.com/openai/codex/issues/24135)) with no non-interactive MCP-approval path short of disabling its sandbox — not a limitation josu's own architecture imposes. Adding a new adapter is meant to be a declarative config entry (invocation command + structured-output field mapping), not a hand-written integration, for any CLI that clears the same non-interactive-approval bar.
- **Bounded sub-tasks get delegated.** A static, developer-overridable guide decides what's safe to hand off; a ranked fallback chain (free/local candidates first) picks who actually does it.
- **Both sides share one context graph.** Backed by [gortex](https://github.com/zzet/gortex), queried through a fixed two-tool MCP surface so the schema footprint never grows with the graph.
- **Nothing runs unattended without a safety net.** Wall-clock timeouts, per-call timeouts, a local run log, and a hard rule against `--dangerously-skip-permissions`-style bypasses.

## Getting started

### Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- A hosted CLI agent on `PATH` for `josu run` to drive as a subprocess — currently only the [`claude`](https://claude.com/product/claude-code) CLI (see the `[[orchestrator.adapters]]` allowlist below); not needed just to run the daemon or the `delegate` CLI escape hatch
- The [`gortex`](https://github.com/zzet/gortex) CLI on `PATH` — powers the shared code-relationship graph. Install it with `curl -fsSL https://get.gortex.dev | sh` or see its own [installation docs](https://github.com/zzet/gortex#installation)
- An OpenAI-chat-compatible local server for the delegate worker (e.g. [Ollama](https://ollama.com) serving its `/v1` endpoint) — only needed once you configure at least one local delegate candidate

### Install

```bash
git clone <this-repo>
cd josu
uv sync
```

This installs josu's own CLI (`josu`) into `.venv`, runnable via `uv run josu ...` or directly once the virtualenv is active.

### Configure

josu reads `josu.toml` from an XDG-style path — `~/.config/josu/josu.toml`, or `$XDG_CONFIG_HOME/josu/josu.toml` if set — never from the project directory itself (so it can't accidentally get swept into a worktree commit). Create it with at least one delegate candidate, a chain routing some task type to it, and an orchestrator adapter:

```toml
# ~/.config/josu/josu.toml

[[delegate.candidates]]
name = "local-qwen"
endpoint = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible endpoint
local = true
model = "qwen2.5-coder:7b"              # the only curated model in v1 (see src/josu/models/curated.py)

[[delegation.chains]]
task_type = "file_summarization"
candidates = ["local-qwen"]

[orchestrator]
wall_clock_timeout_seconds = 1200        # whole-run budget for `josu run`, defaults to 20 minutes

[[orchestrator.adapters]]
name = "claude-code"
command = "claude"
args = ["-p", "--strict-mcp-config", "{mcp_config}", "--allowedTools", "Read({worktree}/**)", "{task}"]
structured_output_mode = "stream-json"
field_mapping = { result = "result" }
mcp_approval_verified = { verified = true, verified_date = 2026-01-01 }
```

`josu.toml` should be readable by your user only (`chmod 600`) — it may reference credential env-var names for remote candidates, and josu warns (or refuses, in strict mode) if it's group/world-readable. See `src/josu/CLAUDE.md.template` for the fuller delegation-guide prose worth adapting into your own project's `CLAUDE.md`, and `docs/plans/` for every `[[...]]` field's full schema and rationale.

### Run it

```bash
# 1. Start the daemon (spawns/reuses gortex, serves both MCP servers + internal routes)
uv run josu daemon start --target /path/to/your/repo

# 2. In your repo, install the post-commit hook that drives proactive checks
cd /path/to/your/repo
uv run josu init

# 3. Hand a task to the hosted orchestrator loop (worktree -> adapter -> diff review -> merge -> reindex)
uv run josu run "Add input validation to the signup form"
```

Other commands:

| Command | What it does |
|---|---|
| `josu daemon start` | Runs the daemon in the foreground — required before `run`, `delegate`, or the commit-hook checks can reach it |
| `josu init` | Installs (or chains to an existing) `post-commit` git hook for commit-triggered proactive checks |
| `josu run <task>` | Runs a task end-to-end through the hosted orchestrator loop; requires the daemon already running |
| `josu delegate <task_type> <task>` | Routes one bounded task straight to the local delegate worker, bypassing the hosted orchestrator — a manual escape hatch for when the configured hosted agent is quota/rate-limit exhausted |
| `josu log [run_id]` | Renders a run-log record (defaults to the most recent run) |
| `josu cleanup` | Lists (and optionally removes) abandoned or crash-orphaned worktrees |

Run `josu <command> --help` for the full flag list on any of these.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, testing conventions, and how to submit changes.

## Project status

Early and under active development (v0.1.0) — architecture and scope are still settling. `docs/brainstorms/` holds the product-level reasoning (problem framing, alternatives considered, what's explicitly out of scope); `docs/plans/` holds the corresponding implementation plans. Both are living documents that get revised as the project's own assumptions get tested against reality — worth reading before assuming any given design decision is final.
