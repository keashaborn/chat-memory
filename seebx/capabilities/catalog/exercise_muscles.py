from __future__ import annotations

"""Versioned public contract for the canonical normalized muscle catalog."""

from collections.abc import Mapping, Sequence
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError


MUSCLE_CATALOG_CONTRACT_VERSION = "seebx_normalized_muscle_catalog_v1"
EXERCISE_MUSCLE_CONTRACT_VERSION = "seebx_exercise_muscle_profile_v1"
ROLE_ORDER = {"primary": 0, "secondary": 1, "stabilizer": 2}


class ExerciseMuscleContractError(RuntimeError):
    """Raised when canonical database rows cannot satisfy the public contract."""


class ExerciseMuscleNotFoundError(ExerciseMuscleContractError):
    """Raised when no public, active exercise matches the requested identity."""


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MuscleCatalogItemV1(_ContractModel):
    muscle_slug: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=160)
    parent_slug: str | None = Field(default=None, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    aliases: tuple[str, ...] = ()


class MuscleCatalogResponseV1(_ContractModel):
    contract_version: Literal["seebx_normalized_muscle_catalog_v1"] = (
        MUSCLE_CATALOG_CONTRACT_VERSION
    )
    locale: str = Field(min_length=2, max_length=10)
    query: str = Field(max_length=120)
    region: str = Field(max_length=80)
    muscles: tuple[MuscleCatalogItemV1, ...]


class ExerciseMuscleMappingV1(_ContractModel):
    muscle_slug: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=160)
    parent_slug: str | None = Field(default=None, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    aliases: tuple[str, ...] = ()
    role: Literal["primary", "secondary", "stabilizer"]
    weight: float = Field(ge=0.0, le=1.0)


class ExerciseMuscleProfileV1(_ContractModel):
    contract_version: Literal["seebx_exercise_muscle_profile_v1"] = (
        EXERCISE_MUSCLE_CONTRACT_VERSION
    )
    exercise_id: UUID
    exercise_slug: str = Field(min_length=1, max_length=160)
    exercise_display_name: str = Field(min_length=1, max_length=240)
    locale: str = Field(min_length=2, max_length=10)
    mapped: bool
    normalized_relationships_authoritative: Literal[True] = True
    legacy_arrays_authoritative: Literal[False] = False
    mappings: tuple[ExerciseMuscleMappingV1, ...]
    compatibility_primary_muscles: tuple[str, ...]
    compatibility_secondary_muscles: tuple[str, ...]


def _row_dict(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    try:
        return dict(row)
    except (TypeError, ValueError) as error:
        raise ExerciseMuscleContractError("canonical_row_invalid") from error


def _aliases(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExerciseMuscleContractError("canonical_aliases_invalid")
    cleaned: set[str] = set()
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise ExerciseMuscleContractError("canonical_alias_invalid")
        cleaned.add(raw.strip())
    return tuple(sorted(cleaned, key=lambda item: (item.casefold(), item)))


def build_muscle_catalog_response(
    rows: Sequence[Mapping[str, Any] | Any],
    *,
    query: str,
    region: str,
    locale: str,
) -> MuscleCatalogResponseV1:
    items: list[MuscleCatalogItemV1] = []
    seen: set[str] = set()
    try:
        for raw in rows:
            row = _row_dict(raw)
            slug = str(row.get("muscle_slug") or "")
            if not slug or slug in seen:
                raise ExerciseMuscleContractError("canonical_muscle_identity_invalid")
            seen.add(slug)
            items.append(
                MuscleCatalogItemV1(
                    muscle_slug=slug,
                    display_name=row.get("display_name"),
                    parent_slug=row.get("parent_slug"),
                    region=row.get("region"),
                    aliases=_aliases(row.get("aliases")),
                )
            )
        items.sort(key=lambda item: (item.display_name.casefold(), item.muscle_slug))
        return MuscleCatalogResponseV1(
            locale=locale,
            query=query,
            region=region,
            muscles=tuple(items),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ExerciseMuscleContractError("canonical_muscle_catalog_invalid") from error


def build_exercise_muscle_profile(
    rows: Sequence[Mapping[str, Any] | Any],
    *,
    locale: str,
) -> ExerciseMuscleProfileV1:
    if not rows:
        raise ExerciseMuscleNotFoundError("public_exercise_not_found")

    normalized = [_row_dict(row) for row in rows]
    first = normalized[0]
    exercise_identity = (
        first.get("exercise_id"),
        first.get("exercise_slug"),
        first.get("exercise_display_name"),
    )
    mappings: list[ExerciseMuscleMappingV1] = []
    seen: set[tuple[str, str]] = set()

    try:
        for row in normalized:
            if (
                row.get("exercise_id"),
                row.get("exercise_slug"),
                row.get("exercise_display_name"),
            ) != exercise_identity:
                raise ExerciseMuscleContractError("exercise_identity_drift")

            muscle_slug = row.get("muscle_slug")
            if muscle_slug is None:
                continue
            role = str(row.get("role") or "")
            identity = (str(muscle_slug), role)
            if role not in ROLE_ORDER or identity in seen:
                raise ExerciseMuscleContractError("exercise_muscle_identity_invalid")
            seen.add(identity)
            weight = row.get("weight")
            if weight is None:
                raise ExerciseMuscleContractError("exercise_muscle_weight_missing")
            mappings.append(
                ExerciseMuscleMappingV1(
                    muscle_slug=identity[0],
                    display_name=row.get("muscle_display_name"),
                    parent_slug=row.get("parent_slug"),
                    region=row.get("region"),
                    aliases=_aliases(row.get("aliases")),
                    role=role,
                    weight=float(weight),
                )
            )

        mappings.sort(
            key=lambda item: (
                ROLE_ORDER[item.role],
                -item.weight,
                item.display_name.casefold(),
                item.muscle_slug,
            )
        )

        def projection(role: str) -> tuple[str, ...]:
            names: list[str] = []
            seen_names: set[str] = set()
            for item in mappings:
                if item.role != role or item.display_name in seen_names:
                    continue
                seen_names.add(item.display_name)
                names.append(item.display_name)
            return tuple(names)

        return ExerciseMuscleProfileV1(
            exercise_id=exercise_identity[0],
            exercise_slug=exercise_identity[1],
            exercise_display_name=exercise_identity[2],
            locale=locale,
            mapped=bool(mappings),
            mappings=tuple(mappings),
            compatibility_primary_muscles=projection("primary"),
            compatibility_secondary_muscles=projection("secondary"),
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ExerciseMuscleContractError("exercise_muscle_profile_invalid") from error


__all__ = [
    "EXERCISE_MUSCLE_CONTRACT_VERSION",
    "MUSCLE_CATALOG_CONTRACT_VERSION",
    "ExerciseMuscleContractError",
    "ExerciseMuscleNotFoundError",
    "ExerciseMuscleProfileV1",
    "MuscleCatalogResponseV1",
    "build_exercise_muscle_profile",
    "build_muscle_catalog_response",
]
