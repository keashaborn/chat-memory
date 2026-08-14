from __future__ import annotations

"""Dormant, injected-dependency, concurrency-one worker orchestration."""

import argparse
import asyncio
from dataclasses import dataclass
from enum import Enum
import json
import os
import sys
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from uuid import UUID

from ..contracts import ContractViolation, canonical_sha256, require_uuid
from ..exclusive_cutover import (
    ExclusiveMemoryConfigurationError,
    ExclusiveMemoryMode,
    exclusive_memory_mode,
)
from ..projection import build_projection_point, render_projection_surface
from .qdrant_adapter import (
    QdrantDeleteReceipt,
    QdrantPreflightReceipt,
    QdrantUpsertReceipt,
)


WORKER_MODE_ENV = "GOVERNED_MEMORY_WORKER_MODE"


class WorkKind(str, Enum):
    SOURCE_ERASURE = "source_erasure"
    INGEST = "ingest"
    ADMISSION = "admission"
    EXTRACTION = "extraction"
    PROJECTION_UPSERT = "projection_upsert"
    PROJECTION_DELETE = "projection_delete"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionWork:
    work_id: UUID
    provider_request: Mapping[str, Any] | None = None
    local_failure_code: str | None = None
    kind: WorkKind = WorkKind.EXTRACTION

    def __post_init__(self) -> None:
        require_uuid(self.work_id, "invalid_worker_work_id")
        if (self.provider_request is None) == (self.local_failure_code is None):
            raise ContractViolation("invalid_worker_provider_request")
        if self.provider_request is not None and not isinstance(
            self.provider_request,
            Mapping,
        ):
            raise ContractViolation("invalid_worker_provider_request")
        if self.local_failure_code not in {
            None,
            "local_serialization_failed_before_send",
        }:
            raise ContractViolation("invalid_worker_local_failure")


