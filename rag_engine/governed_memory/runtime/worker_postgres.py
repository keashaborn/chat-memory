from __future__ import annotations

"""PostgreSQL-authoritative repository for the one-shot successor worker."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Mapping
from uuid import UUID

from ..admission import recompute_claim_state_sha256
from ..contracts import (
    ContractViolation,
    canonical_sha256,
    require_key,
    require_sha256,
    require_utc,
    require_uuid,
)
from ..extraction import (
    CANONICAL_PREDICATE_CATALOG_SHA256,
    build_provider_request,
    parse_predicate_catalog,
)
from ..postgres_adapter import (
    extraction_lease_to_provider_inputs,
    normalize_postgres_record,
)
from ..projection import (
    EMBEDDING_MODEL,
    PROJECTION_CONTRACT_SHA256,
    PROJECTION_OUTBOX_FIELDS,
    RELATIONAL_RENDERER_SHA256,
    build_projection_delete,
)
from ..retrieval import AUTHORITATIVE_RETRIEVAL_ROW_FIELDS
from .once_worker import (
    ExtractionWork,
    OnceWorkerOutcome,
    OnceWorkerReceipt,
    ProjectionDeleteWork,
    ProjectionUpsertWork,
    WorkerLocalFailure,
    WorkKind,
    WorkerWork,
)
from .openai_adapters import (
    DispatchReceipt,
    EMBEDDING_ENDPOINT_SHA256,
    EXTRACTION_SCHEMA_KEY,
    ExtractionCompletion,
    OpenAIBeforeSendFailure,
    OpenAIOutcomeUnknownFailure,
    OpenAITerminalFailure,
    embedding_request_sha256,
)
from .pilot_marker import pilot_marker_from_row
from .qdrant_adapter import (
    QDRANT_ALIAS,
    QDRANT_PHYSICAL_COLLECTION,
    QdrantDeleteReceipt,
    QdrantUpsertReceipt,
    QdrantWriteOutcomeUnknown,
)
from .qdrant_transport import QdrantTransportFailure


EXTRACTION_SCHEMA_SHA256 = sha256(EXTRACTION_SCHEMA_KEY.encode("utf-8")).hexdigest()
PRIVACY_MANIFEST_SHA256 = sha256(
    b"governed-memory-successor-pilot-privacy-v1"
).hexdigest()
WORKER_RUNTIME_CONTRACT_SHA256 = sha256(
    b"governed-memory-successor-worker-runtime-v1"
).hexdigest()

_READ_PILOT_MARKER_SQL = "SELECT * FROM memory_private.read_pilot_marker()"
_READ_PILOT_CLOCK_SQL = "SELECT pg_catalog.transaction_timestamp()"
PILOT_MAXIMUM_DURATION = timedelta(days=14)
_NEXT_WORKER_LANE_SQL = "SELECT memory_private.next_worker_lane()"
_AUTO_ADMIT_SQL = (
    "SELECT * FROM memory_private.auto_admit_one_ordinary_proposal()"
)
_LEASE_EXTRACTION_SQL = (
    "SELECT * FROM memory_private.lease_extraction_jobs("
    "$1::text,1,$2::integer,'openai'::text,$3::text,$4::text,$5::text,"
    "'responses.create'::text,$6::text,$7::integer,$8::integer)"
)
_LEASE_PROJECTION_SQL = (
    "SELECT * FROM memory_private.lease_projection_jobs("
    "$1::text,1,$2::integer,$3::text)"
)
_MARK_PROVIDER_DISPATCHED_SQL = (
    "SELECT * FROM memory_private.mark_provider_call_dispatched("
    "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text,$7::text)"
)
_MARK_PROJECTION_EMBEDDING_DISPATCHED_SQL = (
    "SELECT * FROM memory_private.mark_projection_embedding_dispatched("
    "$1::uuid,$2::uuid,$3::text,$4::text,$5::text,$6::text,$7::text,"
    "$8::text)"
)
_COMPLETE_EXTRACTION_SQL = (
    "SELECT * FROM memory_private.complete_extraction("
    "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text,"
    "$7::integer,$8::integer,$9::text,$10::jsonb)"
)
_FINISH_PROJECTION_SQL = (
    "SELECT * FROM memory_private.finish_projection_job("
    "$1::uuid,$2::uuid,$3::text,$4::text,$5::text,$6::text,$7::text,$8::text)"
)
_FINALIZE_DELETION_SQL = (
    "SELECT * FROM memory_private.finalize_claim_deletion("
    "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::uuid,$6::uuid,$7::text,"
    "$8::integer,$9::text,$10::text,$11::timestamptz,$12::timestamptz,"
    "$13::text,$14::text)"
)


PROJECTION_LEASE_FIELDS = (
    "owner_user_id",
    "outbox_id",
    "claim_id",
    "revision_id",
    "revision_number",
    "operation_id",
    "operation",
    "sequence_number",
    "point_id",
    "collection_alias",
    "revision_sha256",
    "selection_binding_sha256",
    "predicate_catalog_sha256",
    "projection_contract_sha256",
    "dimensions",
    "embedding_model",
    "renderer_sha256",
    "projection_manifest_sha256",
    "retrieval_text",
    "retrieval_text_sha256",
    "embedding_input_sha256",
    "source_sha256",
    "lifecycle_state",
    "is_current",
    "predicate",
    "epistemic_state",
    "sensitivity",
    "domains",
    "intents",
    "surface",
    "requires_explicit",
    "projectable",
    "valid_from",
    "valid_to",
    "claim_updated_at",
    "state_sha256",
    "semantic_key_sha256",
    "claim_identity_sha256",
    "subject_entity_key",
    "subject_entity_type",
    "subject_display_name",
    "object_kind",
    "object_entity_key",
    "object_entity_type",
    "object_display_name",
    "object_literal",
    "lease_token",
    "lease_expires_at",
)


@dataclass(slots=True)
class _ExtractionMetadata:
    job_id: UUID
    lease_token: UUID
    provider_call_id: UUID
    request: Mapping[str, Any] | None


@dataclass(slots=True)
class _ProjectionMetadata:
    outbox_id: UUID
    lease_token: UUID
    owner_user_id: UUID
    claim_id: UUID
    revision_id: UUID
    operation_id: UUID
    revision_sha256: str
    state_sha256: str
    sequence_number: int
    projection_manifest_sha256: str
    lifecycle_state: str
    completion_timestamp: datetime | None = None


class PostgresOnceWorkerRepository:
    """One connection, one advisory-locked invocation, at most one queue claim."""

    def __init__(
        self,
        connection: Any,
        *,
        worker_id: str,
        extraction_model: str,
        predicate_catalog: Mapping[str, Any],
        expected_pilot_id: str,
        expected_pilot_contract_sha256: str,
        expected_authorization_receipt_sha256: str,
        lease_seconds: int = 120,
        max_output_tokens: int = 4_096,
        provider_timeout_ms: int = 30_000,
    ) -> None:
        if connection is None:
            raise ContractViolation("worker_postgres_connection_required")
        if worker_id != "governed-memory-pilot-worker-1":
            raise ContractViolation("worker_id_mismatch")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            raise ContractViolation("worker_lease_seconds_invalid")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8_192:
            raise ContractViolation("worker_max_output_tokens_invalid")
        if type(provider_timeout_ms) is not int or not 1_000 <= provider_timeout_ms <= 120_000:
            raise ContractViolation("worker_provider_timeout_invalid")
        catalog = parse_predicate_catalog(predicate_catalog)
        self._connection = connection
        self._worker_id = worker_id
        self._extraction_model = extraction_model
        self._predicate_catalog = predicate_catalog
        self._predicate_catalog_sha256 = catalog.catalog_sha256
        self._expected_pilot_id = require_key(
            expected_pilot_id,
            "invalid_expected_pilot_id",
        )
        self._expected_pilot_contract_sha256 = require_sha256(
            expected_pilot_contract_sha256,
            "invalid_expected_pilot_contract_sha256",
        )
        self._expected_authorization_receipt_sha256 = require_sha256(
            expected_authorization_receipt_sha256,
            "invalid_expected_authorization_receipt_sha256",
        )
        self._lease_seconds = lease_seconds
        self._max_output_tokens = max_output_tokens
        self._provider_timeout_ms = provider_timeout_ms
        self._claimed: dict[UUID, _ExtractionMetadata | _ProjectionMetadata] = {}

    @staticmethod
    def _rows(values: Any, code: str) -> list[dict[str, Any]]:
        rows = [dict(value) for value in values]
        if len(rows) > 1:
            raise ContractViolation(code)
        return rows

    @staticmethod
    def _uuid(value: object, code: str) -> UUID:
        try:
            parsed = value if isinstance(value, UUID) else UUID(str(value))
        except (TypeError, ValueError) as error:
            raise ContractViolation(code) from error
        return require_uuid(parsed, code)

    async def pilot_ever_started(self) -> bool:
        rows = self._rows(
            await self._connection.fetch(_READ_PILOT_MARKER_SQL),
            "pilot_marker_cardinality_violation",
        )
        if not rows:
            return False
        marker = pilot_marker_from_row(rows[0])
        if (
            marker.pilot_id != self._expected_pilot_id
            or marker.pilot_contract_sha256
            != self._expected_pilot_contract_sha256
            or marker.authorization_receipt_sha256
            != self._expected_authorization_receipt_sha256
        ):
            raise ContractViolation("pilot_marker_identity_mismatch")
        transaction_time = require_utc(
            await self._connection.fetchval(_READ_PILOT_CLOCK_SQL),
            "invalid_pilot_clock",
        )
        pilot_age = transaction_time - marker.started_at
        if pilot_age < timedelta(0) or pilot_age >= PILOT_MAXIMUM_DURATION:
            raise ContractViolation("pilot_marker_outside_authorized_window")
        return True

    async def auto_admit_one_ordinary_proposal(
        self,
    ) -> OnceWorkerReceipt | None:
        rows = self._rows(
            await self._connection.fetch(_AUTO_ADMIT_SQL),
            "automatic_admission_cardinality_violation",
        )
        if not rows:
            return None
        row = rows[0]
        if row.get("outcome") != "admitted":
            raise ContractViolation("invalid_automatic_admission_outcome")
        proposal_id = self._uuid(
            row.get("proposal_id"), "invalid_automatic_admission_proposal"
        )
        for field in ("claim_id", "revision_id", "outbox_id"):
            self._uuid(
                row.get(field), f"invalid_automatic_admission_{field}"
            )
        material = {
            "outcome": "admitted",
            "proposal_id": str(proposal_id),
            "claim_id": str(row["claim_id"]),
            "revision_id": str(row["revision_id"]),
            "outbox_id": str(row["outbox_id"]),
        }
        return OnceWorkerReceipt(
            outcome=OnceWorkerOutcome.COMPLETED,
            work_kind=WorkKind.ADMISSION,
            work_id=proposal_id,
            qdrant_preflight_sha256=None,
            receipt_sha256=canonical_sha256(
                "governed_memory.automatic_admission_receipt", material
            ),
        )

    async def next_worker_lane(self) -> str:
        lane = await self._connection.fetchval(_NEXT_WORKER_LANE_SQL)
        if lane not in {"bridge", "extraction", "projection"}:
            raise ContractViolation("invalid_worker_lane")
        return lane

    async def _claim_extraction(self) -> ExtractionWork | None:
        rows = self._rows(
            await self._connection.fetch(
                _LEASE_EXTRACTION_SQL,
                self._worker_id,
                self._lease_seconds,
                self._extraction_model,
                EXTRACTION_SCHEMA_SHA256,
                self._predicate_catalog_sha256,
                PRIVACY_MANIFEST_SHA256,
                self._max_output_tokens,
                self._provider_timeout_ms,
            ),
            "extraction_claim_cardinality_violation",
        )
        if not rows:
            return None
        lease = rows[0]
        inputs = extraction_lease_to_provider_inputs(lease)
        job = inputs["job"]
        lease_contract = inputs["lease"]
        if not isinstance(job, Mapping) or not isinstance(lease_contract, Mapping):
            raise ContractViolation("invalid_extraction_lease_mapping")
        job_id = self._uuid(job["job_id"], "invalid_worker_job_id")
        lease_token = self._uuid(
            lease_contract["lease_token"],
            "invalid_worker_lease_token",
        )
        provider_call_id = self._uuid(
            job["provider_call_id"],
            "invalid_worker_provider_call_id",
        )
        if inputs["context_lookup"] is not None:
            request: Mapping[str, Any] | None = None
            work = ExtractionWork(
                work_id=job_id,
                local_failure_code="local_serialization_failed_before_send",
            )
        else:
            built = build_provider_request(
                job,
                inputs["evidence"],  # type: ignore[arg-type]
                self._extraction_model,
                EXTRACTION_SCHEMA_KEY,
                self._predicate_catalog,
                bounded_context=None,
                max_output_tokens=self._max_output_tokens,
            )
            request = built
            work = ExtractionWork(work_id=job_id, provider_request=built)
        self._claimed[job_id] = _ExtractionMetadata(
            job_id=job_id,
            lease_token=lease_token,
            provider_call_id=provider_call_id,
            request=request,
        )
        return work

    @staticmethod
    def _projection_outbox(row: Mapping[str, Any]) -> dict[str, Any]:
        outbox = {
            "owner_user_id": str(row["owner_user_id"]),
            "claim_id": str(row["claim_id"]),
            "revision_id": str(row["revision_id"]),
            "operation_id": str(row["operation_id"]),
            "operation": row["operation"],
            "sequence_number": row["sequence_number"],
            "revision_sha256": row["revision_sha256"],
            "selection_binding_sha256": row["selection_binding_sha256"],
            "retrieval_text_sha256": row["retrieval_text_sha256"],
            "embedding_input_sha256": row["embedding_input_sha256"],
            "projection_contract_sha256": row["projection_contract_sha256"],
            "projection_manifest_sha256": row["projection_manifest_sha256"],
            "state": "claimed",
        }
        if tuple(outbox) != PROJECTION_OUTBOX_FIELDS:
            raise ContractViolation("invalid_projection_lease_outbox_mapping")
        return outbox

    @staticmethod
    def _projection_claim(row: Mapping[str, Any]) -> dict[str, Any]:
        aliases = {
            "projection_sequence": "sequence_number",
            "state_sha256": "state_sha256",
            "updated_at": "claim_updated_at",
        }
        claim = normalize_postgres_record(
            {
                field: row[aliases.get(field, field)]
                for field in AUTHORITATIVE_RETRIEVAL_ROW_FIELDS
            }
        )
        if tuple(sorted(claim)) != AUTHORITATIVE_RETRIEVAL_ROW_FIELDS:
            raise ContractViolation("invalid_projection_lease_claim_mapping")
        return claim

    async def _claim_projection(self) -> WorkerWork | None:
        rows = self._rows(
            await self._connection.fetch(
                _LEASE_PROJECTION_SQL,
                self._worker_id,
                self._lease_seconds,
                WORKER_RUNTIME_CONTRACT_SHA256,
            ),
            "projection_claim_cardinality_violation",
        )
        if not rows:
            return None
        row = rows[0]
        if tuple(row) != PROJECTION_LEASE_FIELDS:
            raise ContractViolation("invalid_projection_lease_row")
        if (
            str(row["point_id"]) != str(row["claim_id"])
            or row["collection_alias"] != QDRANT_ALIAS
            or row["predicate_catalog_sha256"]
            != CANONICAL_PREDICATE_CATALOG_SHA256
            or row["projection_contract_sha256"] != PROJECTION_CONTRACT_SHA256
            or row["dimensions"] != 3_072
            or row["embedding_model"] != EMBEDDING_MODEL
            or row["renderer_sha256"] != RELATIONAL_RENDERER_SHA256
            or row["is_current"] is not True
        ):
            raise ContractViolation("projection_lease_authority_mismatch")
        operation = row["operation"]
        if operation not in {"upsert", "delete"}:
            raise ContractViolation("invalid_projection_lease_operation")
        outbox = self._projection_outbox(row)
        claim: dict[str, Any] | None = None
        if row["operation"] == "upsert":
            claim = self._projection_claim(row)
            if claim["state_sha256"] != recompute_claim_state_sha256(claim):
                raise ContractViolation("projection_lease_state_sha256_mismatch")
        elif row["operation"] == "delete":
            require_sha256(
                row["state_sha256"],
                "invalid_projection_delete_state_sha256",
            )
            if row["lifecycle_state"] not in {
                "correction_pending",
                "retracted",
                "deletion_pending",
            }:
                raise ContractViolation("invalid_projection_delete_lifecycle")
        outbox_id = self._uuid(row["outbox_id"], "invalid_outbox_id")
        metadata = _ProjectionMetadata(
            outbox_id=outbox_id,
            lease_token=self._uuid(
                row["lease_token"], "invalid_projection_lease_token"
            ),
            owner_user_id=self._uuid(
                row["owner_user_id"], "invalid_projection_owner"
            ),
            claim_id=self._uuid(row["claim_id"], "invalid_projection_claim"),
            revision_id=self._uuid(
                row["revision_id"], "invalid_projection_revision"
            ),
            operation_id=self._uuid(
                row["operation_id"], "invalid_projection_operation"
            ),
            revision_sha256=require_sha256(
                row["revision_sha256"], "invalid_projection_revision_sha256"
            ),
            state_sha256=require_sha256(
                row["state_sha256"], "invalid_projection_state_sha256"
            ),
            sequence_number=int(row["sequence_number"]),
            projection_manifest_sha256=require_sha256(
                row["projection_manifest_sha256"],
                "invalid_projection_manifest_sha256",
            ),
            lifecycle_state=str(row["lifecycle_state"]),
        )
        self._claimed[outbox_id] = metadata
        if row["operation"] == "upsert":
            if claim is None:
                raise ContractViolation("invalid_projection_lease_claim_mapping")
            return ProjectionUpsertWork(
                work_id=outbox_id,
                claim=claim,
                outbox_record=outbox,
            )
        if row["operation"] == "delete":
            return ProjectionDeleteWork(
                work_id=outbox_id,
                delete_command=build_projection_delete(
                    outbox,
                    collection_alias=QDRANT_ALIAS,
                    physical_collection=QDRANT_PHYSICAL_COLLECTION,
                ),
            )
        raise AssertionError("closed projection operation was not returned")

    async def claim_one(self) -> WorkerWork | None:
        if self._claimed:
            raise ContractViolation("worker_repository_already_claimed")
        extraction = await self._claim_extraction()
        return extraction if extraction is not None else await self._claim_projection()

    async def claim_one_for_lane(self, lane: str) -> WorkerWork | None:
        if self._claimed:
            raise ContractViolation("worker_repository_already_claimed")
        if lane == "extraction":
            return await self._claim_extraction()
        if lane == "projection":
            return await self._claim_projection()
        raise ContractViolation("invalid_worker_lane")

    def _metadata(
        self, work: WorkerWork
    ) -> _ExtractionMetadata | _ProjectionMetadata:
        metadata = self._claimed.get(work.work_id)
        if metadata is None:
            raise ContractViolation("worker_completion_without_claim")
        return metadata

    async def mark_provider_dispatched(
        self,
        work: ExtractionWork,
        request: Mapping[str, Any],
    ) -> None:
        metadata = self._metadata(work)
        if not isinstance(metadata, _ExtractionMetadata) or metadata.request is None:
            raise ContractViolation("provider_dispatch_without_extraction_claim")
        row = await self._connection.fetchrow(
            _MARK_PROVIDER_DISPATCHED_SQL,
            metadata.job_id,
            metadata.lease_token,
            metadata.provider_call_id,
            request["request_sha256"],
            request["selected_sha256"],
            request["selection_binding_sha256"],
            request["predicate_catalog_sha256"],
        )
        if row is None or dict(row).get("outcome") != "dispatched":
            raise ContractViolation("provider_dispatch_receipt_invalid")

    async def mark_projection_embedding_dispatched(
        self,
        work: ProjectionUpsertWork,
        receipt: DispatchReceipt,
    ) -> None:
        metadata = self._metadata(work)
        if not isinstance(metadata, _ProjectionMetadata):
            raise ContractViolation("embedding_dispatch_without_projection_claim")
        expected_input_sha256 = require_sha256(
            work.outbox_record.get("embedding_input_sha256"),
            "invalid_projection_embedding_input_sha256",
        )
        if (
            not isinstance(receipt, DispatchReceipt)
            or receipt.input_sha256 != expected_input_sha256
            or receipt.endpoint_sha256 != EMBEDDING_ENDPOINT_SHA256
        ):
            raise ContractViolation("embedding_dispatch_receipt_mismatch")
        request_sha256 = embedding_request_sha256(receipt)
        row = await self._connection.fetchrow(
            _MARK_PROJECTION_EMBEDDING_DISPATCHED_SQL,
            metadata.outbox_id,
            metadata.lease_token,
            receipt.operation,
            receipt.model,
            receipt.endpoint_sha256,
            receipt.input_sha256,
            receipt.request_body_sha256,
            request_sha256,
        )
        value = dict(row) if row is not None else {}
        if (
            tuple(value) != ("outcome", "dispatched_at")
            or value["outcome"] != "dispatched"
            or not isinstance(value["dispatched_at"], datetime)
        ):
            raise ContractViolation("embedding_dispatch_receipt_invalid")

    async def _complete_extraction(
        self,
        work: ExtractionWork,
        result: object,
    ) -> None:
        metadata = self._metadata(work)
        if (
            not isinstance(metadata, _ExtractionMetadata)
            or metadata.request is None
            or not isinstance(result, ExtractionCompletion)
        ):
            raise ContractViolation("invalid_extraction_worker_completion")
        validated = result.validated_result
        receipt = validated.get("receipt") if isinstance(validated, Mapping) else None
        if not isinstance(receipt, Mapping):
            raise ContractViolation("invalid_extraction_completion_receipt")
        row = await self._connection.fetchrow(
            _COMPLETE_EXTRACTION_SQL,
            metadata.job_id,
            metadata.lease_token,
            metadata.provider_call_id,
            "completed",
            metadata.request["request_sha256"],
            validated["response_sha256"],
            receipt["input_tokens"],
            receipt["output_tokens"],
            None,
            validated["proposals"],
        )
        if row is None or dict(row).get("outcome") not in {"completed", "replayed"}:
            raise ContractViolation("invalid_extraction_database_completion")

    async def _finish_projection(
        self,
        work: ProjectionUpsertWork | ProjectionDeleteWork,
        result: object,
    ) -> None:
        metadata = self._metadata(work)
        if not isinstance(metadata, _ProjectionMetadata):
            raise ContractViolation("invalid_projection_worker_completion")
        if isinstance(work, ProjectionUpsertWork):
            if not isinstance(result, QdrantUpsertReceipt):
                raise ContractViolation("invalid_projection_upsert_receipt")
            arguments = (
                metadata.outbox_id,
                metadata.lease_token,
                "applied",
                result.physical_collection,
                result.vector_sha256,
                None,
                None,
                None,
            )
            row = await self._connection.fetchrow(_FINISH_PROJECTION_SQL, *arguments)
            if row is None or dict(row).get("outcome") not in {"applied", "replayed"}:
                raise ContractViolation("invalid_projection_database_completion")
            return
        if not isinstance(result, QdrantDeleteReceipt):
            raise ContractViolation("invalid_projection_delete_receipt")
        completion_time = metadata.completion_timestamp
        async with self._connection.transaction():
            if completion_time is None:
                completion_time = await self._connection.fetchval(
                    "SELECT pg_catalog.transaction_timestamp()"
                )
            row = await self._connection.fetchrow(
                _FINISH_PROJECTION_SQL,
                metadata.outbox_id,
                metadata.lease_token,
                "applied",
                result.physical_collection,
                None,
                result.absence_verification_sha256,
                result.verification_receipt_sha256,
                None,
            )
            if row is None or dict(row).get("outcome") not in {"applied", "replayed"}:
                raise ContractViolation("invalid_projection_database_completion")
            if metadata.lifecycle_state == "deletion_pending":
                finalized = await self._connection.fetchrow(
                    _FINALIZE_DELETION_SQL,
                    metadata.operation_id,
                    metadata.owner_user_id,
                    metadata.claim_id,
                    metadata.state_sha256,
                    metadata.outbox_id,
                    metadata.revision_id,
                    metadata.revision_sha256,
                    metadata.sequence_number,
                    metadata.projection_manifest_sha256,
                    result.physical_collection,
                    completion_time,
                    completion_time,
                    result.absence_verification_sha256,
                    result.verification_receipt_sha256,
                )
                if finalized is None or dict(finalized).get("outcome") not in {
                    "deleted",
                    "replayed",
                }:
                    raise ContractViolation("invalid_claim_deletion_finalization")
        metadata.completion_timestamp = completion_time

    async def complete(self, work: WorkerWork, result: object) -> None:
        if isinstance(work, ExtractionWork):
            await self._complete_extraction(work, result)
        elif isinstance(work, (ProjectionUpsertWork, ProjectionDeleteWork)):
            await self._finish_projection(work, result)
        else:
            raise ContractViolation("invalid_worker_completion_kind")

    @staticmethod
    def _projection_failure(error: Exception) -> tuple[str, str]:
        if isinstance(error, QdrantTransportFailure):
            if str(error) == "qdrant_request_rejected_before_send":
                return "failed_terminal", "projection_contract_violation"
            return "retryable", "qdrant_unavailable"
        if isinstance(error, QdrantWriteOutcomeUnknown):
            return "retryable", "qdrant_verification_inconclusive"
        if isinstance(error, OpenAIOutcomeUnknownFailure):
            return "failed_terminal", "embedding_dispatch_outcome_unknown"
        if isinstance(error, (OpenAIBeforeSendFailure, OpenAITerminalFailure)):
            return "failed_terminal", "embedding_contract_violation"
        if isinstance(error, ContractViolation):
            code = str(error)
            if "dimension" in code:
                return "failed_terminal", "qdrant_dimension_mismatch"
            if any(value in code for value in ("collection", "alias", "target")):
                return "failed_terminal", "qdrant_collection_mismatch"
            if any(value in code for value in ("readback", "outcome_unknown")):
                return "retryable", "qdrant_verification_inconclusive"
            if "receipt" in code:
                return "failed_terminal", "qdrant_receipt_contract_violation"
        return "failed_terminal", "projection_contract_violation"

    async def fail(self, work: WorkerWork, error: Exception) -> None:
        metadata = self._metadata(work)
        if isinstance(work, ExtractionWork):
            if not isinstance(metadata, _ExtractionMetadata):
                raise ContractViolation("invalid_extraction_failure_metadata")
            request_sha256: str | None = None
            if isinstance(error, WorkerLocalFailure):
                outcome = "retryable_failure"
                reason = error.reason_code
            elif isinstance(error, OpenAIBeforeSendFailure):
                outcome = "retryable_failure"
                reason = error.reason_code
            elif isinstance(error, OpenAITerminalFailure):
                outcome = "terminal_failure"
                reason = error.reason_code
                if metadata.request is not None:
                    request_sha256 = str(metadata.request["request_sha256"])
            elif isinstance(error, OpenAIOutcomeUnknownFailure):
                outcome = "outcome_unknown"
                reason = error.reason_code
                if metadata.request is not None:
                    request_sha256 = str(metadata.request["request_sha256"])
            else:
                outcome = "retryable_failure"
                reason = "local_serialization_failed_before_send"
            row = await self._connection.fetchrow(
                _COMPLETE_EXTRACTION_SQL,
                metadata.job_id,
                metadata.lease_token,
                metadata.provider_call_id,
                outcome,
                request_sha256,
                None,
                None,
                None,
                reason,
                [],
            )
            if row is None or dict(row).get("outcome") not in {
                outcome,
                "replayed",
            }:
                raise ContractViolation("invalid_extraction_failure_completion")
            return
        if not isinstance(metadata, _ProjectionMetadata):
            raise ContractViolation("invalid_projection_failure_metadata")
        outcome, reason = self._projection_failure(error)
        row = await self._connection.fetchrow(
            _FINISH_PROJECTION_SQL,
            metadata.outbox_id,
            metadata.lease_token,
            outcome,
            None,
            None,
            None,
            None,
            reason,
        )
        if row is None or dict(row).get("outcome") not in {
            outcome,
            "retryable",
            "failed_terminal",
            "replayed",
        }:
            raise ContractViolation("invalid_projection_failure_completion")


class PostgresWorkerLaneRepository:
    """Narrow one-lane view over the shared PostgreSQL work repository."""

    def __init__(
        self,
        repository: PostgresOnceWorkerRepository,
        *,
        lane: str,
    ) -> None:
        if not isinstance(repository, PostgresOnceWorkerRepository):
            raise ContractViolation("worker_repository_required")
        if lane not in {"extraction", "projection"}:
            raise ContractViolation("invalid_worker_lane")
        self._repository = repository
        self._lane = lane

    async def pilot_ever_started(self) -> bool:
        return await self._repository.pilot_ever_started()

    async def claim_one(self) -> WorkerWork | None:
        return await self._repository.claim_one_for_lane(self._lane)

    async def complete(self, work: WorkerWork, result: object) -> None:
        await self._repository.complete(work, result)

    async def fail(self, work: WorkerWork, error: Exception) -> None:
        await self._repository.fail(work, error)


__all__ = [
    "EXTRACTION_SCHEMA_SHA256",
    "PRIVACY_MANIFEST_SHA256",
    "PROJECTION_LEASE_FIELDS",
    "WORKER_RUNTIME_CONTRACT_SHA256",
    "PostgresOnceWorkerRepository",
    "PostgresWorkerLaneRepository",
]
