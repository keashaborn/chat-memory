from __future__ import annotations

"""Single fail-closed startup contract for the successor-only candidate.

Absence, ``legacy``, and every value other than the exact successor identifier
refuse startup.  The previous commit remains the rollback artifact; this tree
does not contain a switch back into legacy Memory.
"""

from enum import Enum
import os
from typing import Mapping


EXCLUSIVE_MODE_ENV = "GOVERNED_MEMORY_EXCLUSIVE_MODE"


class ExclusiveMemoryMode(str, Enum):
    SUCCESSOR_PILOT = "successor_pilot"


class ExclusiveMemoryConfigurationError(RuntimeError):
    pass


def exclusive_memory_mode(
    environ: Mapping[str, str] | None = None,
) -> ExclusiveMemoryMode:
    values = os.environ if environ is None else environ
    raw = values.get(EXCLUSIVE_MODE_ENV)
    try:
        return ExclusiveMemoryMode(raw)
    except ValueError as exc:
        raise ExclusiveMemoryConfigurationError(
            "invalid_governed_memory_exclusive_mode"
        ) from exc


def successor_pilot_is_exclusive(
    environ: Mapping[str, str] | None = None,
) -> bool:
    return exclusive_memory_mode(environ) is ExclusiveMemoryMode.SUCCESSOR_PILOT


def legacy_memory_surfaces_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    exclusive_memory_mode(environ)
    return False


__all__ = [
    "EXCLUSIVE_MODE_ENV",
    "ExclusiveMemoryConfigurationError",
    "ExclusiveMemoryMode",
    "exclusive_memory_mode",
    "legacy_memory_surfaces_enabled",
    "successor_pilot_is_exclusive",
]
