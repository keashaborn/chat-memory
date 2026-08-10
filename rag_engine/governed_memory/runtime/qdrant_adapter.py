from __future__ import annotations

"""Exact-target Qdrant operations for the clean Memory successor.

The adapter has no create, list, discovery, alias-swap, or adoption operation.
Its injected transport is the only network-capable dependency.
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from ..contracts import (
    ContractViolation,
    canonical_sha256,
    require_exact_int,
    require_key,
    require_sha256,
    require_sorted_unique,
    require_uuid,
)
from ..projection import (
    DEFAULT_DIMENSIONS,
    DISTANCE as PROJECTION_DISTANCE,
    QDRANT_PAYLOAD_FIELDS,
    normalize_embedding,
    qdrant_absence_verification_sha256,
    vector_sha256,
)
from ..retrieval import CANDIDATE_FIELDS, validate_vector_candidates
from .calibration import CalibrationDecision, SCORE_SCALE, qdrant_score_to_micros


QDRANT_ALIAS = "governed_memory_active"
QDRANT_PHYSICAL_COLLECTION = "governed_memory_9a54cf123493_000001"
_QDRANT_DISTANCE_BY_PROJECTION = MappingProxyType({"dot": "Dot"})
try:
    QDRANT_DISTANCE = _QDRANT_DISTANCE_BY_PROJECTION[PROJECTION_DISTANCE]
except KeyError as exc:
    raise RuntimeError("unsupported_projection_distance") from exc
QDRANT_VECTOR_SIZE = DEFAULT_DIMENSIONS
QDRANT_MAX_SEARCH_LIMIT = 8
QDRANT_REQUIRED_PAYLOAD_INDEXES = MappingProxyType(
    {
        "is_current": "bool",
        "lifecycle_state": "keyword",
        "owner_user_id": "keyword",
        "predicate": "keyword",
        "projectable": "bool",
        "requires_explicit": "bool",
    }
)
QDRANT_SEARCH_PAYLOAD_FIELDS = tuple(
    field for field in CANDIDATE_FIELDS if field != "score"
)


class QdrantTransport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]: ...


class QdrantWriteOutcomeUnknown(RuntimeError):
    """Transport proves dispatch but cannot prove the write response."""


@dataclass(frozen=True, slots=True, kw_only=True)
class QdrantPreflightReceipt:
    alias: str
    physical_collection: str
    vector_size: int
    distance: str
    payload_indexes: tuple[tuple[str, str], ...]
    receipt_sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class QdrantUpsertReceipt:
    point_id: UUID
    vector_sha256: str
    payload_sha256: str
    physical_collection: str
    resolved_by_readback: bool
    verification_receipt_sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class QdrantDeleteReceipt:
    point_id: UUID
    physical_collection: str
    collection_alias: str
    absence_verification_sha256: str
    verification_receipt_sha256: str


def _result(value: Mapping[str, Any], code: str) -> Any:
    if not isinstance(value, Mapping) or "result" not in value:
        raise ContractViolation(code)
    return value["result"]


def _uuid(value: object, code: str) -> UUID:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc
    return require_uuid(parsed, code)


def _index_type(value: object) -> str | None:
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, Mapping):
        candidate = value.get("data_type")
        return candidate.lower() if isinstance(candidate, str) else None
    return None


class ExactQdrantAdapter:
    """Qdrant v1 REST contract restricted to two configured resource names."""

    def __init__(self, transport: QdrantTransport) -> None:
        self._transport = transport

    async def _alias_target(self) -> str:
        response = await self._transport.request(
            "GET",
            f"/collections/{QDRANT_PHYSICAL_COLLECTION}/aliases",
        )
        result = _result(response, "qdrant_alias_response_invalid")
        if not isinstance(result, Mapping) or set(result) != {"aliases"}:
            raise ContractViolation("qdrant_alias_response_invalid")
        aliases = result.get("aliases")
        if not isinstance(aliases, list) or len(aliases) != 1:
            raise ContractViolation("qdrant_alias_missing_or_ambiguous")
        item = aliases[0]
        if not isinstance(item, Mapping) or set(item) != {
            "alias_name",
            "collection_name",
        }:
            raise ContractViolation("qdrant_alias_response_invalid")
        if item["alias_name"] != QDRANT_ALIAS:
            raise ContractViolation("qdrant_alias_identity_mismatch")
        target = item["collection_name"]
        if target != QDRANT_PHYSICAL_COLLECTION:
            raise ContractViolation("qdrant_alias_target_mismatch")
        return target

    async def preflight(self) -> QdrantPreflightReceipt:
        """Read only the configured alias and physical collection; never adopt."""

        target = await self._alias_target()
        response = await self._transport.request(
            "GET",
            f"/collections/{QDRANT_PHYSICAL_COLLECTION}",
        )
        result = _result(response, "qdrant_collection_response_invalid")
        if not isinstance(result, Mapping):
            raise ContractViolation("qdrant_collection_response_invalid")
        try:
            vectors = result["config"]["params"]["vectors"]
            payload_schema = result["payload_schema"]
        except (KeyError, TypeError) as exc:
            raise ContractViolation("qdrant_collection_response_invalid") from exc
        if not isinstance(vectors, Mapping):
            raise ContractViolation("qdrant_named_vectors_not_allowed")
        if vectors.get("size") != QDRANT_VECTOR_SIZE:
            raise ContractViolation("qdrant_dimension_mismatch")
        if vectors.get("distance") != QDRANT_DISTANCE:
            raise ContractViolation("qdrant_distance_mismatch")
        if not isinstance(payload_schema, Mapping):
            raise ContractViolation("qdrant_payload_schema_invalid")
        for field, expected_type in QDRANT_REQUIRED_PAYLOAD_INDEXES.items():
            if _index_type(payload_schema.get(field)) != expected_type:
                raise ContractViolation("qdrant_required_payload_index_missing")
        indexes = tuple(QDRANT_REQUIRED_PAYLOAD_INDEXES.items())
        material = {
            "alias": QDRANT_ALIAS,
            "distance": QDRANT_DISTANCE,
            "payload_indexes": [list(item) for item in indexes],
            "physical_collection": target,
            "vector_size": QDRANT_VECTOR_SIZE,
        }
        return QdrantPreflightReceipt(
            alias=QDRANT_ALIAS,
            distance=QDRANT_DISTANCE,
            payload_indexes=indexes,
            physical_collection=target,
            receipt_sha256=canonical_sha256(
                "governed_memory.qdrant_preflight_receipt",
                material,
            ),
            vector_size=QDRANT_VECTOR_SIZE,
        )

    async def _retrieve(
        self,
        collection: str,
        point_id: UUID,
        *,
        with_payload: bool,
        with_vector: bool,
    ) -> list[Mapping[str, Any]]:
        if collection not in {QDRANT_ALIAS, QDRANT_PHYSICAL_COLLECTION}:
            raise ContractViolation("qdrant_unconfigured_collection")
        response = await self._transport.request(
            "POST",
            f"/collections/{collection}/points",
            {
                "ids": [str(point_id)],
                "with_payload": with_payload,
                "with_vector": with_vector,
            },
        )
        result = _result(response, "qdrant_retrieve_response_invalid")
        if not isinstance(result, list) or not all(
            isinstance(item, Mapping) for item in result
        ):
            raise ContractViolation("qdrant_retrieve_response_invalid")
        return result

    @staticmethod
    def _validated_point(point: Mapping[str, Any]) -> tuple[UUID, str, str]:
        if not isinstance(point, Mapping) or tuple(sorted(point)) != (
            "payload",
            "payload_sha256",
            "point_id",
            "vector",
        ):
            raise ContractViolation("invalid_qdrant_projection_point")
        point_id = _uuid(point["point_id"], "invalid_qdrant_point_id")
        payload = point["payload"]
        vector = point["vector"]
        if not isinstance(payload, Mapping) or tuple(sorted(payload)) != QDRANT_PAYLOAD_FIELDS:
            raise ContractViolation("invalid_qdrant_projection_payload")
        payload_hash = canonical_sha256(
            "governed_memory.projection_payload",
            payload,
        )
        if payload_hash != require_sha256(
            point["payload_sha256"],
            "invalid_qdrant_payload_sha256",
        ):
            raise ContractViolation("qdrant_payload_sha256_mismatch")
        vector_hash = vector_sha256(vector)
        if vector_hash != require_sha256(
            payload.get("vector_sha256"),
            "invalid_qdrant_vector_sha256",
        ):
            raise ContractViolation("qdrant_vector_sha256_mismatch")
        if payload.get("claim_id") != str(point_id):
            raise ContractViolation("qdrant_point_claim_mismatch")
        return point_id, vector_hash, payload_hash

    async def _verify_upsert_readback(
        self,
        point: Mapping[str, Any],
        point_id: UUID,
        vector_hash: str,
        payload_hash: str,
    ) -> None:
        rows = await self._retrieve(
            QDRANT_PHYSICAL_COLLECTION,
            point_id,
            with_payload=True,
            with_vector=True,
        )
        if len(rows) != 1:
            raise ContractViolation("qdrant_upsert_outcome_unknown")
        row = rows[0]
        if _uuid(row.get("id"), "invalid_qdrant_readback_point_id") != point_id:
            raise ContractViolation("qdrant_upsert_readback_mismatch")
        payload = row.get("payload")
        vector = row.get("vector")
        if not isinstance(payload, Mapping) or not isinstance(vector, (list, tuple)):
            raise ContractViolation("qdrant_upsert_readback_mismatch")
        if tuple(sorted(payload)) != QDRANT_PAYLOAD_FIELDS:
            raise ContractViolation("qdrant_upsert_readback_mismatch")
        if canonical_sha256(
            "governed_memory.projection_payload",
            payload,
        ) != payload_hash or vector_sha256(vector) != vector_hash:
            raise ContractViolation("qdrant_upsert_readback_mismatch")
        if payload.get("vector_sha256") != vector_hash:
            raise ContractViolation("qdrant_upsert_readback_mismatch")

    async def upsert_projection_point(
        self,
        point: Mapping[str, Any],
    ) -> QdrantUpsertReceipt:
        point_id, vector_hash, payload_hash = self._validated_point(point)
        await self.preflight()
        resolved_by_readback = False
        response: Mapping[str, Any] | None = None
        try:
            response = await self._transport.request(
                "PUT",
                (
                    f"/collections/{QDRANT_PHYSICAL_COLLECTION}"
                    "/points?wait=true"
                ),
                {
                    "points": [
                        {
                            "id": str(point_id),
                            "payload": point["payload"],
                            "vector": point["vector"],
                        }
                    ]
                },
            )
        except QdrantWriteOutcomeUnknown:
            resolved_by_readback = True
        if response is not None:
            result = response.get("result") if isinstance(response, Mapping) else None
            if not isinstance(result, Mapping) or result.get("status") != "completed":
                resolved_by_readback = True
        await self._verify_upsert_readback(
            point,
            point_id,
            vector_hash,
            payload_hash,
        )
        material = {
            "payload_sha256": payload_hash,
            "physical_collection": QDRANT_PHYSICAL_COLLECTION,
            "point_id": str(point_id),
            "resolved_by_readback": resolved_by_readback,
            "vector_sha256": vector_hash,
        }
        return QdrantUpsertReceipt(
            payload_sha256=payload_hash,
            physical_collection=QDRANT_PHYSICAL_COLLECTION,
            point_id=point_id,
            resolved_by_readback=resolved_by_readback,
            vector_sha256=vector_hash,
            verification_receipt_sha256=canonical_sha256(
                "governed_memory.qdrant_upsert_receipt",
                material,
            ),
        )

    async def delete_projection_point(
        self,
        delete_command: Mapping[str, Any],
    ) -> QdrantDeleteReceipt:
        if not isinstance(delete_command, Mapping):
            raise ContractViolation("invalid_qdrant_delete_command")
        if delete_command.get("collection_alias") != QDRANT_ALIAS or delete_command.get(
            "physical_collection"
        ) != QDRANT_PHYSICAL_COLLECTION:
            raise ContractViolation("qdrant_delete_target_mismatch")
        point_id = _uuid(
            delete_command.get("point_id"),
            "invalid_qdrant_delete_point_id",
        )
        sequence = require_exact_int(
            delete_command.get("projection_sequence"),
            code="invalid_qdrant_delete_sequence",
            minimum=1,
        )
        await self.preflight()
        try:
            await self._transport.request(
                "POST",
                (
                    f"/collections/{QDRANT_PHYSICAL_COLLECTION}"
                    "/points/delete?wait=true"
                ),
                {"points": [str(point_id)]},
            )
        except QdrantWriteOutcomeUnknown:
            pass
        await self._alias_target()
        alias_rows = await self._retrieve(
            QDRANT_ALIAS,
            point_id,
            with_payload=False,
            with_vector=False,
        )
        physical_rows = await self._retrieve(
            QDRANT_PHYSICAL_COLLECTION,
            point_id,
            with_payload=False,
            with_vector=False,
        )
        absence_hash = qdrant_absence_verification_sha256(
            alias_absent=not alias_rows,
            alias_target_verified=True,
            collection_alias=QDRANT_ALIAS,
            physical_absent=not physical_rows,
            physical_collection=QDRANT_PHYSICAL_COLLECTION,
            point_id=point_id,
            projection_sequence=sequence,
        )
        material = {
            "absence_verification_sha256": absence_hash,
            "collection_alias": QDRANT_ALIAS,
            "physical_collection": QDRANT_PHYSICAL_COLLECTION,
            "point_id": str(point_id),
            "projection_sequence": sequence,
        }
        return QdrantDeleteReceipt(
            absence_verification_sha256=absence_hash,
            collection_alias=QDRANT_ALIAS,
            physical_collection=QDRANT_PHYSICAL_COLLECTION,
            point_id=point_id,
            verification_receipt_sha256=canonical_sha256(
                "governed_memory.qdrant_delete_verification_receipt",
                material,
            ),
        )

    async def search_owner_candidates(
        self,
        *,
        owner_user_id: UUID,
        query_vector: Sequence[float | int],
        allowed_predicates: Sequence[str],
        limit: int,
        calibration: CalibrationDecision,
    ) -> tuple[dict[str, Any], ...]:
        if not isinstance(calibration, CalibrationDecision):
            raise ContractViolation("invalid_retrieval_calibration")
        if not calibration.retrieval_enabled:
            return ()
        threshold = require_exact_int(
            calibration.threshold_micros,
            code="invalid_calibration_threshold",
            maximum=SCORE_SCALE,
        )
        owner = require_uuid(owner_user_id, "invalid_qdrant_search_owner")
        record_limit = require_exact_int(
            limit,
            code="invalid_qdrant_search_limit",
            minimum=1,
            maximum=QDRANT_MAX_SEARCH_LIMIT,
        )
        predicates = tuple(allowed_predicates)
        if not 1 <= len(predicates) <= 8:
            raise ContractViolation("invalid_qdrant_search_predicates")
        require_sorted_unique(predicates, code="invalid_qdrant_search_predicates")
        for predicate in predicates:
            require_key(predicate, "invalid_qdrant_search_predicate")
        vector = normalize_embedding(query_vector)
        await self.preflight()
        response = await self._transport.request(
            "POST",
            f"/collections/{QDRANT_ALIAS}/points/search",
            {
                "filter": {
                    "must": [
                        {
                            "key": "owner_user_id",
                            "match": {"value": str(owner)},
                        },
                        {
                            "key": "lifecycle_state",
                            "match": {"value": "active"},
                        },
                        {"key": "is_current", "match": {"value": True}},
                        {"key": "projectable", "match": {"value": True}},
                        {
                            "key": "predicate",
                            "match": {"any": list(predicates)},
                        },
                        {
                            "key": "requires_explicit",
                            "match": {"value": False},
                        },
                    ]
                },
                "limit": record_limit,
                "params": {"exact": True},
                "score_threshold": threshold / SCORE_SCALE,
                "vector": vector,
                "with_payload": {"include": list(QDRANT_SEARCH_PAYLOAD_FIELDS)},
                "with_vector": False,
            },
        )
        result = _result(response, "qdrant_search_response_invalid")
        if not isinstance(result, list) or len(result) > record_limit:
            raise ContractViolation("qdrant_search_response_invalid")
        candidates: list[dict[str, Any]] = []
        for hit in result:
            if not isinstance(hit, Mapping) or "vector" in hit:
                raise ContractViolation("qdrant_search_vector_returned")
            payload = hit.get("payload")
            if not isinstance(payload, Mapping) or tuple(sorted(payload)) != QDRANT_SEARCH_PAYLOAD_FIELDS:
                raise ContractViolation("qdrant_search_payload_not_allowlisted")
            if _uuid(hit.get("id"), "invalid_qdrant_search_point_id") != _uuid(
                payload.get("claim_id"),
                "invalid_qdrant_search_claim_id",
            ):
                raise ContractViolation("qdrant_search_point_claim_mismatch")
            score = hit.get("score")
            if qdrant_score_to_micros(score) < threshold:
                raise ContractViolation("qdrant_search_score_below_threshold")
            candidate = dict(payload)
            candidate["score"] = float(score)  # validated by fixed conversion above
            candidates.append(candidate)
        candidates.sort(key=lambda item: (-item["score"], item["claim_id"]))
        return validate_vector_candidates(owner, candidates)


__all__ = [
    "ExactQdrantAdapter",
    "QDRANT_ALIAS",
    "QDRANT_DISTANCE",
    "QDRANT_MAX_SEARCH_LIMIT",
    "QDRANT_PHYSICAL_COLLECTION",
    "QDRANT_REQUIRED_PAYLOAD_INDEXES",
    "QDRANT_SEARCH_PAYLOAD_FIELDS",
    "QDRANT_VECTOR_SIZE",
    "QdrantDeleteReceipt",
    "QdrantPreflightReceipt",
    "QdrantTransport",
    "QdrantUpsertReceipt",
    "QdrantWriteOutcomeUnknown",
]
