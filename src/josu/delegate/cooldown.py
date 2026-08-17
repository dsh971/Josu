"""Per-candidate failure memory for josu's delegate fallback chain (U1,
feat/delegate-candidate-circuit-breaker plan).

`orchestrator/circuit_breaker.py`'s `CircuitBreaker` bounds the WHOLE run's
wall-clock budget; this module is the per-candidate analog `delegate/
chain.py`'s `execute_chain()` was missing -- without it, a candidate that's
been failing repeatedly still gets attempted (and times out) on every
subsequent task before the chain advances, and since every delegate call is
serialized behind `delegate/queue.py`'s single lock, that wasted timeout is
lock-hold time every other queued caller waits behind.

Every method here is synchronous with no `await` inside. That is load-bearing,
not incidental: nothing in this module acquires a lock of its own, so its
"safe to read/write from a single-threaded asyncio event loop without a race"
property depends entirely on asyncio's cooperative scheduling never yielding
control mid-update. A future change that makes `record_failure()`/
`record_success()`/`is_in_cooldown()` `async def` with an `await` inside would
silently reopen a check-then-act race between concurrent callers -- do not
add one.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _CandidateHealth:
    """One candidate's live state: how many qualifying failures it has
    accumulated since its last success, and -- once that count reaches the
    store's `failure_threshold` -- the `clock()` timestamp its cooldown
    expires at. `cooldown_expiry` is `None` both before the candidate has
    ever tripped and after `record_success()` clears it early."""

    consecutive_failures: int = 0
    cooldown_expiry: float | None = None


class CandidateCooldownStore:
    """Tracks, per candidate name, whether it should be skipped right now.

    A candidate absent from the store (never seen, or cleared by a success)
    is implicitly healthy -- no seeding loop is needed at construction time.
    `clock` mirrors `orchestrator/circuit_breaker.py`'s `CircuitBreaker`
    constructor shape (`time.monotonic` in production, a fake clock in
    tests) for deterministic, non-sleeping cooldown-expiry assertions.
    """

    def __init__(
        self,
        failure_threshold: int,
        cooldown_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold <= 0:
            raise ValueError(f"failure_threshold must be positive, got {failure_threshold}")
        # `<= 0` alone doesn't reject `nan`/`inf` (code-review fix): NaN
        # comparisons are always False and `inf > 0` is True, so a `nan`
        # cooldown would make `is_in_cooldown()` always False (a silent
        # permanent no-op) and `inf` would make it always True once
        # tripped (permanently excluded until a restart). This constructor
        # is reachable directly, not only via `config/__init__.py`'s own
        # `math.isfinite()`-guarded loader, so it enforces the same
        # invariant itself rather than trusting every caller to.
        if not math.isfinite(cooldown_seconds) or cooldown_seconds <= 0:
            raise ValueError(f"cooldown_seconds must be a positive, finite number, got {cooldown_seconds}")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._health: dict[str, _CandidateHealth] = {}

    def record_failure(self, name: str) -> None:
        """One more qualifying failure for `name`. Once the running count
        reaches `failure_threshold`, sets a cooldown expiry `cooldown_seconds`
        from now -- `is_in_cooldown(name)` returns `True` until then."""
        health = self._health.setdefault(name, _CandidateHealth())
        health.consecutive_failures += 1
        if health.consecutive_failures >= self.failure_threshold:
            health.cooldown_expiry = self._clock() + self.cooldown_seconds

    def record_success(self, name: str) -> None:
        """A successful call to `name` resets its consecutive-failure count
        to zero and clears any in-progress cooldown immediately -- a
        candidate does not need to wait out a cooldown it has already
        demonstrated it can serve."""
        self._health[name] = _CandidateHealth()

    def is_in_cooldown(self, name: str) -> bool:
        """Whether `name` should be skipped right now. `False` for a
        candidate never seen (implicit healthy-by-default) or whose cooldown
        has elapsed -- callers do not need to call anything to "clear" an
        expired cooldown; this read alone treats it as over.

        A naturally-expired cooldown resets `name`'s health state entirely
        (code-review fix), not just the boolean this method returns.
        Without that reset, `consecutive_failures` stays at its stale
        pre-expiry value -- since only `record_success()` otherwise clears
        it -- so a single failure after recovery (with no intervening
        success) would immediately re-trip cooldown off the old count
        instead of starting a fresh one, contradicting "a fresh failure
        starts a new count toward another cooldown." Every call site
        (`chain.py`'s `_attempt()`) checks `is_in_cooldown()` before any
        `record_failure()` for the same attempt can fire, so this lazy
        reset-on-read always happens before it would matter.
        """
        health = self._health.get(name)
        if health is None or health.cooldown_expiry is None:
            return False
        if self._clock() < health.cooldown_expiry:
            return True
        del self._health[name]
        return False
