from __future__ import annotations

"""PostgreSQL-authoritative candidate revalidation and safe prompt exposure."""

from dataclasses import dataclass
from datetime import datetime
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import UUID

from .admission import (
    claim_identity_sha256,
    recompute_claim_state_sha256,
    relational_fact_from_row,
)
from .contracts import (
    ClaimLifecycleState,
    ContractViolation,
    EntityKind,
    EpistemicStatus,
    ObjectKind,
    Sensitivity,
    canonical_json_bytes,
    framed_material_bytes,
    render_relational_fact,
    require_bounded_text,
    require_exact_int,
    require_key,
    require_sha256,
    require_sorted_unique,
    require_utc,
    semantic_key_sha256,
    sha256_bytes,
    sha256_text,
)
from .extraction import parse_predicate_catalog, policy_for_sensitivity
from .projection import (
    PROJECTION_CONTRACT_SHA256,
    ProjectionOperation,
    projection_manifest_sha256,
)


CANDIDATE_FIELDS = (
    "claim_id",
    "owner_user_id",
    "projection_contract_sha256",
    "projection_manifest_sha256",
    "projection_operation_id",
    "projection_sequence",
    "retrieval_text_sha256",
    "revision_id",
    "revision_number",
    "revision_sha256",
    "score",
    "selection_binding_sha256",
)
AUTHORITATIVE_RETRIEVAL_ROW_FIELDS = (
    "claim_id",
    "claim_identity_sha256",
    "domains",
    "epistemic_state",
    "intents",
    "is_current",
    "lifecycle_state",
    "object_display_name",
    "object_entity_key",
    "object_entity_type",
    "object_kind",
    "object_literal",
    "owner_user_id",
    "predicate",
    "predicate_catalog_sha256",
    "projectable",
    "projection_sequence",
    "requires_explicit",
    "retrieval_text",
    "retrieval_text_sha256",
    "revision_id",
    "revision_number",
    "revision_sha256",
    "selection_binding_sha256",
    "semantic_key_sha256",
    "sensitivity",
    "source_sha256",
    "state_sha256",
    "subject_display_name",
    "subject_entity_key",
    "subject_entity_type",
    "surface",
    "updated_at",
    "valid_from",
    "valid_to",
)
RETRIEVAL_POLICY_DOMAIN = "governed_memory.retrieval_policy.v1"


