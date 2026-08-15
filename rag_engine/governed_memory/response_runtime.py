from __future__ import annotations

"""Lazy production composition for successor Memory response retrieval.

Construction is inert. Only an eligible ``successor_pilot`` request loads the
approved calibration/catalog and builds adapters. Network resources remain
lazy until the provider performs retrieval. PostgreSQL is authoritative;
Qdrant is restricted to the exact derived read target.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import http.client
import json
from pathlib import Path
import threading
from typing import Any
from urllib.parse import urlsplit

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    require_sha256,
)
from rag_engine.governed_memory.extraction import parse_predicate_catalog
from rag_engine.governed_memory.http_service import (
    DEFAULT_GOVERNED_MEMORY_POOL_FACTORY,
    POSTGRES_DSN_ENV,
    _configure_pool_connection,
    _preflight_connection,
    _reset_connection,
)
from rag_engine.governed_memory.response_postgres import (
    PostgresSuccessorResponseRepository,
)
from rag_engine.governed_memory.response_contracts import SuccessorResponseActorBinding
from rag_engine.governed_memory.response_provider import (
    SuccessorGovernedMemoryAssemblyProviderV1,
    SuccessorResponseConfigurationError,
)
from rag_engine.governed_memory.response_relevance import (
    OpenAIResponseRelevanceGateV1,
)
from rag_engine.governed_memory.runtime.calibration import (
    CalibrationDecision,
    load_calibration_decision,
)
from rag_engine.governed_memory.runtime.environment import QDRANT_API_KEY_ENV
from rag_engine.governed_memory.runtime.https_transport import (
    HttpsRequestRejectedBeforeSend,
    RetryFreeBoundedHttpsTransport,
    json_headers,
)
from rag_engine.governed_memory.runtime.openai_adapters import (
    OpenAIEmbeddingAdapter,
    OpenAIEndpointConfig,
)
from rag_engine.governed_memory.runtime.qdrant_adapter import (
    QDRANT_ALIAS,
    QDRANT_PHYSICAL_COLLECTION,
    ExactQdrantAdapter,
)
from rag_engine.governed_memory.runtime.qdrant_transport import (
    _qdrant_wire_value,
)


SUCCESSOR_OPENAI_API_KEY_ENV = "GOVERNED_MEMORY_OPENAI_API_KEY"
SUCCESSOR_CALIBRATION_PATH_ENV = "GOVERNED_MEMORY_CALIBRATION_PATH"
SUCCESSOR_CALIBRATION_ARTIFACT_SHA256_ENV = (
    "GOVERNED_MEMORY_CALIBRATION_ARTIFACT_SHA256"
)
SUCCESSOR_CALIBRATION_APPROVAL_SHA256_ENV = (
    "GOVERNED_MEMORY_CALIBRATION_APPROVAL_RECEIPT_SHA256"
)

EXPECTED_POSTGRES_HOST = "127.0.0.1"
EXPECTED_POSTGRES_PORT = 55_432
EXPECTED_POSTGRES_DATABASE = "governed_memory"
EXPECTED_POSTGRES_ROLE = "governed_memory_api"
EXPECTED_QDRANT_HOST = "127.0.0.1"
EXPECTED_QDRANT_PORT = 6_343

DEFAULT_PREDICATE_CATALOG_PATH = (
    Path(__file__).resolve().parent / "provider_assets" / "predicate_catalog.json"
)
_MAX_QDRANT_RESPONSE_BYTES = 2_097_152
_QDRANT_TIMEOUT_SECONDS = 5.0

_READ_FUNCTION = "memory_private.read_claim_candidates(uuid[])"
_BINDING_FUNCTION = (
    "memory_private.record_answer_binding("
    "uuid,uuid,uuid,text,text,text[],text[],text[],integer,integer,text,text,"
    "boolean,uuid[],uuid[],text,text,text,text,text)"
)
_RESPONSE_FUNCTION_PREFLIGHT_SQL = f"""
WITH exact_functions AS (
  SELECT
    pg_catalog.to_regprocedure('{_READ_FUNCTION}') AS read_function,
    pg_catalog.to_regprocedure('{_BINDING_FUNCTION}') AS binding_function
)
SELECT
  read_function::text AS read_function,
  binding_function::text AS binding_function,
  pg_catalog.has_function_privilege(
    session_user,
    read_function,
    'EXECUTE'
  ) AS can_read,
  pg_catalog.has_function_privilege(
    session_user,
    binding_function,
    'EXECUTE'
  ) AS can_bind
