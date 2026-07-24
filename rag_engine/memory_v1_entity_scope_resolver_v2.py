from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .memory_v1_entity_scope_v2 import (
    EntityScopeModeV2,
    MemoryClaimEntityScopeV2,
    MemoryClaimSelectorContextV2,
    ObjectScopeKindV2,
    PredicateEntityScopeRuleV2,
)
from .memory_v1_selection_envelope import (
    MemorySelectionRequestBindingV1,
    MemorySelectionRequestV1,
)


ENTITY_SCOPE_SNAPSHOT_VERSION = "memory_entity_scope_snapshot_v2"
ENTITY_SCOPE_RESOLUTION_POLICY_VERSION = "memory_entity_resolution_policy_v2_1"
MAX_SNAPSHOT_ENTITIES = 1000
MAX_SNAPSHOT_EDGES = 2000
ENTITY_TYPES = frozenset(
    {
        "animal",
        "concept",
        "object",
        "organization",
        "person",
        "place",
        "project",
        "self",
    }
)
FAMILY_RELATION_PREDICATES = frozenset(
    {
        "relationship.aunt_or_uncle_of",
        "relationship.cousin_of",
        "relationship.grandparent_of",
        "relationship.in_law_of",
        "relationship.parent_of",
        "relationship.relative_of",
        "relationship.romantic_partner_of",
        "relationship.sibling_of",
        "relationship.spouse_of",
    }
)
ENTITY_OBJECT_PREDICATES = frozenset(
    {
        *FAMILY_RELATION_PREDICATES,
        "occupation.works_as",
        "relationship.caregiver_for",
        "relationship.has_pet",
        "residence.lives_at",
    }
)
SELF_LITERAL_PREDICATES = frozenset(
    {
        "health.user_reported_observation",
        "preference.life",
        "stance.reported",
    }
)
PET_LITERAL_PREDICATES = frozenset(
    {
        "identity.name",
        "identity.name_canonical",
        "life_event.died",
        "pet.breed",
        "pet.sex",
    }
)
FAMILY_LITERAL_PREDICATES = frozenset(
    {
        "identity.name",
        "life_event.died",
    }
)


class EntityScopeResolutionError(RuntimeError):
    """Fail-closed outcome when a trustworthy entity scope cannot be built."""


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
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


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split()).strip()


class EntityScopeSnapshotEntityV2(StrictFrozenModel):
    entity_id: UUID
    entity_type: str
    canonical_name: str = Field(min_length=1, max_length=500)
    normalized_name: str = Field(min_length=1, max_length=500)
    aliases: tuple[str, ...] = ()
    identity_state: str | None = Field(default=None, max_length=120)
    relationship_role: str | None = Field(default=None, max_length=120)

    @field_validator("entity_type")
    @classmethod
    def closed_entity_type(cls, value: str) -> str:
        if value not in ENTITY_TYPES:
            raise ValueError("snapshot entity type is outside the closed enum")
        return value

    @field_validator("normalized_name")
    @classmethod
    def canonical_normalized_name(cls, value: str) -> str:
        if value != _normalized(value):
            raise ValueError("snapshot normalized name is not canonical")
        return value

    @field_validator("aliases")
    @classmethod
    def canonical_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalized(item) for item in value)
        if (
            any(not item for item in normalized)
            or normalized != value
            or value != tuple(sorted(set(value)))
        ):
            raise ValueError("snapshot aliases must be normalized, sorted, and unique")
        return value


