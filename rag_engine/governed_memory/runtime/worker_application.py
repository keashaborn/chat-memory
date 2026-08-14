from __future__ import annotations

"""Fail-closed production composition for the inactive one-shot worker."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from importlib import resources
import json
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit
from uuid import UUID

from ..contracts import ContractViolation, require_key, require_sha256
from ..extraction import parse_predicate_catalog
from .environment import QDRANT_API_KEY_ENV
from .deletion_coordinator import InactiveDeletionCoordinator
from .deletion_postgres import (
    PostgresConversationDeletionRepository,
    PostgresSuccessorDeletionRepository,
)
from .https_transport import RetryFreeBoundedHttpsTransport
from .once_worker import (
    OnceWorker,
    OnceWorkerReceipt,
    ProjectionUpsertWork,
    WorkerLocalFailure,
)
from .openai_adapters import (
    DispatchReceipt,
    OpenAIEmbeddingAdapter,
    OpenAIEndpointConfig,
    OpenAIResponsesAdapter,
)
from .qdrant_adapter import ExactQdrantAdapter
from .qdrant_transport import LoopbackQdrantTransport, QDRANT_RUNTIME_URL
from .worker_postgres import (
    PostgresOnceWorkerRepository,
    PostgresWorkerLaneRepository,
)
from .worker_bridge import (
    BridgeIngestWorker,
    FairOnceRunner,
    PostgresConversationBridge,
    PostgresSuccessorIngest,
)


POSTGRES_DSN_ENV = "GOVERNED_MEMORY_POSTGRES_DSN"
CONVERSATION_POSTGRES_DSN_ENV = (
    "GOVERNED_MEMORY_CONVERSATION_POSTGRES_DSN"
)
OPENAI_API_KEY_ENV = "GOVERNED_MEMORY_OPENAI_API_KEY"
OPENAI_EXTRACTION_MODEL_ENV = "GOVERNED_MEMORY_OPENAI_EXTRACTION_MODEL"
EXPECTED_PILOT_ID_ENV = "GOVERNED_MEMORY_EXPECTED_PILOT_ID"
EXPECTED_PILOT_CONTRACT_SHA256_ENV = (
    "GOVERNED_MEMORY_EXPECTED_PILOT_CONTRACT_SHA256"
)
EXPECTED_AUTHORIZATION_RECEIPT_SHA256_ENV = (
    "GOVERNED_MEMORY_EXPECTED_AUTHORIZATION_RECEIPT_SHA256"
)
QDRANT_URL_ENV = "GOVERNED_MEMORY_QDRANT_URL"
WORKER_ID = "governed-memory-pilot-worker-1"
POSTGRES_HOST = "127.0.0.1"
POSTGRES_PORT = 55_432
POSTGRES_DATABASE = "governed_memory"
POSTGRES_ROLE = "governed_memory_worker"
POSTGRES_SERVER_PORT = 5_432
CONVERSATION_POSTGRES_HOST = "127.0.0.1"
CONVERSATION_POSTGRES_PORT = 5_432
CONVERSATION_POSTGRES_DATABASE = "memory"
CONVERSATION_POSTGRES_ROLE = "governed_memory_worker"
WORKER_ADVISORY_LOCK_DOMAIN = "governed-memory-successor-worker-concurrency-one-v1"
WORKER_ADVISORY_LOCK_KEY = int.from_bytes(
    sha256(WORKER_ADVISORY_LOCK_DOMAIN.encode("ascii")).digest()[:8],
    byteorder="big",
    signed=True,
)

_DATABASE_IDENTITY_SQL = (
    "SELECT current_database() AS database_name, session_user AS session_role, "
    "pg_catalog.inet_server_addr()::text AS server_address, "
    "pg_catalog.inet_server_port() AS server_port, "
    "current_setting('server_version_num')::integer AS server_version_num"
)
_TRY_LOCK_SQL = "SELECT pg_catalog.pg_try_advisory_lock($1::bigint)"
_UNLOCK_SQL = "SELECT pg_catalog.pg_advisory_unlock($1::bigint)"


class WorkerRuntimeRefusal(RuntimeError):
    """Content-free refusal safe for stderr and systemd status."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _secret(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 16_384
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise WorkerRuntimeRefusal("governed_memory_worker_configuration_invalid")
    return value


