from __future__ import annotations

"""Single fail-closed switch for retiring every legacy Memory surface.

The default deliberately preserves the currently deployed runtime.  The
successor value is consumed only after an explicit service restart, at which
point legacy HTTP routers and compatibility writers are absent together.
"""

from enum import Enum
import os
from typing import Mapping


EXCLUSIVE_MODE_ENV = "GOVERNED_MEMORY_EXCLUSIVE_MODE"


class ExclusiveMemoryMode(str, Enum):
    LEGACY = "legacy"
    SUCCESSOR_PILOT = "successor_pilot"


class ExclusiveMemoryConfigurationError(RuntimeError):
    pass


def exclusive_memory_mode(
    environ: Mapping[str, str] | None = None,
) -> ExclusiveMemoryMode:
    values = os.environ if environ is None else environ
    raw = values.get(EXCLUSIVE_MODE_ENV, ExclusiveMemoryMode.LEGACY.value)
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
    return exclusive_memory_mode(environ) is ExclusiveMemoryMode.LEGACY


# Process-lifetime authority. Import fails closed on an invalid startup value;
# callers must not re-read mutable environment state per request.
EXCLUSIVE_MEMORY_MODE = exclusive_memory_mode()


__all__ = [
    "EXCLUSIVE_MODE_ENV",
    "EXCLUSIVE_MEMORY_MODE",
    "ExclusiveMemoryConfigurationError",
    "ExclusiveMemoryMode",
    "exclusive_memory_mode",
    "legacy_memory_surfaces_enabled",
    "successor_pilot_is_exclusive",
]
