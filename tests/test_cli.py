"""Tests for the `josu` CLI's argparse surface (`src/josu/cli.py`) --
specifically `--help` text quality, per docs/plans/2026-08-06-001-fix-cli-
ease-of-use-plan.md (origin: docs/brainstorms/2026-08-05-cli-ease-of-use-
requirements.md): user-facing help text must not leak internal plan/
requirement IDs or internal module paths, and must reflect real, current
values rather than pointing at source.
"""

from __future__ import annotations

import argparse
import re

from josu.cli import build_parser
from josu.config.chains import DELEGABLE_TASK_TYPES


def _all_parsers(parser: argparse.ArgumentParser):
    """Recursively yield `parser` and every nested subparser (`daemon start`
    is a subparser of a subparser, so a flat walk over the top-level
    parser's own `choices` alone would miss it)."""
    yield parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                yield from _all_parsers(sub)


def _parser_by_prog(parser: argparse.ArgumentParser, prog: str) -> argparse.ArgumentParser:
    return next(p for p in _all_parsers(parser) if p.prog == prog)


def test_no_help_text_references_internal_plan_ids():
    """U1: covers the whole constructed parser tree, not just the five
    originally-flagged lines, so a future help-string addition reintroducing
    this pattern is caught automatically."""
    parser = build_parser()
    for p in _all_parsers(parser):
        help_text = p.format_help()
        assert not re.search(r"\(U\d|\(R\d", help_text), (
            f"internal plan-ID reference found in {p.prog!r} --help text"
        )


def test_delegate_help_lists_actual_task_types():
    """U2: the real, current task-type values are named, not a pointer to
    the source constant."""
    parser = build_parser()
    delegate_parser = _parser_by_prog(parser, "josu delegate")
    help_text = delegate_parser.format_help()

    for task_type in DELEGABLE_TASK_TYPES:
        assert task_type in help_text
    assert "DELEGABLE_TASK_TYPES" not in help_text
    assert "config/chains.py" not in help_text


def test_delegate_help_task_types_built_from_constant_not_hardcoded(monkeypatch):
    """U2 regression guard: the help text must be built from
    `DELEGABLE_TASK_TYPES` at parser-construction time, not a separately
    maintained literal -- proven by swapping the constant's contents and
    confirming the constructed help text reflects the swap."""
    monkeypatch.setattr("josu.cli.DELEGABLE_TASK_TYPES", frozenset({"totally_new_task_type"}))
    parser = build_parser()
    delegate_parser = _parser_by_prog(parser, "josu delegate")
    help_text = delegate_parser.format_help()

    assert "totally_new_task_type" in help_text
    assert "file_summarization" not in help_text


def test_config_help_consistent_and_no_internal_module_reference():
    """U3: `daemon start`, `run`, and `delegate`'s `--config` help all state
    the real default path and none references an internal module."""
    parser = build_parser()
    for prog in ("josu daemon start", "josu run", "josu delegate"):
        help_text = _parser_by_prog(parser, prog).format_help()
        assert "~/.config/josu/josu.toml" in help_text
        assert "$XDG_CONFIG_HOME/josu/josu.toml" in help_text
        assert "config/__init__.py" not in help_text