def _validated_postgres_dsn(
    value: str,
    *,
    host: str,
    port: int,
    database: str,
    role: str,
) -> str:
    try:
        target = urlsplit(value)
        parsed_port = target.port
    except ValueError as error:
        raise WorkerRuntimeRefusal(
            "governed_memory_worker_configuration_invalid"
        ) from error
    if (
        target.scheme not in {"postgres", "postgresql"}
        or target.hostname != host
        or parsed_port != port
        or unquote(target.username or "") != role
        or target.password is None
        or not target.password
        or target.path != f"/{database}"
        or target.query
        or target.fragment
    ):
        raise WorkerRuntimeRefusal("governed_memory_worker_configuration_invalid")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerRuntimeConfig:
    postgres_dsn: str = field(repr=False)
    conversation_postgres_dsn: str = field(repr=False)
    openai_api_key: str = field(repr=False)
    extraction_model: str
    expected_pilot_id: str
    expected_pilot_contract_sha256: str
    expected_authorization_receipt_sha256: str = field(repr=False)
    qdrant_url: str
    qdrant_api_key: str = field(repr=False)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
    ) -> "WorkerRuntimeConfig":
        if not isinstance(environment, Mapping):
            raise WorkerRuntimeRefusal(
                "governed_memory_worker_configuration_invalid"
            )
        postgres_dsn = _validated_postgres_dsn(
            _secret(environment, POSTGRES_DSN_ENV),
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            database=POSTGRES_DATABASE,
            role=POSTGRES_ROLE,
        )
        conversation_postgres_dsn = _validated_postgres_dsn(
            _secret(environment, CONVERSATION_POSTGRES_DSN_ENV),
            host=CONVERSATION_POSTGRES_HOST,
            port=CONVERSATION_POSTGRES_PORT,
            database=CONVERSATION_POSTGRES_DATABASE,
            role=CONVERSATION_POSTGRES_ROLE,
        )
        qdrant_url = environment.get(QDRANT_URL_ENV)
        if qdrant_url != QDRANT_RUNTIME_URL:
            raise WorkerRuntimeRefusal(
                "governed_memory_worker_configuration_invalid"
            )
        try:
            expected_pilot_id = require_key(
                _secret(environment, EXPECTED_PILOT_ID_ENV),
                "invalid_expected_pilot_id",
            )
            expected_pilot_contract_sha256 = require_sha256(
                _secret(
                    environment,
                    EXPECTED_PILOT_CONTRACT_SHA256_ENV,
                ),
                "invalid_expected_pilot_contract_sha256",
            )
            expected_authorization_receipt_sha256 = require_sha256(
                _secret(
                    environment,
                    EXPECTED_AUTHORIZATION_RECEIPT_SHA256_ENV,
                ),
                "invalid_expected_authorization_receipt_sha256",
            )
        except ContractViolation as error:
            raise WorkerRuntimeRefusal(
                "governed_memory_worker_configuration_invalid"
            ) from error
        config = cls(
            postgres_dsn=postgres_dsn,
            conversation_postgres_dsn=conversation_postgres_dsn,
            openai_api_key=_secret(environment, OPENAI_API_KEY_ENV),
            extraction_model=_secret(environment, OPENAI_EXTRACTION_MODEL_ENV),
            expected_pilot_id=expected_pilot_id,
            expected_pilot_contract_sha256=(
                expected_pilot_contract_sha256
            ),
            expected_authorization_receipt_sha256=(
                expected_authorization_receipt_sha256
            ),
            qdrant_url=qdrant_url,
            qdrant_api_key=_secret(environment, QDRANT_API_KEY_ENV),
        )
        try:
            OpenAIEndpointConfig(
                api_key=config.openai_api_key,
                extraction_model=config.extraction_model,
            )
            LoopbackQdrantTransport(
                base_url=config.qdrant_url,
                api_key=config.qdrant_api_key,
            )
        except ContractViolation as error:
            raise WorkerRuntimeRefusal(
                "governed_memory_worker_configuration_invalid"
            ) from error
        return config


