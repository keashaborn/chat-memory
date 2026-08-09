from __future__ import annotations

"""One-item PostgreSQL-only OpenAI review admission contract.

The operation consumes an immutable ``v5_2_openai_packet_route_event`` already
stored by the provider packet router.  It admits exactly one reported-stance
observation, stages the stored relational bundle, records accepted entailment,
stages one claim projection, and records an explicit projection review.  It
does not materialize a claim, release an outbox row, write Qdrant, call a model,
or read a filesystem review artifact.
"""

import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Mapping, Protocol, Sequence
from uuid import UUID

from scripts.memory_v1_projection_v5_2_contract import (
    validate_packet,
    validate_projection_schema,
    validate_registry,
)
from scripts.memory_v1_v5_2_projection_dispatch import build_packet, stable_json


ACTOR_CONTEXT_VERSION = "memory_v1_verified_reviewer_context_v1"
COMMAND_VERSION = "memory_v1_openai_review_admission_command_v1"
RECEIPT_VERSION = "memory_v1_openai_review_admission_receipt_v1"
POLICY_VERSION = "memory_v1_openai_review_admission_policy_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OpenAIReviewAdmissionError(RuntimeError):
    """Raised when authority, state, replay, or lineage validation fails."""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _require_sha(value: object, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise OpenAIReviewAdmissionError(f"{label} must be a lowercase SHA-256")


def _row(row: Mapping[str, Any] | Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError) as exc:
        raise OpenAIReviewAdmissionError(
            f"database result is missing required field {key}"
        ) from exc


def _uuid(value: object, label: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise OpenAIReviewAdmissionError(f"{label} must be a UUID") from exc


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


@dataclass(frozen=True, slots=True)
class VerifiedReviewerContextV1:
    owner_user_id: UUID
    reviewer_type: Literal["user", "admin"]
    reviewer_ref: str
    authentication_manifest_sha256: str
    contract_version: Literal["memory_v1_verified_reviewer_context_v1"] = (
        ACTOR_CONTEXT_VERSION
    )

    def __post_init__(self) -> None:
        _uuid(self.owner_user_id, "owner_user_id")
        if self.reviewer_type not in {"user", "admin"}:
            raise OpenAIReviewAdmissionError("reviewer_type is invalid")
        if not self.reviewer_ref.strip() or len(self.reviewer_ref.encode()) > 500:
            raise OpenAIReviewAdmissionError("reviewer_ref is invalid")
        _require_sha(
            self.authentication_manifest_sha256,
            "authentication_manifest_sha256",
        )
        if self.contract_version != ACTOR_CONTEXT_VERSION:
            raise OpenAIReviewAdmissionError("reviewer context version is invalid")


@dataclass(frozen=True, slots=True)
class OpenAIReviewAdmissionCommandV1:
    entity_resolution_request_id: UUID
    route_event_id: UUID
    expected_stage_bundle_sha256: str
    plan_id: UUID
    entailment_request_id: UUID
    reason: str
    reason_codes: tuple[str, ...]
    contract_version: Literal["memory_v1_openai_review_admission_command_v1"] = (
        COMMAND_VERSION
    )

    def __post_init__(self) -> None:
        identifiers = (
            self.entity_resolution_request_id,
            self.route_event_id,
            self.plan_id,
            self.entailment_request_id,
        )
        for value, label in zip(
            identifiers,
            (
                "entity_resolution_request_id",
                "route_event_id",
                "plan_id",
                "entailment_request_id",
            ),
            strict=True,
        ):
            _uuid(value, label)
        if len(set(identifiers)) != len(identifiers):
            raise OpenAIReviewAdmissionError("command identifiers must be distinct")
        _require_sha(
            self.expected_stage_bundle_sha256,
            "expected_stage_bundle_sha256",
        )
        if not self.reason.strip() or len(self.reason.encode()) > 2000:
            raise OpenAIReviewAdmissionError("reason is invalid")
        if not 1 <= len(self.reason_codes) <= 20:
            raise OpenAIReviewAdmissionError("reason_codes cardinality is invalid")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise OpenAIReviewAdmissionError(
                "reason_codes must be sorted and unique"
            )
        if any(not code or len(code.encode()) > 120 for code in self.reason_codes):
            raise OpenAIReviewAdmissionError("reason_codes contain an invalid value")
        if self.contract_version != COMMAND_VERSION:
            raise OpenAIReviewAdmissionError("command version is invalid")

    def material(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "entity_resolution_request_id": str(
                self.entity_resolution_request_id
            ),
            "route_event_id": str(self.route_event_id),
            "expected_stage_bundle_sha256": self.expected_stage_bundle_sha256,
            "plan_id": str(self.plan_id),
            "entailment_request_id": str(self.entailment_request_id),
            "reason": self.reason.strip(),
            "reason_codes": list(self.reason_codes),
            "policy_version": POLICY_VERSION,
        }

    @property
    def command_manifest_sha256(self) -> str:
        return _sha256_text(_canonical(self.material()))


@dataclass(frozen=True, slots=True)
class OpenAIReviewAdmissionReceiptV1:
    owner_user_id_sha256: str
    command_manifest_sha256: str
    admission_manifest_sha256: str
    admission_id: UUID
    operation_id: UUID
    entailment_request_id: UUID
    route_event_id: UUID
    packet_id: UUID
    evidence_id: UUID
    batch_id: UUID
    observation_id: UUID
    plan_id: UUID
    projection_review_id: UUID
    projection_review_authorization_sha256: str
    projection_apply_manifest_sha256: str
    outcome: Literal["applied", "replayed"]
    rows_written: int
    receipt_sha256: str
    contract_version: Literal["memory_v1_openai_review_admission_receipt_v1"] = (
        RECEIPT_VERSION
    )

    def material(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "owner_user_id_sha256": self.owner_user_id_sha256,
            "command_manifest_sha256": self.command_manifest_sha256,
            "admission_manifest_sha256": self.admission_manifest_sha256,
            "admission_id": str(self.admission_id),
            "operation_id": str(self.operation_id),
            "entailment_request_id": str(self.entailment_request_id),
            "route_event_id": str(self.route_event_id),
            "packet_id": str(self.packet_id),
            "evidence_id": str(self.evidence_id),
            "batch_id": str(self.batch_id),
            "observation_id": str(self.observation_id),
            "plan_id": str(self.plan_id),
            "projection_review_id": str(self.projection_review_id),
            "projection_review_authorization_sha256": (
                self.projection_review_authorization_sha256
            ),
            "projection_apply_manifest_sha256": (
                self.projection_apply_manifest_sha256
            ),
            "outcome": self.outcome,
            "rows_written": self.rows_written,
            "external_model_calls": 0,
            "qdrant_writes": 0,
            "claim_writes": 0,
            "outbox_releases": 0,
        }

    def __post_init__(self) -> None:
        for label in (
            "owner_user_id_sha256",
            "command_manifest_sha256",
            "admission_manifest_sha256",
            "projection_review_authorization_sha256",
            "projection_apply_manifest_sha256",
            "receipt_sha256",
        ):
            _require_sha(getattr(self, label), label)
        if self.outcome not in {"applied", "replayed"}:
            raise OpenAIReviewAdmissionError("receipt outcome is invalid")
        if type(self.rows_written) is not int or self.rows_written < 0:
            raise OpenAIReviewAdmissionError("rows_written is invalid")
        if self.receipt_sha256 != self.binding_sha256():
            raise OpenAIReviewAdmissionError("receipt hash mismatch")

    def binding_sha256(self) -> str:
        values = (
            self.contract_version,
            self.owner_user_id_sha256,
            self.command_manifest_sha256,
            self.admission_manifest_sha256,
            str(self.admission_id),
            str(self.operation_id),
            str(self.entailment_request_id),
            str(self.route_event_id),
            str(self.packet_id),
            str(self.evidence_id),
            str(self.batch_id),
            str(self.observation_id),
            str(self.plan_id),
            str(self.projection_review_id),
            self.projection_review_authorization_sha256,
            self.projection_apply_manifest_sha256,
        )
        return _sha256_text("|".join(values))

    @classmethod
    def create(cls, **values: Any) -> "OpenAIReviewAdmissionReceiptV1":
        provided = values.pop("receipt_sha256", None)
        expected = _sha256_text(
            "|".join(
                (
                    RECEIPT_VERSION,
                    values["owner_user_id_sha256"],
                    values["command_manifest_sha256"],
                    values["admission_manifest_sha256"],
                    str(values["admission_id"]),
                    str(values["operation_id"]),
                    str(values["entailment_request_id"]),
                    str(values["route_event_id"]),
                    str(values["packet_id"]),
                    str(values["evidence_id"]),
                    str(values["batch_id"]),
                    str(values["observation_id"]),
                    str(values["plan_id"]),
                    str(values["projection_review_id"]),
                    values["projection_review_authorization_sha256"],
                    values["projection_apply_manifest_sha256"],
                )
            )
        )
        if provided is not None and provided != expected:
            raise OpenAIReviewAdmissionError(
                "database admission receipt hash mismatch"
            )
        return cls(receipt_sha256=expected, **values)


class _Transaction(Protocol):
    async def __aenter__(self) -> object: ...
    async def __aexit__(self, *args: object) -> object: ...


class AdmissionConnectionV1(Protocol):
    def is_in_transaction(self) -> bool: ...
    def transaction(self, **kwargs: object) -> _Transaction: ...
    async def execute(self, query: str, *args: object) -> object: ...
    async def fetchval(self, query: str, *args: object) -> object: ...
    async def fetchrow(self, query: str, *args: object) -> Mapping[str, Any] | None: ...


class VerifiedReviewerBinderV1(Protocol):
    async def bind_verified_reviewer(
        self,
        conn: AdmissionConnectionV1,
        reviewer: VerifiedReviewerContextV1,
    ) -> None: ...


def _require_top_level(conn: AdmissionConnectionV1) -> None:
    if conn.is_in_transaction():
        raise OpenAIReviewAdmissionError(
            "admission requires a top-level database connection"
        )


@asynccontextmanager
async def _serializable(
    conn: AdmissionConnectionV1,
) -> AsyncIterator[None]:
    _require_top_level(conn)
    async with conn.transaction(isolation="serializable"):
        yield


@lru_cache(maxsize=1)
def _projection_registry() -> dict[str, dict[str, Any]]:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "specs/memory_v1_projection_plan_v5_2.schema.json").read_text()
    )
    registry = json.loads(
        (root / "specs/memory_v1_predicate_registry_v5_2.json").read_text()
    )
    validate_projection_schema(schema)
    return validate_registry(registry)


