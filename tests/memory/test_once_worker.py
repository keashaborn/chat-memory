from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.projection import build_projection_delete
from rag_engine.governed_memory.runtime.once_worker import (
    ExtractionWork,
    OnceWorker,
    OnceWorkerOutcome,
    ProjectionDeleteWork,
    ProjectionUpsertWork,
    main,
)
from rag_engine.governed_memory.runtime.qdrant_adapter import (
    QDRANT_ALIAS,
    QDRANT_PHYSICAL_COLLECTION,
    QdrantDeleteReceipt,
    QdrantPreflightReceipt,
    QdrantUpsertReceipt,
)
from tests.memory._fixtures import (
    deterministic_vector,
    make_claim_row,
    make_projection_outbox,
)


ROOT = Path(__file__).resolve().parents[2]
UNIT = (
    ROOT
    / "ops"
    / "governed_memory"
    / "systemd"
    / "governed-memory-worker.service.in"
)
WORK_ID = UUID("11111111-1111-4111-8111-111111111111")
HASH = "a" * 64


class FakeRepository:
    def __init__(self, work: object = None, *, pilot_started: bool = True) -> None:
        self.work = work
        self.pilot_started = pilot_started
        self.pilot_reads = 0
        self.claims = 0
        self.completions: list[tuple[object, object]] = []
        self.failures: list[tuple[object, Exception]] = []

    async def pilot_ever_started(self) -> bool:
        self.pilot_reads += 1
        return self.pilot_started

    async def claim_one(self) -> object:
        self.claims += 1
        return self.work

    async def complete(self, work: object, result: object) -> None:
        self.completions.append((work, result))

    async def fail(self, work: object, error: Exception) -> None:
        self.failures.append((work, error))


class FakeProvider:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[object] = []
        self.error = error

    async def extract(self, request: object) -> object:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return {"response_sha256": HASH}


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> object:
        self.calls.append(text)
        return deterministic_vector()


class FakeQdrant:
    def __init__(self, *, block_preflight: asyncio.Event | None = None) -> None:
        self.preflights = 0
        self.upserts: list[object] = []
        self.deletes: list[object] = []
        self.block_preflight = block_preflight

    async def preflight(self) -> QdrantPreflightReceipt:
        self.preflights += 1
        if self.block_preflight is not None:
            await self.block_preflight.wait()
        return QdrantPreflightReceipt(
            alias=QDRANT_ALIAS,
            distance="Cosine",
            payload_indexes=(("owner_user_id", "keyword"),),
            physical_collection=QDRANT_PHYSICAL_COLLECTION,
            receipt_sha256=HASH,
            vector_size=3_072,
        )

    async def upsert_projection_point(self, point: object) -> QdrantUpsertReceipt:
        self.upserts.append(point)
        return QdrantUpsertReceipt(
            payload_sha256="b" * 64,
            physical_collection=QDRANT_PHYSICAL_COLLECTION,
            point_id=UUID(str(point["point_id"])),  # type: ignore[index]
            resolved_by_readback=False,
            vector_sha256="c" * 64,
            verification_receipt_sha256="d" * 64,
        )

    async def delete_projection_point(self, command: object) -> QdrantDeleteReceipt:
        self.deletes.append(command)
        return QdrantDeleteReceipt(
            absence_verification_sha256="b" * 64,
            collection_alias=QDRANT_ALIAS,
            physical_collection=QDRANT_PHYSICAL_COLLECTION,
            point_id=UUID(str(command["point_id"])),  # type: ignore[index]
            verification_receipt_sha256="c" * 64,
        )


def worker(repository: FakeRepository, **overrides: object) -> tuple[OnceWorker, FakeProvider, FakeEmbedder, FakeQdrant]:
    provider = overrides.get("provider") or FakeProvider()
    embedder = overrides.get("embedder") or FakeEmbedder()
    qdrant = overrides.get("qdrant") or FakeQdrant()
    return (
        OnceWorker(
            embedder=embedder,  # type: ignore[arg-type]
            provider=provider,  # type: ignore[arg-type]
            qdrant=qdrant,  # type: ignore[arg-type]
            repository=repository,
        ),
        provider,  # type: ignore[return-value]
        embedder,  # type: ignore[return-value]
        qdrant,  # type: ignore[return-value]
    )


class OnceWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_false_pilot_marker_refuses_before_qdrant_or_claim(self) -> None:
        repository = FakeRepository(pilot_started=False)
        instance, provider, embedder, qdrant = worker(repository)
        with self.assertRaisesRegex(ContractViolation, "pilot_never_started"):
            await instance.run_once()
        self.assertEqual(repository.claims, 0)
        self.assertEqual(qdrant.preflights, 0)
        self.assertEqual(provider.calls, [])
        self.assertEqual(embedder.calls, [])

    async def test_no_work_claims_once_without_qdrant_io(self) -> None:
        repository = FakeRepository()
        instance, provider, embedder, qdrant = worker(repository)
        receipt = await instance.run_once()
        self.assertEqual(receipt.outcome, OnceWorkerOutcome.NO_WORK)
        self.assertIsNone(receipt.qdrant_preflight_sha256)
        self.assertEqual(repository.claims, 1)
        self.assertEqual(qdrant.preflights, 0)
        self.assertEqual(provider.calls, [])
        self.assertEqual(embedder.calls, [])

    async def test_each_invocation_processes_exactly_one_injected_work_kind(self) -> None:
        extraction = ExtractionWork(
            provider_request={"request_sha256": HASH},
            work_id=WORK_ID,
        )
        repository = FakeRepository(extraction)
        instance, provider, embedder, qdrant = worker(repository)
        receipt = await instance.run_once()
        self.assertEqual(receipt.work_kind.value, "extraction")  # type: ignore[union-attr]
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(embedder.calls, [])
        self.assertEqual(qdrant.preflights, 0)
        self.assertEqual(qdrant.upserts, [])
        self.assertEqual(len(repository.completions), 1)

        claim = make_claim_row()
        upsert = ProjectionUpsertWork(
            claim=claim,
            outbox_record=make_projection_outbox(claim),
            work_id=WORK_ID,
        )
        repository = FakeRepository(upsert)
        instance, provider, embedder, qdrant = worker(repository)
        receipt = await instance.run_once()
        self.assertEqual(receipt.work_kind.value, "projection_upsert")  # type: ignore[union-attr]
        self.assertEqual(provider.calls, [])
        self.assertEqual(qdrant.preflights, 1)
        self.assertEqual(len(embedder.calls), 1)
        self.assertEqual(len(qdrant.upserts), 1)
        self.assertEqual(len(repository.completions), 1)

        retracted = make_claim_row(lifecycle_state="retracted", projection_sequence=2)
        command = build_projection_delete(
            make_projection_outbox(retracted, operation="delete"),
            collection_alias=QDRANT_ALIAS,
            physical_collection=QDRANT_PHYSICAL_COLLECTION,
        )
        deletion = ProjectionDeleteWork(delete_command=command, work_id=WORK_ID)
        repository = FakeRepository(deletion)
        instance, provider, embedder, qdrant = worker(repository)
        receipt = await instance.run_once()
        self.assertEqual(receipt.work_kind.value, "projection_delete")  # type: ignore[union-attr]
        self.assertEqual(provider.calls, [])
        self.assertEqual(qdrant.preflights, 1)
        self.assertEqual(embedder.calls, [])
        self.assertEqual(len(qdrant.deletes), 1)
        self.assertEqual(len(repository.completions), 1)

    async def test_provider_failure_is_not_retried_or_completed_automatically(self) -> None:
        repository = FakeRepository(
            ExtractionWork(
                provider_request={"request_sha256": HASH},
                work_id=WORK_ID,
            )
        )
        provider = FakeProvider(error=RuntimeError("synthetic_unknown_outcome"))
        instance, _, _, _ = worker(repository, provider=provider)
        with self.assertRaisesRegex(RuntimeError, "synthetic_unknown_outcome"):
            await instance.run_once()
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(repository.claims, 1)
        self.assertEqual(repository.completions, [])
        self.assertEqual(len(repository.failures), 1)
        self.assertIs(repository.failures[0][0], repository.work)

    async def test_typed_local_failure_never_calls_provider(self) -> None:
        work = ExtractionWork(
            local_failure_code="local_serialization_failed_before_send",
            work_id=WORK_ID,
        )
        repository = FakeRepository(work)
        instance, provider, embedder, qdrant = worker(repository)
        with self.assertRaisesRegex(
            RuntimeError,
            "local_serialization_failed_before_send",
        ):
            await instance.run_once()
        self.assertEqual(provider.calls, [])
        self.assertEqual(embedder.calls, [])
        self.assertEqual(qdrant.preflights, 0)
        self.assertEqual(len(repository.failures), 1)

    async def test_concurrent_invocation_is_refused_instead_of_queued(self) -> None:
        gate = asyncio.Event()
        qdrant = FakeQdrant(block_preflight=gate)
        claim = make_claim_row()
        repository = FakeRepository(
            ProjectionUpsertWork(
                claim=claim,
                outbox_record=make_projection_outbox(claim),
                work_id=WORK_ID,
            )
        )
        instance, _, _, _ = worker(repository, qdrant=qdrant)
        first = asyncio.create_task(instance.run_once())
        while qdrant.preflights == 0:
            await asyncio.sleep(0)
        with self.assertRaisesRegex(
            ContractViolation,
            "worker_concurrency_one_exceeded",
        ):
            await instance.run_once()
        gate.set()
        await first
        self.assertEqual(repository.claims, 1)