class ProcessGuard(Protocol):
    async def acquire(self) -> bool: ...

    async def release(self) -> bool: ...


class OnceWorkerRunner(Protocol):
    async def run_once(self) -> OnceWorkerReceipt: ...


class PostgresAdvisoryGuard:
    def __init__(self, connection: Any) -> None:
        if connection is None:
            raise ContractViolation("worker_lock_connection_required")
        self._connection = connection
        self._held = False

    async def acquire(self) -> bool:
        if self._held:
            raise WorkerRuntimeRefusal(
                "governed_memory_worker_lock_state_invalid"
            )
        acquired = await self._connection.fetchval(
            _TRY_LOCK_SQL,
            WORKER_ADVISORY_LOCK_KEY,
        )
        if type(acquired) is not bool:
            raise WorkerRuntimeRefusal(
                "governed_memory_worker_lock_state_invalid"
            )
        self._held = acquired
        return acquired

    async def release(self) -> bool:
        if not self._held:
            raise WorkerRuntimeRefusal(
                "governed_memory_worker_lock_state_invalid"
            )
        released = await self._connection.fetchval(
            _UNLOCK_SQL,
            WORKER_ADVISORY_LOCK_KEY,
        )
        self._held = False
        if released is not True:
            raise WorkerRuntimeRefusal(
                "governed_memory_worker_lock_release_failed"
            )
        return True


class GuardedOnceRunner:
    """Acquire the session lock before any pilot read or queue mutation."""

    def __init__(self, *, guard: ProcessGuard, worker: OnceWorkerRunner) -> None:
        self._guard = guard
        self._worker = worker

    async def run_once(self) -> OnceWorkerReceipt:
        if await self._guard.acquire() is not True:
            raise WorkerRuntimeRefusal(
                "governed_memory_worker_lock_contended"
            )
        try:
            return await self._worker.run_once()
        finally:
            await self._guard.release()


class RuntimeExtractionProvider:
    def __init__(
        self,
        *,
        repository: PostgresOnceWorkerRepository,
        adapter: OpenAIResponsesAdapter,
    ) -> None:
        self._repository = repository
        self._adapter = adapter

    async def extract(self, request: Mapping[str, Any]) -> object:
        try:
            work_id = UUID(str(request["job_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise WorkerLocalFailure(
                "local_serialization_failed_before_send"
            ) from error
        from .once_worker import ExtractionWork

        work = ExtractionWork(work_id=work_id, provider_request=request)
        loop = asyncio.get_running_loop()

        def mark_dispatched(_receipt: object) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self._repository.mark_provider_dispatched(work, request),
                loop,
            )
            future.result()

        return await asyncio.to_thread(
            self._adapter.invoke,
            request,
            mark_dispatched=mark_dispatched,
        )


class RuntimeEmbeddingProvider:
    def __init__(
        self,
        *,
        repository: PostgresOnceWorkerRepository,
        adapter: OpenAIEmbeddingAdapter,
    ) -> None:
        self._repository = repository
        self._adapter = adapter

    async def embed(self, work: ProjectionUpsertWork, text: str) -> object:
        if not isinstance(work, ProjectionUpsertWork):
            raise WorkerLocalFailure(
                "local_serialization_failed_before_send"
            )
        loop = asyncio.get_running_loop()

        def mark_dispatched(receipt: DispatchReceipt) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self._repository.mark_projection_embedding_dispatched(
                    work,
                    receipt,
                ),
                loop,
            )
            future.result()

        completion = await asyncio.to_thread(
            self._adapter.invoke,
            text,
            mark_dispatched=mark_dispatched,
        )
        return completion.vector


