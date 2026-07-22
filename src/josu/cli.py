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

from josu.daemon import DEFAULT_HOST, DEFAULT_PORT


def _default_graph_out_dir() -> Path:
    return Path.cwd() / ".josu" / "graphify-out"


def _cmd_daemon_start(args: argparse.Namespace) -> int:
    from josu.daemon import run

    graph_out_dir = Path(args.graph_out_dir) if args.graph_out_dir else _default_graph_out_dir()
    target = Path(args.target) if args.target else Path.cwd()
    print(f"Starting josu daemon on {args.host}:{args.port} (graph: {graph_out_dir}, target: {target})")
    run(graph_out_dir, host=args.host, port=args.port, target=target)
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
    start_parser.set_defaults(func=_cmd_daemon_start)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    exit_code = args.func(args)
    sys.exit(exit_code)
