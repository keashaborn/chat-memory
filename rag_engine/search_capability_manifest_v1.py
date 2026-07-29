from __future__ import annotations

"""Server-owned description of bounded research capabilities.

This contract describes what the authenticated application may ask its search
orchestrator to do. It does not claim that external search ran for the current
response and it contains no browser-controlled fields.
"""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


SEARCH_CAPABILITY_MANIFEST_VERSION = "search_capability_manifest_v1"
SEARCH_CAPABILITY_AUTHORITY = "seebx_search_plan_v1"
SEARCH_CAPABILITY_ROUTES = ("current_news", "trusted_health")
SEARCH_CAPABILITY_MODES = ("indexed", "live")
SEARCH_CAPABILITY_POLICY_PACKS = (
    "current_news",
    "health",
    "nutrition",
    "exercise",
    "software_security",
)

TEXT_SEARCH_AUTHORIZATION_BASIS = "supabase_fresh_web_search_v1"
VOICE_SEARCH_AUTHORIZATION_BASIS = "supabase_fresh_voice_lease_v1"

_MODEL_BRIEF = (
    "Server-mediated research is available for authorized requests through "
    "bounded current-news and trusted-evidence routes. The currently supported "
    "categories are only current news and recent events; trusted health and "
    "medical evidence; nutrition and food evidence; exercise and training "
    "evidence; and official software or cybersecurity references and current "
    "events. Do not imply support for other categories, "
    "general fact-checking, arbitrary page retrieval, or unrestricted "
    "browsing. The server, not the browser or model, selects the route, source "
    "policy, and budget. Do not claim that this system cannot search the web "
    "or consult sources merely because no direct browser tool is present. Do "
    "not claim that research ran for this response unless retrieved evidence "
    "and citations were actually supplied. For an unsupported or unavailable "
    "lookup, state the specific limitation without denying the supported "
    "server-mediated research capability."
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


class SearchCapabilityManifestV1(_StrictFrozenModel):
    contract_version: Literal[SEARCH_CAPABILITY_MANIFEST_VERSION] = (
        SEARCH_CAPABILITY_MANIFEST_VERSION
    )
    authority: Literal[SEARCH_CAPABILITY_AUTHORITY] = SEARCH_CAPABILITY_AUTHORITY
    authorization_basis: Literal[
        TEXT_SEARCH_AUTHORIZATION_BASIS,
        VOICE_SEARCH_AUTHORIZATION_BASIS,
    ]
    available_routes: tuple[
        Literal["current_news", "trusted_health"], ...
    ] = SEARCH_CAPABILITY_ROUTES
    available_modes: tuple[Literal["indexed", "live"], ...] = (
        SEARCH_CAPABILITY_MODES
    )
    policy_packs: tuple[
        Literal[
            "current_news",
            "health",
            "nutrition",
            "exercise",
            "software_security",
        ],
        ...,
    ] = SEARCH_CAPABILITY_POLICY_PACKS
    model_brief: Literal[_MODEL_BRIEF] = _MODEL_BRIEF
    manifest_sha256: str

    @field_validator("manifest_sha256")
    @classmethod
    def hash_shape(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("manifest_sha256 must be a lowercase SHA-256")
        return value

    @field_validator("available_routes")
    @classmethod
    def exact_routes(
        cls,
        value: tuple[Literal["current_news", "trusted_health"], ...],
    ) -> tuple[Literal["current_news", "trusted_health"], ...]:
        if value != SEARCH_CAPABILITY_ROUTES:
            raise ValueError("search capability routes differ from server policy")
        return value

    @field_validator("available_modes")
    @classmethod
    def exact_modes(
        cls,
        value: tuple[Literal["indexed", "live"], ...],
    ) -> tuple[Literal["indexed", "live"], ...]:
        if value != SEARCH_CAPABILITY_MODES:
            raise ValueError("search capability modes differ from server policy")
        return value

    @field_validator("policy_packs")
    @classmethod
    def exact_policy_packs(
        cls,
        value: tuple[
            Literal[
                "current_news",
                "health",
                "nutrition",
                "exercise",
                "software_security",
            ],
            ...,
        ],
    ) -> tuple[
        Literal[
            "current_news",
            "health",
            "nutrition",
            "exercise",
            "software_security",
        ],
        ...,
    ]:
        if value != SEARCH_CAPABILITY_POLICY_PACKS:
            raise ValueError("search capability policy packs differ from server policy")
        return value

    @model_validator(mode="after")
    def exact_manifest(self) -> "SearchCapabilityManifestV1":
        payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        if self.manifest_sha256 != _sha256(payload):
            raise ValueError("search capability manifest hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        authorization_basis: Literal[
            TEXT_SEARCH_AUTHORIZATION_BASIS,
            VOICE_SEARCH_AUTHORIZATION_BASIS,
        ],
    ) -> "SearchCapabilityManifestV1":
        payload = {
            "contract_version": SEARCH_CAPABILITY_MANIFEST_VERSION,
            "authority": SEARCH_CAPABILITY_AUTHORITY,
            "authorization_basis": authorization_basis,
            "available_routes": SEARCH_CAPABILITY_ROUTES,
            "available_modes": SEARCH_CAPABILITY_MODES,
            "policy_packs": SEARCH_CAPABILITY_POLICY_PACKS,
            "model_brief": _MODEL_BRIEF,
        }
        return cls(**payload, manifest_sha256=_sha256(payload))

    @classmethod
    def from_wire_json(
        cls,
        value: str | bytes,
    ) -> "SearchCapabilityManifestV1":
        try:
            return cls.model_validate_json(value)
        except Exception:
            raise ValueError("invalid search capability manifest wire") from None


__all__ = [
    "SEARCH_CAPABILITY_AUTHORITY",
    "SEARCH_CAPABILITY_MANIFEST_VERSION",
    "TEXT_SEARCH_AUTHORIZATION_BASIS",
    "VOICE_SEARCH_AUTHORIZATION_BASIS",
    "SearchCapabilityManifestV1",
]
