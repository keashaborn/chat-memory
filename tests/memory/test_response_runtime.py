from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.http_service import POSTGRES_DSN_ENV
from rag_engine.governed_memory.response_contracts import (
    SuccessorResponseActorBinding,
)
from rag_engine.governed_memory.response_provider import (
    SuccessorGovernedMemoryAssemblyProviderV1,
    SuccessorResponseConfigurationError,
)
from rag_engine.governed_memory.response_postgres import (
    PostgresSuccessorResponseRepository,
    SuccessorResponsePostgresError,
)
from rag_engine.governed_memory.response_runtime import (
    DEFAULT_PREDICATE_CATALOG_PATH,
    EXPECTED_POSTGRES_HOST,
    EXPECTED_POSTGRES_PORT,
    EXPECTED_QDRANT_HOST,
    EXPECTED_QDRANT_PORT,
    QDRANT_API_KEY_ENV,
    SUCCESSOR_CALIBRATION_APPROVAL_SHA256_ENV,
    SUCCESSOR_CALIBRATION_ARTIFACT_SHA256_ENV,
    SUCCESSOR_CALIBRATION_PATH_ENV,
    SUCCESSOR_OPENAI_API_KEY_ENV,
    LoopbackQdrantRetrievalTransport,
    SuccessorResponseRuntime,
    SuccessorResponseRuntimeSettings,
)
from rag_engine.governed_memory.runtime.calibration import (
    CalibrationCase,
    build_candidate_calibration,
    calibration_artifact_sha256,
    fixed_score_to_micros,
)
from rag_engine.governed_memory.runtime.qdrant_adapter import (
    QDRANT_PHYSICAL_COLLECTION,
)
from rag_engine.governed_memory import response_runtime as response_runtime_module
from tests.memory._fixtures import (
    CLAIM_A,
    OWNER_A,
    RESPONSE_A,
    THREAD_A,
    make_response_actor,
)


ROOT = Path(__file__).resolve().parents[2]
APPROVAL = "b" * 64


def approved_artifact() -> dict[str, object]:
    artifact = build_candidate_calibration(
        (
            CalibrationCase(
                case_id="case.a",
                expected_relevant=True,
                score_micros=fixed_score_to_micros("0.910000"),
            ),
            CalibrationCase(
                case_id="case.b",
                expected_relevant=False,
                score_micros=fixed_score_to_micros("0.200000"),
            ),
        ),
        holdout_manifest_sha256="a" * 64,
    )
    artifact["status"] = "approved"
    artifact["retrieval_enabled"] = True
    artifact["approval_receipt_sha256"] = APPROVAL
    artifact["artifact_sha256"] = calibration_artifact_sha256(artifact)
    return artifact


def environment(path: Path, artifact_hash: str) -> dict[str, str]:
    return {
        POSTGRES_DSN_ENV: (
            "postgresql://governed_memory_api:synthetic-secret@"
            "127.0.0.1:55432/governed_memory"
        ),
        SUCCESSOR_OPENAI_API_KEY_ENV: "sk-synthetic-response-key",
        QDRANT_API_KEY_ENV: "synthetic-not-a-real-qdrant-key",
        SUCCESSOR_CALIBRATION_PATH_ENV: str(path),
        SUCCESSOR_CALIBRATION_ARTIFACT_SHA256_ENV: artifact_hash,
        SUCCESSOR_CALIBRATION_APPROVAL_SHA256_ENV: APPROVAL,
    }


class FakeHttpResponse:
    status = 200

    def __init__(self, body: dict[str, object]) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self, _maximum: int) -> bytes:
        return self._body

    def getheader(self, name: str, default: str = "") -> str:
        return "application/json" if name.lower() == "content-type" else default


class FakeHttpConnection:
    def __init__(self, response: FakeHttpResponse) -> None:
        self.response = response
        self.requests: list[tuple[object, ...]] = []
        self.closed = False

    def request(self, *args: object, **kwargs: object) -> None:
        self.requests.append((*args, kwargs))

    def getresponse(self) -> FakeHttpResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class FakeAcquire:
    def __init__(self, connection: object) -> None:
        self.connection = connection

    async def __aenter__(self) -> object:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakePool:
    def __init__(self, connection: object) -> None:
        self.connection = connection
        self.closed = 0

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)

    async def close(self) -> None:
        self.closed += 1


