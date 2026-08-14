# Using josu

A deeper, practical companion to [README.md](../README.md)'s quick start — real command output, full config field reference, and current known limitations. Read the README first for *why* josu exists; this doc is about actually running it.

## Known limitation: the daemon doesn't start today

**`josu daemon start`, and everything that depends on it (`josu run`, `josu delegate`), currently fail against the real [gortex](https://github.com/zzet/gortex) CLI.** Gortex's flag surface has moved since josu's integration was written — `--http-addr` no longer exists (gortex now uses `--bind`/`--port`), `--index`/`--no-daemon` are silently-ignored no-ops on gortex's current "auto-start a daemon and proxy to it" model, and the `/healthz` endpoint josu polls for readiness doesn't respond. This is a real, open issue, not a config mistake on your end — a correctly-configured `josu.toml` and a working `gortex` install will still hit it.

What this means in practice: `josu init`, `josu log`, and `josu cleanup` all work today, in full, without the daemon. `josu daemon start`, `josu run`, and `josu delegate` do not — you'll see gortex's own raw CLI usage text dumped to your terminal when you try. There's no workaround short of the gortex-compatibility fix landing; this doc will drop this section once it does.

## Prerequisites

- **Python 3.12+ and [uv](https://docs.astral.sh/uv/).** You don't need to manually install Python 3.12 yourself — `uv sync` provisions its own managed Python 3.12 toolchain automatically, even if your system's default `python3` is older.
- **The [`gortex`](https://github.com/zzet/gortex) CLI on `PATH`** — only actually reachable once the [known limitation](#known-limitation-the-daemon-doesnt-start-today) above is resolved, but install it now so you're ready: `curl -fsSL https://get.gortex.dev | sh`.
- **An OpenAI-chat-compatible local server** for the delegate worker — [Ollama](https://ollama.com) serving its `/v1` endpoint is the reference target. Only needed once you configure at least one local delegate candidate; not needed for `init`/`log`/`cleanup`.
- **A hosted CLI agent on `PATH`** — currently only [`claude`](https://claude.com/product/claude-code) — only needed for `josu run` once the daemon works; not needed for anything covered in this doc today.

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

### Orchestrator adapter (for `josu run`, once the daemon works)

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

Renders a run-log record — defaults to the most recently started run if you omit `run_id`. Before any runs exist yet (which is the case until the daemon works):

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

Documented in [README.md](../README.md#run-it) for the intended flow. Blocked today — see [Known limitation](#known-limitation-the-daemon-doesnt-start-today) at the top of this doc.

## Troubleshooting

**`josu daemon not reachable at 127.0.0.1:8765 -- start it with josu daemon start`** — exactly what it says: `josu run` and `josu delegate` both need a running daemon first. Right now, starting that daemon is itself blocked (see [Known limitation](#known-limitation-the-daemon-doesnt-start-today)).

**`gortex exited with code 1 during startup: Error: unknown flag: --http-addr`** — the known gortex-compatibility issue. Not something you can fix locally by changing config.

**A config warning appears every time you run a command** — the warning reflects the actual current state of `josu.toml`; it'll stop once you fix what it names (permissions, a malformed entry, an unset env var).