FROM exact_functions
"""


def _required_secret(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 16_384
        or any(character.isspace() for character in value)
    ):
        raise SuccessorResponseConfigurationError(
            "successor_response_runtime_configuration_invalid"
        )
    return value


def _required_sha256(environment: Mapping[str, str], name: str) -> str:
    try:
        return require_sha256(
            environment.get(name),
            "successor_response_runtime_configuration_invalid",
        )
    except ContractViolation as exc:
        raise SuccessorResponseConfigurationError(
            "successor_response_runtime_configuration_invalid"
        ) from exc


def _required_openai_key(environment: Mapping[str, str]) -> str:
    value = _required_secret(environment, SUCCESSOR_OPENAI_API_KEY_ENV)
    try:
        json_headers(bearer_token=value)
    except HttpsRequestRejectedBeforeSend as exc:
        raise SuccessorResponseConfigurationError(
            "successor_response_runtime_configuration_invalid"
        ) from exc
    return value


def _exact_postgres_dsn(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SuccessorResponseConfigurationError(
            "successor_response_postgres_target_invalid"
        ) from exc
    if (
        parsed.scheme != "postgresql"
        or parsed.hostname != EXPECTED_POSTGRES_HOST
        or port != EXPECTED_POSTGRES_PORT
        or parsed.username != EXPECTED_POSTGRES_ROLE
        or parsed.password is None
        or not parsed.password
        or parsed.path != f"/{EXPECTED_POSTGRES_DATABASE}"
        or parsed.query
        or parsed.fragment
    ):
        raise SuccessorResponseConfigurationError(
            "successor_response_postgres_target_invalid"
        )
    return value


@dataclass(frozen=True, slots=True)
class SuccessorResponseRuntimeSettings:
    postgres_dsn: str = field(repr=False)
    openai_api_key: str = field(repr=False)
    qdrant_api_key: str = field(repr=False)
    calibration_path: Path
    expected_calibration_artifact_sha256: str
    expected_calibration_approval_sha256: str

    def __post_init__(self) -> None:
        _exact_postgres_dsn(self.postgres_dsn)
        try:
            json_headers(bearer_token=self.openai_api_key)
        except HttpsRequestRejectedBeforeSend as exc:
            raise SuccessorResponseConfigurationError(
                "successor_response_runtime_configuration_invalid"
            ) from exc
        if (
            not isinstance(self.qdrant_api_key, str)
            or not self.qdrant_api_key
            or self.qdrant_api_key != self.qdrant_api_key.strip()
            or len(self.qdrant_api_key.encode("utf-8")) > 1_024
            or any(
                ord(character) < 33 or ord(character) == 127
                for character in self.qdrant_api_key
            )
        ):
            raise SuccessorResponseConfigurationError(
                "successor_response_runtime_configuration_invalid"
            )
        if (
            not isinstance(self.calibration_path, Path)
            or not self.calibration_path.is_absolute()
        ):
            raise SuccessorResponseConfigurationError(
                "successor_response_calibration_path_invalid"
            )
        try:
            require_sha256(
                self.expected_calibration_artifact_sha256,
                "successor_response_runtime_configuration_invalid",
            )
            require_sha256(
                self.expected_calibration_approval_sha256,
                "successor_response_runtime_configuration_invalid",
            )
        except ContractViolation as exc:
            raise SuccessorResponseConfigurationError(
                "successor_response_runtime_configuration_invalid"
            ) from exc

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
    ) -> "SuccessorResponseRuntimeSettings":
        dsn = _required_secret(environment, POSTGRES_DSN_ENV)
        raw_path = environment.get(SUCCESSOR_CALIBRATION_PATH_ENV)
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or raw_path != raw_path.strip()
        ):
            raise SuccessorResponseConfigurationError(
                "successor_response_calibration_path_invalid"
            )
        return cls(
            postgres_dsn=_exact_postgres_dsn(dsn),
            openai_api_key=_required_openai_key(environment),
            qdrant_api_key=_required_secret(environment, QDRANT_API_KEY_ENV),
            calibration_path=Path(raw_path),
            expected_calibration_artifact_sha256=_required_sha256(
                environment,
                SUCCESSOR_CALIBRATION_ARTIFACT_SHA256_ENV,
            ),
            expected_calibration_approval_sha256=_required_sha256(
                environment,
                SUCCESSOR_CALIBRATION_APPROVAL_SHA256_ENV,
            ),
        )


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _load_predicate_catalog(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 131_072:
            raise ValueError("predicate_catalog_size")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_closed_json_object,
        )
        if not isinstance(value, dict):
            raise ValueError("predicate_catalog_shape")
        parse_predicate_catalog(value)
        return value
    except Exception as exc:
        raise SuccessorResponseConfigurationError(
            "successor_response_predicate_catalog_invalid"
        ) from exc


def _load_approved_calibration(
    settings: SuccessorResponseRuntimeSettings,
) -> CalibrationDecision:
    decision = load_calibration_decision(
        settings.calibration_path,
        expected_artifact_sha256=(
            settings.expected_calibration_artifact_sha256
        ),
        expected_approval_receipt_sha256=(
            settings.expected_calibration_approval_sha256
        ),
    )
    if not decision.retrieval_enabled:
        raise SuccessorResponseConfigurationError(
            "successor_response_calibration_not_approved"
        )
    return decision


QdrantConnectionFactory = Callable[
    [str, int, float],
    http.client.HTTPConnection,
]


def _default_qdrant_connection(
    host: str,
    port: int,
    timeout: float,
) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(host, port=port, timeout=timeout)


class LoopbackQdrantRetrievalTransport:
    """Retry-free, read-only transport for the exact successor collection."""

    def __init__(
        self,
        *,
        api_key: str,
        connection_factory: QdrantConnectionFactory = (
            _default_qdrant_connection
        ),
    ) -> None:
        if not callable(connection_factory):
            raise ContractViolation("invalid_qdrant_connection_factory")
        if (
            not isinstance(api_key, str)
            or not api_key
            or api_key != api_key.strip()
            or len(api_key.encode("utf-8")) > 1_024
            or any(
                ord(character) < 33 or ord(character) == 127
                for character in api_key
            )
        ):
            raise ContractViolation("qdrant_response_api_key_invalid")
        self._api_key = api_key
        self._connection_factory = connection_factory
        self._allowed = {
            (
                "GET",
                f"/collections/{QDRANT_PHYSICAL_COLLECTION}/aliases",
            ),
            ("GET", f"/collections/{QDRANT_PHYSICAL_COLLECTION}"),
            ("POST", f"/collections/{QDRANT_ALIAS}/points/search"),
        }

    async def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]:
        if (method, path) not in self._allowed:
            raise ContractViolation("qdrant_response_transport_write_forbidden")
        if (method == "POST") != (body is not None):
            raise ContractViolation("qdrant_response_transport_body_invalid")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._request_sync, method, path, body),
                timeout=_QDRANT_TIMEOUT_SECONDS + 1,
            )
        except ContractViolation:
            raise
        except Exception as exc:
            raise ContractViolation("qdrant_response_transport_unavailable") from exc

    def _request_sync(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None,
    ) -> Mapping[str, Any]:
        encoded = None
        if body is not None:
            try:
                encoded = json.dumps(
                    _qdrant_wire_value(body),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (ContractViolation, TypeError, ValueError) as exc:
                raise ContractViolation(
                    "qdrant_response_transport_body_invalid"
                ) from exc
        headers = {
            "Accept": "application/json",
            "api-key": self._api_key,
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        connection = self._connection_factory(
            EXPECTED_QDRANT_HOST,
            EXPECTED_QDRANT_PORT,
            _QDRANT_TIMEOUT_SECONDS,
        )
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            raw = response.read(_MAX_QDRANT_RESPONSE_BYTES + 1)
            content_type = response.getheader("Content-Type", "")
            if (
                response.status != 200
                or content_type.split(";", 1)[0].strip().lower()
                != "application/json"
                or not raw
                or len(raw) > _MAX_QDRANT_RESPONSE_BYTES
            ):
                raise ContractViolation("qdrant_response_transport_rejected")
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_closed_json_object,
            )
            if not isinstance(value, dict):
                raise ContractViolation("qdrant_response_transport_invalid")
            return value
        except ContractViolation:
            raise
        except Exception as exc:
            raise ContractViolation("qdrant_response_transport_unavailable") from exc
        finally:
            try:
                connection.close()
            except Exception:
                pass


class OneShotOpenAIResponseEmbedder:
    def __init__(self, api_key: str) -> None:
        self._adapter = OpenAIEmbeddingAdapter(
            config=OpenAIEndpointConfig(
                api_key=api_key,
                extraction_model="gpt-5.1",
            ),
            transport=RetryFreeBoundedHttpsTransport(timeout_seconds=30.0),
        )
        self._used = False

    async def embed(self, text: str) -> tuple[float, ...]:
        if self._used:
            raise ContractViolation("response_embedding_provider_reused")
        self._used = True
        dispatch_count = 0

        def mark_dispatched(_receipt: object) -> None:
            nonlocal dispatch_count
            dispatch_count += 1
            if dispatch_count != 1:
                raise ContractViolation("response_embedding_dispatch_repeated")

        completion = await asyncio.to_thread(
            self._adapter.invoke,
            text,
            mark_dispatched=mark_dispatched,
        )
        if dispatch_count != 1:
            raise ContractViolation("response_embedding_dispatch_missing")
        return completion.vector


PoolFactory = Callable[..., Awaitable[Any]]


class _LazyPoolAcquire:
    def __init__(self, runtime: "SuccessorResponseRuntime") -> None:
        self._runtime = runtime
        self._delegate: Any | None = None

    async def __aenter__(self) -> Any:
        pool = await self._runtime._pool_for_requests()
        self._delegate = pool.acquire()
        return await self._delegate.__aenter__()

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        assert self._delegate is not None
        return await self._delegate.__aexit__(exc_type, exc, traceback)


class _LazyPoolFacade:
    def __init__(self, runtime: "SuccessorResponseRuntime") -> None:
        self._runtime = runtime

    def acquire(self) -> _LazyPoolAcquire:
        return _LazyPoolAcquire(self._runtime)


class SuccessorResponseRuntime:
    """Process-lifetime lazy resources, bound to one immutable configuration."""

    def __init__(
        self,
        *,
        pool_factory: PoolFactory = DEFAULT_GOVERNED_MEMORY_POOL_FACTORY,
        predicate_catalog_path: Path = DEFAULT_PREDICATE_CATALOG_PATH,
    ) -> None:
        if not callable(pool_factory) or not isinstance(
            predicate_catalog_path,
            Path,
        ):
            raise SuccessorResponseConfigurationError(
                "successor_response_runtime_configuration_invalid"
            )
        self._pool_factory = pool_factory
        self._predicate_catalog_path = predicate_catalog_path
        self._configuration_lock = threading.Lock()
        self._pool_lock = asyncio.Lock()
        self._settings: SuccessorResponseRuntimeSettings | None = None
        self._predicate_catalog: dict[str, object] | None = None
        self._calibration: CalibrationDecision | None = None
        self._pool: Any | None = None
        self._openai_client: Any | None = None

    def provider(
        self,
        binding: SuccessorResponseActorBinding,
        environment: Mapping[str, str],
    ) -> SuccessorGovernedMemoryAssemblyProviderV1:
        if not isinstance(binding, SuccessorResponseActorBinding):
            raise SuccessorResponseConfigurationError(
                "successor_response_actor_invalid"
            )
        settings = SuccessorResponseRuntimeSettings.from_environment(environment)
        with self._configuration_lock:
            if self._settings is None:
                catalog = _load_predicate_catalog(self._predicate_catalog_path)
                calibration = _load_approved_calibration(settings)
                self._settings = settings
                self._predicate_catalog = catalog
                self._calibration = calibration
            elif settings != self._settings:
                raise SuccessorResponseConfigurationError(
                    "successor_response_runtime_configuration_changed"
                )
            assert self._predicate_catalog is not None
            assert self._calibration is not None
            if self._openai_client is None:
                # This module is packaged in the isolated worker runtime,
                # which deliberately excludes the Brains-only OpenAI SDK.
                # Import it only when Brains constructs a response provider.
                from openai import OpenAI

                self._openai_client = OpenAI(api_key=settings.openai_api_key)
            openai_client = self._openai_client
            catalog_copy = json.loads(
                json.dumps(
                    self._predicate_catalog,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            calibration = self._calibration
        try:
            return SuccessorGovernedMemoryAssemblyProviderV1(
                actor=binding,
                repository=PostgresSuccessorResponseRepository(
                    _LazyPoolFacade(self)
                ),
                vector_index=ExactQdrantAdapter(
                    LoopbackQdrantRetrievalTransport(
                        api_key=settings.qdrant_api_key
                    )
                ),
                embedder=OneShotOpenAIResponseEmbedder(
                    settings.openai_api_key
                ),
                relevance_gate=OpenAIResponseRelevanceGateV1(
                    client=openai_client
                ),
                predicate_catalog=catalog_copy,
                calibration=calibration,
            )
        except SuccessorResponseConfigurationError:
            raise
        except Exception as exc:
            raise SuccessorResponseConfigurationError(
                "successor_response_runtime_configuration_invalid"
            ) from exc

    async def _pool_for_requests(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is not None:
                return self._pool
            settings = self._settings
            if settings is None:
                raise SuccessorResponseConfigurationError(
                    "successor_response_runtime_unconfigured"
                )
            pool: Any | None = None
            try:
                pool = await self._pool_factory(
                    dsn=settings.postgres_dsn,
                    min_size=1,
                    max_size=1,
                    max_queries=500,
                    max_inactive_connection_lifetime=60.0,
                    command_timeout=8.0,
                    reset=_reset_connection,
                    init=_configure_pool_connection,
                    server_settings={
                        "application_name": "governed_memory_response",
                        "statement_timeout": "8000",
                        "lock_timeout": "2000",
                        "idle_in_transaction_session_timeout": "8000",
                    },
                )
                async with pool.acquire() as connection:
                    await _preflight_connection(connection)
                    row = await connection.fetchrow(
                        _RESPONSE_FUNCTION_PREFLIGHT_SQL
                    )
                if (
                    row is None
                    or row["read_function"] != _READ_FUNCTION
                    or row["binding_function"] != _BINDING_FUNCTION
                    or row["can_read"] is not True
                    or row["can_bind"] is not True
                ):
                    raise SuccessorResponseConfigurationError(
                        "successor_response_postgres_contract_invalid"
                    )
            except SuccessorResponseConfigurationError:
                if pool is not None:
                    await pool.close()
                raise
            except Exception as exc:
                if pool is not None:
                    await pool.close()
                raise SuccessorResponseConfigurationError(
                    "successor_response_postgres_unavailable"
                ) from exc
            self._pool = pool
            return pool

    async def close(self) -> None:
        async with self._pool_lock:
            pool = self._pool
            self._pool = None
            if pool is not None:
                await pool.close()
        with self._configuration_lock:
            client = self._openai_client
            self._openai_client = None
        if client is not None:
            await asyncio.to_thread(client.close)


__all__ = [
    "DEFAULT_PREDICATE_CATALOG_PATH",
    "EXPECTED_POSTGRES_DATABASE",
    "EXPECTED_POSTGRES_HOST",
    "EXPECTED_POSTGRES_PORT",
    "EXPECTED_POSTGRES_ROLE",
    "EXPECTED_QDRANT_HOST",
    "EXPECTED_QDRANT_PORT",
    "QDRANT_API_KEY_ENV",
    "LoopbackQdrantRetrievalTransport",
    "OneShotOpenAIResponseEmbedder",
    "SUCCESSOR_CALIBRATION_APPROVAL_SHA256_ENV",
    "SUCCESSOR_CALIBRATION_ARTIFACT_SHA256_ENV",
    "SUCCESSOR_CALIBRATION_PATH_ENV",
    "SUCCESSOR_OPENAI_API_KEY_ENV",
    "SuccessorResponseRuntime",
    "SuccessorResponseRuntimeSettings",
]
