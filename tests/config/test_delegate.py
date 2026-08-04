"""Tests for the delegate-candidate registry TOML schema (U11, R30, R31)."""

from __future__ import annotations

import pytest

from josu.config.delegate import DelegateCandidate, load_delegate_config


def test_candidate_schema_validates_all_fields():
    candidate = DelegateCandidate.model_validate(
        {
            "name": "ollama-qwen",
            "endpoint": "http://localhost:11434/v1",
            "api_key_env": None,
            "local": True,
            "model": "qwen2.5-coder:7b",
        }
    )
    assert candidate.name == "ollama-qwen"
    assert candidate.endpoint == "http://localhost:11434/v1"
    assert candidate.api_key_env is None
    assert candidate.local is True
    assert candidate.model == "qwen2.5-coder:7b"


def test_candidate_schema_has_no_field_capable_of_holding_a_raw_secret():
    """R31 is enforced at the schema level: there is no `api_key` field at
    all, only `api_key_env` (a reference to an env var name)."""
    fields = DelegateCandidate.model_fields
    assert "api_key_env" in fields
    assert "api_key" not in fields


def test_load_delegate_config_parses_multiple_candidates():
    data = {
        "delegate": {
            "candidates": [
                {
                    "name": "local-ollama",
                    "endpoint": "http://localhost:11434/v1",
                    "local": True,
                    "model": "qwen2.5-coder:7b",
                },
                {
                    "name": "remote-kimi",
                    "endpoint": "https://api.example.com/v1",
                    "api_key_env": "JOSU_KIMI_API_KEY",
                    "local": False,
                    "model": "kimi-k2",
                },
            ]
        }
    }
    config, warnings = load_delegate_config(data)
    assert [c.name for c in config.candidates] == ["local-ollama", "remote-kimi"]
    assert config.candidates[1].local is False


def test_missing_delegate_section_yields_empty_candidate_list():
    config, warnings = load_delegate_config({})
    assert config.candidates == []
    assert warnings == []


def test_unset_api_key_env_produces_warning_without_crashing(monkeypatch):
    monkeypatch.delenv("JOSU_TEST_UNSET_KEY", raising=False)
    data = {
        "delegate": {
            "candidates": [
                {
                    "name": "remote-candidate",
                    "endpoint": "https://api.example.com/v1",
                    "api_key_env": "JOSU_TEST_UNSET_KEY",
                    "local": False,
                    "model": "some-model",
                },
            ]
        }
    }
    config, warnings = load_delegate_config(data)
    assert len(config.candidates) == 1
    assert len(warnings) == 1
    assert "remote-candidate" in warnings[0]
    assert "JOSU_TEST_UNSET_KEY" in warnings[0]


def test_set_api_key_env_produces_no_warning(monkeypatch):
    monkeypatch.setenv("JOSU_TEST_SET_KEY", "sk-does-not-matter-here")
    data = {
        "delegate": {
            "candidates": [
                {
                    "name": "remote-candidate",
                    "endpoint": "https://api.example.com/v1",
                    "api_key_env": "JOSU_TEST_SET_KEY",
                    "local": False,
                    "model": "some-model",
                },
            ]
        }
    }
    config, warnings = load_delegate_config(data)
    assert warnings == []


def test_unset_api_key_env_warning_never_includes_the_secret_value(monkeypatch):
    monkeypatch.setenv("JOSU_TEST_ANOTHER_KEY", "sk-must-not-appear-in-warning")
    monkeypatch.delenv("JOSU_TEST_UNSET_KEY_2", raising=False)
    data = {
        "delegate": {
            "candidates": [
                {
                    "name": "remote-candidate",
                    "endpoint": "https://api.example.com/v1",
                    "api_key_env": "JOSU_TEST_UNSET_KEY_2",
                    "local": False,
                    "model": "some-model",
                },
            ]
        }
    }
    _, warnings = load_delegate_config(data)
    joined = " ".join(warnings)
    assert "sk-must-not-appear-in-warning" not in joined


def test_candidate_missing_required_field_raises():
    with pytest.raises(Exception):
        DelegateCandidate.model_validate({"name": "incomplete", "endpoint": "http://x"})


def test_one_bad_candidate_entry_does_not_crash_the_whole_load():
    """Mirrors config/orchestrator.py's convention: a single malformed
    `[[delegate.candidates]]` entry is excluded and warned about, not a
    whole-`[delegate]`-section crash -- matching this module's own
    docstring claim that a bad candidate degrades only that candidate."""
    data = {
        "delegate": {
            "candidates": [
                {
                    "name": "good-candidate",
                    "endpoint": "http://localhost:11434/v1",
                    "local": True,
                    "model": "qwen2.5-coder:7b",
                },
                {
                    # Missing required fields (`local`, `model`) -- malformed.
                    "name": "bad-candidate",
                    "endpoint": "http://x",
                },
            ]
        }
    }
    config, warnings = load_delegate_config(data)

    assert [c.name for c in config.candidates] == ["good-candidate"]
    assert len(warnings) == 1
    assert "bad-candidate" in warnings[0]
