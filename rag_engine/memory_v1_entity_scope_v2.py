from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any, Literal, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ENTITY_SCOPE_VERSION = "memory_claim_entity_scope_v2"
CLAIM_SELECTOR_CONTEXT_VERSION = "memory_claim_selector_context_v2"
RESOLUTION_POLICY_VERSION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,119}$")
PREDICATE_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RULES = 40
MAX_ENTITIES = 100


class EntityScopeError(RuntimeError):
    """Fail-closed error at the server-resolved entity boundary."""


class EntityScopeModeV2(str, Enum):
    SELF_PROFILE = "self_profile"
    NAMED_ENTITY = "named_entity"
    FAMILY_PROFILE = "family_profile"
    PET_PROFILE = "pet_profile"
    RELATIONSHIP_NEIGHBORHOOD = "relationship_neighborhood"
    UNSCOPED_PREDICATE_ONLY = "unscoped_predicate_only"


class ObjectScopeKindV2(str, Enum):
    LITERAL_ONLY = "literal_only"
    ALLOWLISTED_ENTITY = "allowlisted_entity"


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


def _validate_sha256(value: str, field: str) -> str:
    if SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _canonical_uuids(value: tuple[UUID, ...], field: str) -> tuple[UUID, ...]:
    if len(value) > MAX_ENTITIES:
        raise ValueError(f"{field} exceeds the entity limit")
    if len(value) != len(set(value)) or value != tuple(sorted(value, key=str)):
        raise ValueError(f"{field} must be sorted and unique")
    return value


class PredicateEntityScopeRuleV2(StrictFrozenModel):
    predicate: str = Field(min_length=3, max_length=128)
    subject_entity_ids: tuple[UUID, ...]
    object_scope: ObjectScopeKindV2
    object_entity_ids: tuple[UUID, ...] = ()

    @field_validator("predicate")
    @classmethod
    def exact_predicate(cls, value: str) -> str:
        if PREDICATE_RE.fullmatch(value) is None:
            raise ValueError("entity-scope predicate must be exact and canonical")
        return value

    @field_validator("subject_entity_ids", "object_entity_ids")
    @classmethod
    def canonical_ids(cls, value: tuple[UUID, ...], info: Any) -> tuple[UUID, ...]:
        return _canonical_uuids(value, info.field_name)

    @model_validator(mode="after")
    def closed_endpoints(self) -> "PredicateEntityScopeRuleV2":
        if not self.subject_entity_ids:
            raise ValueError("entity-scope rule requires at least one subject")
        if self.object_scope == ObjectScopeKindV2.LITERAL_ONLY:
            if self.object_entity_ids:
                raise ValueError("literal-only rule cannot allow object entities")
        elif not self.object_entity_ids:
            raise ValueError("entity-object rule requires an object allowlist")
        return self