async def _bind(
    conn: AdmissionConnectionV1,
    binder: VerifiedReviewerBinderV1,
    reviewer: VerifiedReviewerContextV1,
) -> None:
    await binder.bind_verified_reviewer(conn, reviewer)
    bound = await conn.fetchval("SELECT memory.current_actor_user_id()")
    if _uuid(bound, "bound actor") != reviewer.owner_user_id:
        raise OpenAIReviewAdmissionError("verified reviewer actor binding failed")


def _receipt_from_row(
    row: Mapping[str, Any],
    *,
    outcome: Literal["applied", "replayed"] | None = None,
    rows_written: int | None = None,
) -> OpenAIReviewAdmissionReceiptV1:
    database_outcome = str(_row(row, "outcome"))
    selected_outcome = outcome or database_outcome
    if selected_outcome not in {"applied", "replayed"}:
        raise OpenAIReviewAdmissionError("database receipt outcome is invalid")
    database_rows = int(_row(row, "rows_written"))
    return OpenAIReviewAdmissionReceiptV1.create(
        owner_user_id_sha256=str(_row(row, "owner_user_id_sha256")),
        command_manifest_sha256=str(_row(row, "command_manifest_sha256")),
        admission_manifest_sha256=str(_row(row, "admission_manifest_sha256")),
        admission_id=_uuid(_row(row, "admission_id"), "admission_id"),
        operation_id=_uuid(_row(row, "operation_id"), "operation_id"),
        entailment_request_id=_uuid(
            _row(row, "entailment_request_id"), "entailment_request_id"
        ),
        route_event_id=_uuid(_row(row, "route_event_id"), "route_event_id"),
        packet_id=_uuid(_row(row, "packet_id"), "packet_id"),
        evidence_id=_uuid(_row(row, "evidence_id"), "evidence_id"),
        batch_id=_uuid(_row(row, "batch_id"), "batch_id"),
        observation_id=_uuid(_row(row, "observation_id"), "observation_id"),
        plan_id=_uuid(_row(row, "plan_id"), "plan_id"),
        projection_review_id=_uuid(
            _row(row, "projection_review_id"), "projection_review_id"
        ),
        projection_review_authorization_sha256=str(
            _row(row, "projection_review_authorization_sha256")
        ),
        projection_apply_manifest_sha256=str(
            _row(row, "projection_apply_manifest_sha256")
        ),
        outcome=selected_outcome,
        rows_written=database_rows if rows_written is None else rows_written,
        receipt_sha256=str(_row(row, "receipt_sha256")),
    )


