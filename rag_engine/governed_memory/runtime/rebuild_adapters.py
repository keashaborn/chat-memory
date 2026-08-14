from __future__ import annotations

"""Closed live adapters for the successor rebuild controller."""

import asyncio
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import UUID

from ..contracts import (
    ContractViolation,
    canonical_sha256,
    require_exact_int,
    require_sha256,
    sha256_text,
)
from ..postgres_adapter import PROJECTION_REBUILD_ROW_FIELDS
from ..projection import (
    EMBEDDING_MODEL,
    QDRANT_PAYLOAD_FIELDS,
    vector_sha256,
)
from .openai_adapters import (
    DispatchReceipt,
    OpenAIEmbeddingAdapter,
)
from .qdrant_adapter import (
    QDRANT_ALIAS,
    QDRANT_DISTANCE,
    QDRANT_REQUIRED_PAYLOAD_INDEXES,
    QDRANT_VECTOR_SIZE,
)
from .rebuild_controller import (
    REBUILD_INITIAL_MANIFEST_SHA256,
    RebuildEmbedding,
    RebuildTargetReceipt,
    rebuild_manifest_step_sha256,
)
from .rebuild_qdrant_transport import RebuildQdrantResponse


_READ_REBUILD_BATCH_SQL = (
    "SELECT * FROM memory_private.read_projection_rebuild_batch("
    "$1::uuid,$2::uuid,$3::integer)"
)
_CREATE_BODY = MappingProxyType(
    {
        "vectors": MappingProxyType(
            {"size": QDRANT_VECTOR_SIZE, "distance": QDRANT_DISTANCE}
        ),
        "on_disk_payload": True,
        "replication_factor": 1,
    }
)


class RebuildAdminTransport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        accepted_statuses: frozenset[int] = frozenset({200}),
    ) -> RebuildQdrantResponse: ...


def _result(response: RebuildQdrantResponse, code: str) -> Any:
    if not isinstance(response, RebuildQdrantResponse) or "result" not in response.body:
        raise ContractViolation(code)
    return response.body["result"]


def _index_type(value: object) -> str | None:
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, Mapping):
        data_type = value.get("data_type")
        return data_type.lower() if isinstance(data_type, str) else None
    return None


def _mutation_ack(response: RebuildQdrantResponse) -> None:
    result = _result(response, "rebuild_qdrant_mutation_response_invalid")
    if result is True and response.body.get("status") == "ok":
        return
    if isinstance(result, Mapping) and result.get("status") == "completed":
        return
    raise ContractViolation("rebuild_qdrant_mutation_not_acknowledged")


