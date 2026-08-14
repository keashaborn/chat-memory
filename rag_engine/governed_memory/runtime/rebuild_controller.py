from __future__ import annotations

"""Successor-only, PostgreSQL-authoritative Qdrant rebuild orchestration.

The controller cannot discover resources or delete a collection. Preparation,
cutover, and rollback are separate calls so the old physical collection remains
available until a later, explicitly authorized retirement step.
"""

from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from ..contracts import (
    ContractViolation,
    canonical_sha256,
    require_exact_int,
    require_sha256,
    require_uuid,
    sha256_text,
)
from ..postgres_adapter import projection_rebuild_row_to_inputs
from ..projection import (
    EMBEDDING_MODEL,
    QDRANT_PAYLOAD_FIELDS,
    build_rebuild_projection_point,
)
from .qdrant_adapter import (
    QDRANT_ALIAS,
    QDRANT_DISTANCE,
    QDRANT_REQUIRED_PAYLOAD_INDEXES,
    QDRANT_VECTOR_SIZE,
)


_COLLECTION_RE = re.compile(
    r"governed_memory_9a54cf123493_(?P<generation>[0-9]{6})\Z",
    re.ASCII,
)
REBUILD_INITIAL_MANIFEST_SHA256 = sha256_text(
    "governed-memory-successor-rebuild-manifest-v1"
)