class FakePreflightConnection:
    async def fetchrow(self, _query: str) -> dict[str, object]:
        return {
            "read_function": "memory_private.read_claim_candidates(uuid[])",
            "binding_function": (
                "memory_private.record_answer_binding("
                "uuid,uuid,uuid,text,text,text[],text[],text[],integer,integer,"
                "text,text,boolean,uuid[],uuid[],text,text,text,text,text)"
            ),
            "can_read": True,
            "can_bind": True,
        }


class FakeBindingConnection:
    def __init__(self, receipt: dict[str, object]) -> None:
        self.receipt = receipt

    async def fetchrow(self, _query: str, *_arguments: object) -> dict[str, object]:
        return self.receipt


class _TestablePostgresSuccessorResponseRepository(
    PostgresSuccessorResponseRepository
):
    def __init__(self, connection: FakeBindingConnection) -> None:
        super().__init__(object())
        self.connection = connection
        self.rolled_back = False

    @asynccontextmanager
    async def _transaction(self, _actor: SuccessorResponseActorBinding):
        try:
            yield self.connection
        except Exception:
            self.rolled_back = True
            raise


def dispatched_binding() -> dict[str, object]:
    return {
        "owner_user_id": str(OWNER_A),
        "dispatch_state": "dispatched",
        "outcome": "exposed",
        "response_id": str(RESPONSE_A),
        "query_sha256": "1" * 64,
        "policy_sha256": "2" * 64,
        "allowed_predicates": ["preference"],
        "domains": [],
        "intents": [],
        "max_records": 8,
        "policy_revision": 1,
        "renderer_sha256": "3" * 64,
        "prompt_sha256": "4" * 64,
        "explicit_recall": False,
        "selected_claim_ids": [str(CLAIM_A)],
        "injected_claim_ids": [str(CLAIM_A)],
        "selection_manifest_sha256": "5" * 64,
        "injection_manifest_sha256": "6" * 64,
    }


class SuccessorResponsePostgresReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_database_receipt_is_accepted(self) -> None:
        binding = dispatched_binding()
        repository = _TestablePostgresSuccessorResponseRepository(
            FakeBindingConnection(
                {
                    "binding_id": UUID(
                        "77777777-7777-4777-8777-777777777777"
                    ),
                    "selection_manifest_sha256": binding[
                        "selection_manifest_sha256"
                    ],
                    "injection_manifest_sha256": binding[
                        "injection_manifest_sha256"
                    ],
                }
            )
        )
        await repository.persist_answer_binding(
            actor=make_response_actor(),
            operation_id=UUID("34343434-3434-4434-8434-343434343434"),
            thread_id=THREAD_A,
            binding=binding,
            memory_block="bounded synthetic memory block",
            outbound_request='{"messages":[]}',
        )
        self.assertFalse(repository.rolled_back)

    async def test_malformed_wire_answer_uuid_is_rejected(self) -> None:
        binding = dispatched_binding()
        binding["response_id"] = "not-a-uuid"
        repository = _TestablePostgresSuccessorResponseRepository(
            FakeBindingConnection(
                {
                    "binding_id": UUID(
                        "77777777-7777-4777-8777-777777777777"
                    ),
                    "selection_manifest_sha256": binding[
                        "selection_manifest_sha256"
                    ],
                    "injection_manifest_sha256": binding[
                        "injection_manifest_sha256"
                    ],
                }
            )
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "invalid_response_memory_answer",
        ):
            await repository.persist_answer_binding(
                actor=make_response_actor(),
                operation_id=UUID("34343434-3434-4434-8434-343434343434"),
                thread_id=THREAD_A,
                binding=binding,
                memory_block="bounded synthetic memory block",
                outbound_request='{"messages":[]}',
            )

    async def test_conflicting_database_receipt_fails_closed(self) -> None:
        binding = dispatched_binding()
        for receipt in (
            {
                "binding_id": UUID(
                    "77777777-7777-4777-8777-777777777777"
                ),
                "selection_manifest_sha256": "9" * 64,
                "injection_manifest_sha256": binding[
                    "injection_manifest_sha256"
                ],
            },
            {
                "binding_id": "not-a-uuid",
                "selection_manifest_sha256": binding[
                    "selection_manifest_sha256"
                ],
                "injection_manifest_sha256": binding[
                    "injection_manifest_sha256"
                ],
            },
            {
                "binding_id": UUID(
                    "77777777-7777-4777-8777-777777777777"
                ),
                "selection_manifest_sha256": binding[
                    "selection_manifest_sha256"
                ],
                "injection_manifest_sha256": binding[
                    "injection_manifest_sha256"
                ],
                "unexpected_compatibility_field": "legacy",
            },
        ):
            with self.subTest(receipt=receipt):
                repository = _TestablePostgresSuccessorResponseRepository(
                    FakeBindingConnection(receipt)
                )
                with self.assertRaises(SuccessorResponsePostgresError):
                    await repository.persist_answer_binding(
                        actor=make_response_actor(),
                        operation_id=UUID(
                            "34343434-3434-4434-8434-343434343434"
                        ),
                        thread_id=THREAD_A,
                        binding=binding,
                        memory_block="bounded synthetic memory block",
                        outbound_request='{"messages":[]}',
                    )
                self.assertTrue(repository.rolled_back)