class PostgresProjectionRebuildRepository:
    def __init__(self, connection: Any) -> None:
        if connection is None:
            raise ContractViolation("rebuild_postgres_connection_required")
        self._connection = connection

    async def read_projection_rebuild_batch(
        self,
        *,
        after_owner_user_id: UUID | None,
        after_claim_id: UUID | None,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        checked_limit = require_exact_int(
            limit,
            code="invalid_rebuild_batch_size",
            minimum=1,
            maximum=1_000,
        )
        if (after_owner_user_id is None) != (after_claim_id is None):
            raise ContractViolation("incomplete_rebuild_cursor")
        rows = await self._connection.fetch(
            _READ_REBUILD_BATCH_SQL,
            after_owner_user_id,
            after_claim_id,
            checked_limit,
        )
        result = tuple(dict(row) for row in rows)
        if len(result) > checked_limit or any(
            tuple(row) != PROJECTION_REBUILD_ROW_FIELDS for row in result
        ):
            raise ContractViolation("invalid_projection_rebuild_batch")
        return result


class OpenAIRebuildEmbeddingProvider:
    def __init__(self, adapter: OpenAIEmbeddingAdapter) -> None:
        if not isinstance(adapter, OpenAIEmbeddingAdapter):
            raise ContractViolation("rebuild_embedding_adapter_required")
        self._adapter = adapter

    async def embed_rebuild_text(self, text: str) -> RebuildEmbedding:
        dispatches: list[DispatchReceipt] = []

        def mark_dispatched(receipt: DispatchReceipt) -> None:
            if dispatches:
                raise ContractViolation("duplicate_rebuild_embedding_dispatch")
            dispatches.append(receipt)

        completion = await asyncio.to_thread(
            self._adapter.invoke,
            text,
            mark_dispatched=mark_dispatched,
        )
        if (
            len(dispatches) != 1
            or completion.dispatch_receipt != dispatches[0]
            or completion.model != EMBEDDING_MODEL
            or completion.dimensions != QDRANT_VECTOR_SIZE
            or completion.dispatch_receipt.input_sha256 != sha256_text(text)
        ):
            raise ContractViolation("rebuild_embedding_completion_mismatch")
        return RebuildEmbedding(
            vector=completion.vector,
            model=completion.model,
            input_sha256=completion.dispatch_receipt.input_sha256,
        )


class ExactRebuildQdrantStore:
    """Create one next-generation target, verify it, and atomically move alias."""

    def __init__(self, transport: RebuildAdminTransport) -> None:
        if transport is None:
            raise ContractViolation("rebuild_qdrant_transport_required")
        self._transport = transport
        self._source: str | None = None
        self._target: str | None = None

    async def _aliases(self, collection: str) -> tuple[dict[str, Any], ...]:
        response = await self._transport.request(
            "GET", f"/collections/{collection}/aliases"
        )
        result = _result(response, "rebuild_qdrant_alias_response_invalid")
        if not isinstance(result, Mapping) or set(result) != {"aliases"}:
            raise ContractViolation("rebuild_qdrant_alias_response_invalid")
        aliases = result["aliases"]
        if not isinstance(aliases, list):
            raise ContractViolation("rebuild_qdrant_alias_response_invalid")
        checked: list[dict[str, Any]] = []
        for row in aliases:
            if not isinstance(row, Mapping) or set(row) != {
                "alias_name",
                "collection_name",
            }:
                raise ContractViolation("rebuild_qdrant_alias_response_invalid")
            checked.append(dict(row))
        return tuple(checked)

    async def _inspect(
        self,
        collection: str,
        *,
        allow_absent: bool = False,
    ) -> tuple[int, dict[str, str]] | None:
        response = await self._transport.request(
            "GET",
            f"/collections/{collection}",
            accepted_statuses=(
                frozenset({200, 404}) if allow_absent else frozenset({200})
            ),
        )
        if response.status == 404:
            return None
        result = _result(response, "rebuild_qdrant_collection_response_invalid")
        if not isinstance(result, Mapping):
            raise ContractViolation("rebuild_qdrant_collection_response_invalid")
        try:
            vectors = result["config"]["params"]["vectors"]
            payload_schema = result["payload_schema"]
            point_count = result["points_count"]
        except (KeyError, TypeError) as exc:
            raise ContractViolation(
                "rebuild_qdrant_collection_response_invalid"
            ) from exc
        if (
            not isinstance(vectors, Mapping)
            or vectors.get("size") != QDRANT_VECTOR_SIZE
            or vectors.get("distance") != QDRANT_DISTANCE
            or not isinstance(payload_schema, Mapping)
        ):
            raise ContractViolation("rebuild_qdrant_collection_contract_mismatch")
        count = require_exact_int(
            point_count,
            code="invalid_rebuild_qdrant_point_count",
            minimum=0,
        )
        indexes = {
            str(field): str(kind)
            for field, value in payload_schema.items()
            if (kind := _index_type(value)) is not None
        }
        return count, indexes

    async def create_empty_target(
        self,
        *,
        alias: str,
        source_collection: str,
        target_collection: str,
        vector_size: int,
        distance: str,
        payload_indexes: Mapping[str, str],
    ) -> None:
        if (
            alias != QDRANT_ALIAS
            or source_collection == target_collection
            or vector_size != QDRANT_VECTOR_SIZE
            or distance != QDRANT_DISTANCE
            or dict(payload_indexes) != dict(QDRANT_REQUIRED_PAYLOAD_INDEXES)
        ):
            raise ContractViolation("rebuild_qdrant_identity_mismatch")
        if await self._aliases(source_collection) != (
            {"alias_name": alias, "collection_name": source_collection},
        ):
            raise ContractViolation("rebuild_qdrant_source_alias_mismatch")
        source = await self._inspect(source_collection)
        if source is None:
            raise ContractViolation("rebuild_qdrant_source_absent")
        for field, expected in QDRANT_REQUIRED_PAYLOAD_INDEXES.items():
            if source[1].get(field) != expected:
                raise ContractViolation("rebuild_qdrant_source_index_mismatch")
        target = await self._inspect(target_collection, allow_absent=True)
        if target is None:
            response = await self._transport.request(
                "PUT",
                f"/collections/{target_collection}",
                _CREATE_BODY,
            )
            _mutation_ack(response)
            target = await self._inspect(target_collection)
        if target is None:
            raise ContractViolation("rebuild_qdrant_target_absent")
        indexes = target[1]
        for field, expected in QDRANT_REQUIRED_PAYLOAD_INDEXES.items():
            existing = indexes.get(field)
            if existing is not None and existing != expected:
                raise ContractViolation("rebuild_qdrant_target_index_mismatch")
            if existing is None:
                response = await self._transport.request(
                    "PUT",
                    f"/collections/{target_collection}/index?wait=true",
                    {"field_name": field, "field_schema": expected},
                )
                _mutation_ack(response)
        final = await self._inspect(target_collection)
        if final is None or any(
            final[1].get(field) != expected
            for field, expected in QDRANT_REQUIRED_PAYLOAD_INDEXES.items()
        ):
            raise ContractViolation("rebuild_qdrant_target_contract_mismatch")
        self._source = source_collection
        self._target = target_collection

    async def upsert_rebuild_point(
        self,
        *,
        target_collection: str,
        point: Mapping[str, Any],
    ) -> None:
        if target_collection != self._target:
            raise ContractViolation("rebuild_qdrant_target_not_prepared")
        payload = point.get("payload")
        vector = point.get("vector")
        if (
            not isinstance(payload, Mapping)
            or tuple(sorted(payload)) != QDRANT_PAYLOAD_FIELDS
            or not isinstance(vector, (list, tuple))
        ):
            raise ContractViolation("invalid_rebuild_projection_point")
        payload_sha256 = canonical_sha256(
            "governed_memory.projection_payload", payload
        )
        if (
            point.get("payload_sha256") != payload_sha256
            or vector_sha256(vector) != payload.get("vector_sha256")
            or str(point.get("point_id")) != payload.get("claim_id")
        ):
            raise ContractViolation("rebuild_projection_point_hash_mismatch")
        response = await self._transport.request(
            "PUT",
            f"/collections/{target_collection}/points?wait=true",
            {
                "points": [
                    {
                        "id": str(point["point_id"]),
                        "payload": payload,
                        "vector": vector,
                    }
                ]
            },
        )
        _mutation_ack(response)
        readback = await self._transport.request(
            "POST",
            f"/collections/{target_collection}/points",
            {
                "ids": [str(point["point_id"])],
                "with_payload": True,
                "with_vector": True,
            },
        )
        rows = _result(readback, "rebuild_qdrant_readback_invalid")
        if not isinstance(rows, list) or len(rows) != 1:
            raise ContractViolation("rebuild_qdrant_readback_mismatch")
        row = rows[0]
        if (
            not isinstance(row, Mapping)
            or str(row.get("id")) != str(point["point_id"])
            or row.get("payload") != payload
            or not isinstance(row.get("vector"), list)
            or vector_sha256(row["vector"]) != payload.get("vector_sha256")
        ):
            raise ContractViolation("rebuild_qdrant_readback_mismatch")

    async def _scroll_points(
        self,
        target_collection: str,
        expected_point_count: int,
    ) -> list[dict[str, Any]]:
        points: list[dict[str, Any]] = []
        offset: object | None = None
        seen_offsets: set[str] = set()
        while True:
            body: dict[str, object] = {
                "limit": 256,
                "with_payload": True,
                "with_vector": True,
            }
            if offset is not None:
                body["offset"] = offset
            response = await self._transport.request(
                "POST",
                f"/collections/{target_collection}/points/scroll",
                body,
            )
            result = _result(response, "rebuild_qdrant_scroll_invalid")
            if not isinstance(result, Mapping) or not set(result).issubset(
                {"points", "next_page_offset"}
            ):
                raise ContractViolation("rebuild_qdrant_scroll_invalid")
            rows = result.get("points")
            if not isinstance(rows, list):
                raise ContractViolation("rebuild_qdrant_scroll_invalid")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ContractViolation("rebuild_qdrant_scroll_invalid")
                payload = row.get("payload")
                vector = row.get("vector")
                if (
                    not isinstance(payload, Mapping)
                    or tuple(sorted(payload)) != QDRANT_PAYLOAD_FIELDS
                    or not isinstance(vector, list)
                    or str(row.get("id")) != payload.get("claim_id")
                    or vector_sha256(vector) != payload.get("vector_sha256")
                ):
                    raise ContractViolation("rebuild_qdrant_scroll_point_invalid")
                points.append(
                    {
                        "point_id": str(row["id"]),
                        "payload": dict(payload),
                        "vector": vector,
                        "payload_sha256": canonical_sha256(
                            "governed_memory.projection_payload", payload
                        ),
                    }
                )
                if len(points) > expected_point_count:
                    raise ContractViolation("rebuild_qdrant_unexpected_points")
            offset = result.get("next_page_offset")
            if offset is None:
                return points
            offset_key = repr(offset)
            if offset_key in seen_offsets:
                raise ContractViolation("rebuild_qdrant_scroll_did_not_advance")
            seen_offsets.add(offset_key)

    async def verify_target(
        self,
        *,
        target_collection: str,
        expected_point_count: int,
        expected_manifest_sha256: str,
    ) -> RebuildTargetReceipt:
        if target_collection != self._target:
            raise ContractViolation("rebuild_qdrant_target_not_prepared")
        checked_count = require_exact_int(
            expected_point_count,
            code="invalid_rebuild_target_point_count",
            minimum=0,
        )
        expected_manifest = require_sha256(
            expected_manifest_sha256,
            "invalid_rebuild_target_manifest",
        )
        inspected = await self._inspect(target_collection)
        if inspected is None or inspected[0] != checked_count:
            raise ContractViolation("rebuild_qdrant_target_count_mismatch")
        points = await self._scroll_points(target_collection, checked_count)
        points.sort(
            key=lambda point: (
                str(point["payload"]["owner_user_id"]),
                str(point["point_id"]),
            )
        )
        manifest = REBUILD_INITIAL_MANIFEST_SHA256
        for index, point in enumerate(points, start=1):
            manifest = rebuild_manifest_step_sha256(manifest, index, point)
        if len(points) != checked_count or manifest != expected_manifest:
            raise ContractViolation("rebuild_qdrant_target_manifest_mismatch")
        material = {
            "manifest_sha256": manifest,
            "point_count": checked_count,
            "target_collection": target_collection,
        }
        return RebuildTargetReceipt(
            **material,
            verification_receipt_sha256=canonical_sha256(
                "governed_memory.successor_rebuild_target_verification_receipt",
                material,
            ),
        )

    async def swap_alias(
        self,
        *,
        alias: str,
        expected_current_collection: str,
        target_collection: str,
        point_count: int,
        manifest_sha256: str,
    ) -> str:
        if (
            alias != QDRANT_ALIAS
            or {expected_current_collection, target_collection}
            != {self._source, self._target}
            or expected_current_collection == target_collection
        ):
            raise ContractViolation("rebuild_qdrant_alias_identity_mismatch")
        await self.verify_target(
            target_collection=self._target or "",
            expected_point_count=point_count,
            expected_manifest_sha256=manifest_sha256,
        )
        if await self._aliases(expected_current_collection) != (
            {
                "alias_name": alias,
                "collection_name": expected_current_collection,
            },
        ):
            raise ContractViolation("rebuild_qdrant_alias_source_mismatch")
        if await self._aliases(target_collection):
            raise ContractViolation("rebuild_qdrant_alias_target_not_empty")
        response = await self._transport.request(
            "POST",
            "/collections/aliases",
            {
                "actions": [
                    {"delete_alias": {"alias_name": alias}},
                    {
                        "create_alias": {
                            "alias_name": alias,
                            "collection_name": target_collection,
                        }
                    },
                ]
            },
        )
        _mutation_ack(response)
        if await self._aliases(expected_current_collection):
            raise ContractViolation("rebuild_qdrant_alias_readback_mismatch")
        if await self._aliases(target_collection) != (
            {"alias_name": alias, "collection_name": target_collection},
        ):
            raise ContractViolation("rebuild_qdrant_alias_readback_mismatch")
        material = {
            "alias": alias,
            "manifest_sha256": require_sha256(
                manifest_sha256, "invalid_rebuild_target_manifest"
            ),
            "point_count": point_count,
            "previous_collection": expected_current_collection,
            "target_collection": target_collection,
        }
        return canonical_sha256(
            "governed_memory.successor_rebuild_alias_verification_receipt",
            material,
        )


__all__ = [
    "ExactRebuildQdrantStore",
    "OpenAIRebuildEmbeddingProvider",
    "PostgresProjectionRebuildRepository",
    "RebuildAdminTransport",
]
