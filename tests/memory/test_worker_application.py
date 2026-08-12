from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

from rag_engine.governed_memory.extraction import parse_predicate_catalog
from rag_engine.governed_memory.contracts import sha256_text
from rag_engine.governed_memory.runtime.once_worker import (
    ProjectionUpsertWork,
    WorkerLocalFailure,
    main,
)
from rag_engine.governed_memory.runtime.openai_adapters import (
    DispatchReceipt,
    EMBEDDING_ENDPOINT_SHA256,
)
from rag_engine.governed_memory.runtime.worker_application import (
    CONVERSATION_POSTGRES_DSN_ENV,
    EXPECTED_AUTHORIZATION_RECEIPT_SHA256_ENV,
    EXPECTED_PILOT_CONTRACT_SHA256_ENV,
    EXPECTED_PILOT_ID_ENV,
    GuardedOnceRunner,
    POSTGRES_DSN_ENV,
    QDRANT_API_KEY_ENV,
    QDRANT_URL_ENV,
    OPENAI_API_KEY_ENV,
    OPENAI_EXTRACTION_MODEL_ENV,
    RuntimeExtractionProvider,
    RuntimeEmbeddingProvider,
    PostgresAdvisoryGuard,
    WORKER_ADVISORY_LOCK_KEY,
    WorkerRuntimeConfig,
    WorkerRuntimeRefusal,
    _configure_connection,
    _load_predicate_catalog,
    create_runtime_once_runner,
)
from rag_engine.governed_memory.runtime.qdrant_transport import QDRANT_RUNTIME_URL
from tests.memory._fixtures import PROJECTION_A, make_claim_row, make_projection_outbox


class FakeGuard:
    def __init__(
        self,
        *,
        acquired: bool = True,
        events: list[str] | None = None,
    ) -> None:
        self.acquired = acquired
        self.events = events
        self.acquire_calls = 0
        self.release_calls = 0

    async def acquire(self) -> bool:
        self.acquire_calls += 1
        if self.events is not None:
            self.events.append("lock_acquired")
        return self.acquired

    async def release(self) -> bool:
        self.release_calls += 1
        if self.events is not None:
            self.events.append("lock_released")
        return True


class FakeWorker:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.failure = failure
        self.events = events
        self.calls = 0

    async def run_once(self) -> object:
        self.calls += 1
        if self.events is not None:
            self.events.append("worker_ran")
        if self.failure is not None:
            raise self.failure
        return object()


def valid_environment() -> dict[str, str]:
    return {
        "GOVERNED_MEMORY_WORKER_MODE": "on",
        POSTGRES_DSN_ENV: (
            "postgresql://governed_memory_worker:synthetic-password@"
            "127.0.0.1:55432/governed_memory"
        ),
        CONVERSATION_POSTGRES_DSN_ENV: (
            "postgresql://governed_memory_worker:synthetic-password@"
            "127.0.0.1:5432/memory"
        ),
        OPENAI_API_KEY_ENV: "synthetic-not-a-real-openai-key",
        OPENAI_EXTRACTION_MODEL_ENV: "synthetic-extraction-model",
        EXPECTED_PILOT_ID_ENV: "governed_memory_pilot_synthetic",
        EXPECTED_PILOT_CONTRACT_SHA256_ENV: "a" * 64,
        EXPECTED_AUTHORIZATION_RECEIPT_SHA256_ENV: "b" * 64,
        QDRANT_URL_ENV: QDRANT_RUNTIME_URL,
        QDRANT_API_KEY_ENV: "synthetic-not-a-real-qdrant-key",
    }