def _load_predicate_catalog() -> Mapping[str, Any]:
    raw = resources.files(
        "rag_engine.governed_memory.provider_assets"
    ).joinpath("predicate_catalog.json").read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise WorkerRuntimeRefusal(
            "governed_memory_worker_configuration_invalid"
        ) from error
    if not isinstance(value, Mapping):
        raise WorkerRuntimeRefusal("governed_memory_worker_configuration_invalid")
    try:
        parse_predicate_catalog(value)
    except ContractViolation as error:
        raise WorkerRuntimeRefusal(
            "governed_memory_worker_configuration_invalid"
        ) from error
    return value


async def _connect(dsn: str, application_name: str) -> Any:
    import asyncpg

    return await asyncpg.connect(
        dsn=dsn,
        command_timeout=30,
        server_settings={
            "application_name": application_name,
            "lock_timeout": "2s",
            "statement_timeout": "30s",
        },
    )


async def _default_successor_connect(config: WorkerRuntimeConfig) -> Any:
    return await _connect(
        config.postgres_dsn,
        "governed-memory-successor-worker",
    )


async def _default_conversation_connect(config: WorkerRuntimeConfig) -> Any:
    return await _connect(
        config.conversation_postgres_dsn,
        "governed-memory-conversation-bridge-worker",
    )


ConnectionFactory = Callable[[WorkerRuntimeConfig], Awaitable[Any]]


async def _configure_connection(
    connection: Any,
    *,
    database: str = POSTGRES_DATABASE,
    role: str = POSTGRES_ROLE,
    minimum_server_version: int = 160_000,
) -> None:
    await connection.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=lambda value: json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ),
        decoder=json.loads,
    )
    row = await connection.fetchrow(_DATABASE_IDENTITY_SQL)
    identity = dict(row) if row is not None else {}
    expected = {
        "database_name": database,
        "session_role": role,
        "server_port": POSTGRES_SERVER_PORT,
    }
    if (
        any(identity.get(key) != value for key, value in expected.items())
        or not isinstance(identity.get("server_address"), str)
        or not identity["server_address"]
        or type(identity.get("server_version_num")) is not int
        or not minimum_server_version
        <= identity["server_version_num"]
        < 170_000
    ):
        raise WorkerRuntimeRefusal(
            "governed_memory_worker_database_identity_mismatch"
        )


