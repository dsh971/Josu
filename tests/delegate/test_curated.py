"""Tests for the curated model registry and pre-flight resource check (U2, R26)."""

from __future__ import annotations

from josu.models.curated import preflight_check


def test_curated_model_within_available_ram_passes():
    result = preflight_check("qwen2.5-coder:7b", available_ram_gb=16.0)
    assert result.ok
    assert result.curated
    assert result.reason is None


def test_curated_model_exceeding_available_ram_is_refused():
    result = preflight_check("qwen2.5-coder:7b", available_ram_gb=2.0)
    assert not result.ok
    assert result.curated
    assert "GB" in result.reason


def test_non_curated_model_is_allowed_but_flagged_untested():
    result = preflight_check("some-random-model:latest", available_ram_gb=64.0)
    assert result.ok
    assert not result.curated
    assert "untested" in result.reason
