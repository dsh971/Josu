"""Tests for the delegation fallback-chain TOML schema and resolution logic
(U3, R5-R7, R32-R34, R39).

Pure config logic -- tmp_path fixtures write real TOML, `tomllib` parses it,
and `load_chains_config()`/`load_delegate_config()` validate it. No mocks,
matching this repo's existing config-test conventions
(tests/config/test_delegate.py, tests/config/test_config.py).
"""

from __future__ import annotations

import tomllib

from josu.config.chains import (
    DELEGABLE_TASK_TYPES,
    HOSTED_ONLY_TASK_TYPES,
    PROACTIVE_CHECK_TASK_TYPE,
    ChainsConfig,
    DelegationChain,
    default_chain_order,
    load_chains_config,
    resolve_chain,
    resolve_proactive_check_chain,
)
from josu.config.delegate import load_delegate_config


def _parse(toml_text: str, tmp_path) -> dict:
    config_path = tmp_path / "josu.toml"
    config_path.write_text(toml_text, encoding="utf-8")
    with config_path.open("rb") as f:
        return tomllib.load(f)


def _registry(data: dict) -> dict:
    delegate_config, _ = load_delegate_config(data)
    return {c.name: c for c in delegate_config.candidates}


_TWO_CANDIDATES_TOML = """
[[delegate.candidates]]
name = "local-ollama"
endpoint = "http://localhost:11434/v1"
local = true
model = "qwen2.5-coder:7b"

[[delegate.candidates]]
name = "remote-kimi"
endpoint = "https://api.example.com/v1"
api_key_env = "JOSU_KIMI_API_KEY"
local = false
model = "kimi-k2"
"""


def test_chain_with_local_and_remote_resolves_free_local_first_by_default(tmp_path):
    """Covers R33: with no explicit order given, free/local candidates rank
    before remote ones -- even when the remote candidate is listed first in
    TOML -- and remote is eligible here because allow_remote is set."""
    # `allow_remote` must precede any `[[table]]` header -- TOML scopes bare
    # keys to the most recently opened table, so placing it after
    # `_TWO_CANDIDATES_TOML`'s tables would silently nest it inside the last
    # `[[delegate.candidates]]` entry instead of leaving it top-level.
    toml_text = (
        "allow_remote = true\n\n"
        + _TWO_CANDIDATES_TOML
        + """
[[delegation.chains]]
task_type = "file_summarization"
candidates = ["remote-kimi", "local-ollama"]
"""
    )
    data = _parse(toml_text, tmp_path)
    registry = _registry(data)
    chains_config, warnings = load_chains_config(data)
    assert warnings == []

    resolved = resolve_chain("file_summarization", chains_config, registry)
    assert [c.name for c in resolved] == ["local-ollama", "remote-kimi"]


def test_remote_candidate_excluded_unless_allow_remote_set(tmp_path):
    """A chain referencing a remote candidate excludes it by default; setting
    the top-level allow_remote flag makes it eligible with no other config
    change."""
    toml_text = (
        _TWO_CANDIDATES_TOML
        + """
[[delegation.chains]]
task_type = "file_summarization"
candidates = ["local-ollama", "remote-kimi"]
"""
    )
    data = _parse(toml_text, tmp_path)
    registry = _registry(data)
    chains_config, _ = load_chains_config(data)
    assert chains_config.allow_remote is False

    resolved = resolve_chain("file_summarization", chains_config, registry)
    assert [c.name for c in resolved] == ["local-ollama"]

    # Same TOML candidates/order, only the top-level flag changes -- prepended
    # before any table header so it stays a genuine top-level key (see note
    # in the previous test about TOML's bare-key-scoped-to-last-table rule).
    toml_text_with_flag = "allow_remote = true\n\n" + toml_text
    data2 = _parse(toml_text_with_flag, tmp_path)
    registry2 = _registry(data2)
    chains_config2, _ = load_chains_config(data2)
    assert chains_config2.allow_remote is True

    resolved2 = resolve_chain("file_summarization", chains_config2, registry2)
    assert {c.name for c in resolved2} == {"local-ollama", "remote-kimi"}


def test_explicit_order_overrides_free_local_first_default(tmp_path):
    """A chain with explicit_order = true preserves the TOML-authored
    candidates order, rather than being reordered free-local-first."""
    toml_text = (
        "allow_remote = true\n\n"
        + _TWO_CANDIDATES_TOML
        + """
[[delegation.chains]]
task_type = "file_summarization"
candidates = ["remote-kimi", "local-ollama"]
explicit_order = true
"""
    )
    data = _parse(toml_text, tmp_path)
    registry = _registry(data)
    chains_config, _ = load_chains_config(data)

    resolved = resolve_chain("file_summarization", chains_config, registry)
    assert [c.name for c in resolved] == ["remote-kimi", "local-ollama"]


