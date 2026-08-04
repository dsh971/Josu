"""Wall-clock timeout on the whole Claude Code run (U5, R23).

Distinct from `delegate/queue.py`'s per-delegate-call timeout (U11/U12):
that timeout bounds ONE delegate call's own execution once it begins
running, not the orchestrator's overall run, and explicitly starts only
once the call begins executing -- time spent queued behind another call
never counts against it (see `queue.py`'s module docstring; also the
plan's Key Technical Decisions: "Both timeout clocks start when a call
begins executing, not while queued").

`CircuitBreaker` mirrors that SAME queued-vs-executing distinction one
level up, for the orchestrator's own wall-clock budget: its timer is
pausable, so a caller that knows its run is currently blocked on a queued
(not yet executing) delegate call -- including a multi-candidate chain
attempt (U12) -- can exclude that wait from counting toward this timeout,
via `pause()`/`resume()` (or the `excluded()` context-manager form). This
module has no import dependency on `delegate/` itself (out of scope for
this unit, per the plan's file list); `pause()`/`resume()` are the generic
hook a future integration (e.g. the daemon signaling queue-wait) would call
-- this module only guarantees the mechanism behaves correctly when paused,
not that anything currently calls `pause()` for a real queued delegate wait.

`run_under_circuit_breaker()` is the entry point for the orchestrator's own
use: run the WHOLE Claude Code invocation (e.g. a thin wrapper around
`adapters.claude_code.run(...)`) with `breaker.remaining_seconds` threaded
through as that invocation's own `timeout` -- reusing `adapter.py`'s
existing `subprocess.run(..., timeout=...)` mechanism (which already
cleanly terminates the child process on expiry) rather than reinventing
process termination here. A `subprocess.TimeoutExpired` raised from that
call is translated into `CircuitBreakerTimeoutError`, a distinguishable
"stopped and surfaced" outcome (R23), not a bare subprocess exception left
for the caller to interpret.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import TypeVar

T = TypeVar("T")


class CircuitBreakerTimeoutError(RuntimeError):
    """Raised when a run's ACTIVE (non-paused) wall-clock time exceeds its
    configured timeout -- the "stops and is surfaced" outcome R23 requires,
    distinguishable from a generic timeout so a caller (U6's future run log)
    can record which limit tripped."""

    def __init__(self, timeout_seconds: float, elapsed_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            f"circuit breaker tripped: run exceeded its {timeout_seconds}s wall-clock "
            f"timeout ({elapsed_seconds:.3f}s of active run time elapsed)"
        )


class CircuitBreaker:
    """A pausable wall-clock stopwatch bounding one Claude Code run.

    `elapsed_seconds` only accumulates while the breaker is running
    (started and not paused) -- time spent `pause()`d is never counted
    (see module docstring). Uses `time.monotonic()` by default, never
    wall-clock `time.time()`, so a system clock adjustment mid-run can't
    perturb the measurement; a `clock` callable can be substituted (tests
    use this for deterministic, non-sleeping assertions).
    """

    def __init__(
        self,
        timeout_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds}")
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._accumulated: float = 0.0
        self._segment_start: float | None = None
        self._paused = False

    def start(self) -> None:
        """Begin timing. Idempotent -- a no-op if already running or
        currently paused (call `resume()` to come back from a pause)."""
        if self._segment_start is None and not self._paused:
            self._segment_start = self._clock()

    def pause(self) -> None:
        """Stop counting elapsed time until `resume()` -- for a caller that
        knows its run is currently blocked on a queued, not-yet-executing
        delegate call. A no-op if already paused or never started."""
        if self._paused or self._segment_start is None:
            self._paused = True
            return
        self._accumulated += self._clock() - self._segment_start
        self._segment_start = None
        self._paused = True

    def resume(self) -> None:
        """Resume counting after `pause()`. A no-op (re-starts a fresh
        segment) even if called without a prior `pause()`."""
        self._paused = False
        self._segment_start = self._clock()

    @contextmanager
    def excluded(self):
        """Context-manager form of `pause()`/`resume()` around a block of
        code known to be queued-wait time, not active run time -- e.g.
        wrapping a delegate call while it sits behind another in
        `DelegateQueue`."""
        self.pause()
        try:
            yield
        finally:
            self.resume()

    @property
    def elapsed_seconds(self) -> float:
        """Total ACTIVE (non-paused) elapsed time so far."""
        total = self._accumulated
        if self._segment_start is not None and not self._paused:
            total += self._clock() - self._segment_start
        return total

    @property
    def remaining_seconds(self) -> float:
        """`timeout_seconds` minus active elapsed time so far, floored at 0."""
        return max(0.0, self.timeout_seconds - self.elapsed_seconds)

    def check(self) -> None:
        """Raise `CircuitBreakerTimeoutError` if active elapsed time has
        already exceeded the configured timeout."""
        elapsed = self.elapsed_seconds
        if elapsed > self.timeout_seconds:
            raise CircuitBreakerTimeoutError(self.timeout_seconds, elapsed)


def run_under_circuit_breaker(breaker: CircuitBreaker, fn: Callable[[float], T]) -> T:
    """Run `fn` -- typically a thin wrapper around the whole Claude Code
    invocation, e.g. `lambda timeout: claude_code.run(..., timeout=timeout)`
    -- bounded by `breaker`'s remaining wall-clock budget (R23).

    Starts `breaker` if not already running, checks it hasn't already
    tripped, then calls `fn(breaker.remaining_seconds)` -- `fn` is
    responsible for actually enforcing that value as its own timeout
    (typically by passing it straight through to `subprocess.run(...,
    timeout=...)`, which is what `adapter.py`'s `invoke_adapter()` already
    does). A `subprocess.TimeoutExpired` raised out of `fn` is caught here
    and re-raised as `CircuitBreakerTimeoutError` -- the run stops (the
    subprocess has already been killed by `subprocess.run`'s own timeout
    handling by the time this exception reaches here) and is surfaced as a
    clearly-distinguishable circuit-breaker trip, not a bare subprocess
    exception.
    """
    breaker.start()
    breaker.check()
    try:
        return fn(breaker.remaining_seconds)
    except subprocess.TimeoutExpired as exc:
        raise CircuitBreakerTimeoutError(breaker.timeout_seconds, breaker.elapsed_seconds) from exc
