"""Delegation-guide task-type categories and fallback-chain schema
(U3, R5-R7, R32-R34, R39).

Loaded from a `[[delegation.chains]]` TOML section via stdlib `tomllib` and
validated with a plain `pydantic.BaseModel`, mirroring U11's
`config/delegate.py` TOML+pydantic pattern in its own module rather than
sharing one (chains and candidates are separate concerns that happen to
compose, not one schema).

This module only builds and resolves chains -- it does not call the
delegate worker and is not wired into `delegate_to_local`'s MCP call path
(that's U12's job, once `task_type` is threaded through the tool schema).
Everything here is a pure function/class over already-parsed config data
plus a candidate registry (U11's `DelegateConfig.candidates`), so it's
directly unit-testable without a daemon, queue, or MCP harness.

Two, deliberately separate, chain-building code paths exist:

- `resolve_chain()` -- the general per-task-type resolver. Free-local-first
  ordering (R33) is the *default*; a chain can opt into an explicit,
  developer-authored order via `explicit_order = true` (R6). A remote
  candidate is excluded from the result unless the top-level `allow_remote`
  flag is set (Key Technical Decision -- a structural safeguard, not a
  per-chain setting, since the risk it guards against is "any remote
  candidate configured at all", not "this one chain"). A chain can also set
  `stays_hosted = true` to force the sub-task to stay with the hosted
  orchestrator regardless of what candidates are otherwise configured for
  it (R7 -- the developer's explicit override of the guide's default).
- `resolve_proactive_check_chain()` -- a distinct function, not a runtime
  filter layered on `resolve_chain()`, restricted to `local=True`
  candidates *by construction*. It never returns a remote candidate no
  matter what `allow_remote` is set to or how the chain is ordered in TOML
  (R39): F3 fires on every commit/save, so an unbounded fallback to a paid
  remote candidate on that cadence would produce real, unpredictable cost.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel

from josu.config.delegate import DelegateCandidate

# --- Starting v1 task-type categories (origin doc's delegation guide, R5) ---
#
# These are the guide's *defaults* (R6) -- names a developer's own
# `[[delegation.chains]]` entries are expected to use, and that
# `CLAUDE.md.template` documents in prose. They are not independently
# enforced by `resolve_chain()`: a task type with no matching chain simply
# resolves to an empty candidate list (nothing to delegate to), and a
# developer can still author a chain for a "stays-with-hosted" category if
# they choose to -- the guide is overridable (R6), not a hard-coded gate.
# The one hard, code-level override is `DelegationChain.stays_hosted` (R7).

DELEGABLE_TASK_TYPES = frozenset(
    {
        "file_summarization",
        "directory_summarization",
        "boilerplate_scaffolding",
        "simple_search_extraction",
        "pattern_matched_test_generation",
    }
)

HOSTED_ONLY_TASK_TYPES = frozenset(
    {
        "complex_refactor",
        "architecture_decision",
        "ambiguous_requirements",
        "security_sensitive_change",
    }
)

# The proactive-check category (R15, R39) is distinct from both of the
# above -- it isn't a delegable *sub-task* of an orchestrator run at all,
# it's a standing local-only re-evaluation of the graph. Kept as its own
# name so it can never collide with a developer-defined task type in either
# set above.
PROACTIVE_CHECK_TASK_TYPE = "proactive_check"


class DelegationChain(BaseModel):
    """One `[[delegation.chains]]` TOML table.

    `candidates` names reference entries in `config/delegate.py`'s
    `DelegateConfig.candidates` registry by `name`, resolved by the caller
    of `resolve_chain()`/`resolve_proactive_check_chain()` -- this schema
    only stores the names, it doesn't validate them against a registry at
    parse time (the registry lives in a different TOML section, loaded by
    a different module).
    """

    task_type: str
    candidates: list[str] = []

    # R33: free-local-first is the default ordering; set this to preserve
    # `candidates`' TOML-authored order as-is instead.
    explicit_order: bool = False

    # R7: developer override forcing this task type to stay with the
    # hosted orchestrator, regardless of what candidates are listed above
    # or what the guide's default category (DELEGABLE_TASK_TYPES /
    # HOSTED_ONLY_TASK_TYPES) would otherwise suggest.
    stays_hosted: bool = False


class ChainsConfig(BaseModel):
    """The composed `[delegation]` section plus the top-level `allow_remote`
    opt-in.

    `allow_remote` is deliberately not nested under `[delegation]` in the
    source TOML -- see `load_chains_config()` -- it's a single, top-level,
    config-wide flag: a structural safeguard against ever silently sending
    task/scope/graph-context content off-machine, not a per-chain setting
    (Key Technical Decisions).
    """

    chains: list[DelegationChain] = []
    allow_remote: bool = False


def load_chains_config(data: dict) -> tuple[ChainsConfig, list[str]]:
    """Validate the `[delegation]` section (plus the top-level `allow_remote`
    flag) of an already-parsed TOML document.

    Returns `(config, warnings)`, mirroring `config/delegate.py`'s
    `load_delegate_config()` -- warnings never crash config loading.
    """
    section = data.get("delegation", {})
    merged = dict(section)
    merged["allow_remote"] = data.get("allow_remote", False)
    config = ChainsConfig.model_validate(merged)

    warnings: list[str] = []
    seen_task_types: set[str] = set()
    for chain in config.chains:
        if chain.task_type in seen_task_types:
            warnings.append(
                f"delegation chain for task_type {chain.task_type!r} is defined "
                "more than once; only the first matching entry is used"
            )
        seen_task_types.add(chain.task_type)

    return config, warnings


def default_chain_order(candidates: Sequence[DelegateCandidate]) -> list[DelegateCandidate]:
    """Free-local-first ordering (R33): local candidates before remote,
    preserving each group's relative order (a stable sort, so ties within
    "local" or "remote" keep their original TOML-authored relative order).
    """
    return sorted(candidates, key=lambda c: not c.local)


def _find_chain(chains_config: ChainsConfig, task_type: str) -> DelegationChain | None:
    for chain in chains_config.chains:
        if chain.task_type == task_type:
            return chain
    return None


def _lookup_candidates(
    names: Sequence[str], registry: Mapping[str, DelegateCandidate]
) -> list[DelegateCandidate]:
    return [registry[name] for name in names if name in registry]


def resolve_chain(
    task_type: str,
    chains_config: ChainsConfig,
    registry: Mapping[str, DelegateCandidate],
) -> list[DelegateCandidate]:
    """Resolve `task_type` to its ordered, filtered fallback chain.

    - No matching `[[delegation.chains]]` entry, or a chain with
      `stays_hosted = true`, resolves to an empty list (R7): the sub-task
      stays with the hosted orchestrator.
    - Ordering is free-local-first by default (R33) unless the chain sets
      `explicit_order = true`, in which case the TOML-authored `candidates`
      order is preserved as-is.
    - A remote candidate is excluded from the result unless
      `chains_config.allow_remote` is `True`, independent of that
      candidate's position in TOML (Key Technical Decision).
    """
    chain = _find_chain(chains_config, task_type)
    if chain is None or chain.stays_hosted:
        return []

    resolved = _lookup_candidates(chain.candidates, registry)

    if not chain.explicit_order:
        resolved = default_chain_order(resolved)

    if not chains_config.allow_remote:
        resolved = [c for c in resolved if c.local]

    return resolved


def resolve_proactive_check_chain(
    chains_config: ChainsConfig,
    registry: Mapping[str, DelegateCandidate],
) -> list[DelegateCandidate]:
    """Resolve the `proactive_check` category's chain.

    Local-only *by construction* (R39): filters to `local=True` candidates
    and never returns a remote one, regardless of `chains_config.allow_remote`
    or how the chain is ordered in TOML -- a distinct code path from
    `resolve_chain()`, not a runtime filter applied on top of it.
    """
    chain = _find_chain(chains_config, PROACTIVE_CHECK_TASK_TYPE)
    if chain is None:
        return []

    resolved = [c for c in _lookup_candidates(chain.candidates, registry) if c.local]

    if not chain.explicit_order:
        resolved = default_chain_order(resolved)

    return resolved
