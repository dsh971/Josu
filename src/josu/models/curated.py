"""Curated local model registry and pre-flight resource check (R2, R26).

Only qwen2.5-coder:7b is curated for v1 -- a single verified default rather
than a speculative tier list, expanded later once real delegate usage
surfaces where it falls short. Anything else is treated as the generic
OpenAI-compatible fallback (R2): allowed through, but flagged untested since
there's no known resource profile to check it against.
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class CuratedModel:
    name: str
    min_ram_gb: float
    description: str


CURATED_MODELS: dict[str, CuratedModel] = {
    "qwen2.5-coder:7b": CuratedModel(
        name="qwen2.5-coder:7b",
        # Observed ~3.1 GB resident (llama-server RSS) serving a single request;
        # 5.0 GB leaves headroom for KV cache growth under a longer context.
        min_ram_gb=5.0,
        description="7B coding model, curated default for bounded delegate tasks.",
    ),
}


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    model: str
    curated: bool
    reason: str | None = None


def preflight_check(model: str, available_ram_gb: float | None = None) -> PreflightResult:
    """Estimate whether `model` fits available RAM before any load is attempted (R26)."""
    if available_ram_gb is None:
        available_ram_gb = psutil.virtual_memory().available / (1024**3)

    curated = CURATED_MODELS.get(model)
    if curated is None:
        return PreflightResult(
            ok=True,
            model=model,
            curated=False,
            reason=(
                f"{model} is not in the curated list -- using the generic "
                "OpenAI-compatible adapter (untested)"
            ),
        )

    if curated.min_ram_gb > available_ram_gb:
        return PreflightResult(
            ok=False,
            model=model,
            curated=True,
            reason=(
                f"{model} needs ~{curated.min_ram_gb:.1f} GB RAM, only "
                f"{available_ram_gb:.1f} GB available"
            ),
        )
    return PreflightResult(ok=True, model=model, curated=True, reason=None)