class EntityScopeSnapshotEdgeV2(StrictFrozenModel):
    claim_id: UUID
    predicate: str = Field(min_length=3, max_length=128)
    subject_entity_id: UUID
    subject_entity_type: str
    object_entity_id: UUID
    object_entity_type: str

    @field_validator("predicate")
    @classmethod
    def canonical_predicate(cls, value: str) -> str:
        if re.fullmatch(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*", value) is None:
            raise ValueError("snapshot edge predicate is not canonical")
        return value

    @field_validator("subject_entity_type", "object_entity_type")
    @classmethod
    def closed_entity_type(cls, value: str) -> str:
        if value not in ENTITY_TYPES:
            raise ValueError("snapshot edge entity type is outside the closed enum")
        return value


class _EntityScopeSnapshotPayloadV2(StrictFrozenModel):
    contract_version: Literal[ENTITY_SCOPE_SNAPSHOT_VERSION]
    owner_user_id: UUID
    entities: tuple[EntityScopeSnapshotEntityV2, ...]
    edges: tuple[EntityScopeSnapshotEdgeV2, ...]

    @field_validator("entities")
    @classmethod
    def canonical_entities(
        cls, value: tuple[EntityScopeSnapshotEntityV2, ...]
    ) -> tuple[EntityScopeSnapshotEntityV2, ...]:
        if not 1 <= len(value) <= MAX_SNAPSHOT_ENTITIES:
            raise ValueError("entity snapshot must contain 1 to 1000 entities")
        ids = tuple(item.entity_id for item in value)
        if ids != tuple(sorted(set(ids), key=str)):
            raise ValueError("snapshot entities must be sorted and unique")
        return value

    @field_validator("edges")
    @classmethod
    def canonical_edges(
        cls, value: tuple[EntityScopeSnapshotEdgeV2, ...]
    ) -> tuple[EntityScopeSnapshotEdgeV2, ...]:
        if len(value) > MAX_SNAPSHOT_EDGES:
            raise ValueError("entity snapshot exceeds 2000 edges")
        keys = tuple(
            (
                item.predicate,
                str(item.subject_entity_id),
                str(item.object_entity_id),
                str(item.claim_id),
            )
            for item in value
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("snapshot edges must be sorted and unique")
        return value

    @model_validator(mode="after")
    def reconcile_edges(self) -> "_EntityScopeSnapshotPayloadV2":
        entities = {item.entity_id: item for item in self.entities}
        for edge in self.edges:
            subject = entities.get(edge.subject_entity_id)
            object_entity = entities.get(edge.object_entity_id)
            if subject is None or object_entity is None:
                raise ValueError("snapshot edge endpoint is absent")
            if (
                subject.entity_type != edge.subject_entity_type
                or object_entity.entity_type != edge.object_entity_type
            ):
                raise ValueError("snapshot edge endpoint type changed")
        return self


class MemoryEntityScopeSnapshotV2(_EntityScopeSnapshotPayloadV2):
    snapshot_sha256: str

    @model_validator(mode="after")
    def verify_snapshot_hash(self) -> "MemoryEntityScopeSnapshotV2":
        payload = self.model_dump(mode="json", exclude={"snapshot_sha256"})
        if self.snapshot_sha256 != _sha256(payload):
            raise ValueError("entity snapshot hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        owner_user_id: UUID,
        entities: tuple[EntityScopeSnapshotEntityV2, ...],
        edges: tuple[EntityScopeSnapshotEdgeV2, ...],
    ) -> "MemoryEntityScopeSnapshotV2":
        sorted_entities = tuple(sorted(entities, key=lambda item: str(item.entity_id)))
        sorted_edges = tuple(
            sorted(
                edges,
                key=lambda item: (
                    item.predicate,
                    str(item.subject_entity_id),
                    str(item.object_entity_id),
                    str(item.claim_id),
                ),
            )
        )
        payload = _EntityScopeSnapshotPayloadV2(
            contract_version=ENTITY_SCOPE_SNAPSHOT_VERSION,
            owner_user_id=owner_user_id,
            entities=sorted_entities,
            edges=sorted_edges,
        )
        value = payload.model_dump(mode="json")
        return cls(**payload.model_dump(), snapshot_sha256=_sha256(value))

    def strict_revalidated(self) -> "MemoryEntityScopeSnapshotV2":
        return type(self).model_validate_json(self.model_dump_json())


def _literal_rule(
    predicate: str, subjects: set[UUID]
) -> PredicateEntityScopeRuleV2:
    if not subjects:
        raise EntityScopeResolutionError("literal entity scope has no subjects")
    return PredicateEntityScopeRuleV2(
        predicate=predicate,
        subject_entity_ids=tuple(sorted(subjects, key=str)),
        object_scope=ObjectScopeKindV2.LITERAL_ONLY,
        object_entity_ids=(),
    )


def _entity_rule(
    predicate: str, subjects: set[UUID], objects: set[UUID]
) -> PredicateEntityScopeRuleV2:
    if not subjects or not objects:
        raise EntityScopeResolutionError("entity edge scope has no endpoints")
    return PredicateEntityScopeRuleV2(
        predicate=predicate,
        subject_entity_ids=tuple(sorted(subjects, key=str)),
        object_scope=ObjectScopeKindV2.ALLOWLISTED_ENTITY,
        object_entity_ids=tuple(sorted(objects, key=str)),
    )


def _query_matches_entity(query: str, entity: EntityScopeSnapshotEntityV2) -> bool:
    for candidate in (entity.normalized_name, *entity.aliases):
        if re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", query):
            return True
    return False


def resolve_memory_claim_selector_context_v2(
    *,
    request: MemorySelectionRequestV1,
    claim_context: Mapping[str, Any],
    snapshot: MemoryEntityScopeSnapshotV2,
) -> MemoryClaimSelectorContextV2:
    request = request.strict_revalidated()
    snapshot = snapshot.strict_revalidated()
    if snapshot.owner_user_id != request.owner_user_id:
        raise EntityScopeResolutionError("entity snapshot crossed the owner boundary")

    domain = str(claim_context.get("domain") or "").strip()
    allowed = tuple(
        sorted(
            {
                str(item).strip().casefold()
                for item in claim_context.get("allowed_predicates", ())
                if str(item).strip()
            }
        )
    )
    if not domain or not allowed:
        raise EntityScopeResolutionError("entity scope requires a classified domain")

    entities = {item.entity_id: item for item in snapshot.entities}
    self_ids = {
        item.entity_id
        for item in snapshot.entities
        if item.entity_type == "self"
        and item.identity_state == "trusted_owner_self"
    }
    if len(self_ids) != 1:
        raise EntityScopeResolutionError(
            "entity scope requires exactly one trusted owner-self"
        )
    self_id = next(iter(self_ids))
    query = _normalized(request.query_text)
    named_matches = {
        item.entity_id
        for item in snapshot.entities
        if item.entity_type not in {"self", "project"}
        and _query_matches_entity(query, item)
    }

    pet_ids = {
        edge.object_entity_id
        for edge in snapshot.edges
        if edge.predicate == "relationship.has_pet"
        and edge.subject_entity_id == self_id
        and edge.object_entity_type == "animal"
    }
    family_ids = {
        item.entity_id
        for item in snapshot.entities
        if item.entity_type == "person"
        and str(item.relationship_role or "").startswith("family:")
    }
    for edge in snapshot.edges:
        if edge.predicate not in FAMILY_RELATION_PREDICATES:
            continue
        if edge.subject_entity_id == self_id:
            family_ids.add(edge.object_entity_id)
        if edge.object_entity_id == self_id:
            family_ids.add(edge.subject_entity_id)

    rules: list[PredicateEntityScopeRuleV2] = []
    mode: EntityScopeModeV2
    hint_values: set[str] = set()

    if domain == "name_correction":
        mode = EntityScopeModeV2.NAMED_ENTITY
        subjects = {
            entity_id
            for entity_id in named_matches
            if entities[entity_id].entity_type == "animal"
        }
        if len(subjects) != 1:
            raise EntityScopeResolutionError(
                "name correction requires one exact animal entity"
            )
        hint_values.update(entities[item].normalized_name for item in subjects)
        for predicate in allowed:
            if predicate not in {"identity.name", "identity.name_canonical"}:
                raise EntityScopeResolutionError(
                    "name correction requested an invalid predicate"
                )
            rules.append(_literal_rule(predicate, subjects))
    elif domain in {"pet_profile", "pet_loss"}:
        mode = EntityScopeModeV2.PET_PROFILE
        animal_matches = {
            item
            for item in named_matches
            if entities[item].entity_type == "animal"
        }
        subjects = animal_matches or pet_ids
        if not subjects:
            raise EntityScopeResolutionError("pet scope has no governed animals")
        hint_values.update(entities[item].normalized_name for item in animal_matches)
        for predicate in allowed:
            if predicate == "relationship.has_pet":
                objects = subjects & pet_ids
                if not objects:
                    raise EntityScopeResolutionError(
                        "pet relationship scope has no governed edge"
                    )
                rules.append(_entity_rule(predicate, {self_id}, objects))
            elif predicate in PET_LITERAL_PREDICATES:
                rules.append(_literal_rule(predicate, subjects))
            else:
                raise EntityScopeResolutionError(
                    "pet scope requested an invalid predicate"
                )
    elif domain in {"family_death", "family_profile"}:
        mode = EntityScopeModeV2.FAMILY_PROFILE
        person_matches = {
            item
            for item in named_matches
            if entities[item].entity_type == "person"
        }
        subjects = person_matches or family_ids
        if not subjects:
            raise EntityScopeResolutionError("family scope has no governed relatives")
        hint_values.update(entities[item].normalized_name for item in person_matches)
        for predicate in allowed:
            if predicate in FAMILY_LITERAL_PREDICATES:
                rules.append(_literal_rule(predicate, subjects))
            elif predicate in FAMILY_RELATION_PREDICATES:
                matching = [
                    edge
                    for edge in snapshot.edges
                    if edge.predicate == predicate
                    and edge.subject_entity_id in family_ids | {self_id}
                    and edge.object_entity_id in family_ids | {self_id}
                ]
                if matching:
                    rules.append(
                        _entity_rule(
                            predicate,
                            {edge.subject_entity_id for edge in matching},
                            {edge.object_entity_id for edge in matching},
                        )
                    )
            else:
                raise EntityScopeResolutionError(
                    "family scope requested an invalid predicate"
                )
    elif domain in {"stance_recall", "health_behavior"}:
        mode = EntityScopeModeV2.SELF_PROFILE
        for predicate in allowed:
            if predicate not in SELF_LITERAL_PREDICATES:
                raise EntityScopeResolutionError(
                    "self scope requested an invalid predicate"
                )
            rules.append(_literal_rule(predicate, {self_id}))
    elif domain == "life_context":
        mode = EntityScopeModeV2.RELATIONSHIP_NEIGHBORHOOD
        for predicate in allowed:
            if predicate in SELF_LITERAL_PREDICATES:
                rules.append(_literal_rule(predicate, {self_id}))
                continue
            if predicate not in ENTITY_OBJECT_PREDICATES:
                raise EntityScopeResolutionError(
                    "life-context scope requested an invalid predicate"
                )
            matching = [
                edge
                for edge in snapshot.edges
                if edge.predicate == predicate and edge.subject_entity_id == self_id
            ]
            if matching:
                rules.append(
                    _entity_rule(
                        predicate,
                        {self_id},
                        {edge.object_entity_id for edge in matching},
                    )
                )
    else:
        raise EntityScopeResolutionError("claim domain has no V2 entity policy")

    if not rules:
        raise EntityScopeResolutionError("entity scope resolved no predicate rules")
    binding = MemorySelectionRequestBindingV1.from_request(request)
    scope = MemoryClaimEntityScopeV2.create(
        owner_user_id=request.owner_user_id,
        selection_trace_id=request.selection_trace_id,
        mode=mode,
        resolution_policy_version=ENTITY_SCOPE_RESOLUTION_POLICY_VERSION,
        request_binding_sha256=binding.request_binding_sha256,
        query_entity_hint_sha256s=tuple(
            sorted(_text_sha256(value) for value in hint_values)
        ),
        predicate_rules=tuple(rules),
    )
    return MemoryClaimSelectorContextV2.create(entity_scope=scope)


__all__ = [
    "ENTITY_SCOPE_RESOLUTION_POLICY_VERSION",
    "ENTITY_SCOPE_SNAPSHOT_VERSION",
    "EntityScopeResolutionError",
    "EntityScopeSnapshotEdgeV2",
    "EntityScopeSnapshotEntityV2",
    "MemoryEntityScopeSnapshotV2",
    "resolve_memory_claim_selector_context_v2",
]