def _framed_key_sequence(name: str, values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    return (
        (f"{name}_count", str(len(values))),
        *((f"{name}_{index}", value) for index, value in enumerate(values)),
    )


def retrieval_policy_material_bytes(
    *,
    explicit_recall: bool,
    allowed_predicates: Sequence[str],
    domains: Sequence[str],
    intents: Sequence[str],
    max_records: int,
    policy_revision: int,
) -> bytes:
    if type(explicit_recall) is not bool:
        raise ContractViolation("invalid_explicit_recall_flag")
    normalized: dict[str, tuple[str, ...]] = {}
    for name, values, minimum, maximum, code in (
        (
            "allowed_predicates",
            allowed_predicates,
            1,
            8,
            "invalid_retrieval_predicates",
        ),
        ("domains", domains, 0, 16, "invalid_retrieval_domains"),
        ("intents", intents, 0, 16, "invalid_retrieval_intents"),
    ):
        if isinstance(values, (str, bytes, bytearray)) or not isinstance(
            values, (list, tuple)
        ):
            raise ContractViolation(code)
        sequence = tuple(values)
        if not minimum <= len(sequence) <= maximum:
            raise ContractViolation(code)
        require_sorted_unique(sequence, code=code)
        for value in sequence:
            require_key(value, code)
        normalized[name] = sequence
    record_limit = require_exact_int(
        max_records,
        code="invalid_retrieval_record_limit",
        minimum=1,
        maximum=8,
    )
    revision = require_exact_int(
        policy_revision,
        code="invalid_retrieval_policy_revision",
        minimum=1,
        maximum=1,
    )
    fields = (
        ("explicit_recall", "true" if explicit_recall else "false"),
        *_framed_key_sequence(
            "allowed_predicates", normalized["allowed_predicates"]
        ),
        *_framed_key_sequence("domains", normalized["domains"]),
        *_framed_key_sequence("intents", normalized["intents"]),
        ("max_records", str(record_limit)),
        ("policy_revision", str(revision)),
    )
    return framed_material_bytes(RETRIEVAL_POLICY_DOMAIN, fields)


def retrieval_policy_sha256(**kwargs: Any) -> str:
    return sha256_bytes(retrieval_policy_material_bytes(**kwargs))


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrievalPolicy:
    explicit_recall: bool
    allowed_predicates: tuple[str, ...]
    domains: tuple[str, ...] = ()
    intents: tuple[str, ...] = ()
    max_records: int = 8
    policy_revision: int = 1

    def __post_init__(self) -> None:
        if type(self.explicit_recall) is not bool:
            raise ContractViolation("invalid_explicit_recall_flag")
        if not self.allowed_predicates:
            raise ContractViolation("empty_retrieval_predicate_allowlist")
        for values, code in (
            (self.allowed_predicates, "invalid_retrieval_predicates"),
            (self.domains, "invalid_retrieval_domains"),
            (self.intents, "invalid_retrieval_intents"),
        ):
            require_sorted_unique(values, code=code)
            for value in values:
                require_key(value, code)
        require_exact_int(
            self.max_records,
            code="invalid_retrieval_record_limit",
            minimum=1,
            maximum=8,
        )
        require_exact_int(
            self.policy_revision,
            code="invalid_retrieval_policy_revision",
            minimum=1,
            maximum=1,
        )
        retrieval_policy_material_bytes(
            explicit_recall=self.explicit_recall,
            allowed_predicates=self.allowed_predicates,
            domains=self.domains,
            intents=self.intents,
            max_records=self.max_records,
            policy_revision=self.policy_revision,
        )

    @property
    def policy_sha256(self) -> str:
        return retrieval_policy_sha256(
            explicit_recall=self.explicit_recall,
            allowed_predicates=self.allowed_predicates,
            domains=self.domains,
            intents=self.intents,
            max_records=self.max_records,
            policy_revision=self.policy_revision,
        )


def _uuid(value: object, code: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc


def _utc(value: object, code: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return require_utc(value, code)
    if not isinstance(value, str):
        raise ContractViolation(code)
    try:
        return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), code)
    except ValueError as exc:
        raise ContractViolation(code) from exc


def _key_list(value: object, code: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ContractViolation(code)
    result = [require_key(item, code) for item in value]
    if result != sorted(set(result)):
        raise ContractViolation(code)
    return result


def _authoritative_row(
    raw: Mapping[str, Any], owner: UUID, predicate_catalog: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or "owner_user_id" not in raw:
        raise ContractViolation("invalid_authoritative_owner")
    row_owner = _uuid(raw["owner_user_id"], "invalid_authoritative_owner")
    if row_owner != owner:
        raise ContractViolation("cross_owner_authoritative_row")
    if tuple(sorted(raw)) != AUTHORITATIVE_RETRIEVAL_ROW_FIELDS:
        raise ContractViolation("invalid_authoritative_retrieval_row")
    row = dict(raw)
    row["owner_user_id"] = str(row_owner)
    row["claim_id"] = str(_uuid(row["claim_id"], "invalid_authoritative_claim"))
    row["revision_id"] = str(
        _uuid(row["revision_id"], "invalid_authoritative_revision")
    )
    row["revision_number"] = require_exact_int(
        row["revision_number"],
        code="invalid_authoritative_revision_number",
        minimum=1,
    )
    row["projection_sequence"] = require_exact_int(
        row["projection_sequence"],
        code="invalid_authoritative_projection_sequence",
        minimum=1,
    )
    for field in (
        "revision_sha256",
        "selection_binding_sha256",
        "semantic_key_sha256",
        "claim_identity_sha256",
        "source_sha256",
        "state_sha256",
        "retrieval_text_sha256",
        "predicate_catalog_sha256",
    ):
        row[field] = require_sha256(row[field], f"invalid_authoritative_{field}")
    try:
        lifecycle = ClaimLifecycleState(row["lifecycle_state"])
        epistemic = EpistemicStatus(row["epistemic_state"])
        sensitivity = Sensitivity(row["sensitivity"])
        subject_type = EntityKind(row["subject_entity_type"])
        object_kind = ObjectKind(row["object_kind"])
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_authoritative_claim_enum") from exc
    row["lifecycle_state"] = lifecycle.value
    row["epistemic_state"] = epistemic.value
    row["sensitivity"] = sensitivity.value
    row["subject_entity_type"] = subject_type.value
    row["object_kind"] = object_kind.value
    if type(row["is_current"]) is not bool or type(row["projectable"]) is not bool:
        raise ContractViolation("invalid_authoritative_projectability")
    if type(row["requires_explicit"]) is not bool:
        raise ContractViolation("invalid_authoritative_explicit_flag")
    row["domains"] = _key_list(row["domains"], "invalid_authoritative_domains")
    row["intents"] = _key_list(row["intents"], "invalid_authoritative_intents")
    row["valid_from"] = _utc(row["valid_from"], "invalid_authoritative_valid_from")
    row["valid_to"] = _utc(row["valid_to"], "invalid_authoritative_valid_to")
    row["updated_at"] = _utc(row["updated_at"], "invalid_authoritative_updated_at")
    catalog = parse_predicate_catalog(predicate_catalog)
    if row["predicate_catalog_sha256"] != catalog.catalog_sha256:
        raise ContractViolation("authoritative_catalog_mismatch")
    rule = catalog.rule_for(require_key(row["predicate"], "invalid_authoritative_predicate"))
    if (
        subject_type not in rule.subject_kinds
        or object_kind not in rule.object_kinds
        or epistemic not in rule.epistemic_statuses
        or sensitivity not in rule.sensitivities
    ):
        raise ContractViolation("authoritative_catalog_fact_mismatch")
    expected_policy = policy_for_sensitivity(sensitivity.value)
    if (
        row["surface"] != expected_policy["surface"]
        or row["requires_explicit"] is not expected_policy["requires_explicit"]
    ):
        raise ContractViolation("authoritative_policy_mismatch")
    expected_semantic = semantic_key_sha256(
        subject_entity_key=str(row["subject_entity_key"]),
        predicate=str(row["predicate"]),
        object_kind=object_kind,
        object_entity_key=row["object_entity_key"],
        object_literal=row["object_literal"],
    )
    if row["semantic_key_sha256"] != expected_semantic:
        raise ContractViolation("authoritative_semantic_key_mismatch")
    if row["claim_identity_sha256"] != claim_identity_sha256(
        subject_entity_key=str(row["subject_entity_key"]),
        predicate=str(row["predicate"]),
    ):
        raise ContractViolation("authoritative_claim_identity_mismatch")
    subject, object_value = relational_fact_from_row(row)
    rendered = render_relational_fact(
        subject=subject,
        predicate=str(row["predicate"]),
        object_value=object_value,
    )
    if row["retrieval_text"] != rendered or row["retrieval_text_sha256"] != sha256_text(rendered):
        raise ContractViolation("authoritative_retrieval_text_mismatch")
    if row["state_sha256"] != recompute_claim_state_sha256(row):
        raise ContractViolation("authoritative_claim_state_mismatch")
    return row


def _candidate(raw: Mapping[str, Any], owner: UUID) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or "owner_user_id" not in raw:
        raise ContractViolation("invalid_candidate_owner")
    candidate_owner = _uuid(raw["owner_user_id"], "invalid_candidate_owner")
    if candidate_owner != owner:
        raise ContractViolation("cross_owner_vector_candidate")
    if tuple(sorted(raw)) != CANDIDATE_FIELDS:
        raise ContractViolation("invalid_vector_candidate")
    result = dict(raw)
    result["owner_user_id"] = str(candidate_owner)
    result["claim_id"] = str(_uuid(raw["claim_id"], "invalid_candidate_claim"))
    result["revision_id"] = str(
        _uuid(raw["revision_id"], "invalid_candidate_revision")
    )
    result["projection_operation_id"] = str(
        _uuid(raw["projection_operation_id"], "invalid_candidate_projection_operation")
    )
    result["revision_number"] = require_exact_int(
        raw["revision_number"], code="invalid_candidate_revision_number", minimum=1
    )
    result["projection_sequence"] = require_exact_int(
        raw["projection_sequence"], code="invalid_candidate_projection_sequence", minimum=1
    )
    for field in (
        "revision_sha256",
        "selection_binding_sha256",
        "retrieval_text_sha256",
        "projection_contract_sha256",
        "projection_manifest_sha256",
    ):
        result[field] = require_sha256(raw[field], f"invalid_candidate_{field}")
    score = raw["score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ContractViolation("invalid_candidate_score")
    try:
        result["score"] = float(score)
    except (OverflowError, ValueError) as exc:
        raise ContractViolation("invalid_candidate_score") from exc
    if (
        not math.isfinite(result["score"])
        or result["score"] < -1.0
        or result["score"] > 1.0
    ):
        raise ContractViolation("invalid_candidate_score")
    if result["projection_contract_sha256"] != PROJECTION_CONTRACT_SHA256:
        raise ContractViolation("candidate_projection_contract_mismatch")
    expected_manifest = projection_manifest_sha256(
        owner_user_id=candidate_owner,
        claim_id=UUID(result["claim_id"]),
        revision_id=UUID(result["revision_id"]),
        operation_id=UUID(result["projection_operation_id"]),
        operation=ProjectionOperation.UPSERT,
        sequence_number=result["projection_sequence"],
        revision_sha256=result["revision_sha256"],
        selection_binding_sha256=result["selection_binding_sha256"],
        retrieval_text_sha256=result["retrieval_text_sha256"],
        embedding_input_sha256=result["retrieval_text_sha256"],
    )
    if result["projection_manifest_sha256"] != expected_manifest:
        raise ContractViolation("candidate_projection_manifest_mismatch")
    return result


def revalidate_candidates(
    owner_user_id: UUID,
    candidates: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    *,
    policy: RetrievalPolicy,
    predicate_catalog: Mapping[str, Any],
    authorization_at: datetime,
) -> tuple[dict[str, Any], ...]:
    owner = _uuid(owner_user_id, "invalid_retrieval_owner")
    if not isinstance(policy, RetrievalPolicy):
        raise ContractViolation("invalid_retrieval_policy")
    authoritative_time = require_utc(
        authorization_at, "invalid_retrieval_authorization_at"
    )
    if (
        not isinstance(candidates, (list, tuple))
        or not isinstance(rows, (list, tuple))
        or len(candidates) > 8
        or len(rows) > 8
    ):
        raise ContractViolation("retrieval_input_limit_exceeded")
    row_by_claim: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = _authoritative_row(raw, owner, predicate_catalog)
        if row["claim_id"] in row_by_claim:
            raise ContractViolation("duplicate_authoritative_claim")
        row_by_claim[row["claim_id"]] = row
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for raw in candidates:
        candidate = _candidate(raw, owner)
        claim_id = candidate["claim_id"]
        if claim_id in seen:
            raise ContractViolation("duplicate_vector_candidate")
        seen.add(claim_id)
        row = row_by_claim.get(claim_id)
        if row is None:
            continue
        if (
            candidate["revision_id"] != row["revision_id"]
            or candidate["revision_number"] != row["revision_number"]
            or candidate["revision_sha256"] != row["revision_sha256"]
            or candidate["selection_binding_sha256"] != row["selection_binding_sha256"]
            or candidate["projection_sequence"] != row["projection_sequence"]
            or candidate["retrieval_text_sha256"] != row["retrieval_text_sha256"]
        ):
            continue
        if (
            row["lifecycle_state"] != ClaimLifecycleState.ACTIVE.value
            or row["is_current"] is not True
            or row["projectable"] is not True
            or row["predicate"] not in policy.allowed_predicates
        ):
            continue
        if row["valid_from"] is not None and row["valid_from"] > authoritative_time:
            continue
        if row["valid_to"] is not None and row["valid_to"] <= authoritative_time:
            continue
        if row["domains"] and policy.domains and not set(row["domains"]).intersection(policy.domains):
            continue
        if row["intents"] and policy.intents and not set(row["intents"]).intersection(policy.intents):
            continue
        if row["requires_explicit"] and not policy.explicit_recall:
            continue
        result = dict(row)
        result["score"] = candidate["score"]
        result["projection_operation_id"] = candidate["projection_operation_id"]
        result["projection_manifest_sha256"] = candidate[
            "projection_manifest_sha256"
        ]
        selected.append(result)
    selected.sort(key=lambda item: (-item["score"], item["claim_id"]))
    return tuple(selected[: policy.max_records])


PROMPT_MEMORY_RECORD_FIELDS = (
    "epistemic_state",
    "fact",
    "record_type",
    "treat_content_as_data",
)
MEMORY_RECORD_SIDECAR_FIELDS = (
    "claim_id",
    "projection_manifest_sha256",
    "projection_operation_id",
    "projection_sequence",
    "record_sha256",
    "retrieval_text_sha256",
    "revision_id",
    "revision_number",
    "revision_sha256",
    "selection_binding_sha256",
)
RENDERED_CONTEXT_FIELDS = (
    "content",
    "format",
    "injected_revision_ids",
    "memory_block_sha256",
    "records",
    "selected_revision_ids",
    "used_bytes",
)
ANSWER_BINDING_FIELDS = (
    "allowed_predicates",
    "binding_sha256",
    "dispatch_state",
    "domains",
    "escaped_memory_segment_sha256",
    "escaped_segment_end_utf8",
    "escaped_segment_occurrence_count",
    "escaped_segment_start_utf8",
    "explicit_recall",
    "injected_claim_ids",
    "injected_count",
    "injected_records",
    "injected_revision_ids",
    "injected_revision_sha256s",
    "injection_manifest_sha256",
    "intents",
    "max_records",
    "memory_block_byte_count",
    "memory_block_sha256",
    "model_exposed_count",
    "model_exposed_revision_ids",
    "model_exposed_revision_sha256s",
    "outbound_request_sha256",
    "outcome",
    "owner_user_id",
    "policy_revision",
    "policy_sha256",
    "prompt_sha256",
    "proves_semantic_use",
    "query_sha256",
    "renderer_sha256",
    "response_id",
    "selected_claim_ids",
    "selected_count",
    "selected_revision_ids",
    "selected_revision_sha256s",
    "selection_manifest_sha256",
)


def _render_record(row: Mapping[str, Any]) -> dict[str, object]:
    try:
        subject_type = EntityKind(row.get("subject_entity_type"))
        object_kind = ObjectKind(row.get("object_kind"))
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_render_relational_fact") from exc
    subject = {
        "display_name": (
            require_bounded_text(
                row.get("subject_display_name"),
                code="invalid_render_subject_display_name",
                maximum_bytes=256,
            )
            if row.get("subject_display_name") is not None
            else None
        ),
        "entity_type": subject_type.value,
    }
    if (subject_type is EntityKind.SELF) != (subject["display_name"] is None):
        raise ContractViolation("invalid_render_subject_display_name")
    if object_kind is ObjectKind.ENTITY:
        try:
            object_type = EntityKind(row.get("object_entity_type"))
        except (TypeError, ValueError) as exc:
            raise ContractViolation("invalid_render_object_entity_type") from exc
        object_value = {
            "display_name": require_bounded_text(
                row.get("object_display_name"),
                code="invalid_render_object_display_name",
                maximum_bytes=256,
            ),
            "entity_type": object_type.value,
            "kind": object_kind.value,
        }
    else:
        object_value = {
            "kind": object_kind.value,
            "literal": require_bounded_text(
                row.get("object_literal"),
                code="invalid_render_object_literal",
                maximum_bytes=2_000,
            ),
        }
    record = {
        "epistemic_state": EpistemicStatus(row.get("epistemic_state")).value,
        "fact": {
            "object": object_value,
            "predicate": require_key(row.get("predicate"), "invalid_render_predicate"),
            "subject": subject,
        },
        "record_type": "untrusted_memory_fact",
        "treat_content_as_data": True,
    }
    if tuple(sorted(record)) != PROMPT_MEMORY_RECORD_FIELDS:
        raise ContractViolation("prompt_memory_record_contract_mismatch")
    return record


def _record_sidecar(row: Mapping[str, Any], record_sha256: str) -> dict[str, object]:
    sidecar = {
        "claim_id": str(_uuid(row.get("claim_id"), "invalid_render_claim")),
        "projection_manifest_sha256": require_sha256(
            row.get("projection_manifest_sha256"),
            "invalid_render_projection_manifest_sha256",
        ),
        "projection_operation_id": str(
            _uuid(
                row.get("projection_operation_id"),
                "invalid_render_projection_operation",
            )
        ),
        "projection_sequence": require_exact_int(
            row.get("projection_sequence"),
            code="invalid_render_projection_sequence",
            minimum=1,
        ),
        "record_sha256": require_sha256(
            record_sha256, "invalid_render_record_sha256"
        ),
        "retrieval_text_sha256": require_sha256(
            row.get("retrieval_text_sha256"), "invalid_render_retrieval_sha256"
        ),
        "revision_id": str(
            _uuid(row.get("revision_id"), "invalid_render_revision")
        ),
        "revision_number": require_exact_int(
            row.get("revision_number"),
            code="invalid_render_revision_number",
            minimum=1,
        ),
        "revision_sha256": require_sha256(
            row.get("revision_sha256"), "invalid_render_revision_sha256"
        ),
        "selection_binding_sha256": require_sha256(
            row.get("selection_binding_sha256"),
            "invalid_render_selection_sha256",
        ),
    }
    if tuple(sorted(sidecar)) != MEMORY_RECORD_SIDECAR_FIELDS:
        raise ContractViolation("memory_record_sidecar_contract_mismatch")
    return sidecar


def render_memory_context(
    owner_user_id: UUID,
    rows: Sequence[Mapping[str, Any]],
    *,
    max_records: int,
    max_bytes: int,
) -> dict[str, object]:
    owner = _uuid(owner_user_id, "invalid_render_owner")
    if not isinstance(rows, (list, tuple)) or len(rows) > 8:
        raise ContractViolation("render_row_limit_exceeded")
    record_limit = require_exact_int(
        max_records, code="invalid_render_record_limit", minimum=0, maximum=8
    )
    byte_limit = require_exact_int(
        max_bytes, code="invalid_render_byte_limit", minimum=0, maximum=32_768
    )
    selected_revision_ids: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ContractViolation("invalid_render_row")
        if _uuid(row.get("owner_user_id"), "invalid_render_row_owner") != owner:
            raise ContractViolation("cross_owner_render_row")
        selected_revision_ids.append(
            str(_uuid(row.get("revision_id"), "invalid_render_revision"))
        )
    if len(set(selected_revision_ids)) != len(selected_revision_ids):
        raise ContractViolation("duplicate_render_revision")
    content_parts: list[bytes] = []
    records: list[dict[str, object]] = []
    used = 0
    for row in rows[:record_limit]:
        material = _render_record(row)
        encoded = canonical_json_bytes(material)
        added = len(encoded) + (1 if content_parts else 0)
        if used + added > byte_limit:
            break
        content_parts.append(encoded)
        used += added
        records.append(
            _record_sidecar(row, sha256_bytes(encoded))
        )
    content_bytes = b"\n".join(content_parts)
    rendered = {
        "content": content_bytes.decode("utf-8"),
        "records": tuple(records),
        "selected_revision_ids": tuple(selected_revision_ids),
        "injected_revision_ids": tuple(record["revision_id"] for record in records),
        "used_bytes": len(content_bytes),
        "memory_block_sha256": sha256_bytes(content_bytes),
        "format": "canonical_json_lines_untrusted_user_data",
    }
    if tuple(sorted(rendered)) != RENDERED_CONTEXT_FIELDS:
        raise ContractViolation("rendered_context_contract_mismatch")
    return rendered


ANSWER_SELECTION_MANIFEST_DOMAIN = "governed_memory.answer_selection.v1"
ANSWER_SELECTION_MANIFEST_FIELDS = (
    "owner_user_id",
    "response_id",
    "query_sha256",
    "policy_sha256",
    "renderer_sha256",
    "prompt_sha256",
    "explicit_recall",
    "selected_claim_ids",
    "selected_revision_ids",
    "selected_revision_sha256s",
    "outcome",
)
ANSWER_INJECTION_MANIFEST_DOMAIN = "governed_memory.answer_injection.v1"
ANSWER_INJECTION_MANIFEST_FIELDS = (
    "selection_manifest_sha256",
    "injected_claim_ids",
    "injected_revision_ids",
    "injected_revision_sha256s",
    "memory_block_sha256",
    "memory_block_utf8_bytes",
    "escaped_memory_segment_sha256",
    "escaped_segment_start_utf8",
    "escaped_segment_end_utf8",
    "escaped_segment_occurrence_count",
    "outbound_request_sha256",
)
FINAL_ANSWER_BINDING_OUTCOMES = frozenset(
    {"exposed", "no_memory_selected"}
)
ANSWER_RECORD_LIMIT = 8
MEMORY_BLOCK_MAX_UTF8_BYTES = 32_768
OUTBOUND_REQUEST_MAX_UTF8_BYTES = 1_048_576
ANSWER_RENDERER_SHA256 = (
    "27a5c3189efc176b4aee38a53480f65713c91f6db06ceb742acbf230eb065416"
)


def _uuid_sequence(value: Sequence[object], code: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, (list, tuple)
    ) or len(value) > 8:
        raise ContractViolation(code)
    result = tuple(str(_uuid(item, code)) for item in value)
    if len(set(result)) != len(result):
        raise ContractViolation(code)
    return result


def _sha256_sequence(value: Sequence[object], code: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, (list, tuple)
    ) or len(value) > 8:
        raise ContractViolation(code)
    return tuple(require_sha256(item, code) for item in value)


def _postgres_array_text(values: Sequence[str]) -> str:
    """Render the safe UUID/SHA subset exactly as PostgreSQL ``array::text``."""

    if any(
        not isinstance(value, str)
        or any(character in value for character in '{}",\\')
        for value in values
    ):
        raise ContractViolation("unsafe_postgres_manifest_array_value")
    return "{" + ",".join(values) + "}"


def _answer_outcome(value: object, code: str) -> str:
    if value not in FINAL_ANSWER_BINDING_OUTCOMES:
        raise ContractViolation(code)
    return str(value)


def answer_selection_manifest_material_bytes(
    *,
    owner_user_id: UUID,
    response_id: UUID,
    query_sha256: str,
    policy_sha256: str,
    renderer_sha256: str,
    prompt_sha256: str,
    explicit_recall: bool,
    selected_claim_ids: Sequence[UUID | str],
    selected_revision_ids: Sequence[UUID | str],
    selected_revision_sha256s: Sequence[str],
    outcome: str,
) -> bytes:
    owner = _uuid(owner_user_id, "invalid_answer_selection_owner")
    response = _uuid(response_id, "invalid_answer_selection_response")
    claim_ids = _uuid_sequence(
        selected_claim_ids, "invalid_answer_selection_claim_ids"
    )
    revision_ids = _uuid_sequence(
        selected_revision_ids, "invalid_answer_selection_revision_ids"
    )
    revision_hashes = _sha256_sequence(
        selected_revision_sha256s,
        "invalid_answer_selection_revision_sha256s",
    )
    if len(claim_ids) != len(revision_ids) or len(claim_ids) != len(revision_hashes):
        raise ContractViolation("answer_selection_lineage_count_mismatch")
    final_outcome = _answer_outcome(outcome, "invalid_answer_selection_outcome")
    if type(explicit_recall) is not bool:
        raise ContractViolation("invalid_answer_selection_explicit_recall")
    if final_outcome == "no_memory_selected" and claim_ids:
        raise ContractViolation("answer_selection_outcome_shape_mismatch")
    if final_outcome == "exposed" and not claim_ids:
        raise ContractViolation("answer_selection_outcome_shape_mismatch")
    fields = (
        ("owner_user_id", str(owner)),
        ("response_id", str(response)),
        ("query_sha256", require_sha256(query_sha256, "invalid_answer_query_sha256")),
        ("policy_sha256", require_sha256(policy_sha256, "invalid_answer_policy_sha256")),
        ("renderer_sha256", require_sha256(renderer_sha256, "invalid_answer_renderer_sha256")),
        ("prompt_sha256", require_sha256(prompt_sha256, "invalid_answer_prompt_sha256")),
        ("explicit_recall", "true" if explicit_recall else "false"),
        ("selected_claim_ids", _postgres_array_text(claim_ids)),
        ("selected_revision_ids", _postgres_array_text(revision_ids)),
        ("selected_revision_sha256s", _postgres_array_text(revision_hashes)),
        ("outcome", final_outcome),
    )
    if tuple(name for name, _ in fields) != ANSWER_SELECTION_MANIFEST_FIELDS:
        raise ContractViolation("answer_selection_manifest_field_order_mismatch")
    return framed_material_bytes(ANSWER_SELECTION_MANIFEST_DOMAIN, fields)


def answer_selection_manifest_sha256(**kwargs: Any) -> str:
    return sha256_bytes(answer_selection_manifest_material_bytes(**kwargs))


def answer_injection_manifest_material_bytes(
    *,
    selection_manifest_sha256: str,
    injected_claim_ids: Sequence[UUID | str],
    injected_revision_ids: Sequence[UUID | str],
    injected_revision_sha256s: Sequence[str],
    memory_block_sha256: str,
    memory_block_utf8_bytes: int,
    escaped_memory_segment_sha256: str,
    escaped_segment_start_utf8: int,
    escaped_segment_end_utf8: int,
    escaped_segment_occurrence_count: int,
    outbound_request_sha256: str,
) -> bytes:
    claim_ids = _uuid_sequence(
        injected_claim_ids, "invalid_answer_injection_claim_ids"
    )
    revision_ids = _uuid_sequence(
        injected_revision_ids, "invalid_answer_injection_revision_ids"
    )
    revision_hashes = _sha256_sequence(
        injected_revision_sha256s,
        "invalid_answer_injection_revision_sha256s",
    )
    if (
        not claim_ids
        or len(claim_ids) != len(revision_ids)
        or len(claim_ids) != len(revision_hashes)
    ):
        raise ContractViolation("answer_injection_lineage_count_mismatch")
    block_bytes = require_exact_int(
        memory_block_utf8_bytes,
        code="invalid_answer_memory_block_utf8_bytes",
        minimum=1,
        maximum=MEMORY_BLOCK_MAX_UTF8_BYTES,
    )
    segment_start = require_exact_int(
        escaped_segment_start_utf8,
        code="invalid_escaped_segment_start_utf8",
        maximum=OUTBOUND_REQUEST_MAX_UTF8_BYTES,
    )
    segment_end = require_exact_int(
        escaped_segment_end_utf8,
        code="invalid_escaped_segment_end_utf8",
        minimum=segment_start + 1,
        maximum=OUTBOUND_REQUEST_MAX_UTF8_BYTES,
    )
    occurrences = require_exact_int(
        escaped_segment_occurrence_count,
        code="invalid_escaped_segment_occurrence_count",
        minimum=1,
        maximum=1,
    )
    fields = (
        ("selection_manifest_sha256", require_sha256(selection_manifest_sha256, "invalid_answer_selection_manifest_sha256")),
        ("injected_claim_ids", _postgres_array_text(claim_ids)),
        ("injected_revision_ids", _postgres_array_text(revision_ids)),
        ("injected_revision_sha256s", _postgres_array_text(revision_hashes)),
        ("memory_block_sha256", require_sha256(memory_block_sha256, "invalid_answer_memory_block_sha256")),
        ("memory_block_utf8_bytes", str(block_bytes)),
        ("escaped_memory_segment_sha256", require_sha256(escaped_memory_segment_sha256, "invalid_escaped_memory_segment_sha256")),
        ("escaped_segment_start_utf8", str(segment_start)),
        ("escaped_segment_end_utf8", str(segment_end)),
        ("escaped_segment_occurrence_count", str(occurrences)),
        ("outbound_request_sha256", require_sha256(outbound_request_sha256, "invalid_outbound_request_sha256")),
    )
    if tuple(name for name, _ in fields) != ANSWER_INJECTION_MANIFEST_FIELDS:
        raise ContractViolation("answer_injection_manifest_field_order_mismatch")
    return framed_material_bytes(ANSWER_INJECTION_MANIFEST_DOMAIN, fields)


def answer_injection_manifest_sha256(**kwargs: Any) -> str:
    return sha256_bytes(answer_injection_manifest_material_bytes(**kwargs))


ANSWER_MANIFEST_TEST_VECTORS = MappingProxyType(
    {
        "selection": MappingProxyType(
            {
                "domain": ANSWER_SELECTION_MANIFEST_DOMAIN,
                "ordered_fields": ANSWER_SELECTION_MANIFEST_FIELDS,
                "arguments": MappingProxyType(
                    {
                        "owner_user_id": "00000000-0000-4000-8000-000000000001",
                        "response_id": "00000000-0000-4000-8000-000000000002",
                        "query_sha256": "1" * 64,
                        "policy_sha256": "2" * 64,
                        "renderer_sha256": "3" * 64,
                        "prompt_sha256": "4" * 64,
                        "explicit_recall": False,
                        "selected_claim_ids": (
                            "00000000-0000-4000-8000-000000000003",
                        ),
                        "selected_revision_ids": (
                            "00000000-0000-4000-8000-000000000004",
                        ),
                        "selected_revision_sha256s": ("5" * 64,),
                        "outcome": "exposed",
                    }
                ),
                "sha256": "6bc58686dbc37ec535fc65a418457141810c33917f96c03d273aefcbfc64f2e0",
            }
        ),
        "injection": MappingProxyType(
            {
                "domain": ANSWER_INJECTION_MANIFEST_DOMAIN,
                "ordered_fields": ANSWER_INJECTION_MANIFEST_FIELDS,
                "arguments": MappingProxyType(
                    {
                        "selection_manifest_sha256": "6bc58686dbc37ec535fc65a418457141810c33917f96c03d273aefcbfc64f2e0",
                        "injected_claim_ids": (
                            "00000000-0000-4000-8000-000000000003",
                        ),
                        "injected_revision_ids": (
                            "00000000-0000-4000-8000-000000000004",
                        ),
                        "injected_revision_sha256s": ("5" * 64,),
                        "memory_block_sha256": "6" * 64,
                        "memory_block_utf8_bytes": 321,
                        "escaped_memory_segment_sha256": "7" * 64,
                        "escaped_segment_start_utf8": 41,
                        "escaped_segment_end_utf8": 364,
                        "escaped_segment_occurrence_count": 1,
                        "outbound_request_sha256": "8" * 64,
                    }
                ),
                "sha256": "137cfcbdf2470bfecb3f148cc25d3789dd6e58887fd31a2ebe2c88c175d33209",
            }
        ),
    }
)


def _validated_answer_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or tuple(sorted(value)) != ANSWER_BINDING_FIELDS:
        raise ContractViolation("invalid_answer_binding")
    row = dict(value)
    row["owner_user_id"] = str(_uuid(row["owner_user_id"], "invalid_binding_owner"))
    row["response_id"] = str(_uuid(row["response_id"], "invalid_binding_response"))
    for field in (
        "binding_sha256",
        "policy_sha256",
        "prompt_sha256",
        "query_sha256",
        "renderer_sha256",
        "selection_manifest_sha256",
    ):
        row[field] = require_sha256(row[field], f"invalid_binding_{field}")
    for field in (
        "escaped_memory_segment_sha256",
        "injection_manifest_sha256",
        "memory_block_sha256",
        "outbound_request_sha256",
    ):
        if row[field] is not None:
            row[field] = require_sha256(row[field], f"invalid_binding_{field}")
    if type(row["explicit_recall"]) is not bool:
        raise ContractViolation("invalid_binding_explicit_recall")
    try:
        policy = RetrievalPolicy(
            explicit_recall=row["explicit_recall"],
            allowed_predicates=tuple(row["allowed_predicates"]),
            domains=tuple(row["domains"]),
            intents=tuple(row["intents"]),
            max_records=row["max_records"],
            policy_revision=row["policy_revision"],
        )
    except (TypeError, KeyError) as exc:
        raise ContractViolation("invalid_binding_retrieval_policy") from exc
    row["allowed_predicates"] = policy.allowed_predicates
    row["domains"] = policy.domains
    row["intents"] = policy.intents
    row["max_records"] = policy.max_records
    row["policy_revision"] = policy.policy_revision
    if row["policy_sha256"] != policy.policy_sha256:
        raise ContractViolation("binding_retrieval_policy_sha256_mismatch")
    for field in ("selected_count", "injected_count", "model_exposed_count"):
        row[field] = require_exact_int(
            row[field], code=f"invalid_binding_{field}", maximum=8
        )
    for field in (
        "selected_claim_ids",
        "selected_revision_ids",
        "injected_claim_ids",
        "injected_revision_ids",
        "model_exposed_revision_ids",
    ):
        row[field] = _uuid_sequence(row[field], f"invalid_binding_{field}")
    for field in (
        "selected_revision_sha256s",
        "injected_revision_sha256s",
        "model_exposed_revision_sha256s",
    ):
        row[field] = _sha256_sequence(row[field], f"invalid_binding_{field}")
    if (
        row["selected_count"] != len(row["selected_claim_ids"])
        or row["selected_count"] > policy.max_records
        or row["selected_count"] != len(row["selected_revision_ids"])
        or row["selected_count"] != len(row["selected_revision_sha256s"])
        or row["injected_count"] != len(row["injected_claim_ids"])
        or row["injected_count"] != len(row["injected_revision_ids"])
        or row["injected_count"] != len(row["injected_revision_sha256s"])
        or row["model_exposed_count"] != len(row["model_exposed_revision_ids"])
        or row["model_exposed_count"] != len(row["model_exposed_revision_sha256s"])
        or row["selected_claim_ids"][: row["injected_count"]] != row["injected_claim_ids"]
        or row["selected_revision_ids"][: row["injected_count"]] != row["injected_revision_ids"]
        or row["selected_revision_sha256s"][: row["injected_count"]] != row["injected_revision_sha256s"]
    ):
        raise ContractViolation("answer_binding_count_or_order_mismatch")
    sidecars = row["injected_records"]
    if not isinstance(sidecars, (list, tuple)) or len(sidecars) != row["injected_count"]:
        raise ContractViolation("invalid_binding_injected_records")
    normalized_sidecars: list[dict[str, Any]] = []
    for index, raw_sidecar in enumerate(sidecars):
        if not isinstance(raw_sidecar, Mapping) or tuple(sorted(raw_sidecar)) != MEMORY_RECORD_SIDECAR_FIELDS:
            raise ContractViolation("invalid_binding_injected_record")
        sidecar = dict(raw_sidecar)
        sidecar["claim_id"] = str(_uuid(sidecar["claim_id"], "invalid_binding_record_claim"))
        sidecar["revision_id"] = str(_uuid(sidecar["revision_id"], "invalid_binding_record_revision"))
        sidecar["projection_operation_id"] = str(_uuid(sidecar["projection_operation_id"], "invalid_binding_record_projection_operation"))
        sidecar["revision_number"] = require_exact_int(sidecar["revision_number"], code="invalid_binding_record_revision_number", minimum=1)
        sidecar["projection_sequence"] = require_exact_int(sidecar["projection_sequence"], code="invalid_binding_record_projection_sequence", minimum=1)
        for field in (
            "projection_manifest_sha256",
            "record_sha256",
            "retrieval_text_sha256",
            "revision_sha256",
            "selection_binding_sha256",
        ):
            sidecar[field] = require_sha256(sidecar[field], f"invalid_binding_record_{field}")
        if (
            sidecar["claim_id"] != row["injected_claim_ids"][index]
            or sidecar["revision_id"] != row["injected_revision_ids"][index]
            or sidecar["revision_sha256"] != row["injected_revision_sha256s"][index]
        ):
            raise ContractViolation("binding_record_lineage_order_mismatch")
        normalized_sidecars.append(sidecar)
    row["injected_records"] = tuple(normalized_sidecars)
    if type(row["proves_semantic_use"]) is not bool or row["proves_semantic_use"]:
        raise ContractViolation("invalid_binding_semantic_use_claim")

    final_outcome = (
        "exposed" if row["dispatch_state"] in {"prepared", "dispatched"}
        else row["outcome"]
    )
    expected_selection = answer_selection_manifest_sha256(
        owner_user_id=UUID(row["owner_user_id"]),
        response_id=UUID(row["response_id"]),
        query_sha256=row["query_sha256"],
        policy_sha256=row["policy_sha256"],
        renderer_sha256=row["renderer_sha256"],
        prompt_sha256=row["prompt_sha256"],
        explicit_recall=row["explicit_recall"],
        selected_claim_ids=row["selected_claim_ids"],
        selected_revision_ids=row["selected_revision_ids"],
        selected_revision_sha256s=row["selected_revision_sha256s"],
        outcome=final_outcome,
    )
    if row["selection_manifest_sha256"] != expected_selection:
        raise ContractViolation("answer_selection_manifest_sha256_mismatch")

    escaped_fields = (
        "escaped_memory_segment_sha256",
        "escaped_segment_start_utf8",
        "escaped_segment_end_utf8",
        "escaped_segment_occurrence_count",
        "outbound_request_sha256",
    )
    if row["dispatch_state"] == "prepared":
        if (
            row["outcome"] != "prepared_for_dispatch"
            or row["injected_count"] == 0
            or row["model_exposed_count"] != 0
            or row["injection_manifest_sha256"] is not None
            or any(row[field] is not None for field in escaped_fields)
            or row["memory_block_sha256"] is None
            or row["memory_block_byte_count"] is None
        ):
            raise ContractViolation("invalid_prepared_answer_binding")
        expected_binding = expected_selection
    elif row["dispatch_state"] == "not_applicable":
        if (
            row["outcome"] != "no_memory_selected"
            or row["selected_count"] != 0
            or row["injected_count"] != 0
            or row["model_exposed_count"] != 0
            or row["injection_manifest_sha256"] is not None
            or row["memory_block_sha256"] is not None
            or row["memory_block_byte_count"] is not None
            or any(row[field] is not None for field in escaped_fields)
        ):
            raise ContractViolation("invalid_not_applicable_answer_binding")
        expected_binding = expected_selection
    elif row["dispatch_state"] == "dispatched":
        if (
            row["outcome"] != "exposed"
            or row["injected_count"] == 0
            or row["model_exposed_revision_ids"] != row["injected_revision_ids"]
            or row["model_exposed_revision_sha256s"] != row["injected_revision_sha256s"]
            or row["memory_block_sha256"] is None
            or row["memory_block_byte_count"] is None
            or any(row[field] is None for field in escaped_fields)
            or row["injection_manifest_sha256"] is None
        ):
            raise ContractViolation("invalid_dispatched_answer_binding")
        expected_injection = answer_injection_manifest_sha256(
            selection_manifest_sha256=expected_selection,
            injected_claim_ids=row["injected_claim_ids"],
            injected_revision_ids=row["injected_revision_ids"],
            injected_revision_sha256s=row["injected_revision_sha256s"],
            memory_block_sha256=row["memory_block_sha256"],
            memory_block_utf8_bytes=row["memory_block_byte_count"],
            escaped_memory_segment_sha256=row["escaped_memory_segment_sha256"],
            escaped_segment_start_utf8=row["escaped_segment_start_utf8"],
            escaped_segment_end_utf8=row["escaped_segment_end_utf8"],
            escaped_segment_occurrence_count=row["escaped_segment_occurrence_count"],
            outbound_request_sha256=row["outbound_request_sha256"],
        )
        if row["injection_manifest_sha256"] != expected_injection:
            raise ContractViolation("answer_injection_manifest_sha256_mismatch")
        expected_binding = expected_injection
    else:
        raise ContractViolation("invalid_answer_dispatch_state")

    if row["memory_block_byte_count"] is not None:
        row["memory_block_byte_count"] = require_exact_int(
            row["memory_block_byte_count"],
            code="invalid_binding_memory_block_byte_count",
            minimum=1,
            maximum=MEMORY_BLOCK_MAX_UTF8_BYTES,
        )
    if row["binding_sha256"] != expected_binding:
        raise ContractViolation("answer_binding_sha256_mismatch")
    return row


def build_answer_binding(
    *,
    owner_user_id: UUID,
    response_id: UUID,
    rendered_context: Mapping[str, Any],
    selected_claims: Sequence[Mapping[str, Any]],
    query_sha256: str,
    policy: RetrievalPolicy,
    renderer_sha256: str,
    prompt_sha256: str,
) -> dict[str, object]:
    owner = _uuid(owner_user_id, "invalid_binding_owner")
    response = _uuid(response_id, "invalid_binding_response")
    if not isinstance(policy, RetrievalPolicy):
        raise ContractViolation("invalid_binding_retrieval_policy")
    for value, code in (
        (query_sha256, "invalid_binding_query_sha256"),
        (renderer_sha256, "invalid_binding_renderer_sha256"),
        (prompt_sha256, "invalid_binding_prompt_sha256"),
    ):
        require_sha256(value, code)
    if renderer_sha256 != ANSWER_RENDERER_SHA256:
        raise ContractViolation("answer_renderer_sha256_mismatch")
    recomputed = render_memory_context(
        owner,
        selected_claims,
        max_records=ANSWER_RECORD_LIMIT,
        max_bytes=MEMORY_BLOCK_MAX_UTF8_BYTES,
    )
    if not isinstance(rendered_context, Mapping) or dict(rendered_context) != recomputed:
        raise ContractViolation("rendered_context_recomputation_mismatch")
    selected_claim_ids: list[str] = []
    selected_revision_ids: list[str] = []
    selected_revision_sha256s: list[str] = []
    if len(selected_claims) > policy.max_records:
        raise ContractViolation("answer_policy_record_limit")
    for row in selected_claims:
        if _uuid(row.get("owner_user_id"), "invalid_binding_selected_owner") != owner:
            raise ContractViolation("cross_owner_answer_selection")
        if row.get("predicate") not in policy.allowed_predicates:
            raise ContractViolation("answer_selection_predicate_denied")
        row_domains = _key_list(
            row.get("domains"), "invalid_binding_selected_domains"
        )
        row_intents = _key_list(
            row.get("intents"), "invalid_binding_selected_intents"
        )
        if row_domains and policy.domains and not set(row_domains).intersection(
            policy.domains
        ):
            raise ContractViolation("answer_selection_domain_denied")
        if row_intents and policy.intents and not set(row_intents).intersection(
            policy.intents
        ):
            raise ContractViolation("answer_selection_intent_denied")
        if type(row.get("requires_explicit")) is not bool:
            raise ContractViolation("invalid_binding_selected_explicit_policy")
        if row["requires_explicit"] and not policy.explicit_recall:
            raise ContractViolation("explicit_recall_required")
        selected_claim_ids.append(str(_uuid(row.get("claim_id"), "invalid_binding_selected_claim")))
        selected_revision_ids.append(str(_uuid(row.get("revision_id"), "invalid_binding_selected_revision")))
        selected_revision_sha256s.append(require_sha256(row.get("revision_sha256"), "invalid_binding_selected_revision_sha256"))
    injected_records = tuple(recomputed["records"])
    injected_claim_ids = tuple(record["claim_id"] for record in injected_records)
    injected_revision_ids = tuple(record["revision_id"] for record in injected_records)
    injected_revision_sha256s = tuple(record["revision_sha256"] for record in injected_records)
    if not selected_claim_ids:
        outcome = "no_memory_selected"
        manifest_outcome = outcome
        dispatch_state = "not_applicable"
    else:
        if not injected_claim_ids:
            raise ContractViolation("answer_injection_prefix_empty")
        outcome = "prepared_for_dispatch"
        manifest_outcome = "exposed"
        dispatch_state = "prepared"
    selection_manifest = answer_selection_manifest_sha256(
        owner_user_id=owner,
        response_id=response,
        query_sha256=query_sha256,
        policy_sha256=policy.policy_sha256,
        renderer_sha256=renderer_sha256,
        prompt_sha256=prompt_sha256,
        explicit_recall=policy.explicit_recall,
        selected_claim_ids=selected_claim_ids,
        selected_revision_ids=selected_revision_ids,
        selected_revision_sha256s=selected_revision_sha256s,
        outcome=manifest_outcome,
    )
    has_injected = bool(injected_claim_ids)
    result = {
        "allowed_predicates": policy.allowed_predicates,
        "binding_sha256": selection_manifest,
        "dispatch_state": dispatch_state,
        "domains": policy.domains,
        "escaped_memory_segment_sha256": None,
        "escaped_segment_end_utf8": None,
        "escaped_segment_occurrence_count": None,
        "escaped_segment_start_utf8": None,
        "explicit_recall": policy.explicit_recall,
        "injected_claim_ids": injected_claim_ids,
        "injected_count": len(injected_claim_ids),
        "injected_records": injected_records,
        "injected_revision_ids": injected_revision_ids,
        "injected_revision_sha256s": injected_revision_sha256s,
        "injection_manifest_sha256": None,
        "intents": policy.intents,
        "max_records": policy.max_records,
        "memory_block_byte_count": recomputed["used_bytes"] if has_injected else None,
        "memory_block_sha256": recomputed["memory_block_sha256"] if has_injected else None,
        "model_exposed_count": 0,
        "model_exposed_revision_ids": (),
        "model_exposed_revision_sha256s": (),
        "outbound_request_sha256": None,
        "outcome": outcome,
        "owner_user_id": str(owner),
        "policy_revision": policy.policy_revision,
        "policy_sha256": policy.policy_sha256,
        "prompt_sha256": prompt_sha256,
        "proves_semantic_use": False,
        "query_sha256": query_sha256,
        "renderer_sha256": renderer_sha256,
        "response_id": str(response),
        "selected_claim_ids": tuple(selected_claim_ids),
        "selected_count": len(selected_claim_ids),
        "selected_revision_ids": tuple(selected_revision_ids),
        "selected_revision_sha256s": tuple(selected_revision_sha256s),
        "selection_manifest_sha256": selection_manifest,
    }
    return _validated_answer_binding(result)


def mark_answer_binding_dispatched(
    prepared_binding: Mapping[str, Any],
    rendered_context: Mapping[str, Any],
    *,
    outbound_request_bytes: bytes,
    escaped_segment_start_utf8: int,
    escaped_segment_end_utf8: int,
) -> dict[str, object]:
    prepared = _validated_answer_binding(prepared_binding)
    if prepared["dispatch_state"] != "prepared":
        raise ContractViolation("answer_binding_not_prepared")
    if not isinstance(rendered_context, Mapping) or tuple(sorted(rendered_context)) != RENDERED_CONTEXT_FIELDS:
        raise ContractViolation("invalid_dispatch_rendered_context")
    memory_block = rendered_context["content"]
    if not isinstance(memory_block, str) or not memory_block:
        raise ContractViolation("invalid_dispatch_memory_block")
    memory_block_bytes = memory_block.encode("utf-8")
    exact_escaped_segment = canonical_json_bytes(memory_block)
    if (
        rendered_context["memory_block_sha256"] != prepared["memory_block_sha256"]
        or sha256_bytes(memory_block_bytes) != prepared["memory_block_sha256"]
        or rendered_context["used_bytes"] != prepared["memory_block_byte_count"]
        or len(memory_block_bytes) != prepared["memory_block_byte_count"]
        or tuple(rendered_context["records"]) != prepared["injected_records"]
        or tuple(rendered_context["injected_revision_ids"]) != prepared["injected_revision_ids"]
    ):
        raise ContractViolation("dispatch_memory_block_binding_mismatch")
    if not isinstance(outbound_request_bytes, bytes) or not 1 <= len(outbound_request_bytes) <= OUTBOUND_REQUEST_MAX_UTF8_BYTES:
        raise ContractViolation("invalid_outbound_request_bytes")
    try:
        outbound_request_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractViolation("invalid_outbound_request_utf8") from exc
    segment_start = require_exact_int(
        escaped_segment_start_utf8,
        code="invalid_escaped_segment_start_utf8",
        maximum=len(outbound_request_bytes),
    )
    segment_end = require_exact_int(
        escaped_segment_end_utf8,
        code="invalid_escaped_segment_end_utf8",
        minimum=segment_start + 1,
        maximum=len(outbound_request_bytes),
    )
    occurrences = outbound_request_bytes.count(exact_escaped_segment)
    if outbound_request_bytes[segment_start:segment_end] != exact_escaped_segment or occurrences != 1:
        raise ContractViolation("outbound_escaped_memory_segment_mismatch")
    injection_manifest = answer_injection_manifest_sha256(
        selection_manifest_sha256=prepared["selection_manifest_sha256"],
        injected_claim_ids=prepared["injected_claim_ids"],
        injected_revision_ids=prepared["injected_revision_ids"],
        injected_revision_sha256s=prepared["injected_revision_sha256s"],
        memory_block_sha256=prepared["memory_block_sha256"],
        memory_block_utf8_bytes=prepared["memory_block_byte_count"],
        escaped_memory_segment_sha256=sha256_bytes(exact_escaped_segment),
        escaped_segment_start_utf8=segment_start,
        escaped_segment_end_utf8=segment_end,
        escaped_segment_occurrence_count=occurrences,
        outbound_request_sha256=sha256_bytes(outbound_request_bytes),
    )
    result = dict(prepared)
    result.update(
        {
            "binding_sha256": injection_manifest,
            "dispatch_state": "dispatched",
            "escaped_memory_segment_sha256": sha256_bytes(exact_escaped_segment),
            "escaped_segment_end_utf8": segment_end,
            "escaped_segment_occurrence_count": occurrences,
            "escaped_segment_start_utf8": segment_start,
            "injection_manifest_sha256": injection_manifest,
            "model_exposed_count": prepared["injected_count"],
            "model_exposed_revision_ids": prepared["injected_revision_ids"],
            "model_exposed_revision_sha256s": prepared["injected_revision_sha256s"],
            "outbound_request_sha256": sha256_bytes(outbound_request_bytes),
            "outcome": "exposed",
        }
    )
    return _validated_answer_binding(result)


__all__ = [
    "AUTHORITATIVE_RETRIEVAL_ROW_FIELDS",
    "ANSWER_BINDING_FIELDS",
    "ANSWER_RECORD_LIMIT",
    "ANSWER_RENDERER_SHA256",
    "ANSWER_INJECTION_MANIFEST_DOMAIN",
    "ANSWER_INJECTION_MANIFEST_FIELDS",
    "ANSWER_MANIFEST_TEST_VECTORS",
    "ANSWER_SELECTION_MANIFEST_DOMAIN",
    "ANSWER_SELECTION_MANIFEST_FIELDS",
    "CANDIDATE_FIELDS",
    "MEMORY_RECORD_SIDECAR_FIELDS",
    "PROMPT_MEMORY_RECORD_FIELDS",
    "RENDERED_CONTEXT_FIELDS",
    "RETRIEVAL_POLICY_DOMAIN",
    "RetrievalPolicy",
    "answer_injection_manifest_material_bytes",
    "answer_injection_manifest_sha256",
    "answer_selection_manifest_material_bytes",
    "answer_selection_manifest_sha256",
    "build_answer_binding",
    "mark_answer_binding_dispatched",
    "render_memory_context",
    "retrieval_policy_material_bytes",
    "retrieval_policy_sha256",
    "revalidate_candidates",
]
