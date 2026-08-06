"""Shared helper for rendering a pydantic `ValidationError` into a
warning-safe string (code-review fix, feat/cli-ease-of-use plan).

`str(exc)`/`f"...{exc}"` on a raw `ValidationError` embeds pydantic's own
`input_value` repr for "missing field" and type-mismatch errors -- the
*entire* input mapping, including sibling fields the schema never asked
about. A `[[delegate.candidates]]` entry with a typo'd `api_key` field
(instead of the schema's actual `api_key_env: str | None`) that's ALSO
missing a genuinely required field (e.g. `endpoint`) triggers exactly this
path: pydantic's default rendering echoes the typo'd field's raw value --
a real secret, in the case of a mistyped credential field -- straight into
a warning string. Before U4 (feat/cli-ease-of-use plan) started actually
printing `config.warnings` anywhere, this was a latent, never-observed
risk; U4 makes it a real one, so this helper closes the gap U4 opened.

A leaf module with no dependency on `config/delegate.py`, `config/chains.py`,
or `config/orchestrator.py` -- those three import this, and `config/
__init__.py` imports all three, so this module living inside `config/
__init__.py` itself would be circular.
"""

from __future__ import annotations

from pydantic import ValidationError


def safe_validation_error_detail(exc: ValidationError) -> str:
    """Render `exc` as a warning-safe, human-readable detail string --
    field path + reason for each error, never pydantic's raw `input_value`
    (which can echo back sibling fields the schema didn't ask about,
    including a mistyped credential value). A hand-written `raise
    ValueError(...)` inside a `@field_validator` (e.g. the allowlist and
    declarable-output-mode checks in `config/orchestrator.py`) still comes
    through in full -- only pydantic's own opaque `input_value` dump is
    suppressed, not messages this codebase authored itself."""
    details = exc.errors(include_url=False, include_input=False)
    return "; ".join(f"{'.'.join(str(part) for part in d['loc'])}: {d['msg']}" for d in details)