class _EntityScopePayloadV2(StrictFrozenModel):
    contract_version: Literal[ENTITY_SCOPE_VERSION]
    owner_user_id: UUID
    selection_trace_id: UUID
    mode: EntityScopeModeV2
    resolution_policy_version: str = Field(min_length=1, max_length=120)
    request_binding_sha256: str
    query_entity_hint_sha256s: tuple[str, ...]
    allowed_subject_entity_ids: tuple[UUID, ...]
    allowed_object_entity_ids: tuple[UUID, ...]
    predicate_rules: tuple[PredicateEntityScopeRuleV2, ...]

    @field_validator("resolution_policy_version")
    @classmethod
    def resolution_version(cls, value: str) -> str:
        if RESOLUTION_POLICY_VERSION_RE.fullmatch(value) is None:
            raise ValueError("resolution policy version is invalid")
        return value

    @field_validator("request_binding_sha256")
    @classmethod
    def request_hash(cls, value: str) -> str:
        return _validate_sha256(value, "request_binding_sha256")

    @field_validator("query_entity_hint_sha256s")
    @classmethod
    def hint_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 16 or value != tuple(sorted(set(value))):
            raise ValueError("entity hint hashes must be sorted, unique, and bounded")
        return tuple(_validate_sha256(item, "query_entity_hint_sha256") for item in value)

    @field_validator("allowed_subject_entity_ids", "allowed_object_entity_ids")
    @classmethod
    def canonical_ids(cls, value: tuple[UUID, ...], info: Any) -> tuple[UUID, ...]:
        return _canonical_uuids(value, info.field_name)

    @field_validator("predicate_rules")
    @classmethod
    def canonical_rules(
        cls, value: tuple[PredicateEntityScopeRuleV2, ...]
    ) -> tuple[PredicateEntityScopeRuleV2, ...]:
        if not 1 <= len(value) <= MAX_RULES:
            raise ValueError("entity scope requires 1 to 40 predicate rules")
        predicates = tuple(item.predicate for item in value)
        if predicates != tuple(sorted(set(predicates))):
            raise ValueError("entity-scope rules must be sorted by unique predicate")
        return value

    @model_validator(mode="after")
    def reconcile_allowlists(self) -> "_EntityScopePayloadV2":
        subjects = tuple(
            sorted(
                {entity for rule in self.predicate_rules for entity in rule.subject_entity_ids},
                key=str,
            )
        )
        objects = tuple(
            sorted(
                {entity for rule in self.predicate_rules for entity in rule.object_entity_ids},
                key=str,
            )
        )
        if subjects != self.allowed_subject_entity_ids:
            raise ValueError("scope subject allowlist does not reconcile with rules")
        if objects != self.allowed_object_entity_ids:
            raise ValueError("scope object allowlist does not reconcile with rules")
        if self.mode == EntityScopeModeV2.UNSCOPED_PREDICATE_ONLY:
            raise ValueError(
                "unscoped predicate mode requires a separate registry proof and is disabled"
            )
        return self


class MemoryClaimEntityScopeV2(_EntityScopePayloadV2):
    scope_manifest_sha256: str

    @field_validator("scope_manifest_sha256")
    @classmethod
    def scope_hash(cls, value: str) -> str:
        return _validate_sha256(value, "scope_manifest_sha256")

    @model_validator(mode="after")
    def verify_manifest(self) -> "MemoryClaimEntityScopeV2":
        payload = self.model_dump(mode="json", exclude={"scope_manifest_sha256"})
        if self.scope_manifest_sha256 != _sha256(payload):
            raise ValueError("entity scope manifest hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        owner_user_id: UUID,
        selection_trace_id: UUID,
        mode: EntityScopeModeV2,
        resolution_policy_version: str,
        request_binding_sha256: str,
        query_entity_hint_sha256s: tuple[str, ...],
        predicate_rules: tuple[PredicateEntityScopeRuleV2, ...],
    ) -> "MemoryClaimEntityScopeV2":
        rules = tuple(sorted(predicate_rules, key=lambda item: item.predicate))
        subjects = tuple(
            sorted(
                {entity for rule in rules for entity in rule.subject_entity_ids},
                key=str,
            )
        )
        objects = tuple(
            sorted(
                {entity for rule in rules for entity in rule.object_entity_ids},
                key=str,
            )
        )
        payload = _EntityScopePayloadV2(
            contract_version=ENTITY_SCOPE_VERSION,
            owner_user_id=owner_user_id,
            selection_trace_id=selection_trace_id,
            mode=mode,
            resolution_policy_version=resolution_policy_version,
            request_binding_sha256=request_binding_sha256,
            query_entity_hint_sha256s=tuple(sorted(set(query_entity_hint_sha256s))),
            allowed_subject_entity_ids=subjects,
            allowed_object_entity_ids=objects,
            predicate_rules=rules,
        )
        value = payload.model_dump(mode="json")
        return cls(**payload.model_dump(), scope_manifest_sha256=_sha256(value))

    def strict_revalidated(self) -> "MemoryClaimEntityScopeV2":
        return type(self).model_validate_json(self.model_dump_json())


