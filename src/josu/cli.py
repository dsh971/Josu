"""josu CLI entry point.

Subcommands are added incrementally as their owning implementation unit
lands (see docs/plans/2026-07-21-001-feat-hybrid-local-hosted-coding-agent-plan.md).
`daemon` ships with U1 since daemon.py has to be reachable somehow; `init`,
`run`, `models`, `cleanup`, and `log` land with their respective units.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from josu.config import resolve_config_path
from josu.daemon import DEFAULT_HOST, DEFAULT_PORT


def _default_graph_out_dir() -> Path:
    return Path.cwd() / ".josu" / "graphify-out"


def _cmd_daemon_start(args: argparse.Namespace) -> int:
    from josu.daemon import run

    graph_out_dir = Path(args.graph_out_dir) if args.graph_out_dir else _default_graph_out_dir()
    target = Path(args.target) if args.target else Path.cwd()
    config_path = Path(args.config) if args.config else resolve_config_path()
    print(
        f"Starting josu daemon on {args.host}:{args.port} "
        f"(graph: {graph_out_dir}, target: {target}, config: {config_path})"
    )
    run(graph_out_dir, host=args.host, port=args.port, target=target, config_path=config_path)
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    from josu.observability.runlog import (
        RunNotFoundError,
        default_runlog_dir,
        latest_run_id,
        load_run,
        render_run,
    )

    runlog_dir = Path(args.runlog_dir) if args.runlog_dir else default_runlog_dir(Path.cwd())
    run_id = args.run_id or latest_run_id(runlog_dir)
    if run_id is None:
        print(f"No run log entries found under {runlog_dir}")
        return 1

    try:
        record = load_run(run_id, runlog_dir)
    except RunNotFoundError as exc:
        print(str(exc))
        return 1

    print(render_run(record))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="josu")
    subparsers = parser.add_subparsers(dest="command", required=True)

    daemon_parser = subparsers.add_parser("daemon", help="Manage the josu daemon")
    daemon_subparsers = daemon_parser.add_subparsers(dest="daemon_command", required=True)

    start_parser = daemon_subparsers.add_parser("start", help="Start the daemon in the foreground")
    start_parser.add_argument("--host", default=DEFAULT_HOST)
    start_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    start_parser.add_argument(
        "--graph-out-dir",
        default=None,
        help="Where the graph is persisted (default: ./.josu/graphify-out)",
    )
    start_parser.add_argument(
        "--target",
        default=None,
        help="Project root to build the graph from if none exists yet (default: cwd)",
    )
    start_parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to josu.toml (candidate/chain config). Defaults to the "
            "XDG-style location config/__init__.py resolves "
            "(~/.config/josu/josu.toml, or $XDG_CONFIG_HOME/josu/josu.toml)."
        ),
    )
    start_parser.set_defaults(func=_cmd_daemon_start)

    log_parser = subparsers.add_parser("log", help="Render a run's run-log record")
    log_parser.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Run id to render (default: the most recently started run)",
    )
    log_parser.add_argument(
        "--runlog-dir",
        default=None,
        help="Where run-log records are stored (default: ./.josu/runlog under cwd)",
    )
    log_parser.set_defaults(func=_cmd_log)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    exit_code = args.func(args)
    sys.exit(exit_code)