class OnceWorkerEntrypointTests(unittest.TestCase):
    def test_cli_requires_once_mode_and_injected_composition(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main([], environment={}), 2)
        self.assertIn("governed_memory_worker_once_required", stderr.getvalue())

        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main(["--once"], environment={}), 1)
        self.assertIn("governed_memory_worker_disabled", stderr.getvalue())

        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(
                main(
                    ["--once"],
                    environment={"GOVERNED_MEMORY_WORKER_MODE": "on"},
                ),
                1,
            )
        self.assertIn(
            "governed_memory_worker_configuration_invalid",
            stderr.getvalue(),
        )

    def test_injected_once_runner_is_called_exactly_once(self) -> None:
        calls = 0

        async def run() -> object:
            nonlocal calls
            calls += 1
            return object()

        self.assertEqual(
            main(
                ["--once"],
                environment={"GOVERNED_MEMORY_WORKER_MODE": "on"},
                once_runner=run,  # type: ignore[arg-type]
            ),
            0,
        )
        self.assertEqual(calls, 1)

    def test_systemd_artifact_is_dormant_one_shot_and_not_installable(self) -> None:
        unit = UNIT.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", unit)
        self.assertIn("Restart=no", unit)
        self.assertIn("GOVERNED_MEMORY_WORKER_MODE=off", unit)
        self.assertIn("runtime.once_worker --once", unit)
        self.assertEqual(
            tuple(
                line
                for line in unit.splitlines()
                if line.startswith("ConditionPathExists=")
            ),
            (
                "ConditionPathExists=/etc/governed-memory/worker.env",
                "ConditionPathExists=/etc/governed-memory/pilot.env",
            ),
        )
        self.assertEqual(
            tuple(
                line
                for line in unit.splitlines()
                if line.startswith("EnvironmentFile=")
            ),
            (
                "EnvironmentFile=/etc/governed-memory/worker.env",
                "EnvironmentFile=/etc/governed-memory/pilot.env",
            ),
        )
        self.assertNotIn("[Install]", unit)
        self.assertNotIn("WantedBy=", unit)
        self.assertFalse(any(UNIT.parent.glob("*.timer")))


if __name__ == "__main__":
    unittest.main()
