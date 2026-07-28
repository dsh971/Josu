"""Tests for the whole-run wall-clock circuit breaker (U5, R23).

Two kinds of test here, deliberately kept apart:

- The queued-vs-executing exclusion contract (`pause()`/`resume()`) is
  tested with an injected fake clock -- deterministic, no real sleeping,
  matching how a pure-logic timing contract should be tested (a real-time
  sleep-based version of this test would be slow and flaky).
- `run_under_circuit_breaker()`'s actual "stops and is surfaced" behavior
  is tested against a REAL subprocess (a real `python3 -c "..."` process
  that sleeps past the budget), matching this repo's "real integration
  where possible, hand-written fakes only at the true I/O boundary"
  convention (see e.g. `test_worktree.py`, `test_adapter.py`) -- no mocking
  of `subprocess` itself.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from josu.orchestrator.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerTimeoutError,
    run_under_circuit_breaker,
)


class _FakeClock:
    """A settable fake clock -- `CircuitBreaker` accepts any
    zero-arg-callable `clock`, so tests can advance time deterministically
    without real sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- basic elapsed/timeout accounting -----------------------------------------


def test_elapsed_seconds_accumulates_while_running():
    clock = _FakeClock()
    breaker = CircuitBreaker(60, clock=clock)
    breaker.start()
    clock.advance(10)
    assert breaker.elapsed_seconds == pytest.approx(10.0)


def test_check_raises_once_active_elapsed_exceeds_timeout():
    clock = _FakeClock()
    breaker = CircuitBreaker(10, clock=clock)
    breaker.start()
    clock.advance(11)

    with pytest.raises(CircuitBreakerTimeoutError) as exc_info:
        breaker.check()
    assert exc_info.value.timeout_seconds == 10
    assert exc_info.value.elapsed_seconds == pytest.approx(11.0)


def test_check_does_not_raise_while_under_budget():
    clock = _FakeClock()
    breaker = CircuitBreaker(10, clock=clock)
    breaker.start()
    clock.advance(5)
    breaker.check()  # must not raise


def test_remaining_seconds_floors_at_zero():
    clock = _FakeClock()
    breaker = CircuitBreaker(10, clock=clock)
    breaker.start()
    clock.advance(50)
    assert breaker.remaining_seconds == 0.0


def test_timeout_seconds_must_be_positive():
    with pytest.raises(ValueError):
        CircuitBreaker(0)
    with pytest.raises(ValueError):
        CircuitBreaker(-5)


# --- queued-vs-executing exclusion (matches queue.py's own treatment) --------


def test_paused_time_does_not_count_toward_elapsed():
    """Covers: time spent blocked on a queued, not-yet-executing delegate
    call does not count toward this timeout -- here simulated directly via
    pause()/resume() rather than a real queue integration (out of scope for
    this unit; see module docstring)."""
    clock = _FakeClock()
    breaker = CircuitBreaker(20, clock=clock)

    breaker.start()
    clock.advance(5)  # 5s of real, active work

    breaker.pause()
    clock.advance(95)  # a long queued wait behind another delegate call
    breaker.resume()

    clock.advance(6)  # 6s more active work

    # Only the 5s + 6s of ACTIVE time counts -- the 95s "queued" interval is
    # excluded entirely, exactly like queue.py's own per-call timeout only
    # starts once a call begins executing.
    assert breaker.elapsed_seconds == pytest.approx(11.0)
    breaker.check()  # 11s < 20s budget -- must not raise, even though
    # 5 + 95 + 6 = 106s of real wall-clock time has passed since start()


def test_a_run_that_would_trip_if_queued_time_counted_does_not_trip_when_excluded():
    clock = _FakeClock()
    breaker = CircuitBreaker(15, clock=clock)
    breaker.start()
    clock.advance(3)

    with breaker.excluded():
        clock.advance(1000)  # a huge queued wait -- must not count

    clock.advance(3)
    breaker.check()  # 6s active < 15s budget -- must not raise
    assert breaker.elapsed_seconds == pytest.approx(6.0)


def test_pause_is_idempotent():
    clock = _FakeClock()
    breaker = CircuitBreaker(20, clock=clock)
    breaker.start()
    clock.advance(2)
    breaker.pause()
    breaker.pause()  # a second pause() before resume() must not double-count
    clock.advance(100)
    breaker.resume()
    assert breaker.elapsed_seconds == pytest.approx(2.0)


def test_excluded_resumes_even_if_the_wrapped_block_raises():
    clock = _FakeClock()
    breaker = CircuitBreaker(20, clock=clock)
    breaker.start()

    with pytest.raises(RuntimeError):
        with breaker.excluded():
            clock.advance(1000)
            raise RuntimeError("candidate failed mid-queue-wait")

    clock.advance(1)
    assert breaker.elapsed_seconds == pytest.approx(1.0)


# --- run_under_circuit_breaker(): the whole-run enforcement path -------------


def _sleep_then_succeed_argv(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_run_under_circuit_breaker_returns_normally_when_fn_finishes_in_time():
    breaker = CircuitBreaker(30)

    def fn(timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(_sleep_then_succeed_argv(0.05), timeout=timeout, check=True)

    result = run_under_circuit_breaker(breaker, fn)
    assert result.returncode == 0


def test_a_run_exceeding_its_wall_clock_timeout_stops_and_is_surfaced():
    """Covers: a run exceeding its wall-clock timeout stops and is surfaced
    (R23) -- a real subprocess that would otherwise sleep for 5s is killed
    well before that once the (tiny) circuit-breaker budget is exhausted,
    and the caller sees a clearly-distinguishable CircuitBreakerTimeoutError
    rather than the run silently continuing or hanging."""
    breaker = CircuitBreaker(0.2)

    def fn(timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(_sleep_then_succeed_argv(5), timeout=timeout, check=True)

    with pytest.raises(CircuitBreakerTimeoutError) as exc_info:
        run_under_circuit_breaker(breaker, fn)

    assert exc_info.value.timeout_seconds == 0.2
    # Stopped well short of the fake run's full 5s sleep.
    assert exc_info.value.elapsed_seconds < 5.0


def test_run_under_circuit_breaker_checks_before_calling_fn_if_already_tripped():
    clock = _FakeClock()
    breaker = CircuitBreaker(5, clock=clock)
    breaker.start()
    clock.advance(10)  # already over budget before fn is ever called

    called = False

    def fn(timeout: float):
        nonlocal called
        called = True
        return None

    with pytest.raises(CircuitBreakerTimeoutError):
        run_under_circuit_breaker(breaker, fn)
    assert called is False
