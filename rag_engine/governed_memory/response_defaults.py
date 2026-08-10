from __future__ import annotations

"""Clean deterministic response defaults for the successor pilot."""

from dataclasses import dataclass
from typing import Literal


SUCCESSOR_RESPONSE_DEFAULTS_CONTRACT = (
    "governed_memory_successor_response_defaults_v1"
)


@dataclass(frozen=True, slots=True)
class SuccessorResponseDefaultsV1:
    contract_version: Literal[SUCCESSOR_RESPONSE_DEFAULTS_CONTRACT] = (
        SUCCESSOR_RESPONSE_DEFAULTS_CONTRACT
    )
    response_policy_overlay: None = None


SUCCESSOR_RESPONSE_DEFAULTS = SuccessorResponseDefaultsV1()


__all__ = [
    "SUCCESSOR_RESPONSE_DEFAULTS",
    "SUCCESSOR_RESPONSE_DEFAULTS_CONTRACT",
    "SuccessorResponseDefaultsV1",
]
