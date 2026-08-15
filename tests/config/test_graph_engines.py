"""Tests for the graph-engine connection-target TOML schema (U1)."""

from __future__ import annotations

import pytest

from josu.config.graph_engines import GraphEngineTargetConfig, load_graph_engines_config


def test_engine_schema_validates_all_fields():
    engine = GraphEngineTargetConfig.model_validate(
        {
            "name": "gortex",
            "host": "127.0.0.1",
            "port": 7411,
            "api_key_env": None,
        }
    )
    assert engine.name == "gortex"
    assert engine.host == "127.0.0.1"
    assert engine.port == 7411
    assert engine.api_key_env is None


def test_load_graph_engines_config_parses_single_entry():
    data = {
        "graph": {
            "engines": [
                {"name": "gortex", "host": "127.0.0.1", "port": 7411},
            ]
        }
    }
    config, warnings = load_graph_engines_config(data)
    assert [e.name for e in config.engines] == ["gortex"]
    assert warnings == []


def test_missing_graph_section_yields_empty_engine_list():
    config, warnings = load_graph_engines_config({})
    assert config.engines == []
    assert warnings == []


def test_engine_missing_required_field_is_dropped_with_warning():
    data = {
        "graph": {
            "engines": [
                {"name": "bad-engine", "host": "127.0.0.1"},  # missing port
            ]
        }
    }
    config, warnings = load_graph_engines_config(data)
    assert config.engines == []
    assert len(warnings) == 1
    assert "bad-engine" in warnings[0]


def test_one_bad_engine_entry_does_not_crash_the_whole_load():
    data = {
        "graph": {
            "engines": [
                {"name": "good-engine", "host": "127.0.0.1", "port": 7411},
                {"name": "bad-engine", "host": "127.0.0.1"},  # missing port
            ]
        }
    }
    config, warnings = load_graph_engines_config(data)
    assert [e.name for e in config.engines] == ["good-engine"]
    assert len(warnings) == 1
    assert "bad-engine" in warnings[0]


def test_unset_api_key_env_produces_warning_without_crashing(monkeypatch):
    monkeypatch.delenv("JOSU_TEST_UNSET_GRAPH_KEY", raising=False)
    data = {
        "graph": {
            "engines": [
                {
                    "name": "gortex",
                    "host": "127.0.0.1",
                    "port": 7411,
                    "api_key_env": "JOSU_TEST_UNSET_GRAPH_KEY",
                },
            ]
        }
    }
    config, warnings = load_graph_engines_config(data)
    assert len(config.engines) == 1
    assert len(warnings) == 1
    assert "gortex" in warnings[0]
    assert "JOSU_TEST_UNSET_GRAPH_KEY" in warnings[0]


def test_set_api_key_env_on_loopback_host_produces_no_warning(monkeypatch):
    monkeypatch.setenv("JOSU_TEST_SET_GRAPH_KEY", "token-does-not-matter-here")
    data = {
        "graph": {
            "engines": [
                {
                    "name": "gortex",
                    "host": "127.0.0.1",
                    "port": 7411,
                    "api_key_env": "JOSU_TEST_SET_GRAPH_KEY",
                },
            ]
        }
    }
    _, warnings = load_graph_engines_config(data)
    assert warnings == []


def test_multiple_entries_only_first_used_and_extras_named_in_warning():
    data = {
        "graph": {
            "engines": [
                {"name": "primary", "host": "127.0.0.1", "port": 7411},
                {"name": "secondary", "host": "127.0.0.1", "port": 7412},
            ]
        }
    }
    config, warnings = load_graph_engines_config(data)
    assert [e.name for e in config.engines] == ["primary", "secondary"]
    assert len(warnings) == 1
    assert "primary" in warnings[0]
    assert "secondary" in warnings[0]


def test_non_loopback_host_with_api_key_env_warns_about_cleartext(monkeypatch):
    monkeypatch.setenv("JOSU_TEST_REMOTE_KEY", "token-does-not-matter-here")
    data = {
        "graph": {
            "engines": [
                {
                    "name": "shared-gortex",
                    "host": "gortex.internal.example.com",
                    "port": 7411,
                    "api_key_env": "JOSU_TEST_REMOTE_KEY",
                },
            ]
        }
    }
    _, warnings = load_graph_engines_config(data)
    assert len(warnings) == 1
    assert "shared-gortex" in warnings[0]
    assert "cleartext" in warnings[0]


def test_non_loopback_host_without_api_key_env_produces_no_cleartext_warning():
    data = {
        "graph": {
            "engines": [
                {
                    "name": "shared-gortex",
                    "host": "gortex.internal.example.com",
                    "port": 7411,
                },
            ]
        }
    }
    _, warnings = load_graph_engines_config(data)
    assert warnings == []


def test_engine_schema_missing_required_field_raises():
    with pytest.raises(Exception):
        GraphEngineTargetConfig.model_validate({"name": "incomplete", "host": "127.0.0.1"})