async def _read_durable_receipt(
    conn: AdmissionConnectionV1,
    *,
    reviewer: VerifiedReviewerContextV1,
    command: OpenAIReviewAdmissionCommandV1,
    admission_manifest_sha256: str,
) -> Mapping[str, Any] | None:
    return await conn.fetchrow(
        """
        SELECT * FROM memory.read_owner_openai_review_admission_receipt_v1(
          $1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10
        )
        """,
        command.route_event_id,
        command.expected_stage_bundle_sha256,
        command.entity_resolution_request_id,
        command.plan_id,
        command.entailment_request_id,
        reviewer.reviewer_type,
        reviewer.reviewer_ref,
        command.reason.strip(),
        stable_json(list(command.reason_codes)),
        admission_manifest_sha256,
    )


async def admit_reviewed_openai_observation_v1(
    conn: AdmissionConnectionV1,
    *,
    binder: VerifiedReviewerBinderV1,
    reviewer: VerifiedReviewerContextV1,
    command: OpenAIReviewAdmissionCommandV1,
) -> OpenAIReviewAdmissionReceiptV1:
    """Admit one stored review bundle and produce one reviewed claim projection."""

    async with _serializable(conn):
        await _bind(conn, binder, reviewer)
        reason_codes_json = stable_json(list(command.reason_codes))
        preflight = await conn.fetchrow(
            """
            SELECT * FROM memory.preflight_owner_openai_review_admission_v1(
              $1,$2,$3,$4,$5,$6::jsonb
            )
            """,
            command.route_event_id,
            command.expected_stage_bundle_sha256,
            reviewer.reviewer_type,
            reviewer.reviewer_ref,
            command.reason.strip(),
            reason_codes_json,
        )
        if preflight is None:
            raise OpenAIReviewAdmissionError("admission preflight returned no row")
        admission_manifest = str(_row(preflight, "admission_manifest_sha256"))
        _require_sha(admission_manifest, "admission_manifest_sha256")
        admitted = await conn.fetchrow(
            """
            SELECT * FROM memory.apply_owner_openai_review_admission_v1(
              $1,$2,$3,$4,$5,$6,$7::jsonb,$8
            )
            """,
            command.entity_resolution_request_id,
            command.route_event_id,
            command.expected_stage_bundle_sha256,
            reviewer.reviewer_type,
            reviewer.reviewer_ref,
            command.reason.strip(),
            reason_codes_json,
            admission_manifest,
        )
        if admitted is None:
            raise OpenAIReviewAdmissionError("admission apply returned no row")
        existing_receipt = await _read_durable_receipt(
            conn,
            reviewer=reviewer,
            command=command,
            admission_manifest_sha256=admission_manifest,
        )
        if existing_receipt is not None:
            return _receipt_from_row(existing_receipt)
        observation_id = _uuid(_row(admitted, "observation_id"), "observation_id")
        evidence_id = _uuid(_row(admitted, "evidence_id"), "evidence_id")
        source_row = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
            observation_id,
        )
        entailment_source = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_entailment_source_v5_2($1)",
            observation_id,
        )
        if source_row is None or entailment_source is None:
            raise OpenAIReviewAdmissionError("projection source is absent")
        source = dict(source_row)
        for field in ("object_literal", "project_scope", "temporal"):
            source[field] = _json_value(source.get(field))
        spans = _json_value(_row(entailment_source, "source_spans"))
        if (
            str(_row(source, "owner_user_id")) != str(reviewer.owner_user_id)
            or _uuid(_row(source, "evidence_id"), "source evidence_id") != evidence_id
            or str(_row(source, "predicate")) != "stance.reported"
            or str(_row(source, "projection_class")) != "reported_stance"
            or str(_row(source, "surface_policy"))
            != "relevant_recall_or_explicit_recall"
            or str(_row(source, "modality")) != "reported_belief"
            or str(_row(source, "polarity")) != "affirmed"
            or str(_row(source, "subject_entity_type")) != "self"
            or str(_row(source, "subject_entity_status")) != "active"
            or str(_row(source, "evidence_status")) != "active"
            or not isinstance(spans, list)
            or not spans
        ):
            raise OpenAIReviewAdmissionError(
                "staged observation is outside the governed claim MVP boundary"
            )
        packet = build_packet(str(reviewer.owner_user_id), source)
        validate_packet(packet, str(reviewer.owner_user_id), _projection_registry())
        packet_text = stable_json(packet)
        packet_preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_packet_v5_2($1,$2)",
            command.plan_id,
            packet_text,
        )
        entailment_preflight = await conn.fetchrow(
            """
            SELECT * FROM memory.preflight_observation_entailment_v5(
              $1,'accepted'::memory.observation_entailment_decision_v5,
              'predicate_entailment_v5_1_accepted',$2::jsonb,
              'system','memory_v1_openai_review_admission_v1'
            )
            """,
            observation_id,
            stable_json(spans),
        )
        if packet_preflight is None or entailment_preflight is None:
            raise OpenAIReviewAdmissionError("projection preflight returned no row")
        entailment = await conn.fetchrow(
            """
            SELECT * FROM memory.record_observation_entailment_v5(
              $1,$2,'accepted'::memory.observation_entailment_decision_v5,
              'predicate_entailment_v5_1_accepted',$3::jsonb,
              'system','memory_v1_openai_review_admission_v1',$4
            )
            """,
            command.entailment_request_id,
            observation_id,
            stable_json(spans),
            _row(entailment_preflight, "authorization_manifest_sha256"),
        )
        staged = await conn.fetchrow(
            "SELECT * FROM memory.stage_projection_plan_v5_2($1,$2,$3)",
            command.plan_id,
            packet_text,
            _row(packet_preflight, "owner_manifest_sha256"),
        )
        existing_receipt = await _read_durable_receipt(
            conn,
            reviewer=reviewer,
            command=command,
            admission_manifest_sha256=admission_manifest,
        )
        if existing_receipt is not None:
            return _receipt_from_row(existing_receipt)
        review_preflight = await conn.fetchrow(
            """
            SELECT * FROM memory.preflight_projection_review_v5(
              $1,'p01','authorized'::memory.projection_review_decision_v5,
              $2,$3,$4,$5::jsonb
            )
            """,
            command.plan_id,
            reviewer.reviewer_type,
            reviewer.reviewer_ref,
            command.reason.strip(),
            reason_codes_json,
        )
        if review_preflight is None:
            raise OpenAIReviewAdmissionError("projection review preflight returned no row")
        review_authorization = str(
            _row(review_preflight, "authorization_manifest_sha256")
        )
        reviewed = await conn.fetchrow(
            """
            SELECT * FROM memory.review_projection_v5(
              $1,'p01','authorized'::memory.projection_review_decision_v5,
              $2,$3,$4,$5::jsonb,$6
            )
            """,
            command.plan_id,
            reviewer.reviewer_type,
            reviewer.reviewer_ref,
            command.reason.strip(),
            reason_codes_json,
            review_authorization,
        )
        if reviewed is None:
            raise OpenAIReviewAdmissionError("projection review returned no row")
        projection_review_id = _uuid(
            _row(reviewed, "review_id"), "projection_review_id"
        )
        apply_preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_apply_v5($1,'p01',$2)",
            command.plan_id,
            projection_review_id,
        )
        if apply_preflight is None or str(_row(apply_preflight, "lane")) != "claim":
            raise OpenAIReviewAdmissionError("reviewed projection is not a claim lane")
        projection_apply_manifest = str(
            _row(apply_preflight, "apply_manifest_sha256")
        )
        _require_sha(review_authorization, "projection review authorization")
        _require_sha(projection_apply_manifest, "projection apply manifest")
        finalized = await _read_durable_receipt(
            conn,
            reviewer=reviewer,
            command=command,
            admission_manifest_sha256=admission_manifest,
        )
        if finalized is None:
            raise OpenAIReviewAdmissionError("admission finalization returned no row")
        outcomes = {
            str(_row(admitted, "outcome")),
            str(_row(entailment, "outcome")),
            str(_row(staged, "outcome")),
            str(_row(reviewed, "outcome")),
        }
        if not outcomes <= {"applied", "replayed"}:
            raise OpenAIReviewAdmissionError("admission chain outcome is invalid")
        outcome: Literal["applied", "replayed"] = (
            "replayed" if outcomes == {"replayed"} else "applied"
        )
        rows_written = sum(
            int(_row(value, "rows_written"))
            for value in (admitted, entailment, staged, reviewed)
        )
        receipt = _receipt_from_row(
            finalized,
            outcome=outcome,
            rows_written=rows_written,
        )
        if (
            receipt.admission_manifest_sha256 != admission_manifest
            or receipt.route_event_id != command.route_event_id
            or receipt.packet_id != _uuid(_row(admitted, "packet_id"), "packet_id")
            or receipt.evidence_id != evidence_id
            or receipt.batch_id != _uuid(_row(admitted, "batch_id"), "batch_id")
            or receipt.observation_id != observation_id
        ):
            raise OpenAIReviewAdmissionError("final admission receipt lineage conflicts")
    return receipt