class _ClaimSelectorContextPayloadV2(StrictFrozenModel):
    contract_version: Literal[CLAIM_SELECTOR_CONTEXT_VERSION]
    owner_user_id: UUID
    selection_trace_id: UUID
    request_binding_sha256: str
    allowed_predicates: tuple[str, ...]
    entity_scope: MemoryClaimEntityScopeV2

    @field_validator("request_binding_sha256")
    @classmethod
    def request_hash(cls, value: str) -> str:
        return _validate_sha256(value, "request_binding_sha256")

    @field_validator("allowed_predicates")
    @classmethod
    def predicates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not 1 <= len(value) <= MAX_RULES or value != tuple(sorted(set(value))):
            raise ValueError("allowed predicates must be sorted, unique, and bounded")
        if any(PREDICATE_RE.fullmatch(item) is None for item in value):
            raise ValueError("allowed predicates must be exact and canonical")
        return value

    @model_validator(mode="after")
    def reconcile_scope(self) -> "_ClaimSelectorContextPayloadV2":
        scope = self.entity_scope.strict_revalidated()
        if (
            scope.owner_user_id != self.owner_user_id
            or scope.selection_trace_id != self.selection_trace_id
            or scope.request_binding_sha256 != self.request_binding_sha256
        ):
            raise ValueError("claim selector context does not bind to entity scope")
        rule_predicates = tuple(rule.predicate for rule in scope.predicate_rules)
        if rule_predicates != self.allowed_predicates:
            raise ValueError("predicate permissions do not match entity-scope rules")
        return self


class MemoryClaimSelectorContextV2(_ClaimSelectorContextPayloadV2):
    context_manifest_sha256: str

    @field_validator("context_manifest_sha256")
    @classmethod
    def context_hash(cls, value: str) -> str:
        return _validate_sha256(value, "context_manifest_sha256")

    @model_validator(mode="after")
    def verify_manifest(self) -> "MemoryClaimSelectorContextV2":
        payload = self.model_dump(mode="json", exclude={"context_manifest_sha256"})
        if self.context_manifest_sha256 != _sha256(payload):
            raise ValueError("claim selector context manifest hash mismatch")
        return self

    @classmethod
    def create(cls, *, entity_scope: MemoryClaimEntityScopeV2) -> "MemoryClaimSelectorContextV2":
        scope = entity_scope.strict_revalidated()
        payload = _ClaimSelectorContextPayloadV2(
            contract_version=CLAIM_SELECTOR_CONTEXT_VERSION,
            owner_user_id=scope.owner_user_id,
            selection_trace_id=scope.selection_trace_id,
            request_binding_sha256=scope.request_binding_sha256,
            allowed_predicates=tuple(rule.predicate for rule in scope.predicate_rules),
            entity_scope=scope,
        )
        value = payload.model_dump(mode="json")
        return cls(**payload.model_dump(), context_manifest_sha256=_sha256(value))


def claim_row_matches_entity_scope_v2(
    row: Mapping[str, Any],
    context: MemoryClaimSelectorContextV2,
) -> bool:
    try:
        context = MemoryClaimSelectorContextV2.model_validate_json(
            context.model_dump_json()
        )
        owner = row["owner_user_id"]
        predicate = row["predicate"]
        subject = row["subject_entity_id"]
        object_entity = row["object_entity_id"]
    except Exception as exc:
        raise EntityScopeError("claim row is missing typed entity-scope fields") from exc
    if not isinstance(owner, UUID) or owner != context.owner_user_id:
        raise EntityScopeError("claim row crossed the owner boundary")
    if not isinstance(predicate, str) or PREDICATE_RE.fullmatch(predicate) is None:
        raise EntityScopeError("claim predicate is not canonical")
    if not isinstance(subject, UUID):
        raise EntityScopeError("claim subject entity is invalid")
    if object_entity is not None and not isinstance(object_entity, UUID):
        raise EntityScopeError("claim object entity is invalid")
    rules = {rule.predicate: rule for rule in context.entity_scope.predicate_rules}
    rule = rules.get(predicate)
    if rule is None or subject not in rule.subject_entity_ids:
        return False
    if rule.object_scope == ObjectScopeKindV2.LITERAL_ONLY:
        return object_entity is None
    return object_entity in rule.object_entity_ids


__all__ = [
    "CLAIM_SELECTOR_CONTEXT_VERSION",
    "ENTITY_SCOPE_VERSION",
    "EntityScopeError",
    "EntityScopeModeV2",
    "MemoryClaimEntityScopeV2",
    "MemoryClaimSelectorContextV2",
    "ObjectScopeKindV2",
    "PredicateEntityScopeRuleV2",
    "claim_row_matches_entity_scope_v2",
]