class WorkerLocalFailure(RuntimeError):
    """A claimed item that must fail before any external provider call."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionUpsertWork:
    work_id: UUID
    claim: Mapping[str, Any]
    outbox_record: Mapping[str, Any]
    kind: WorkKind = WorkKind.PROJECTION_UPSERT

    def __post_init__(self) -> None:
        require_uuid(self.work_id, "invalid_worker_work_id")
        if not isinstance(self.claim, Mapping) or not isinstance(
            self.outbox_record,
            Mapping,
        ):
            raise ContractViolation("invalid_worker_projection_upsert")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionDeleteWork:
    work_id: UUID
    delete_command: Mapping[str, Any]
    kind: WorkKind = WorkKind.PROJECTION_DELETE

    def __post_init__(self) -> None:
        require_uuid(self.work_id, "invalid_worker_work_id")
        if not isinstance(self.delete_command, Mapping):
            raise ContractViolation("invalid_worker_projection_delete")


WorkerWork = ExtractionWork | ProjectionUpsertWork | ProjectionDeleteWork


class OnceWorkerRepository(Protocol):
    async def pilot_ever_started(self) -> bool: ...

    async def claim_one(self) -> WorkerWork | None: ...

    async def complete(self, work: WorkerWork, result: object) -> None: ...

    async def fail(self, work: WorkerWork, error: Exception) -> None: ...


class ExtractionProvider(Protocol):
    async def extract(self, request: Mapping[str, Any]) -> object: ...


class EmbeddingProvider(Protocol):
    async def embed(
        self,
        work: ProjectionUpsertWork,
        text: str,
    ) -> Sequence[float | int]: ...


class WorkerQdrant(Protocol):
    async def preflight(self) -> QdrantPreflightReceipt: ...

    async def upsert_projection_point(
        self,
        point: Mapping[str, Any],
    ) -> QdrantUpsertReceipt: ...

    async def delete_projection_point(
        self,
        delete_command: Mapping[str, Any],
    ) -> QdrantDeleteReceipt: ...


class OnceWorkerOutcome(str, Enum):
    NO_WORK = "no_work"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True, kw_only=True)
class OnceWorkerReceipt:
    outcome: OnceWorkerOutcome
    work_kind: WorkKind | None
    work_id: UUID | None
    qdrant_preflight_sha256: str | None
    receipt_sha256: str


class OnceWorker:
    """Claim and execute at most one work item per explicit invocation."""

    def __init__(
        self,
        *,
        repository: OnceWorkerRepository,
        provider: ExtractionProvider,
        embedder: EmbeddingProvider,
        qdrant: WorkerQdrant,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._embedder = embedder
        self._qdrant = qdrant
        self._running = False

    async def run_once(self) -> OnceWorkerReceipt:
        if self._running:
            raise ContractViolation("worker_concurrency_one_exceeded")
        self._running = True
        try:
            started = await self._repository.pilot_ever_started()
            if started is not True:
                raise ContractViolation("pilot_never_started")
            work = await self._repository.claim_one()
            preflight_sha256: str | None = None
            if work is None:
                material = {
                    "outcome": OnceWorkerOutcome.NO_WORK.value,
                    "qdrant_preflight_sha256": None,
                    "work_id": None,
                    "work_kind": None,
                }
                return OnceWorkerReceipt(
                    outcome=OnceWorkerOutcome.NO_WORK,
                    qdrant_preflight_sha256=None,
                    receipt_sha256=canonical_sha256(
                        "governed_memory.once_worker_receipt",
                        material,
                    ),
                    work_id=None,
                    work_kind=None,
                )

            try:
                if isinstance(work, ExtractionWork):
                    if work.local_failure_code is not None:
                        raise WorkerLocalFailure(work.local_failure_code)
                    if work.provider_request is None:
                        raise ContractViolation("invalid_worker_provider_request")
                    result = await self._provider.extract(work.provider_request)
                elif isinstance(work, ProjectionUpsertWork):
                    preflight = await self._qdrant.preflight()
                    preflight_sha256 = preflight.receipt_sha256
                    embedding_input = render_projection_surface(work.claim)
                    vector = await self._embedder.embed(work, embedding_input)
                    point = build_projection_point(
                        work.claim,
                        work.outbox_record,
                        vector,
                    )
                    result = await self._qdrant.upsert_projection_point(point)
                elif isinstance(work, ProjectionDeleteWork):
                    preflight = await self._qdrant.preflight()
                    preflight_sha256 = preflight.receipt_sha256
                    result = await self._qdrant.delete_projection_point(
                        work.delete_command
                    )
                else:
                    raise ContractViolation("invalid_worker_work_kind")
            except Exception as error:
                failure_handler = getattr(self._repository, "fail", None)
                if callable(failure_handler):
                    await failure_handler(work, error)
                raise

            await self._repository.complete(work, result)
            material = {
                "outcome": OnceWorkerOutcome.COMPLETED.value,
                "qdrant_preflight_sha256": preflight_sha256,
                "work_id": str(work.work_id),
                "work_kind": work.kind.value,
            }
            return OnceWorkerReceipt(
                outcome=OnceWorkerOutcome.COMPLETED,
                qdrant_preflight_sha256=preflight_sha256,
                receipt_sha256=canonical_sha256(
                    "governed_memory.once_worker_receipt",
                    material,
                ),
                work_id=work.work_id,
                work_kind=work.kind,
            )
        finally:
            self._running = False


AsyncOnceRunner = Callable[[], Awaitable[OnceWorkerReceipt]]


def _refusal(code: str) -> None:
    print(
        json.dumps(
            {
                "error": {"code": code},
                "schema_version": "governed-memory-worker-refusal-v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    once_runner: AsyncOnceRunner | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="governed-memory-worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="process at most one claimed item and exit",
    )
    arguments = parser.parse_args(argv)
    if not arguments.once:
        _refusal("governed_memory_worker_once_required")
        return 2
    values = environment if environment is not None else os.environ
    if values.get(WORKER_MODE_ENV, "off") != "on":
        _refusal("governed_memory_worker_disabled")
        return 1
    try:
        mode = exclusive_memory_mode(values)
    except ExclusiveMemoryConfigurationError:
        _refusal("governed_memory_worker_exclusive_mode_required")
        return 1
    if mode is not ExclusiveMemoryMode.SUCCESSOR_PILOT:
        _refusal("governed_memory_worker_exclusive_mode_required")
        return 1
    runtime_refusal_type: type[Exception] | None = None
    if once_runner is None:
        try:
            from .worker_application import (
                WorkerRuntimeRefusal,
                create_runtime_once_runner,
            )
        except Exception:
            _refusal("governed_memory_worker_runtime_unavailable")
            return 1
        runtime_refusal_type = WorkerRuntimeRefusal
        try:
            once_runner = create_runtime_once_runner(values)
        except WorkerRuntimeRefusal as error:
            _refusal(error.code)
            return 1
    try:
        asyncio.run(once_runner())
    except Exception as error:
        code = (
            error.code
            if runtime_refusal_type is not None
            and isinstance(error, runtime_refusal_type)
            else "governed_memory_worker_run_failed"
        )
        _refusal(code)
        return 1
    return 0


__all__ = [
    "EmbeddingProvider",
    "ExtractionProvider",
    "ExtractionWork",
    "OnceWorker",
    "OnceWorkerOutcome",
    "OnceWorkerReceipt",
    "OnceWorkerRepository",
    "ProjectionDeleteWork",
    "ProjectionUpsertWork",
    "WORKER_MODE_ENV",
    "WorkKind",
    "WorkerLocalFailure",
    "WorkerQdrant",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