def _collection(value: object, code: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ContractViolation(code)
    match = _COLLECTION_RE.fullmatch(value)
    if match is None:
        raise ContractViolation(code)
    generation = int(match.group("generation"))
    if generation < 1:
        raise ContractViolation(code)
    return value, generation


def _uuid(value: object, code: str) -> UUID:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(code) from exc
    return require_uuid(parsed, code)


def rebuild_manifest_step_sha256(
    previous_sha256: str,
    index: int,
    point: Mapping[str, Any],
) -> str:
    payload = point.get("payload")
    if (
        not isinstance(payload, Mapping)
        or tuple(sorted(payload)) != QDRANT_PAYLOAD_FIELDS
    ):
        raise ContractViolation("invalid_rebuild_projection_point")
    return canonical_sha256(
        "governed_memory.successor_rebuild_manifest_step",
        {
            "index": index,
            "payload_sha256": require_sha256(
                point.get("payload_sha256"),
                "invalid_rebuild_payload_sha256",
            ),
            "point_id": str(_uuid(point.get("point_id"), "invalid_rebuild_point_id")),
            "previous_sha256": require_sha256(
                previous_sha256,
                "invalid_rebuild_previous_manifest",
            ),
            "vector_sha256": require_sha256(
                payload.get("vector_sha256"),
                "invalid_rebuild_vector_sha256",
            ),
        },
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RebuildEmbedding:
    vector: Sequence[float | int] = field(repr=False)
    model: str
    input_sha256: str

    def __post_init__(self) -> None:
        if self.model != EMBEDDING_MODEL:
            raise ContractViolation("rebuild_embedding_model_mismatch")
        require_sha256(self.input_sha256, "invalid_rebuild_embedding_input_sha256")
        if isinstance(self.vector, (str, bytes, bytearray)):
            raise ContractViolation("invalid_rebuild_embedding_vector")


@dataclass(frozen=True, slots=True, kw_only=True)
class RebuildTargetReceipt:
    target_collection: str
    point_count: int
    manifest_sha256: str
    verification_receipt_sha256: str

    def __post_init__(self) -> None:
        _collection(self.target_collection, "invalid_rebuild_target_collection")
        require_exact_int(
            self.point_count,
            code="invalid_rebuild_target_point_count",
            minimum=0,
        )
        require_sha256(self.manifest_sha256, "invalid_rebuild_target_manifest")
        require_sha256(
            self.verification_receipt_sha256,
            "invalid_rebuild_target_verification_receipt",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RebuildPrepareReceipt:
    alias: str
    source_collection: str
    target_collection: str
    point_count: int
    manifest_sha256: str
    target_verification_receipt_sha256: str
    receipt_sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RebuildAliasReceipt:
    alias: str
    previous_collection: str
    active_collection: str
    point_count: int
    manifest_sha256: str
    alias_verification_receipt_sha256: str
    receipt_sha256: str


class ProjectionRebuildRepository(Protocol):
    async def read_projection_rebuild_batch(
        self,
        *,
        after_owner_user_id: UUID | None,
        after_claim_id: UUID | None,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]: ...


class RebuildEmbeddingProvider(Protocol):
    async def embed_rebuild_text(self, text: str) -> RebuildEmbedding: ...


class RebuildQdrantStore(Protocol):
    async def create_empty_target(
        self,
        *,
        alias: str,
        source_collection: str,
        target_collection: str,
        vector_size: int,
        distance: str,
        payload_indexes: Mapping[str, str],
    ) -> None: ...

    async def upsert_rebuild_point(
        self,
        *,
        target_collection: str,
        point: Mapping[str, Any],
    ) -> None: ...

    async def verify_target(
        self,
        *,
        target_collection: str,
        expected_point_count: int,
        expected_manifest_sha256: str,
    ) -> RebuildTargetReceipt: ...

    async def swap_alias(
        self,
        *,
        alias: str,
        expected_current_collection: str,
        target_collection: str,
        point_count: int,
        manifest_sha256: str,
    ) -> str: ...


class SuccessorRebuildController:
    """Prepare and verify a new generation without deleting the old one."""

    def __init__(
        self,
        *,
        repository: ProjectionRebuildRepository,
        embedding_provider: RebuildEmbeddingProvider,
        qdrant: RebuildQdrantStore,
        batch_size: int = 100,
    ) -> None:
        if repository is None or embedding_provider is None or qdrant is None:
            raise ContractViolation("rebuild_dependency_missing")
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._qdrant = qdrant
        self._batch_size = require_exact_int(
            batch_size,
            code="invalid_rebuild_batch_size",
            minimum=1,
            maximum=1_000,
        )

    @staticmethod
    def _identities(source: object, target: object) -> tuple[str, str]:
        source_name, source_generation = _collection(
            source, "invalid_rebuild_source_collection"
        )
        target_name, target_generation = _collection(
            target, "invalid_rebuild_target_collection"
        )
        if target_generation != source_generation + 1:
            raise ContractViolation("rebuild_target_generation_not_next")
        return source_name, target_name

    async def prepare(
        self,
        *,
        source_collection: str,
        target_collection: str,
    ) -> RebuildPrepareReceipt:
        source, target = self._identities(source_collection, target_collection)
        await self._qdrant.create_empty_target(
            alias=QDRANT_ALIAS,
            source_collection=source,
            target_collection=target,
            vector_size=QDRANT_VECTOR_SIZE,
            distance=QDRANT_DISTANCE,
            payload_indexes=QDRANT_REQUIRED_PAYLOAD_INDEXES,
        )
        after_owner: UUID | None = None
        after_claim: UUID | None = None
        prior_key: tuple[UUID, UUID] | None = None
        point_count = 0
        manifest_sha256 = REBUILD_INITIAL_MANIFEST_SHA256
        while True:
            rows = await self._repository.read_projection_rebuild_batch(
                after_owner_user_id=after_owner,
                after_claim_id=after_claim,
                limit=self._batch_size,
            )
            if isinstance(rows, (str, bytes, bytearray)) or len(rows) > self._batch_size:
                raise ContractViolation("invalid_rebuild_batch")
            for raw_row in rows:
                inputs = projection_rebuild_row_to_inputs(raw_row)
                claim = inputs["claim"]
                outbox = inputs["outbox"]
                if not isinstance(claim, Mapping) or not isinstance(outbox, Mapping):
                    raise ContractViolation("invalid_rebuild_inputs")
                key = (
                    _uuid(claim.get("owner_user_id"), "invalid_rebuild_owner"),
                    _uuid(claim.get("claim_id"), "invalid_rebuild_claim"),
                )
                if prior_key is not None and key <= prior_key:
                    raise ContractViolation("rebuild_rows_not_strictly_ordered")
                prior_key = key
                if inputs["applied_physical_collection"] != source:
                    raise ContractViolation("rebuild_applied_collection_mismatch")
                retrieval_text = claim.get("retrieval_text")
                if not isinstance(retrieval_text, str):
                    raise ContractViolation("invalid_rebuild_retrieval_text")
                embedding = await self._embedding_provider.embed_rebuild_text(
                    retrieval_text
                )
                if (
                    not isinstance(embedding, RebuildEmbedding)
                    or embedding.input_sha256 != sha256_text(retrieval_text)
                ):
                    raise ContractViolation("rebuild_embedding_input_mismatch")
                point = build_rebuild_projection_point(
                    claim,
                    outbox,
                    embedding.vector,
                )
                payload = point["payload"]
                if not isinstance(payload, Mapping):
                    raise ContractViolation("invalid_rebuild_projection_point")
                if payload.get("vector_sha256") != inputs["expected_vector_sha256"]:
                    raise ContractViolation("rebuild_vector_sha256_drift")
                await self._qdrant.upsert_rebuild_point(
                    target_collection=target,
                    point=point,
                )
                point_count += 1
                manifest_sha256 = rebuild_manifest_step_sha256(
                    manifest_sha256,
                    point_count,
                    point,
                )
            if len(rows) < self._batch_size:
                break
            if prior_key is None:
                raise ContractViolation("rebuild_cursor_did_not_advance")
            after_owner, after_claim = prior_key
        verified = await self._qdrant.verify_target(
            target_collection=target,
            expected_point_count=point_count,
            expected_manifest_sha256=manifest_sha256,
        )
        if (
            not isinstance(verified, RebuildTargetReceipt)
            or verified.target_collection != target
            or verified.point_count != point_count
            or verified.manifest_sha256 != manifest_sha256
        ):
            raise ContractViolation("rebuild_target_verification_mismatch")
        material = {
            "alias": QDRANT_ALIAS,
            "manifest_sha256": manifest_sha256,
            "point_count": point_count,
            "source_collection": source,
            "target_collection": target,
            "target_verification_receipt_sha256": (
                verified.verification_receipt_sha256
            ),
        }
        return RebuildPrepareReceipt(
            **material,
            receipt_sha256=canonical_sha256(
                "governed_memory.successor_rebuild_prepare_receipt",
                material,
            ),
        )

    async def cutover(self, prepared: RebuildPrepareReceipt) -> RebuildAliasReceipt:
        return await self._move_alias(prepared, rollback=False)

    async def rollback(self, prepared: RebuildPrepareReceipt) -> RebuildAliasReceipt:
        return await self._move_alias(prepared, rollback=True)

    async def _move_alias(
        self,
        prepared: RebuildPrepareReceipt,
        *,
        rollback: bool,
    ) -> RebuildAliasReceipt:
        if not isinstance(prepared, RebuildPrepareReceipt):
            raise ContractViolation("invalid_rebuild_prepare_receipt")
        expected_receipt = canonical_sha256(
            "governed_memory.successor_rebuild_prepare_receipt",
            {
                "alias": prepared.alias,
                "manifest_sha256": prepared.manifest_sha256,
                "point_count": prepared.point_count,
                "source_collection": prepared.source_collection,
                "target_collection": prepared.target_collection,
                "target_verification_receipt_sha256": (
                    prepared.target_verification_receipt_sha256
                ),
            },
        )
        if prepared.alias != QDRANT_ALIAS or prepared.receipt_sha256 != expected_receipt:
            raise ContractViolation("rebuild_prepare_receipt_mismatch")
        previous = (
            prepared.target_collection if rollback else prepared.source_collection
        )
        active = prepared.source_collection if rollback else prepared.target_collection
        alias_receipt = require_sha256(
            await self._qdrant.swap_alias(
                alias=prepared.alias,
                expected_current_collection=previous,
                target_collection=active,
                point_count=prepared.point_count,
                manifest_sha256=prepared.manifest_sha256,
            ),
            "invalid_rebuild_alias_verification_receipt",
        )
        material = {
            "active_collection": active,
            "alias": prepared.alias,
            "alias_verification_receipt_sha256": alias_receipt,
            "manifest_sha256": prepared.manifest_sha256,
            "point_count": prepared.point_count,
            "previous_collection": previous,
        }
        domain = (
            "governed_memory.successor_rebuild_rollback_receipt"
            if rollback
            else "governed_memory.successor_rebuild_cutover_receipt"
        )
        return RebuildAliasReceipt(
            **material,
            receipt_sha256=canonical_sha256(domain, material),
        )


__all__ = [
    "ProjectionRebuildRepository",
    "RebuildAliasReceipt",
    "RebuildEmbedding",
    "RebuildEmbeddingProvider",
    "RebuildPrepareReceipt",
    "RebuildQdrantStore",
    "RebuildTargetReceipt",
    "REBUILD_INITIAL_MANIFEST_SHA256",
    "SuccessorRebuildController",
    "rebuild_manifest_step_sha256",
]
