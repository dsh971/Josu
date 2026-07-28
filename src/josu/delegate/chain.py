"""Fallback-chain execution (U12, R32-R34, R24 revised, R26/R35).

Resolves a task type to its ordered candidate chain (`config/chains.py`,
U3) and tries each candidate in sequence through `queue.py`'s `run_chain()`
(U12), which holds ONE lock acquisition across the whole ordered sequence --
see that module's docstring for why.

Two, deliberately separate, retry layers meet here and must stay separate:

- R34 (chain-advance): `DelegateUnreachableError`, `DelegateRateLimitedError`,
  and `DelegateAPIError` (all from `client.py`) mean the candidate itself is
  unusable right now -- `execute_chain` advances to the next candidate in
  the chain before surfacing anything, exhausting the chain only as a last
  resort. A per-candidate `preflight_check` (R26) failure for a `local=True`
  candidate is treated the same way (the candidate can't even be attempted),
  advancing without ever building a client or making a network call for it.
- R24 (same-candidate retry): a `DelegateMalformedResponseError` means the
  candidate IS reachable and functioning, it just returned garbage once.
  `local_model.py`'s `delegate()` already retries once against the SAME
  candidate before raising that exception -- this module never adds a
  second retry on top of that. But once `delegate()`'s own retry is
  exhausted and the exception reaches `execute_chain`, it is treated exactly
  like the R34 conditions above: the candidate is unusable right now, so the
  chain advances to the next candidate rather than failing the whole
  delegation. The two retry layers stay distinct -- same-candidate retry
  lives entirely inside `delegate()` and is never duplicated here -- but
  what happens AFTER that retry is exhausted is unified with R34's
  chain-advance behavior, per the plan's flowchart (`C -->|malformed
  response| E[retry same candidate once]`, `E -->|still malformed| D[advance
  to next candidate]`).

When every candidate in a resolved chain is exhausted -- via the R34
conditions, a malformed-response retry exhaustion, or any mix of the two --
`execute_chain` raises `ChainExhaustedError` rather than re-raising the last
candidate's own exception directly -- a distinguishable "the whole chain
failed" signal (not a generic error) that U6's future run log and the MCP
tool description's chain-exhausted-fallback instruction (U3) can act on.

This module has no dependency on MCP -- `execute_chain` is a plain async
function over already-resolved config objects and a `DelegateQueue`
instance, callable directly by `server.py`'s `call_tool` (U12) and, later,
U7's quota-fallback path and U9's proactive checks, both of which call it
without going through Claude Code or the MCP transport at all.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from josu.config.chains import ChainsConfig, resolve_chain
from josu.config.delegate import DelegateCandidate
from josu.delegate.client import (
    DelegateAPIError,
    DelegateClient,
    DelegateMalformedResponseError,
    DelegateRateLimitedError,
    DelegateUnreachableError,
    OpenAICompatibleDelegateClient,
)
from josu.delegate.local_model import DEFAULT_TIMEOUT_SECONDS, DelegateResult, delegate
from josu.delegate.queue import DelegateQueue
from josu.graph.engine import GraphEngine
from josu.models.curated import preflight_check

# The exception types that mean "this candidate failed, try the next one"
# (R34). `DelegateMalformedResponseError` IS included here: R24's
# same-candidate retry is handled entirely inside `delegate()` before this
# module ever sees it, but once that retry is exhausted and the exception
# reaches this module, it advances the chain exactly like the other three --
# see the module docstring and the plan's flowchart.
_ADVANCE_ON: tuple[type[BaseException], ...] = (
    DelegateUnreachableError,
    DelegateRateLimitedError,
    DelegateAPIError,
    DelegateMalformedResponseError,
    TimeoutError,
)

ClientFactory = Callable[[DelegateCandidate], DelegateClient]


class NoCandidatesError(Exception):
    """`task_type` resolved to an empty chain -- no matching
    `[[delegation.chains]]` entry, `stays_hosted = true`, or every
    configured candidate was filtered out (e.g. remote-only with
    `allow_remote` unset). Distinct from `ChainExhaustedError`: no attempt
    was ever made, there's nothing to have exhausted.
    """

    def __init__(self, task_type: str) -> None:
        self.task_type = task_type
        super().__init__(f"no delegate candidates configured for task_type {task_type!r}")


@dataclass(frozen=True)
class SkipRecord:
    """One candidate's R34-triggering failure, captured at the moment it's
    caught -- before `execute_chain` advances to the next candidate -- for
    later run-log use (U6). Never carries anything beyond the exception
    class name and (for rate limits) the non-sensitive `Retry-After` value;
    see `client.py`'s exception taxonomy for why nothing more sensitive is
    ever attached to these types in the first place.
    """

    candidate: str
    error: str
    retry_after: str | None = None


class ChainExhaustedError(Exception):
    """Every candidate in the resolved chain failed via the R34 path.

    Distinguishable from any single candidate's own exception so callers
    (the MCP tool result, U6's future run log) can tell "the whole chain is
    exhausted" apart from a generic failure, per R34/the plan's Key
    Technical Decisions.
    """

    def __init__(
        self,
        task_type: str,
        attempted: list[str],
        skip_records: list[SkipRecord],
        last_error: BaseException,
    ) -> None:
        self.task_type = task_type
        self.attempted = list(attempted)
        self.skip_records = list(skip_records)
        self.last_error = last_error
        super().__init__(
            f"delegate chain exhausted for task_type {task_type!r} after trying "
            f"{self.attempted} -- last error: {last_error}"
        )


def _default_client_factory(candidate: DelegateCandidate) -> DelegateClient:
    """Build the production `DelegateClient` for `candidate`, resolving its
    credential lazily (at call time, per `config/delegate.py`'s docstring)
    from the env var it *names* -- never storing or logging the resolved
    value itself. A referenced-but-unset env var resolves to `None`, which
    `OpenAICompatibleDelegateClient` sends as no `Authorization` header at
    all -- the upstream candidate then legitimately reports unauthorized,
    which is still just another `DelegateAPIError` this module's chain-
    advance logic already handles, rather than a special case here.
    """
    api_key = os.environ.get(candidate.api_key_env) if candidate.api_key_env else None
    return OpenAICompatibleDelegateClient(candidate.endpoint, name=candidate.name, api_key=api_key)


async def execute_chain(
    task_type: str,
    task: Any,
    scope: Any = None,
    *,
    chains_config: ChainsConfig,
    registry: Mapping[str, DelegateCandidate],
    queue: DelegateQueue,
    graph_engine: GraphEngine | None = None,
    scope_root: Path | None = None,
    client_factory: ClientFactory | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    available_ram_gb: float | None = None,
) -> DelegateResult:
    """Resolve `task_type`'s fallback chain and try each candidate in order.

    Raises `NoCandidatesError` if the chain resolves empty, or `ChainExhaustedError`
    if every candidate fails -- whether via the R34 conditions (unreachable,
    rate-limited, API error), a candidate's own R24 same-candidate retry
    (inside `delegate()`) being exhausted, or any mix of the two across the
    chain (see module docstring).
    """
    candidates = resolve_chain(task_type, chains_config, registry)
    if not candidates:
        raise NoCandidatesError(task_type)

    factory = client_factory or _default_client_factory
    attempted: list[str] = []
    skip_records: list[SkipRecord] = []

    def _make_attempt(candidate: DelegateCandidate) -> Callable[[], Awaitable[DelegateResult]]:
        async def _attempt() -> DelegateResult:
            attempted.append(candidate.name)

            if candidate.local:
                preflight = preflight_check(candidate.model, available_ram_gb=available_ram_gb)
                if not preflight.ok:
                    exc = DelegateUnreachableError(
                        candidate.name, preflight.reason or "preflight check failed"
                    )
                    skip_records.append(SkipRecord(candidate=candidate.name, error=type(exc).__name__))
                    raise exc

            client = factory(candidate)
            try:
                return await delegate(
                    task,
                    scope,
                    model=candidate.model,
                    graph_engine=graph_engine,
                    scope_root=scope_root,
                    client=client,
                    timeout=timeout,
                )
            except DelegateRateLimitedError as exc:
                # R34's Retry-After capture: recorded here, before this
                # attempt's exception propagates up into run_chain()'s
                # advance-to-next-candidate handling.
                skip_records.append(
                    SkipRecord(
                        candidate=candidate.name,
                        error=type(exc).__name__,
                        retry_after=exc.retry_after,
                    )
                )
                raise
            except (DelegateUnreachableError, DelegateAPIError) as exc:
                skip_records.append(SkipRecord(candidate=candidate.name, error=type(exc).__name__))
                raise
            except DelegateMalformedResponseError as exc:
                # `delegate()` already ran R24's one-retry-same-candidate
                # before raising this -- no second retry is added here. Once
                # it reaches this point the candidate is treated as failed
                # for chain-advance purposes, same as the R34 conditions
                # above: recorded and re-raised so `run_chain()`'s
                # `advance_on` handling moves on to the next candidate.
                skip_records.append(SkipRecord(candidate=candidate.name, error=type(exc).__name__))
                raise

        return _attempt

    attempts: list[tuple[Callable[[], Awaitable[DelegateResult]], float]] = [
        (_make_attempt(candidate), timeout) for candidate in candidates
    ]

    try:
        return await queue.run_chain(attempts, advance_on=_ADVANCE_ON)
    except _ADVANCE_ON as exc:
        raise ChainExhaustedError(task_type, attempted, skip_records, exc) from exc