class WorkerEntrypointRefusalTests(unittest.TestCase):
    def test_mode_off_refuses_before_configuration_is_read(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = main(["--once"], environment={})
        self.assertEqual(result, 1)
        self.assertIn("governed_memory_worker_disabled", stderr.getvalue())
        self.assertNotIn("configuration", stderr.getvalue())

    def test_mode_on_missing_configuration_refuses_without_io(self) -> None:
        environment = dict(os.environ)
        environment["GOVERNED_MEMORY_EXCLUSIVE_MODE"] = "successor_pilot"
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from rag_engine.governed_memory.runtime.once_worker "
                    "import main; raise SystemExit(main(['--once'], "
                    "environment={'GOVERNED_MEMORY_WORKER_MODE': 'on', "
                    "'GOVERNED_MEMORY_EXCLUSIVE_MODE': "
                    "'successor_pilot'}))"
                ),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn(
            "governed_memory_worker_configuration_invalid",
            completed.stderr,
        )
        self.assertEqual(completed.stdout, "")

    def test_exact_configuration_builds_lazy_runner_and_hides_secrets(self) -> None:
        environment = valid_environment()
        config = WorkerRuntimeConfig.from_environment(environment)
        self.assertNotIn(environment[POSTGRES_DSN_ENV], repr(config))
        self.assertNotIn(
            environment[CONVERSATION_POSTGRES_DSN_ENV],
            repr(config),
        )
        self.assertNotIn(environment[OPENAI_API_KEY_ENV], repr(config))
        self.assertNotIn(environment[QDRANT_API_KEY_ENV], repr(config))
        self.assertNotIn(
            environment[EXPECTED_AUTHORIZATION_RECEIPT_SHA256_ENV],
            repr(config),
        )
        runner = create_runtime_once_runner(environment)
        self.assertTrue(callable(runner))

    def test_alternate_postgres_or_qdrant_target_is_rejected(self) -> None:
        for field, replacement in (
            (
                POSTGRES_DSN_ENV,
                "postgresql://governed_memory_worker:synthetic@"
                "127.0.0.1:5432/governed_memory",
            ),
            (
                CONVERSATION_POSTGRES_DSN_ENV,
                "postgresql://governed_memory_worker:synthetic@"
                "127.0.0.1:55432/memory",
            ),
            (QDRANT_URL_ENV, "http://127.0.0.1:6333"),
        ):
            environment = valid_environment()
            environment[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(
                WorkerRuntimeRefusal,
                "governed_memory_worker_configuration_invalid",
            ):
                WorkerRuntimeConfig.from_environment(environment)


class GuardedOnceRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_contention_refuses_before_any_work_mutation(self) -> None:
        guard = FakeGuard(acquired=False)
        worker = FakeWorker()
        runner = GuardedOnceRunner(guard=guard, worker=worker)  # type: ignore[arg-type]
        with self.assertRaisesRegex(
            WorkerRuntimeRefusal,
            "governed_memory_worker_lock_contended",
        ):
            await runner.run_once()
        self.assertEqual(guard.acquire_calls, 1)
        self.assertEqual(guard.release_calls, 0)
        self.assertEqual(worker.calls, 0)

    async def test_success_runs_exactly_one_work_path_and_releases(self) -> None:
        events: list[str] = []
        guard = FakeGuard(events=events)
        worker = FakeWorker(events=events)
        runner = GuardedOnceRunner(guard=guard, worker=worker)  # type: ignore[arg-type]
        await runner.run_once()
        self.assertEqual(worker.calls, 1)
        self.assertEqual(guard.acquire_calls, 1)
        self.assertEqual(guard.release_calls, 1)
        self.assertEqual(events, ["lock_acquired", "worker_ran", "lock_released"])

    async def test_worker_failure_still_releases_cross_process_lock(self) -> None:
        events: list[str] = []
        guard = FakeGuard(events=events)
        worker = FakeWorker(
            failure=RuntimeError("synthetic-worker-failure"),
            events=events,
        )
        runner = GuardedOnceRunner(guard=guard, worker=worker)  # type: ignore[arg-type]
        with self.assertRaisesRegex(RuntimeError, "synthetic-worker-failure"):
            await runner.run_once()
        self.assertEqual(worker.calls, 1)
        self.assertEqual(guard.acquire_calls, 1)
        self.assertEqual(guard.release_calls, 1)
        self.assertEqual(events, ["lock_acquired", "worker_ran", "lock_released"])


class PostgresAdvisoryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_session_lock_is_acquired_and_released(self) -> None:
        class FakeConnection:
            def __init__(self) -> None:
                self.results = [True, True]
                self.calls: list[tuple[str, tuple[object, ...]]] = []

            async def fetchval(self, sql: str, *args: object) -> object:
                self.calls.append((sql, args))
                return self.results.pop(0)

        connection = FakeConnection()
        guard = PostgresAdvisoryGuard(connection)
        self.assertTrue(await guard.acquire())
        self.assertTrue(await guard.release())
        self.assertEqual(
            connection.calls,
            [
                (
                    "SELECT pg_catalog.pg_try_advisory_lock($1::bigint)",
                    (WORKER_ADVISORY_LOCK_KEY,),
                ),
                (
                    "SELECT pg_catalog.pg_advisory_unlock($1::bigint)",
                    (WORKER_ADVISORY_LOCK_KEY,),
                ),
            ],
        )


class RuntimeCompositionContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_identity_accepts_postgres16_container_address(self) -> None:
        class FakeConnection:
            def __init__(self) -> None:
                self.codec_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

            async def set_type_codec(self, *args: object, **kwargs: object) -> None:
                self.codec_calls.append((args, kwargs))

            async def fetchrow(self, sql: str) -> dict[str, object]:
                self.sql = sql
                return {
                    "database_name": "governed_memory",
                    "session_role": "governed_memory_worker",
                    "server_address": "172.18.0.2",
                    "server_port": 5432,
                    "server_version_num": 160_004,
                }

        connection = FakeConnection()
        await _configure_connection(connection)
        self.assertEqual(len(connection.codec_calls), 1)
        self.assertIn("current_database()", connection.sql)

    async def test_wrong_database_identity_is_rejected(self) -> None:
        class FakeConnection:
            async def set_type_codec(self, *args: object, **kwargs: object) -> None:
                return None

            async def fetchrow(self, sql: str) -> dict[str, object]:
                return {
                    "database_name": "postgres",
                    "session_role": "governed_memory_worker",
                    "server_address": "172.18.0.2",
                    "server_port": 5432,
                    "server_version_num": 160_004,
                }

        with self.assertRaisesRegex(
            WorkerRuntimeRefusal,
            "governed_memory_worker_database_identity_mismatch",
        ):
            await _configure_connection(FakeConnection())

    async def test_packaged_catalog_matches_migration_catalog_semantically(self) -> None:
        migration_catalog = json.loads(
            Path("governed-memory-migrations/predicate_catalog.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            parse_predicate_catalog(_load_predicate_catalog()).catalog_sha256,
            parse_predicate_catalog(migration_catalog).catalog_sha256,
        )


class RuntimeExtractionProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_request_refuses_before_adapter_or_repository_call(self) -> None:
        class NeverRepository:
            async def mark_provider_dispatched(self, *args: object) -> None:
                raise AssertionError(f"repository called: {args!r}")

        class NeverAdapter:
            def invoke(self, *args: object, **kwargs: object) -> object:
                raise AssertionError(f"adapter called: {args!r} {kwargs!r}")

        provider = RuntimeExtractionProvider(
            repository=NeverRepository(),  # type: ignore[arg-type]
            adapter=NeverAdapter(),  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(
            WorkerLocalFailure,
            "local_serialization_failed_before_send",
        ):
            await provider.extract({})


class RuntimeEmbeddingProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_marker_completes_before_transport(self) -> None:
        events: list[str] = []
        observed: list[tuple[object, DispatchReceipt]] = []
        text = "synthetic canonical relational fact"
        receipt = DispatchReceipt(
            operation="embeddings.create",
            model="text-embedding-3-large",
            endpoint_sha256=EMBEDDING_ENDPOINT_SHA256,
            input_sha256=sha256_text(text),
            request_body_sha256="d" * 64,
        )

        class FakeRepository:
            async def mark_projection_embedding_dispatched(
                self,
                work: object,
                supplied: DispatchReceipt,
            ) -> None:
                events.append("marker_committed")
                observed.append((work, supplied))

        class FakeAdapter:
            def invoke(self, supplied_text: str, *, mark_dispatched: object) -> object:
                self.text = supplied_text
                mark_dispatched(receipt)  # type: ignore[operator]
                events.append("transport_called")
                return SimpleNamespace(vector=(0.25,))

        claim = make_claim_row()
        work = ProjectionUpsertWork(
            work_id=PROJECTION_A,
            claim=claim,
            outbox_record=make_projection_outbox(claim),
        )
        adapter = FakeAdapter()
        provider = RuntimeEmbeddingProvider(
            repository=FakeRepository(),  # type: ignore[arg-type]
            adapter=adapter,  # type: ignore[arg-type]
        )
        self.assertEqual(await provider.embed(work, text), (0.25,))
        self.assertEqual(events, ["marker_committed", "transport_called"])
        self.assertEqual(observed, [(work, receipt)])
        self.assertEqual(adapter.text, text)

if __name__ == "__main__":
    unittest.main()
