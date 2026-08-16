# Using josu

A deeper, practical companion to [README.md](../README.md)'s quick start — real command output and full config field reference. Read the README first for *why* josu exists; this doc is about actually running it.

## Prerequisites

- **Python 3.12+ and [uv](https://docs.astral.sh/uv/).** You don't need to manually install Python 3.12 yourself — `uv sync` provisions its own managed Python 3.12 toolchain automatically, even if your system's default `python3` is older.
- **The [`gortex`](https://github.com/zzet/gortex) CLI, running and tracking your repo** — `curl -fsSL https://get.gortex.dev | sh` to install, then `gortex daemon start --http-addr 127.0.0.1:7411 --tools facade-v1 --detach && gortex track /path/to/your/repo --wait` to start and track your repo yourself. josu connects to this target if it's reachable and degrades gracefully to direct file exploration otherwise — it never installs, starts, or tracks gortex on your behalf. Skipping this entirely is fine too.
- **An OpenAI-chat-compatible local server** for the delegate worker — [Ollama](https://ollama.com) serving its `/v1` endpoint is the reference target. Only needed once you configure at least one local delegate candidate; not needed for `init`/`log`/`cleanup`.
- **A hosted CLI agent on `PATH`** — currently only [`claude`](https://claude.com/product/claude-code) — only needed for `josu run`; not needed for anything covered in this doc today.

## Install

```bash
git clone <this-repo>
cd josu
uv sync
```

Installs josu's own CLI (`josu`) into `.venv`. Run it via `uv run josu ...`, or activate the virtualenv and call `josu` directly.

## Configure

josu reads `josu.toml` from an XDG-style path — `~/.config/josu/josu.toml`, or `$XDG_CONFIG_HOME/josu/josu.toml` if set — never from your project directory (so it can't get swept into a worktree commit by accident). Every command that touches config (including the ones that work today) reads this same file.

### Minimal example

```toml
# ~/.config/josu/josu.toml

[[delegate.candidates]]
name = "local-qwen"
endpoint = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible endpoint
local = true
model = "qwen2.5-coder:7b"              # the only curated model in v1

[[delegation.chains]]
task_type = "file_summarization"
candidates = ["local-qwen"]
```

`task_type` must be one of the values `josu delegate --help` lists — run it to see the current set, since this doc would otherwise drift out of sync with the source of truth (`src/josu/config/chains.py`'s `DELEGABLE_TASK_TYPES`).

### Multiple candidates, with a remote fallback

A chain tries its candidates in list order — put your free/local candidate first, a paid remote one after:

```toml
[[delegate.candidates]]
name = "local-qwen"
endpoint = "http://localhost:11434/v1"
local = true
model = "qwen2.5-coder:7b"

[[delegate.candidates]]
name = "remote-fallback"
endpoint = "https://api.example.com/v1"
api_key_env = "MY_PROVIDER_API_KEY"     # an env-var *name*, never a raw key
local = false
model = "some-remote-model"

[[delegation.chains]]
task_type = "file_summarization"
candidates = ["local-qwen", "remote-fallback"]
```

`api_key_env` names an environment variable — josu reads the variable's value at call time, never stores or logs it. There's no `api_key` field at all; the schema has no place to put a raw secret. If you reference an env var that isn't actually set, josu warns (see [Permission and validation warnings](#permission-and-validation-warnings)) rather than crashing.

### Orchestrator adapter (for `josu run`)

```toml
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

See `src/josu/CLAUDE.md.template` for the fuller delegation-guide prose worth adapting into your own project's `CLAUDE.md`.

### Graphify (Excel/Word/Google-Workspace files)

gortex doesn't ingest `.docx`/`.xlsx`/`.gdoc`/`.gsheet`/`.gslides` files at all — josu routes those to a narrow secondary engine, graphify, instead. It's opt-in, not installed by default:

```bash
uv sync --extra graphify
```

`.docx`/`.xlsx` convert in-process once that extra is installed — no further setup needed. `.gdoc`/`.gsheet`/`.gslides` (Google Workspace shortcut files) additionally require a separate, user-installed `gws` CLI ([googleworkspace/cli](https://github.com/googleworkspace/cli)), authenticated via your own `gws auth login` — josu never touches that credential. Without `gws` installed and authenticated, a `.gdoc`/`.gsheet`/`.gslides` request degrades to a clear error rather than a crash. There's nothing to configure in `josu.toml` for graphify itself — it's routed automatically by file extension.

### File permissions

`josu.toml` should be readable by your user only:

```bash
chmod 600 ~/.config/josu/josu.toml
```

josu checks this and warns if it's group/world-readable — see the next section for exactly what that looks like.

## Permission and validation warnings

Config problems (bad permissions, a malformed candidate entry, an unset `api_key_env` variable) never crash josu — they're collected as warnings and printed wherever config gets loaded. A world-readable file looks like this:

```
josu daemon: warning: /home/you/.config/josu/josu.toml is group/world-accessible (mode 644) -- it may contain credential env-var references; restrict it to your user only (e.g. chmod 0600), the same convention ssh uses for private key files
```

A malformed candidate entry (missing a required field) names exactly which field, and never echoes back other fields you wrote — including any mistyped credential-shaped value:

```
josu daemon: warning: delegate candidate 'my-candidate' rejected at load time: endpoint: Field required
```

`josu run` prints the same warnings, prefixed `josu run: warning:` — from the same config file, loaded a second time in that process. `josu delegate` never prints them itself (it doesn't load config directly — see its own `--help`); rely on the daemon's own startup output for those.

## Command reference

### `josu init`

Installs (or chains to an existing) `post-commit` git hook that drives commit-triggered proactive checks. Run once per repo you want josu watching:

```console
$ uv run josu init
josu init: installed post-commit hook at /path/to/repo/.git/hooks/post-commit
```

Safe to re-run — it detects the hook is already there and says so instead of reinstalling:

```console
$ uv run josu init
josu init: post-commit hook already installed at /path/to/repo/.git/hooks/post-commit
```

If you already have a Husky, `pre-commit`, or hand-written `post-commit` hook, `josu init` chains to it rather than overwriting it — or aborts with a clear warning if that can't be done safely.

### `josu log [run_id]`

Renders a run-log record — defaults to the most recently started run if you omit `run_id`. Before any runs exist yet:

```console
$ uv run josu log
No run log entries found under /path/to/repo/.josu/runlog
```

### `josu cleanup`

Lists abandoned or crash-orphaned worktrees, with `--remove NAME` / `--remove-all` to clean them up. On a fresh repo:

```console
$ uv run josu cleanup
No abandoned worktrees found.
```

Run `josu <command> --help` for the full, current flag list on any command — it's kept in sync with actual behavior (no internal jargon, no stale defaults) as a matter of project convention; see `CONTRIBUTING.md`'s "Code conventions" section.

### `josu daemon start`, `josu run`, `josu delegate`

Documented in [README.md](../README.md#run-it) for the intended flow. `josu daemon start` runs the daemon in the foreground, connecting to your configured `[[graph.engines]]` target if reachable (degrading gracefully to no graph engine otherwise, never blocking startup — see [Graph engine connectivity](#graph-engine-connectivity) below). `josu run`/`josu delegate` both require the daemon already running.

## Graph engine connectivity

josu never installs, starts, or tracks gortex on your behalf — it connects to whatever `[[graph.engines]]` target you declared in `josu.toml`, the same way you already run the hosted CLI agent yourself. At `josu daemon start`, the target is checked for reachability, gortex version compatibility, and tool-surface capability (the target must be running with `--tools facade-v1`); any failure prints a `josu daemon: warning: ...` line naming what's wrong and the daemon starts anyway with no graph engine for that session, rather than failing to start.

An unreachable target isn't a permanent state for the session, either — a later graph query automatically rechecks the target (rate-limited to roughly once every 30 seconds), so a gortex you start *after* `josu daemon start` becomes usable without restarting josu's daemon.

**`josu daemon start`'s `--target` flag is unrelated to any of this.** It scopes graphify file reads and crash-orphaned-worktree scanning to a repo root (default: cwd) — it has no effect on which graph engine is used or connected to; that's entirely `[[graph.engines]]`'s job in `josu.toml`.

## Troubleshooting

**`josu daemon not reachable at 127.0.0.1:8765 -- start it with josu daemon start`** — exactly what it says: `josu run` and `josu delegate` both need a running daemon first.

**A config warning appears every time you run a command** — the warning reflects the actual current state of `josu.toml`; it'll stop once you fix what it names (permissions, a malformed entry, an unset env var).
