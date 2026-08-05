"""Tests for per-candidate failure memory (U1,
feat/delegate-candidate-circuit-breaker plan).

Tested with an injected fake clock -- deterministic, no real sleeping,
matching `tests/orchestrator/test_circuit_breaker.py`'s convention for a
pure-logic timing contract.
"""

from __future__ import annotations

import pytest

from josu.delegate.cooldown import CandidateCooldownStore


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_fewer_than_threshold_failures_does_not_trip_cooldown():
    store = CandidateCooldownStore(failure_threshold=3, cooldown_seconds=30, clock=_FakeClock())
    store.record_failure("local-a")
    store.record_failure("local-a")
    assert store.is_in_cooldown("local-a") is False


def test_exactly_threshold_consecutive_failures_trips_cooldown():
    store = CandidateCooldownStore(failure_threshold=3, cooldown_seconds=30, clock=_FakeClock())
    for _ in range(3):
        store.record_failure("local-a")
    assert store.is_in_cooldown("local-a") is True


def test_cooldown_expires_after_configured_duration():
    clock = _FakeClock()
    store = CandidateCooldownStore(failure_threshold=2, cooldown_seconds=30, clock=clock)
    store.record_failure("local-a")
    store.record_failure("local-a")
    assert store.is_in_cooldown("local-a") is True

    clock.now += 30
    assert store.is_in_cooldown("local-a") is False


def test_single_failure_after_natural_expiry_does_not_immediately_retrip():
    """Code-review regression test: a candidate that recovers from a
    natural cooldown expiry, then fails once (with no intervening
    success), must NOT immediately re-trip off its stale pre-expiry
    failure count -- a fresh failure starts a new count toward another
    cooldown, matching the origin brainstorm's F2 flow."""
    clock = _FakeClock()
    store = CandidateCooldownStore(failure_threshold=3, cooldown_seconds=30, clock=clock)
    store.record_failure("local-a")
    store.record_failure("local-a")
    store.record_failure("local-a")
    assert store.is_in_cooldown("local-a") is True

    clock.now += 30
    assert store.is_in_cooldown("local-a") is False  # natural expiry, also resets health

    store.record_failure("local-a")
    assert store.is_in_cooldown("local-a") is False  # one fresh failure, not yet at threshold


def test_success_resets_consecutive_failure_count():
    store = CandidateCooldownStore(failure_threshold=3, cooldown_seconds=30, clock=_FakeClock())
    store.record_failure("local-a")
    store.record_failure("local-a")
    store.record_success("local-a")
    # Two more failures (not three) after the reset must not trip cooldown.
    store.record_failure("local-a")
    store.record_failure("local-a")
    assert store.is_in_cooldown("local-a") is False


def test_success_clears_an_in_progress_cooldown_immediately():
    store = CandidateCooldownStore(failure_threshold=2, cooldown_seconds=30, clock=_FakeClock())
    store.record_failure("local-a")
    store.record_failure("local-a")
    assert store.is_in_cooldown("local-a") is True

    store.record_success("local-a")
    assert store.is_in_cooldown("local-a") is False


def test_a_candidate_never_seen_is_healthy_by_default():
    store = CandidateCooldownStore(failure_threshold=1, cooldown_seconds=30, clock=_FakeClock())
    assert store.is_in_cooldown("never-seen") is False


def test_candidates_are_tracked_independently():
    store = CandidateCooldownStore(failure_threshold=2, cooldown_seconds=30, clock=_FakeClock())
    store.record_failure("local-a")
    store.record_failure("local-a")
    store.record_failure("remote-b")
    assert store.is_in_cooldown("local-a") is True
    assert store.is_in_cooldown("remote-b") is False


def test_non_positive_failure_threshold_raises():
    with pytest.raises(ValueError):
        CandidateCooldownStore(failure_threshold=0, cooldown_seconds=30)
    with pytest.raises(ValueError):
        CandidateCooldownStore(failure_threshold=-1, cooldown_seconds=30)


def test_non_positive_cooldown_seconds_raises():
    with pytest.raises(ValueError):
        CandidateCooldownStore(failure_threshold=3, cooldown_seconds=0)
    with pytest.raises(ValueError):
        CandidateCooldownStore(failure_threshold=3, cooldown_seconds=-1)