async def run_runtime_once(
    config: WorkerRuntimeConfig,
    *,
    successor_connection_factory: ConnectionFactory = (
        _default_successor_connect
    ),
    conversation_connection_factory: ConnectionFactory = (
        _default_conversation_connect
    ),
) -> OnceWorkerReceipt:
    connection = await successor_connection_factory(config)
    try:
        await _configure_connection(connection)
        repository = PostgresOnceWorkerRepository(
            connection,
            worker_id=WORKER_ID,
            extraction_model=config.extraction_model,
            predicate_catalog=_load_predicate_catalog(),
            expected_pilot_id=config.expected_pilot_id,
            expected_pilot_contract_sha256=(
                config.expected_pilot_contract_sha256
            ),
            expected_authorization_receipt_sha256=(
                config.expected_authorization_receipt_sha256
            ),
        )
        https_transport = RetryFreeBoundedHttpsTransport(timeout_seconds=30)
        provider_config = OpenAIEndpointConfig(
            api_key=config.openai_api_key,
            extraction_model=config.extraction_model,
        )
        extraction_provider = RuntimeExtractionProvider(
            repository=repository,
            adapter=OpenAIResponsesAdapter(
                config=provider_config,
                transport=https_transport,
            ),
        )
        embedding_provider = RuntimeEmbeddingProvider(
            repository=repository,
            adapter=OpenAIEmbeddingAdapter(
                config=provider_config,
                transport=https_transport,
            ),
        )
        qdrant = ExactQdrantAdapter(
            LoopbackQdrantTransport(
                base_url=config.qdrant_url,
                api_key=config.qdrant_api_key,
            )
        )
        extraction_worker = OnceWorker(
            repository=PostgresWorkerLaneRepository(
                repository,
                lane="extraction",
            ),
            provider=extraction_provider,
            embedder=embedding_provider,
            qdrant=qdrant,
        )
        projection_worker = OnceWorker(
            repository=PostgresWorkerLaneRepository(
                repository,
                lane="projection",
            ),
            provider=extraction_provider,
            embedder=embedding_provider,
            qdrant=qdrant,
        )
        guard = PostgresAdvisoryGuard(connection)
        if await guard.acquire() is not True:
            raise WorkerRuntimeRefusal(
                "governed_memory_worker_lock_contended"
            )
        try:
            if await repository.pilot_ever_started() is not True:
                raise WorkerRuntimeRefusal("pilot_never_started")
            admission = await repository.auto_admit_one_ordinary_proposal()
            if admission is not None:
                return admission
            conversation = await conversation_connection_factory(config)
            try:
                await _configure_connection(
                    conversation,
                    database=CONVERSATION_POSTGRES_DATABASE,
                    role=CONVERSATION_POSTGRES_ROLE,
                    minimum_server_version=150_000,
                )
                return await FairOnceRunner(
                    pilot_repository=repository,
                    lane_scheduler=repository,
                    bridge_worker=BridgeIngestWorker(
                        bridge=PostgresConversationBridge(
                            conversation,
                            worker_id=WORKER_ID,
                        ),
                        successor=PostgresSuccessorIngest(connection),
                    ),
                    extraction_worker=extraction_worker,
                    projection_worker=projection_worker,
                    deletion_worker=InactiveDeletionCoordinator(
                        conversation=PostgresConversationDeletionRepository(
                            conversation
                        ),
                        successor=PostgresSuccessorDeletionRepository(
                            connection
                        ),
                    ),
                ).run_once()
            finally:
                await conversation.close()
        finally:
            await guard.release()
    finally:
        await connection.close()


def create_runtime_once_runner(
    environment: Mapping[str, str],
) -> Callable[[], Awaitable[OnceWorkerReceipt]]:
    config = WorkerRuntimeConfig.from_environment(environment)

    async def run() -> OnceWorkerReceipt:
        return await run_runtime_once(config)

    return run


__all__ = [
    "CONVERSATION_POSTGRES_DATABASE",
    "CONVERSATION_POSTGRES_DSN_ENV",
    "CONVERSATION_POSTGRES_HOST",
    "CONVERSATION_POSTGRES_PORT",
    "CONVERSATION_POSTGRES_ROLE",
    "GuardedOnceRunner",
    "EXPECTED_AUTHORIZATION_RECEIPT_SHA256_ENV",
    "EXPECTED_PILOT_CONTRACT_SHA256_ENV",
    "EXPECTED_PILOT_ID_ENV",
    "OPENAI_API_KEY_ENV",
    "OPENAI_EXTRACTION_MODEL_ENV",
    "POSTGRES_DATABASE",
    "POSTGRES_DSN_ENV",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_ROLE",
    "PostgresAdvisoryGuard",
    "QDRANT_API_KEY_ENV",
    "QDRANT_URL_ENV",
    "RuntimeEmbeddingProvider",
    "RuntimeExtractionProvider",
    "WORKER_ADVISORY_LOCK_KEY",
    "WORKER_ID",
    "WorkerRuntimeConfig",
    "WorkerRuntimeRefusal",
    "create_runtime_once_runner",
    "run_runtime_once",
]