class SuccessorResponseRuntimeSettingsTests(unittest.TestCase):
    def test_default_predicate_catalog_is_successor_packaged_asset(self) -> None:
        package_root = Path(response_runtime_module.__file__).resolve().parent
        self.assertTrue(DEFAULT_PREDICATE_CATALOG_PATH.is_relative_to(package_root))
        self.assertEqual(
            DEFAULT_PREDICATE_CATALOG_PATH,
            package_root / "provider_assets" / "predicate_catalog.json",
        )
        self.assertTrue(DEFAULT_PREDICATE_CATALOG_PATH.is_file())

    def test_exact_postgres_and_external_approval_environment_contract(self) -> None:
        artifact = approved_artifact()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "approved.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            settings = SuccessorResponseRuntimeSettings.from_environment(
                environment(path, str(artifact["artifact_sha256"]))
            )
        self.assertEqual(settings.calibration_path, path)
        self.assertIn(
            f"{EXPECTED_POSTGRES_HOST}:{EXPECTED_POSTGRES_PORT}",
            settings.postgres_dsn,
        )
        self.assertNotIn(settings.openai_api_key, repr(settings))
        self.assertNotIn(settings.qdrant_api_key, repr(settings))

    def test_missing_qdrant_key_refuses_without_secret_exposure(self) -> None:
        artifact = approved_artifact()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "approved.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            values = environment(path, str(artifact["artifact_sha256"]))
            secret = values.pop(QDRANT_API_KEY_ENV)
            with self.assertRaises(SuccessorResponseConfigurationError) as raised:
                SuccessorResponseRuntimeSettings.from_environment(values)
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(
            str(raised.exception),
            "successor_response_runtime_configuration_invalid",
        )

    def test_postgres_target_and_calibration_path_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "approved.json"
            artifact = approved_artifact()
            path.write_text(json.dumps(artifact), encoding="utf-8")
            base = environment(path, str(artifact["artifact_sha256"]))
            invalid_dsns = (
                base[POSTGRES_DSN_ENV].replace("postgresql://", "postgres://"),
                base[POSTGRES_DSN_ENV].replace("127.0.0.1", "localhost"),
                base[POSTGRES_DSN_ENV].replace("55432", "5432"),
                base[POSTGRES_DSN_ENV].replace("governed_memory_api", "brains_app"),
                base[POSTGRES_DSN_ENV].replace(
                    "/governed_memory",
                    "/memory",
                ),
                base[POSTGRES_DSN_ENV] + "?sslmode=disable",
            )
            for dsn in invalid_dsns:
                with self.subTest(dsn=dsn):
                    values = {**base, POSTGRES_DSN_ENV: dsn}
                    with self.assertRaises(SuccessorResponseConfigurationError):
                        SuccessorResponseRuntimeSettings.from_environment(values)
            with self.assertRaises(SuccessorResponseConfigurationError):
                SuccessorResponseRuntimeSettings.from_environment(
                    {**base, SUCCESSOR_CALIBRATION_PATH_ENV: "relative.json"}
                )


class SuccessorResponseRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_and_provider_construction_open_no_resources(self) -> None:
        pool_calls: list[dict[str, object]] = []

        async def pool_factory(**kwargs: object) -> object:
            pool_calls.append(dict(kwargs))
            raise AssertionError("pool must remain lazy")

        artifact = approved_artifact()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "approved.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            runtime = SuccessorResponseRuntime(pool_factory=pool_factory)
            selected = runtime.provider(
                make_response_actor(),
                environment(path, str(artifact["artifact_sha256"])),
            )
            self.assertIsInstance(
                selected,
                SuccessorGovernedMemoryAssemblyProviderV1,
            )
            self.assertEqual(pool_calls, [])
            await runtime.close()
            self.assertEqual(pool_calls, [])

    async def test_unapproved_or_unbound_calibration_cannot_build_provider(self) -> None:
        path = (
            ROOT
            / "ops"
            / "governed_memory"
            / "calibration"
            / "semantic_retrieval.unapproved.json"
        )
        runtime = SuccessorResponseRuntime()
        with self.assertRaisesRegex(
            SuccessorResponseConfigurationError,
            "calibration_not_approved",
        ):
            runtime.provider(make_response_actor(), environment(path, "a" * 64))

    async def test_postgres_pool_opens_once_with_exact_bound_target(self) -> None:
        calls: list[dict[str, object]] = []
        pool = FakePool(FakePreflightConnection())

        async def pool_factory(**kwargs: object) -> FakePool:
            calls.append(dict(kwargs))
            return pool

        artifact = approved_artifact()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "approved.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            runtime = SuccessorResponseRuntime(pool_factory=pool_factory)
            runtime.provider(
                make_response_actor(),
                environment(path, str(artifact["artifact_sha256"])),
            )
            with patch(
                "rag_engine.governed_memory.response_runtime._preflight_connection",
                new=AsyncMock(),
            ) as preflight:
                first = await runtime._pool_for_requests()
                second = await runtime._pool_for_requests()
            self.assertIs(first, pool)
            self.assertIs(second, pool)
            self.assertEqual(len(calls), 1)
            self.assertIn(
                f"{EXPECTED_POSTGRES_HOST}:{EXPECTED_POSTGRES_PORT}",
                str(calls[0]["dsn"]),
            )
            self.assertEqual(calls[0]["min_size"], 1)
            self.assertEqual(calls[0]["max_size"], 1)
            preflight.assert_awaited_once()
            await runtime.close()
            self.assertEqual(pool.closed, 1)

    async def test_qdrant_transport_is_exact_loopback_read_only_and_retry_free(
        self,
    ) -> None:
        targets: list[tuple[str, int, float]] = []
        connection = FakeHttpConnection(FakeHttpResponse({"result": {}}))

        def factory(host: str, port: int, timeout: float) -> FakeHttpConnection:
            targets.append((host, port, timeout))
            return connection

        transport = LoopbackQdrantRetrievalTransport(
            api_key="synthetic-not-a-real-qdrant-key",
            connection_factory=factory
        )
        response = await transport.request(
            "GET",
            f"/collections/{QDRANT_PHYSICAL_COLLECTION}",
        )
        self.assertEqual(response, {"result": {}})
        self.assertEqual(
            targets,
            [(EXPECTED_QDRANT_HOST, EXPECTED_QDRANT_PORT, 5.0)],
        )
        self.assertEqual(len(connection.requests), 1)
        request_kwargs = connection.requests[0][-1]
        self.assertIsInstance(request_kwargs, dict)
        self.assertEqual(
            request_kwargs["headers"],
            {
                "Accept": "application/json",
                "api-key": "synthetic-not-a-real-qdrant-key",
            },
        )
        self.assertTrue(connection.closed)
        with self.assertRaisesRegex(ContractViolation, "write_forbidden"):
            await transport.request(
                "PUT",
                f"/collections/{QDRANT_PHYSICAL_COLLECTION}/points",
                {"points": []},
            )
        self.assertEqual(len(connection.requests), 1)

    async def test_qdrant_search_serializes_finite_vector_numbers(self) -> None:
        connection = FakeHttpConnection(FakeHttpResponse({"result": []}))
        transport = LoopbackQdrantRetrievalTransport(
            api_key="synthetic-not-a-real-qdrant-key",
            connection_factory=lambda _host, _port, _timeout: connection,
        )

        response = await transport.request(
            "POST",
            "/collections/governed_memory_active/points/search",
            {"limit": 1, "vector": [0.25, -0.5]},
        )

        self.assertEqual(response, {"result": []})
        self.assertEqual(len(connection.requests), 1)
        request_kwargs = connection.requests[0][-1]
        self.assertIsInstance(request_kwargs, dict)
        self.assertEqual(
            request_kwargs["body"],
            b'{"limit":1,"vector":[0.25,-0.5]}',
        )

    async def test_qdrant_search_rejects_nonfinite_vector_before_send(
        self,
    ) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            connection = FakeHttpConnection(FakeHttpResponse({"result": []}))
            transport = LoopbackQdrantRetrievalTransport(
                api_key="synthetic-not-a-real-qdrant-key",
                connection_factory=(
                    lambda _host, _port, _timeout: connection
                ),
            )

            with self.subTest(value=value), self.assertRaisesRegex(
                ContractViolation,
                "body_invalid",
            ):
                await transport.request(
                    "POST",
                    "/collections/governed_memory_active/points/search",
                    {"limit": 1, "vector": [value]},
                )
            self.assertEqual(connection.requests, [])


if __name__ == "__main__":
    unittest.main()