def test_proactive_check_chain_never_contains_remote_candidate(tmp_path):
    """Covers R39: even when the general chain-builder ranks a remote
    candidate first for another task type (with allow_remote set), the
    proactive_check category's resolved chain never contains it."""
    toml_text = (
        "allow_remote = true\n\n"
        + _TWO_CANDIDATES_TOML
        + """
[[delegation.chains]]
task_type = "file_summarization"
candidates = ["remote-kimi", "local-ollama"]
explicit_order = true

[[delegation.chains]]
task_type = "proactive_check"
candidates = ["remote-kimi", "local-ollama"]
"""
    )
    data = _parse(toml_text, tmp_path)
    registry = _registry(data)
    chains_config, _ = load_chains_config(data)

    general = resolve_chain("file_summarization", chains_config, registry)
    assert general[0].name == "remote-kimi"

    proactive = resolve_proactive_check_chain(chains_config, registry)
    assert [c.name for c in proactive] == ["local-ollama"]
    assert all(c.local for c in proactive)


def test_proactive_check_chain_empty_when_no_local_candidate_configured(tmp_path):
    toml_text = """
allow_remote = true

[[delegate.candidates]]
name = "remote-only"
endpoint = "https://api.example.com/v1"
api_key_env = "JOSU_REMOTE_KEY"
local = false
model = "some-model"

[[delegation.chains]]
task_type = "proactive_check"
candidates = ["remote-only"]
"""
    data = _parse(toml_text, tmp_path)
    registry = _registry(data)
    chains_config, _ = load_chains_config(data)

    proactive = resolve_proactive_check_chain(chains_config, registry)
    assert proactive == []


def test_developer_override_forces_task_to_stay_hosted(tmp_path):
    """Covers R7: stays_hosted = true forces the sub-task to stay with the
    hosted orchestrator regardless of candidates configured for it."""
    toml_text = (
        _TWO_CANDIDATES_TOML
        + """
[[delegation.chains]]
task_type = "file_summarization"
candidates = ["local-ollama", "remote-kimi"]
stays_hosted = true
"""
    )
    data = _parse(toml_text, tmp_path)
    registry = _registry(data)
    chains_config, _ = load_chains_config(data)

    resolved = resolve_chain("file_summarization", chains_config, registry)
    assert resolved == []


def test_task_type_with_no_matching_chain_resolves_empty(tmp_path):
    data = _parse(_TWO_CANDIDATES_TOML, tmp_path)
    registry = _registry(data)
    chains_config, _ = load_chains_config(data)

    assert resolve_chain("architecture_decision", chains_config, registry) == []


def test_default_chain_order_is_stable_within_each_group():
    from josu.config.delegate import DelegateCandidate

    a = DelegateCandidate(name="local-a", endpoint="http://x", local=True, model="m")
    b = DelegateCandidate(name="local-b", endpoint="http://x", local=True, model="m")
    c = DelegateCandidate(name="remote-a", endpoint="http://x", local=False, model="m")
    d = DelegateCandidate(name="remote-b", endpoint="http://x", local=False, model="m")

    ordered = default_chain_order([c, d, a, b])
    assert [x.name for x in ordered] == ["local-a", "local-b", "remote-a", "remote-b"]


def test_load_chains_config_warns_on_duplicate_task_type(tmp_path):
    toml_text = (
        _TWO_CANDIDATES_TOML
        + """
[[delegation.chains]]
task_type = "file_summarization"
candidates = ["local-ollama"]

[[delegation.chains]]
task_type = "file_summarization"
candidates = ["remote-kimi"]
"""
    )
    data = _parse(toml_text, tmp_path)
    _, warnings = load_chains_config(data)
    assert len(warnings) == 1
    assert "file_summarization" in warnings[0]


def test_missing_delegation_section_yields_empty_chains(tmp_path):
    data = _parse(_TWO_CANDIDATES_TOML, tmp_path)
    config, warnings = load_chains_config(data)
    assert config.chains == []
    assert config.allow_remote is False
    assert warnings == []


def test_delegation_chain_schema_defaults():
    chain = DelegationChain(task_type="file_summarization", candidates=["a"])
    assert chain.explicit_order is False
    assert chain.stays_hosted is False


def test_v1_task_type_categories_are_disjoint():
    assert DELEGABLE_TASK_TYPES.isdisjoint(HOSTED_ONLY_TASK_TYPES)
    assert PROACTIVE_CHECK_TASK_TYPE not in DELEGABLE_TASK_TYPES
    assert PROACTIVE_CHECK_TASK_TYPE not in HOSTED_ONLY_TASK_TYPES


def test_v1_task_type_categories_match_origin_doc_guide():
    assert DELEGABLE_TASK_TYPES == {
        "file_summarization",
        "directory_summarization",
        "boilerplate_scaffolding",
        "simple_search_extraction",
        "pattern_matched_test_generation",
    }
    assert HOSTED_ONLY_TASK_TYPES == {
        "complex_refactor",
        "architecture_decision",
        "ambiguous_requirements",
        "security_sensitive_change",
    }


def test_claude_md_template_contains_current_delegation_guide_content():
    """Covers the 'generated CLAUDE.md in a worktree contains the current
    delegation guide content' scenario -- U3 only produces the template
    itself; copying it into a worktree is U4's job. This asserts the
    template's content is the guide developers/Claude Code will see."""
    from pathlib import Path

    import josu

    template_path = Path(josu.__file__).parent / "CLAUDE.md.template"
    content = template_path.read_text(encoding="utf-8")
    lowered = content.lower()

    assert "chain-exhausted" in lowered
    assert "perform the sub-task yourself" in lowered
    assert "stays_hosted" in content
    assert "proactive_check" in lowered
    assert "allow_remote" in content
