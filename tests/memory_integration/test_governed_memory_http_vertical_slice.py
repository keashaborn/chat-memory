from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
from ipaddress import IPv4Address
import json
import os
import select
import socket
import socketserver
import threading
import time
from typing import Any, AsyncIterator, Mapping
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID

if os.environ.get("GM_VALIDATION_RUN") == "1":
    import asyncpg
    import uvicorn
else:  # Keep offline unit discovery free of the integration dependency.
    asyncpg = None  # type: ignore[assignment]
    uvicorn = None  # type: ignore[assignment]

from cryptography.hazmat.primitives.asymmetric import ec

from rag_engine.governed_memory.auth import (
    ActorRole,
    ActorScope,
    VerifiedActor,
)
from rag_engine.governed_memory.api import route_manifest_sha256
from rag_engine.governed_memory.http_api import create_owner_memory_app
from rag_engine.governed_memory.http_runtime import (
    SupabaseHttpRuntimeConfig,
    create_supabase_actor_resolver,
)
from rag_engine.governed_memory.http_store import PostgresOwnerStore
from rag_engine.governed_memory.contracts import (
    ContractViolation,
    canonical_json_bytes,
    canonical_sha256,
)
from rag_engine.governed_memory.eligibility import EligibilityPolicy
from rag_engine.governed_memory.extraction import (
    CANONICAL_PREDICATE_CATALOG_SHA256,
    build_provider_request,
    validate_provider_result,
)
from rag_engine.governed_memory.projection import (
    PROJECTION_OUTBOX_FIELDS,
    QDRANT_PAYLOAD_FIELDS,
    build_projection_delete,
    build_projection_delete_receipt,
    build_projection_point,
    build_rebuild_projection_point,
    qdrant_absence_verification_sha256,
    vector_sha256,
)
from rag_engine.governed_memory.postgres_adapter import (
    extraction_lease_to_provider_inputs,
    normalize_postgres_record,
    projection_rebuild_row_to_inputs,
)
from rag_engine.governed_memory.conversation_source import (
    split_leased_chat_log_message,
)
from rag_engine.governed_memory.retrieval import (
    ANSWER_RENDERER_SHA256,
    CANDIDATE_FIELDS,
    RetrievalPolicy,
    build_answer_binding,
    mark_answer_binding_dispatched,
    render_memory_context,
    revalidate_candidates,
    validate_vector_candidates,
)
from rag_engine.governed_memory.runtime.calibration import CalibrationDecision
from rag_engine.governed_memory.runtime.qdrant_adapter import (
    ExactQdrantAdapter,
    QDRANT_PHYSICAL_COLLECTION,
    QDRANT_REQUIRED_PAYLOAD_INDEXES,
)
from rag_engine.governed_memory.worker import process_ingest_item
from tests.memory._fixtures import (
    MESSAGE_A,
    OWNER_A,
    OWNER_B,
    PREDICATE_CATALOG,
    SOURCE_TEXT,
    THREAD_A,
    deterministic_vector,
    make_claim_row,
    make_ingest_payload,
    make_projection_outbox,
    make_provider_output,
)
from tests.memory_integration.local_jwks_server import (
    JWKS_ISSUER,
    JWKS_URL,
    LocalJwksAuthority,
)


RUN_MARKER = "governed-memory-successor-disposable:019fe927"
API_BASE_URL = "http://127.0.0.1:18092"
API_HOST = "127.0.0.1"
API_PORT = 18092
AUTH_CONTEXT_SHA256 = "a" * 64
MODEL = "synthetic-extraction-model"
SCHEMA = "governed-memory-extraction"
SCHEMA_SHA256 = sha256(SCHEMA.encode("utf-8")).hexdigest()
PRIVACY_MANIFEST_SHA256 = sha256(
    b"governed-memory-successor-disposable-privacy"
).hexdigest()
PROMPT_SHA256 = "d" * 64
QUERY_SHA256 = "e" * 64
RESPONSE_ID = UUID("99999999-9999-4999-8999-999999999991")
ANSWER_OPERATION_ID = UUID("99999999-9999-4999-8999-999999999992")
CORRECTION_OPERATION_ID = UUID("99999999-9999-4999-8999-999999999993")
RETRACTION_OPERATION_ID = UUID("99999999-9999-4999-8999-999999999994")
DELETION_OPERATION_ID = UUID("99999999-9999-4999-8999-999999999995")
OWNER_B_CORRECTION_OPERATION_ID = UUID("99999999-9999-4999-8999-999999999996")
OWNER_B_RETRACTION_OPERATION_ID = UUID("99999999-9999-4999-8999-999999999997")
OWNER_B_DELETION_OPERATION_ID = UUID("99999999-9999-4999-8999-999999999998")
ALTERNATE_SOURCE_MESSAGE = UUID("99999999-9999-4999-8999-999999999999")
THREAD_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2")
OWNER_B_CLAIM_ID = UUID("22222222-2222-4222-8222-222222222226")
OWNER_B_REVISION_ID = UUID("22222222-2222-4222-8222-222222222227")
ALIAS = "governed_memory_active"
PHYSICAL_A = QDRANT_PHYSICAL_COLLECTION
PHYSICAL_B = "governed_memory_successor_b_019fe927"
INDEX_FIELDS = {
    "owner_user_id": "keyword",
    "lifecycle_state": "keyword",
    "is_current": "bool",
    "projectable": "bool",
    "predicate": "keyword",
    "requires_explicit": "bool",
    "sensitivity": "keyword",
    "domains": "keyword",
    "intents": "keyword",
}
OWNER_TABLES = (
    "answer_binding",
    "audit_event",
    "claim",
    "claim_deletion_receipt",
    "claim_evidence",
    "claim_revision",
    "entity",
    "evidence",
    "extraction_job",
    "projection_outbox",
    "proposal",
    "provider_call",
)


def _validated_relay_target(variable_name: str) -> str:
    value = os.environ.get(variable_name, "")
    try:
        address = IPv4Address(value)
    except ValueError as exc:
        raise AssertionError(f"invalid relay target: {variable_name}") from exc
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    ):
        raise AssertionError(f"unsafe relay target: {variable_name}")
    return str(address)


class _RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True
    request_queue_size = 64

    def __init__(self, local_port: int, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self.stopping = threading.Event()
        self._active_lock = threading.Lock()
        self._active_sockets: set[socket.socket] = set()
        super().__init__(("127.0.0.1", local_port), _RelayHandler)

    def register_sockets(self, *sockets: socket.socket) -> None:
        with self._active_lock:
            self._active_sockets.update(sockets)

    def unregister_sockets(self, *sockets: socket.socket) -> None:
        with self._active_lock:
            self._active_sockets.difference_update(sockets)

    def close_active_sockets(self) -> None:
        with self._active_lock:
            sockets = tuple(self._active_sockets)
        for active_socket in sockets:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                active_socket.close()
            except OSError:
                pass

    def handle_error(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        self.stopping.set()


class _RelayHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not isinstance(server, _RelayServer) or server.stopping.is_set():
            return
        try:
            upstream = socket.create_connection(server.target, timeout=10)
        except OSError:
            return
        server.register_sockets(self.request, upstream)
        try:
            self.request.settimeout(None)
            upstream.settimeout(None)
            readable_sockets = {self.request, upstream}
            while readable_sockets and not server.stopping.is_set():
                readable, _, exceptional = select.select(
                    tuple(readable_sockets),
                    (),
                    tuple(readable_sockets),
                    0.25,
                )
                if exceptional:
                    return
                for source in readable:
                    destination = upstream if source is self.request else self.request
                    try:
                        payload = source.recv(65_536)
                    except OSError:
                        return
                    if not payload:
                        readable_sockets.discard(source)
                        try:
                            destination.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        continue
                    try:
                        destination.sendall(payload)
                    except OSError:
                        return
        finally:
            server.unregister_sockets(self.request, upstream)
            try:
                upstream.close()
            except OSError:
                pass


class _LoopbackTcpRelay:
    def __init__(self, local_port: int, target_host: str, target_port: int) -> None:
        self._server = _RelayServer(local_port, target_host, target_port)
        if self._server.server_address != ("127.0.0.1", local_port):
            self._server.server_close()
            raise AssertionError("relay did not bind the exact loopback endpoint")
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"governed-memory-relay-{local_port}",
        )
        self._started = False
        self._closed = False

    def start(self) -> None:
        if self._started or self._closed:
            raise AssertionError("relay start state is invalid")
        self._thread.start()
        self._started = True
        if not self._thread.is_alive():
            raise AssertionError("relay thread did not start")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.stopping.set()
        if self._started:
            self._server.shutdown()
        self._server.close_active_sockets()
        self._server.server_close()
        if self._started:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise AssertionError("relay thread did not stop")


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError(f"duplicate HTTP response key: {key}")
        result[key] = value
    return result


class QdrantRest:
    def __init__(self, base_url: str) -> None:
        if base_url != "http://127.0.0.1:6339":
            raise RuntimeError("Governed Memory successor Qdrant URL is not the isolated endpoint")
        self.base_url = base_url

    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        allow_not_found: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        encoded = None if body is None else json.dumps(_jsonable(body)).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=encoded,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else {}
        except HTTPError as exc:
            payload = exc.read()
            if allow_not_found and exc.code == 404:
                return exc.code, json.loads(payload) if payload else {}
            raise AssertionError(
                f"Qdrant {method} {path} failed with {exc.code}: "
                f"{payload.decode('utf-8', errors='replace')[:500]}"
            ) from exc

    def collection_exists(self, name: str) -> bool:
        status, _ = self.request(
            "GET", f"/collections/{name}", allow_not_found=True
        )
        return status == 200

    def create_collection(self, name: str) -> None:
        status, _ = self.request(
            "PUT",
            f"/collections/{name}",
            {
                "vectors": {"size": 3072, "distance": "Dot"},
                "shard_number": 1,
                "replication_factor": 1,
                "write_consistency_factor": 1,
                "on_disk_payload": False,
            },
        )
        if status != 200:
            raise AssertionError("Qdrant collection creation did not complete")
        for field_name, field_schema in INDEX_FIELDS.items():
            self.request(
                "PUT",
                f"/collections/{name}/index?wait=true",
                {"field_name": field_name, "field_schema": field_schema},
            )

    def aliases(self) -> dict[str, str]:
        _, payload = self.request("GET", "/aliases")
        return {
            item["alias_name"]: item["collection_name"]
            for item in payload["result"]["aliases"]
        }

    def create_alias(self, alias: str, physical: str) -> None:
        self.request(
            "POST",
            "/collections/aliases?timeout=10",
            {
                "actions": [
                    {
                        "create_alias": {
                            "alias_name": alias,
                            "collection_name": physical,
                        }
                    }
                ]
            },
        )

    def swap_alias(self, alias: str, old: str, new: str) -> None:
        if self.aliases().get(alias) != old:
            raise AssertionError("Qdrant alias did not point to the expected old build")
        self.request(
            "POST",
            "/collections/aliases?timeout=10",
            {
                "actions": [
                    {"delete_alias": {"alias_name": alias}},
                    {
                        "create_alias": {
                            "alias_name": alias,
                            "collection_name": new,
                        }
                    },
                ]
            },
        )
        if self.aliases().get(alias) != new:
            raise AssertionError("Qdrant alias swap did not complete atomically")

    def upsert(self, collection: str, point: Mapping[str, Any]) -> None:
        self.request(
            "PUT",
            f"/collections/{collection}/points?wait=true",
            {
                "points": [
                    {
                        "id": point["point_id"],
                        "vector": point["vector"],
                        "payload": point["payload"],
                    }
                ]
            },
        )

    def delete_point(self, collection: str, point_id: UUID | str) -> None:
        self.request(
            "POST",
            f"/collections/{collection}/points/delete?wait=true",
            {"points": [str(point_id)]},
        )

    def retrieve(self, collection: str, point_id: UUID | str) -> list[dict[str, Any]]:
        _, payload = self.request(
            "POST",
            f"/collections/{collection}/points",
            {
                "ids": [str(point_id)],
                "with_payload": True,
                "with_vector": True,
            },
        )
        return payload["result"]

    def search(
        self,
        collection: str,
        owner_user_id: UUID,
        vector: list[float] | tuple[float, ...],
    ) -> list[dict[str, Any]]:
        payload_fields = [field for field in CANDIDATE_FIELDS if field != "score"]
        _, payload = self.request(
            "POST",
            f"/collections/{collection}/points/search",
            {
                "vector": vector,
                "limit": 8,
                "params": {"exact": True},
                "with_vector": False,
                "with_payload": {"include": payload_fields},
                "filter": {
                    "must": [
                        {
                            "key": "owner_user_id",
                            "match": {"value": str(owner_user_id)},
                        },
                        {
                            "key": "lifecycle_state",
                            "match": {"value": "active"},
                        },
                        {"key": "is_current", "match": {"value": True}},
                        {"key": "projectable", "match": {"value": True}},
                        {
                            "key": "predicate",
                            "match": {"any": ["preference.personal"]},
                        },
                        {
                            "key": "requires_explicit",
                            "match": {"value": False},
                        },
                    ]
                },
            },
        )
        return payload["result"]

    def remove_all(self) -> None:
        aliases = self.aliases()
        if aliases.get(ALIAS) in {PHYSICAL_A, PHYSICAL_B}:
            self.request(
                "POST",
                "/collections/aliases?timeout=10",
                {"actions": [{"delete_alias": {"alias_name": ALIAS}}]},
            )
        for name in (PHYSICAL_A, PHYSICAL_B):
            if self.collection_exists(name):
                self.request("DELETE", f"/collections/{name}?timeout=10")


class AsyncQdrantRestTransport:
    def __init__(self, client: QdrantRest) -> None:
        self._client = client

    async def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]:
        _, payload = self._client.request(method, path, body)
        return payload


@unittest.skipUnless(
    os.environ.get("GM_VALIDATION_RUN") == "1",
    "requires explicitly authorized disposable Governed Memory successor infrastructure",
)
class GovernedMemoryHttpVerticalSliceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        assert asyncpg is not None and uvicorn is not None
        host = os.environ.get("GM_VALIDATION_POSTGRES_HOST")
        port_text = os.environ.get("GM_VALIDATION_POSTGRES_PORT")
        if host != "127.0.0.1" or port_text != "55443":
            self.fail("Governed Memory successor PostgreSQL endpoint is not the isolated endpoint")
        if os.environ.get("GM_VALIDATION_API_URL") != API_BASE_URL:
            self.fail("successor API URL is not the isolated endpoint")
        if os.environ.get("GM_VALIDATION_JWKS_URL") != (
            JWKS_ISSUER + "/.well-known/jwks.json"
        ):
            self.fail("successor JWKS URL is not the isolated endpoint")
        postgres_container_ip = _validated_relay_target(
            "GM_VALIDATION_POSTGRES_CONTAINER_IP"
        )
        qdrant_container_ip = _validated_relay_target(
            "GM_VALIDATION_QDRANT_CONTAINER_IP"
        )
        if postgres_container_ip == qdrant_container_ip:
            self.fail("successor relay targets are not distinct")
        self.relays: list[_LoopbackTcpRelay] = []
        try:
            for local_port, target_host, target_port in (
                (55443, postgres_container_ip, 5432),
                (6339, qdrant_container_ip, 6333),
            ):
                relay = _LoopbackTcpRelay(
                    local_port, target_host, target_port
                )
                self.relays.append(relay)
                relay.start()
        except BaseException:
            self._close_relays()
            raise
        self.addAsyncCleanup(asyncio.to_thread, self._close_relays)
        self.host = host
        self.port = int(port_text)
        self.qdrant = QdrantRest(os.environ.get("GM_VALIDATION_QDRANT_URL", ""))
        self.connections: list[Any] = []
        self.admin = await self._connect("postgres", "successor_disposable_only", "governed_memory")
        self.bridge_admin = await self._connect(
            "postgres", "successor_disposable_only", "memory"
        )
        self.api = await self._connect(
            "governed_memory_api", "successor_api_disposable_only", "governed_memory"
        )
        self.worker = await self._connect(
            "governed_memory_worker",
            "successor_worker_disposable_only",
            "governed_memory",
        )
        self.worker_two = await self._connect(
            "governed_memory_worker",
            "successor_worker_disposable_only",
            "governed_memory",
        )
        self.bridge_worker = await self._connect(
            "governed_memory_worker",
            "successor_worker_disposable_only",
            "memory",
        )
        self.ingest = await self._connect(
            "brains_app",
            "successor_brains_app_disposable_only",
            "memory",
        )
        self.qdrant.remove_all()
        self.addCleanup(self.qdrant.remove_all)

        invocation_text = os.environ.get("GM_VALIDATION_INVOCATION_ID", "")
        service_token = os.environ.get("GM_VALIDATION_SERVICE_TOKEN", "")
        try:
            self.invocation_id = UUID(invocation_text)
        except ValueError:
            self.fail("invalid successor invocation id")
        if not service_token or len(service_token) > 1024:
            self.fail("invalid successor service token")
        self.service_token = service_token

        self.jwks = LocalJwksAuthority(invocation_id=self.invocation_id)
        await asyncio.to_thread(self.jwks.start)
        self.addAsyncCleanup(asyncio.to_thread, self.jwks.close)
        actor_resolver = create_supabase_actor_resolver(
            SupabaseHttpRuntimeConfig(
                allow_disposable_loopback_http=True,
                audience="authenticated",
                expected_service_token=self.service_token,
                issuer=JWKS_ISSUER,
                jwks_url=JWKS_URL,
            ),
            token_clock=lambda: datetime.now(timezone.utc),
        )

        async def configure_pool_connection(connection: Any) -> None:
            await connection.set_type_codec(
                "jsonb",
                schema="pg_catalog",
                encoder=json.dumps,
                decoder=json.loads,
            )

        self.http_pool = await asyncpg.create_pool(
            user="governed_memory_api",
            password="successor_api_disposable_only",
            database="governed_memory",
            host=self.host,
            port=self.port,
            min_size=1,
            max_size=1,
            command_timeout=30,
            init=configure_pool_connection,
        )
        self.addAsyncCleanup(self.http_pool.close)

        app = create_owner_memory_app(
            actor_resolver=actor_resolver,
            facade=PostgresOwnerStore(self.http_pool),
            feature_enabled=True,
        )
        configuration = uvicorn.Config(
            app,
            host=API_HOST,
            port=API_PORT,
            access_log=False,
            log_config=None,
            lifespan="off",
            server_header=False,
        )
        self.http_server = uvicorn.Server(configuration)
        self.http_server.install_signal_handlers = lambda: None
        self.http_server_task = asyncio.create_task(self.http_server.serve())
        self.addAsyncCleanup(self._stop_http_server)
        for _attempt in range(200):
            if self.http_server.started:
                break
            if self.http_server_task.done():
                await self.http_server_task
            await asyncio.sleep(0.025)
        if not self.http_server.started:
            self.fail("successor HTTP server did not start")

        self.owner_a_token = self.jwks.token(OWNER_A)
        self.owner_b_token = self.jwks.token(OWNER_B)

    async def asyncTearDown(self) -> None:
        for connection in reversed(self.connections):
            await connection.close()

    def _close_relays(self) -> None:
        for relay in reversed(self.relays):
            relay.close()

    async def _stop_http_server(self) -> None:
        self.http_server.should_exit = True
        await asyncio.wait_for(self.http_server_task, timeout=10)

    async def _connect(self, user: str, password: str, database: str) -> Any:
        assert asyncpg is not None
        connection = await asyncpg.connect(
            user=user,
            password=password,
            database=database,
            host=self.host,
            port=self.port,
            command_timeout=30,
        )
        await connection.set_type_codec(
            "jsonb",
            schema="pg_catalog",
            encoder=json.dumps,
            decoder=json.loads,
        )
        self.connections.append(connection)
        return connection

    async def http_request(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        body: Mapping[str, Any] | None = None,
        raw_body: bytes | None = None,
        extra_headers: Mapping[str, str] | None = None,
        include_service_token: bool = True,
    ) -> tuple[int, dict[str, Any] | list[Any]]:
        if not path.startswith("/memory/"):
            self.fail("HTTP test path is outside the closed owner surface")
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if include_service_token:
            headers["X-Governed-Memory-Service-Token"] = self.service_token
        if extra_headers:
            headers.update(dict(extra_headers))
        if body is not None and raw_body is not None:
            self.fail("HTTP request has two body representations")
        encoded = raw_body
        if body is not None:
            encoded = json.dumps(
                dict(body),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        if encoded is not None:
            headers["Content-Type"] = "application/json"

        def send() -> tuple[int, dict[str, Any] | list[Any]]:
            request = Request(
                API_BASE_URL + path,
                data=encoded,
                method=method,
                headers=headers,
            )
            try:
                with urlopen(request, timeout=10) as response:
                    status = response.status
                    cache_control = response.headers.get("Cache-Control")
                    raw = response.read(1_048_577)
            except HTTPError as error:
                status = error.code
                cache_control = error.headers.get("Cache-Control")
                raw = error.read(1_048_577)
            if cache_control != "no-store":
                raise AssertionError("HTTP response is not no-store")
            if len(raw) > 1_048_576:
                raise AssertionError("HTTP response exceeded the test bound")
            value = json.loads(raw, object_pairs_hook=_closed_json_object)
            if not isinstance(value, (dict, list)):
                raise AssertionError("HTTP response is not a JSON object or list")
            return status, value

        return await asyncio.to_thread(send)

    async def assert_http_failure(
        self,
        expected_status: int,
        expected_code: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> None:
        status, value = await self.http_request(method, path, **kwargs)
        self.assertEqual(status, expected_status)
        self.assertEqual(value, {"error": {"code": expected_code}})

    @asynccontextmanager
    async def owner_context(
        self, connection: Any, owner_user_id: UUID
    ) -> AsyncIterator[None]:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_catalog.set_config('app.user_id', $1::text, true)",
                str(owner_user_id),
            )
            await connection.execute(
                "SELECT pg_catalog.set_config("
                "'app.auth_context_sha256', $1::text, true)",
                AUTH_CONTEXT_SHA256,
            )
            yield

    @asynccontextmanager
    async def ingest_context(self, owner_user_id: UUID) -> AsyncIterator[None]:
        async with self.ingest.transaction():
            await self.ingest.execute(
                "SELECT pg_catalog.set_config('app.user_id', $1::text, true)",
                str(owner_user_id),
            )
            await self.ingest.execute(
                "SELECT pg_catalog.set_config("
                "'app.auth_context_sha256', $1::text, true)",
                AUTH_CONTEXT_SHA256,
            )
            yield

    async def read_claims(self, owner: UUID, claim_ids: list[UUID]) -> list[dict[str, Any]]:
        async with self.owner_context(self.api, owner):
            rows = await self.api.fetch(
                "SELECT * FROM memory_private.read_claim_candidates($1::uuid[])",
                claim_ids,
            )
        return [normalize_postgres_record(dict(row)) for row in rows]

    async def list_claims(self, owner: UUID) -> list[dict[str, Any]]:
        async with self.owner_context(self.api, owner):
            rows = await self.api.fetch(
                "SELECT * FROM memory_private.list_claims("
                "NULL::uuid, 100, NULL::timestamptz, NULL::uuid)"
            )
        return [normalize_postgres_record(dict(row)) for row in rows]

    @staticmethod
    def projection_outbox(row: Mapping[str, Any]) -> dict[str, Any]:
        result = {
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
        if tuple(result) != PROJECTION_OUTBOX_FIELDS:
            raise AssertionError("projection lease does not map to the closed outbox")
        return result

    @staticmethod
    def qdrant_candidate(item: Mapping[str, Any]) -> dict[str, Any]:
        candidate = dict(item["payload"])
        candidate["score"] = item["score"]
        if tuple(sorted(candidate)) != CANDIDATE_FIELDS:
            raise AssertionError("Qdrant returned a non-candidate payload field")
        return candidate

    async def finish_upsert(
        self, lease: Mapping[str, Any], point: Mapping[str, Any], physical: str
    ) -> None:
        result = await self.worker.fetchrow(
            "SELECT * FROM memory_private.finish_projection_job("
            "$1::uuid,$2::uuid,'applied'::text,$3::text,$4::text,"
            "NULL::text,NULL::text,NULL::text)",
            lease["outbox_id"],
            lease["lease_token"],
            physical,
            point["payload"]["vector_sha256"],
        )
        self.assertEqual(dict(result), {"outcome": "applied", "deletion_ready": False})
        replay = await self.worker.fetchrow(
            "SELECT * FROM memory_private.finish_projection_job("
            "$1::uuid,$2::uuid,'applied'::text,$3::text,$4::text,"
            "NULL::text,NULL::text,NULL::text)",
            lease["outbox_id"],
            lease["lease_token"],
            physical,
            point["payload"]["vector_sha256"],
        )
        self.assertEqual(replay["outcome"], "replayed")

    async def finish_delete(
        self,
        lease: Mapping[str, Any],
        *,
        physical: str,
        finalize: bool,
        deletion_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = build_projection_delete(
            self.projection_outbox(lease),
            collection_alias=ALIAS,
            physical_collection=physical,
        )
        self.assertEqual(self.qdrant.aliases().get(ALIAS), physical)
        self.qdrant.delete_point(physical, command["point_id"])
        alias_absent = not self.qdrant.retrieve(ALIAS, command["point_id"])
        physical_absent = not self.qdrant.retrieve(physical, command["point_id"])
        absence_hash = qdrant_absence_verification_sha256(
            collection_alias=ALIAS,
            physical_collection=physical,
            point_id=UUID(str(command["point_id"])),
            projection_sequence=int(command["projection_sequence"]),
            alias_target_verified=True,
            alias_absent=alias_absent,
            physical_absent=physical_absent,
        )
        async with self.worker.transaction():
            receipt_time = await self.worker.fetchval(
                "SELECT pg_catalog.transaction_timestamp()"
            )
            receipt = build_projection_delete_receipt(
                command,
                applied_at=receipt_time,
                absence_verified_at=receipt_time,
                absence_verification_sha256=absence_hash,
            )
            result = await self.worker.fetchrow(
                "SELECT * FROM memory_private.finish_projection_job("
                "$1::uuid,$2::uuid,'applied'::text,$3::text,NULL::text,"
                "$4::text,$5::text,NULL::text)",
                lease["outbox_id"],
                lease["lease_token"],
                physical,
                receipt["absence_verification_sha256"],
                receipt["receipt_sha256"],
            )
            self.assertTrue(result["deletion_ready"])
            if finalize:
                if deletion_state is None:
                    raise AssertionError("deletion finalization state is required")
                finalized = await self.worker.fetchrow(
                    "SELECT * FROM memory_private.finalize_claim_deletion("
                    "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::uuid,$6::uuid,"
                    "$7::text,$8::integer,$9::text,$10::text,$11::timestamptz,"
                    "$12::timestamptz,$13::text,$14::text)",
                    DELETION_OPERATION_ID,
                    OWNER_A,
                    lease["claim_id"],
                    deletion_state["current_state_sha256"],
                    lease["outbox_id"],
                    lease["revision_id"],
                    lease["revision_sha256"],
                    lease["sequence_number"],
                    lease["projection_manifest_sha256"],
                    physical,
                    receipt_time,
                    receipt_time,
                    receipt["absence_verification_sha256"],
                    receipt["receipt_sha256"],
                )
                self.assertEqual(finalized["outcome"], "deleted")
        return receipt

    async def test_http_chat_a_to_chat_b_rebuild_and_deletion(self) -> None:
        marker = await self.admin.fetchval(
            "SELECT pg_catalog.shobj_description(oid, 'pg_database') "
            "FROM pg_catalog.pg_database WHERE datname = current_database()"
        )
        bridge_marker = await self.bridge_admin.fetchval(
            "SELECT pg_catalog.shobj_description(oid, 'pg_database') "
            "FROM pg_catalog.pg_database WHERE datname = current_database()"
        )
        self.assertEqual(marker, RUN_MARKER)
        self.assertEqual(bridge_marker, RUN_MARKER)
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT pg_catalog.count(*) FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
                "WHERE n.nspname='memory' AND c.relkind='r'"
            ),
            14,
        )
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT pg_catalog.count(*) FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
                "WHERE n.nspname='memory' AND c.relkind='r' "
                "AND c.relrowsecurity AND c.relforcerowsecurity"
            ),
            13,
        )
        lease_result = await self.admin.fetchval(
            "SELECT pg_catalog.pg_get_function_result(p.oid) "
            "FROM pg_catalog.pg_proc AS p JOIN pg_catalog.pg_namespace AS n "
            "ON n.oid=p.pronamespace WHERE n.nspname='memory_private' "
            "AND p.proname='lease_extraction_jobs'"
        )
        self.assertIn("provider_operation_id uuid", lease_result)
        read_result = await self.admin.fetchval(
            "SELECT pg_catalog.pg_get_function_result(p.oid) "
            "FROM pg_catalog.pg_proc AS p JOIN pg_catalog.pg_namespace AS n "
            "ON n.oid=p.pronamespace WHERE n.nspname='memory_private' "
            "AND p.proname='read_claim_candidates'"
        )
        self.assertIn("projection_sequence integer", read_result)
        self.assertIn("state_sha256 text", read_result)
        self.assertNotIn("revision_fact_policy_sha256", read_result)
        policies = await self.admin.fetch(
            "SELECT tablename,policyname,roles,qual,with_check "
            "FROM pg_catalog.pg_policies WHERE schemaname='memory' "
            "ORDER BY tablename,policyname"
        )
        self.assertEqual(
            {(row["tablename"], row["policyname"]) for row in policies},
            {
                (table, policy)
                for table in OWNER_TABLES
                for policy in ("owner_internal", "owner_isolation")
            }
            | {("pilot_marker", "pilot_marker_owner_only")},
        )
        for row in policies:
            if row["policyname"] == "owner_isolation":
                self.assertEqual(row["roles"], ["governed_memory_api"])
                self.assertIn("current_owner_id", row["qual"])
                self.assertEqual(row["qual"], row["with_check"])
            else:
                self.assertEqual(row["roles"], ["governed_memory_owner"])
                self.assertEqual(row["qual"], "true")
                self.assertEqual(row["with_check"], "true")
        forbidden_dml = (
            "SELECT pg_catalog.count(*) FROM memory.claim",
            "INSERT INTO memory.claim(claim_id) "
            "VALUES ('12345678-1234-4234-8234-123456789012'::uuid)",
            "UPDATE memory.claim SET updated_at=pg_catalog.clock_timestamp()",
            "DELETE FROM memory.claim",
        )
        for connection in (self.api, self.worker):
            for statement in forbidden_dml:
                with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                    await connection.execute(statement)

        status_code, status_body = await self.http_request(
            "GET", "/memory/status", token=self.owner_a_token
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(status_body["active_claims"], 0)
        self.assertEqual(status_body["pending_proposals"], 0)

        await self.assert_http_failure(
            401,
            "memory_authentication_required",
            "GET",
            "/memory/status",
            token=None,
        )
        await self.assert_http_failure(
            401,
            "memory_authentication_required",
            "GET",
            "/memory/status",
            token=self.owner_a_token,
            include_service_token=False,
        )
        await self.assert_http_failure(
            401,
            "memory_authentication_required",
            "GET",
            "/memory/status",
            token=self.owner_a_token,
            extra_headers={"X-Governed-Memory-Service-Token": "wrong-disposable-token"},
        )
        await self.assert_http_failure(
            400,
            "memory_request_invalid",
            "GET",
            "/memory/status?owner_user_id=" + str(OWNER_B),
            token=self.owner_a_token,
        )
        await self.assert_http_failure(
            403,
            "memory_authorization_denied",
            "GET",
            "/memory/status",
            token=self.owner_a_token,
            extra_headers={"X-VS-Actor-User-ID": str(OWNER_B)},
        )

        now = int(time.time())
        authentication_invalid_tokens = (
            ("malformed_bearer", "not-a-jwt"),
            (
                "expired",
                self.jwks.token(
                    OWNER_A,
                    claims={"iat": now - 300, "exp": now - 1},
                ),
            ),
            ("wrong_audience", self.jwks.token(OWNER_A, claims={"aud": "other"})),
            (
                "wrong_issuer",
                self.jwks.token(
                    OWNER_A, claims={"iss": "https://wrong.invalid/auth/v1"}
                ),
            ),
            ("unknown_key", self.jwks.token(OWNER_A, key_id="unpublished-key")),
            (
                "bad_signature",
                self.jwks.token(
                    OWNER_A,
                    private_key=ec.generate_private_key(ec.SECP256R1()),
                ),
            ),
            (
                "prohibited_algorithm",
                self.jwks.token(
                    OWNER_A,
                    algorithm="HS256",
                    private_key=b"successor-test-only-hmac-key-000",
                ),
            ),
            ("invalid_subject_uuid", self.jwks.token(OWNER_A, claims={"sub": "invalid"})),
            ("invalid_session_uuid", self.jwks.token(OWNER_A, claims={"session_id": "invalid"})),
            ("missing_required_claim", self.jwks.token(OWNER_A, omit_claims=("session_id",))),
        )
        for _case_name, invalid_token in authentication_invalid_tokens:
            await self.assert_http_failure(
                401,
                "memory_authentication_required",
                "GET",
                "/memory/status",
                token=invalid_token,
            )
        authorization_denied_tokens = (
            self.jwks.token(OWNER_A, claims={"is_anonymous": True}),
            self.jwks.token(OWNER_A, claims={"role": "service_role"}),
        )
        for denied_token in authorization_denied_tokens:
            await self.assert_http_failure(
                403,
                "memory_authorization_denied",
                "GET",
                "/memory/status",
                token=denied_token,
            )

        forged_metadata_token = self.jwks.token(
            OWNER_A,
            claims={"user_metadata": {"owner_user_id": str(OWNER_B)}},
        )
        forged_status, forged_body = await self.http_request(
            "GET", "/memory/status", token=forged_metadata_token
        )
        self.assertEqual(forged_status, 200)
        self.assertEqual(forged_body, status_body)

        await self.assert_http_failure(
            400,
            "memory_request_invalid",
            "POST",
            "/memory/claims/11111111-1111-4111-8111-111111111111/correct",
            token=self.owner_a_token,
            body={"owner_user_id": str(OWNER_B)},
        )
        await self.assert_http_failure(
            400,
            "memory_request_invalid",
            "POST",
            "/memory/claims/11111111-1111-4111-8111-111111111111/correct",
            token=self.owner_a_token,
            raw_body=b'{"operation_id":"a","operation_id":"b"}',
        )

        async with self.ingest_context(OWNER_A):
            transaction_time = await self.ingest.fetchval(
                "SELECT pg_catalog.transaction_timestamp()"
            )
            policy = EligibilityPolicy(ingest_after=transaction_time)
            payload = make_ingest_payload(
                created_at=transaction_time,
                window_ordinal=0,
            )
            payload["exchange_id"] = str(MESSAGE_A)
            payload["window_id"] = str(MESSAGE_A)
            payload["window_sha256"] = canonical_sha256(
                "governed_memory.ingest_window",
                {
                    "owner_user_id": OWNER_A,
                    "thread_id": THREAD_A,
                    "exchange_id": MESSAGE_A,
                    "window_id": MESSAGE_A,
                    "message_id": MESSAGE_A,
                    "content_sha256": payload["content_sha256"],
                },
            )
            await self.ingest.execute(
                "INSERT INTO public.threads("
                "id,owner_user_id,user_id,title) "
                "VALUES($1::uuid,$2::uuid,$2::text,'Synthetic thread')",
                THREAD_A,
                OWNER_A,
            )
            inserted = await self.ingest.fetchrow(
                "INSERT INTO public.chat_log("
                "id,owner_user_id,user_id,source,text,thread_id,created_at) "
                "VALUES($1::uuid,$2::uuid,$2::text,'frontend/chat:user',"
                "$3::text,$4::uuid,pg_catalog.transaction_timestamp()) "
                "RETURNING id,created_at",
                MESSAGE_A,
                OWNER_A,
                payload["content"],
                THREAD_A,
            )
            self.assertEqual(inserted["created_at"], transaction_time)
            source_created_at = inserted["created_at"]
            enqueued = await self.ingest.fetchrow(
                "SELECT * FROM "
                "memory_ingest_private.enqueue_chat_log_message("
                "$1::uuid,$2::text)",
                MESSAGE_A,
                policy.policy_sha256,
            )
        self.assertEqual(enqueued["outcome"], "enqueued")

        async with self.ingest_context(OWNER_A):
            await self.ingest.execute(
                "INSERT INTO public.chat_log("
                "id,owner_user_id,user_id,source,text,thread_id,created_at) "
                "VALUES($1::uuid,$2::uuid,$2::text,'trusted-web',"
                "'Synthetic alternate source.', $3::uuid,"
                "pg_catalog.transaction_timestamp())",
                ALTERNATE_SOURCE_MESSAGE,
                OWNER_A,
                THREAD_A,
            )
            with self.assertRaises(asyncpg.PostgresError) as alternate_source:
                await self.ingest.fetchrow(
                    "SELECT * FROM "
                    "memory_ingest_private.enqueue_chat_log_message("
                    "$1::uuid,$2::text)",
                    ALTERNATE_SOURCE_MESSAGE,
                    policy.policy_sha256,
                )
        self.assertEqual(alternate_source.exception.sqlstate, "22023")

        bridge_lease = await self.bridge_worker.fetchrow(
            "SELECT * FROM memory_ingest_private.lease_memory_ingest("
            "'successor_bridge_worker',1,120)"
        )
        for runtime_role in ("memory_ingest_writer", "governed_memory_worker"):
            for relation in (
                "public.chat_log",
                "public.threads",
                "public.chat_attachments",
                "memory_ingest_private.memory_ingest_outbox",
            ):
                self.assertFalse(
                    await self.bridge_admin.fetchval(
                        "SELECT pg_catalog.has_table_privilege("
                        "$1::text,$2::text,'SELECT')",
                        runtime_role,
                        relation,
                    )
                )
        with self.assertRaises(asyncpg.PostgresError) as direct_source_read:
            await self.bridge_worker.fetchval(
                "SELECT pg_catalog.count(*) FROM public.chat_log"
            )
        self.assertEqual(direct_source_read.exception.sqlstate, "42501")
        with self.assertRaises(asyncpg.PostgresError) as wrong_lease_token:
            await self.bridge_worker.fetchrow(
                "SELECT * FROM "
                "memory_ingest_private.read_leased_chat_log_message("
                "$1::uuid,pg_catalog.gen_random_uuid())",
                bridge_lease["outbox_id"],
            )
        self.assertEqual(wrong_lease_token.exception.sqlstate, "40001")
        leased_chat_log = await self.bridge_worker.fetchrow(
            "SELECT * FROM "
            "memory_ingest_private.read_leased_chat_log_message("
            "$1::uuid,$2::uuid)",
            bridge_lease["outbox_id"],
            bridge_lease["lease_token"],
        )
        lease_envelope, payload = split_leased_chat_log_message(
            dict(leased_chat_log)
        )
        self.assertNotIn("attachments", payload)
        self.assertNotIn("attachment_ids", payload)
        transaction_time = await self.bridge_worker.fetchval(
            "SELECT pg_catalog.clock_timestamp()"
        )
        actor = VerifiedActor(
            owner_user_id=OWNER_A,
            actor_id=UUID("33333333-3333-4333-8333-333333333333"),
            session_id=UUID("34444444-4444-4444-8444-444444444444"),
            role=ActorRole.WORKER,
            scopes=(ActorScope.PROCESS_MEMORY_INGEST,),
            authentication_manifest_sha256=AUTH_CONTEXT_SHA256,
            authenticated_at=transaction_time,
        )
        plan = process_ingest_item(
            payload,
            lease_envelope=lease_envelope,
            actor=actor,
            expected_owner_user_id=OWNER_A,
            transaction_time=transaction_time,
        )
        self.assertEqual(plan["decision"], "send_external")
        selected = dict(plan["selected_evidence"])

        evidence_sql = (
            "SELECT * FROM memory_private.record_selected_evidence("
            "$1::uuid,$2::uuid,$3::text,$4::uuid,$5::uuid,$6::uuid,"
            "$7::text,$8::text,$9::text,$10::integer,$11::integer,"
            "$12::uuid,$13::text,$14::timestamptz,$15::text,$16::text,"
            "$17::text)"
        )
        evidence_args = (
            OWNER_A,
            MESSAGE_A,
            bridge_lease["source_binding_sha256"],
            MESSAGE_A,
            THREAD_A,
            MESSAGE_A,
            payload["window_sha256"],
            payload["content_sha256"],
            selected["selected_sha256"],
            selected["start_utf8"],
            selected["end_utf8"],
            None,
            None,
            source_created_at,
            "send_external",
            policy.policy_sha256,
            selected["selected_text"],
        )
        concurrent = await asyncio.gather(
            self.worker.fetchrow(evidence_sql, *evidence_args),
            self.worker_two.fetchrow(evidence_sql, *evidence_args),
        )
        self.assertEqual(
            {row["outcome"] for row in concurrent}, {"accepted", "replayed"}
        )
        evidence_result = next(row for row in concurrent if row["outcome"] == "accepted")
        self.assertEqual(concurrent[0]["evidence_id"], concurrent[1]["evidence_id"])
        acknowledged = await self.bridge_worker.fetchval(
            "SELECT memory_ingest_private.ack_memory_ingest("
            "$1::uuid,$2::uuid,'send_external'::text,$3::uuid,$4::uuid)",
            bridge_lease["outbox_id"],
            bridge_lease["lease_token"],
            evidence_result["evidence_id"],
            evidence_result["extraction_job_id"],
        )
        self.assertEqual(acknowledged, "completed")
        async with self.ingest_context(OWNER_A):
            with self.assertRaises(asyncpg.PostgresError) as historical_enqueue:
                await self.ingest.fetchrow(
                    "SELECT * FROM "
                    "memory_ingest_private.enqueue_chat_log_message("
                    "$1::uuid,$2::text)",
                    MESSAGE_A,
                    policy.policy_sha256,
                )
        self.assertEqual(historical_enqueue.exception.sqlstate, "22023")
        async with self.ingest_context(OWNER_B):
            with self.assertRaises(asyncpg.PostgresError) as cross_owner_enqueue:
                await self.ingest.fetchrow(
                    "SELECT * FROM "
                    "memory_ingest_private.enqueue_chat_log_message("
                    "$1::uuid,$2::text)",
                    MESSAGE_A,
                    policy.policy_sha256,
                )
        self.assertEqual(cross_owner_enqueue.exception.sqlstate, "22023")
        await self.bridge_admin.execute(
            "UPDATE memory_ingest_private.memory_ingest_outbox "
            "SET created_at=pg_catalog.transaction_timestamp()-interval '31 days', "
            "content_hash_expires_at="
            "pg_catalog.transaction_timestamp()-interval '30 days 1 hour', "
            "purge_after=pg_catalog.transaction_timestamp()-interval '1 day' "
            "WHERE outbox_id=$1::uuid",
            bridge_lease["outbox_id"],
        )
        self.assertEqual(
            await self.bridge_worker.fetchval(
                "SELECT memory_ingest_private.purge_terminal_memory_ingest(10)"
            ),
            1,
        )
        self.assertEqual(
            await self.bridge_admin.fetchval(
                "SELECT pg_catalog.count(*) FROM "
                "memory_ingest_private.memory_ingest_outbox"
            ),
            0,
        )
        del plan
        del selected

        catalog_sha256 = CANONICAL_PREDICATE_CATALOG_SHA256
        first_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_extraction_jobs("
            "'successor_extractor',1,120,$1::text,$2::text,$3::text,$4::text,"
            "'responses.create'::text,$5::text,4096,30000)",
            "synthetic",
            MODEL,
            SCHEMA_SHA256,
            catalog_sha256,
            PRIVACY_MANIFEST_SHA256,
        )
        failed_before_send = await self.worker.fetchrow(
            "SELECT * FROM memory_private.complete_extraction("
            "$1::uuid,$2::uuid,$3::uuid,'retryable_failure'::text,"
            "NULL::text,NULL::text,NULL::integer,NULL::integer,"
            "'connection_failed_before_send'::text,'[]'::jsonb)",
            first_lease["job_id"],
            first_lease["lease_token"],
            first_lease["provider_call_id"],
        )
        self.assertEqual(failed_before_send["outcome"], "retryable_failure")
        replay_before_send = await self.worker.fetchrow(
            "SELECT * FROM memory_private.complete_extraction("
            "$1::uuid,$2::uuid,$3::uuid,'retryable_failure'::text,"
            "NULL::text,NULL::text,NULL::integer,NULL::integer,"
            "'connection_failed_before_send'::text,'[]'::jsonb)",
            first_lease["job_id"],
            first_lease["lease_token"],
            first_lease["provider_call_id"],
        )
        self.assertEqual(replay_before_send["outcome"], "replayed")
        await self.admin.execute(
            "UPDATE memory.extraction_job "
            "SET available_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE job_id=$1::uuid",
            first_lease["job_id"],
        )
        extraction_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_extraction_jobs("
            "'successor_extractor',1,120,$1::text,$2::text,$3::text,$4::text,"
            "'responses.create'::text,$5::text,4096,30000)",
            "synthetic",
            MODEL,
            SCHEMA_SHA256,
            catalog_sha256,
            PRIVACY_MANIFEST_SHA256,
        )
        self.assertEqual(extraction_lease["attempt_number"], 2)
        lease_inputs = extraction_lease_to_provider_inputs(dict(extraction_lease))
        source_lookup = lease_inputs["source_lookup"]
        self.assertEqual(
            source_lookup,
            {
                "owner_user_id": str(OWNER_A),
                "message_id": str(MESSAGE_A),
                "thread_id": str(THREAD_A),
                "content_sha256": payload["content_sha256"],
            },
        )
        self.assertIsNone(lease_inputs["context_lookup"])
        bounded_context = None
        cold_evidence = lease_inputs["evidence"]
        self.assertEqual(
            sha256(cold_evidence["selected_text"].encode("utf-8")).hexdigest(),
            cold_evidence["selected_sha256"],
        )
        request = build_provider_request(
            lease_inputs["job"],
            cold_evidence,
            MODEL,
            SCHEMA,
            PREDICATE_CATALOG,
            bounded_context=bounded_context,
        )
        dispatched = await self.worker.fetchrow(
            "SELECT * FROM memory_private.mark_provider_call_dispatched("
            "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text,$7::text)",
            extraction_lease["job_id"],
            extraction_lease["lease_token"],
            extraction_lease["provider_call_id"],
            request["request_sha256"],
            extraction_lease["selected_sha256"],
            extraction_lease["selection_binding_sha256"],
            extraction_lease["predicate_catalog_sha256"],
        )
        self.assertEqual(dispatched["outcome"], "dispatched")
        with self.assertRaises(asyncpg.PostgresError) as duplicate_dispatch:
            await self.worker.fetchrow(
                "SELECT * FROM memory_private.mark_provider_call_dispatched("
                "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text,$7::text)",
                extraction_lease["job_id"],
                extraction_lease["lease_token"],
                extraction_lease["provider_call_id"],
                request["request_sha256"],
                extraction_lease["selected_sha256"],
                extraction_lease["selection_binding_sha256"],
                extraction_lease["predicate_catalog_sha256"],
            )
        self.assertEqual(duplicate_dispatch.exception.sqlstate, "55000")
        batch = validate_provider_result(request, make_provider_output())
        completed = await self.worker.fetchrow(
            "SELECT * FROM memory_private.complete_extraction("
            "$1::uuid,$2::uuid,$3::uuid,'completed'::text,$4::text,$5::text,"
            "128,16,NULL::text,$6::jsonb)",
            extraction_lease["job_id"],
            extraction_lease["lease_token"],
            extraction_lease["provider_call_id"],
            request["request_sha256"],
            batch["response_sha256"],
            batch["proposals"],
        )
        self.assertEqual(dict(completed), {"outcome": "completed", "proposal_count": 1})
        completed_replay = await self.worker.fetchrow(
            "SELECT * FROM memory_private.complete_extraction("
            "$1::uuid,$2::uuid,$3::uuid,'completed'::text,$4::text,$5::text,"
            "128,16,NULL::text,$6::jsonb)",
            extraction_lease["job_id"],
            extraction_lease["lease_token"],
            extraction_lease["provider_call_id"],
            request["request_sha256"],
            batch["response_sha256"],
            batch["proposals"],
        )
        self.assertEqual(completed_replay["outcome"], "replayed")
        del batch
        owner_b_proposals_status, owner_b_proposals = await self.http_request(
            "GET", "/memory/proposals", token=self.owner_b_token
        )
        self.assertEqual(owner_b_proposals_status, 200)
        self.assertEqual(owner_b_proposals, [])
        proposals_status, review_rows = await self.http_request(
            "GET", "/memory/proposals", token=self.owner_a_token
        )
        self.assertEqual(proposals_status, 200)
        self.assertEqual(len(review_rows), 1)
        review_surface = dict(review_rows[0])
        self.assertEqual(review_surface["source_excerpt"], SOURCE_TEXT)
        self.assertEqual(review_surface["subject_entity_type"], "self")
        self.assertEqual(review_surface["predicate"], "preference.personal")
        self.assertEqual(review_surface["object_kind"], "literal")
        self.assertEqual(review_surface["object_literal"], "cobalt")

        review_body = {
            "decision": "admit",
            "expected_predicate_catalog_sha256": review_surface[
                "predicate_catalog_sha256"
            ],
            "expected_proposal_sha256": review_surface["proposal_sha256"],
            "expected_selected_sha256": review_surface["selected_sha256"],
            "expected_selection_binding_sha256": review_surface[
                "selection_binding_sha256"
            ],
            "expected_source_sha256": review_surface["source_sha256"],
            "operation_id": review_surface["operation_id"],
            "reason_codes": ["explicit_owner_review"],
        }
        proposal_path = f"/memory/proposals/{review_surface['proposal_id']}/review"
        await self.assert_http_failure(
            404,
            "memory_resource_not_found",
            "POST",
            proposal_path,
            token=self.owner_b_token,
            body=review_body,
        )
        review_status, review = await self.http_request(
            "POST",
            proposal_path,
            token=self.owner_a_token,
            body=review_body,
        )
        self.assertEqual(review_status, 200)
        self.assertEqual(review["outcome"], "admitted")
        claim_id = UUID(review["claim_id"])
        claim_status, claim_surface = await self.http_request(
            "GET", f"/memory/claims/{claim_id}", token=self.owner_a_token
        )
        self.assertEqual(claim_status, 200)
        self.assertEqual(claim_surface["claim_id"], str(claim_id))
        owner_a_claims_status, owner_a_claims = await self.http_request(
            "GET", "/memory/claims", token=self.owner_a_token
        )
        self.assertEqual(owner_a_claims_status, 200)
        self.assertEqual(
            [row["claim_id"] for row in owner_a_claims], [str(claim_id)]
        )
        owner_b_claims_status, owner_b_claims = await self.http_request(
            "GET", "/memory/claims", token=self.owner_b_token
        )
        self.assertEqual(owner_b_claims_status, 200)
        self.assertEqual(owner_b_claims, [])
        await self.assert_http_failure(
            404,
            "memory_resource_not_found",
            "GET",
            f"/memory/claims/{claim_id}",
            token=self.owner_b_token,
        )
        claims = await self.read_claims(OWNER_A, [claim_id])
        self.assertEqual(len(claims), 1)
        self.assertEqual(await self.read_claims(OWNER_B, [claim_id]), [])
        owner_b_correction_body = {
            "expected_predicate_catalog_sha256": claim_surface[
                "predicate_catalog_sha256"
            ],
            "expected_revision_sha256": claim_surface["revision_sha256"],
            "expected_state_sha256": claim_surface["current_state_sha256"],
            "operation_id": str(OWNER_B_CORRECTION_OPERATION_ID),
            "replacement": {
                "epistemic_state": "supported",
                "object_display_name": None,
                "object_entity_type": None,
                "object_kind": "literal",
                "object_literal": "owner-b-must-not-change-owner-a",
                "sensitivity": "ordinary",
            },
        }
        await self.assert_http_failure(
            404,
            "memory_resource_not_found",
            "POST",
            f"/memory/claims/{claim_id}/correct",
            token=self.owner_b_token,
            body=owner_b_correction_body,
        )
        owner_b_retraction_body = {
            "expected_revision_sha256": claim_surface["revision_sha256"],
            "expected_state_sha256": claim_surface["current_state_sha256"],
            "operation_id": str(OWNER_B_RETRACTION_OPERATION_ID),
        }
        await self.assert_http_failure(
            404,
            "memory_resource_not_found",
            "POST",
            f"/memory/claims/{claim_id}/retract",
            token=self.owner_b_token,
            body=owner_b_retraction_body,
        )
        owner_b_deletion_body = {
            "expected_revision_sha256": claim_surface["revision_sha256"],
            "expected_state_sha256": claim_surface["current_state_sha256"],
            "operation_id": str(OWNER_B_DELETION_OPERATION_ID),
        }
        await self.assert_http_failure(
            404,
            "memory_resource_not_found",
            "DELETE",
            f"/memory/claims/{claim_id}",
            token=self.owner_b_token,
            body=owner_b_deletion_body,
        )
        status_code, status_body = await self.http_request(
            "GET", "/memory/status", token=self.owner_a_token
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(status_body["active_claims"], 1)
        owner_b_status_code, owner_b_status = await self.http_request(
            "GET", "/memory/status", token=self.owner_b_token
        )
        self.assertEqual(owner_b_status_code, 200)
        self.assertEqual(owner_b_status["active_claims"], 0)
        operation_status, operation_surface = await self.http_request(
            "GET",
            f"/memory/operations/{review_surface['operation_id']}",
            token=self.owner_a_token,
        )
        self.assertEqual(operation_status, 200)
        self.assertEqual(
            operation_surface["operation_id"], review_surface["operation_id"]
        )
        await self.assert_http_failure(
            404,
            "memory_resource_not_found",
            "GET",
            f"/memory/operations/{review_surface['operation_id']}",
            token=self.owner_b_token,
        )

        await self.admin.execute("GRANT SELECT ON memory.claim TO governed_memory_api")
        try:
            async with self.owner_context(self.api, OWNER_A):
                self.assertEqual(
                    await self.api.fetchval("SELECT count(*) FROM memory.claim"), 1
                )
            async with self.owner_context(self.api, OWNER_B):
                self.assertEqual(
                    await self.api.fetchval("SELECT count(*) FROM memory.claim"), 0
                )
        finally:
            await self.admin.execute("REVOKE SELECT ON memory.claim FROM governed_memory_api")

        self.qdrant.create_collection(PHYSICAL_A)
        self.qdrant.create_alias(ALIAS, PHYSICAL_A)
        exact_qdrant = ExactQdrantAdapter(AsyncQdrantRestTransport(self.qdrant))
        preflight = await exact_qdrant.preflight()
        self.assertEqual(preflight.physical_collection, PHYSICAL_A)
        self.assertEqual(preflight.vector_size, 3072)
        self.assertEqual(preflight.distance, "Dot")
        self.assertEqual(
            dict(preflight.payload_indexes),
            dict(QDRANT_REQUIRED_PAYLOAD_INDEXES),
        )
        projection_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'successor_projector',1,120)"
        )
        point = build_projection_point(
            claims[0], self.projection_outbox(projection_lease), deterministic_vector()
        )
        upsert_receipt = await exact_qdrant.upsert_projection_point(point)
        self.assertEqual(upsert_receipt.physical_collection, PHYSICAL_A)
        self.assertFalse(upsert_receipt.resolved_by_readback)
        stored = self.qdrant.retrieve(PHYSICAL_A, claim_id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(tuple(sorted(stored[0]["payload"])), QDRANT_PAYLOAD_FIELDS)
        self.assertEqual(len(stored[0]["vector"]), 3072)
        self.assertEqual(
            vector_sha256(stored[0]["vector"]), point["payload"]["vector_sha256"]
        )
        await self.finish_upsert(projection_lease, point, PHYSICAL_A)

        owner_b_claim = make_claim_row(
            owner_user_id=OWNER_B,
            claim_id=OWNER_B_CLAIM_ID,
            revision_id=OWNER_B_REVISION_ID,
        )
        owner_b_point = build_projection_point(
            owner_b_claim,
            make_projection_outbox(owner_b_claim, state="claimed"),
            deterministic_vector(),
        )
        self.qdrant.upsert(PHYSICAL_A, owner_b_point)
        calibration = CalibrationDecision(
            artifact_sha256="a" * 64,
            reason_code="disposable_compatibility_proof_only",
            retrieval_enabled=True,
            threshold_micros=0,
        )
        adapter_results = list(
            await exact_qdrant.search_owner_candidates(
                owner_user_id=OWNER_A,
                query_vector=point["vector"],
                allowed_predicates=("preference.personal",),
                limit=8,
                calibration=calibration,
            )
        )
        self.assertEqual(
            tuple(sorted(adapter_results[0])),
            CANDIDATE_FIELDS,
        )
        self.assertEqual(
            [item["claim_id"] for item in adapter_results],
            [str(claim_id)],
        )
        owner_a_results = self.qdrant.search(ALIAS, OWNER_A, list(point["vector"]))
        self.assertEqual([item["id"] for item in owner_a_results], [str(claim_id)])
        owner_b_result = self.qdrant.search(ALIAS, OWNER_B, list(point["vector"]))[0]
        with self.assertRaisesRegex(ContractViolation, "cross_owner_vector_candidate"):
            validate_vector_candidates(OWNER_A, [self.qdrant_candidate(owner_b_result)])
        self.qdrant.delete_point(PHYSICAL_A, OWNER_B_CLAIM_ID)

        candidates = [self.qdrant_candidate(item) for item in owner_a_results]
        validated = validate_vector_candidates(OWNER_A, candidates)
        authoritative = await self.read_claims(
            OWNER_A, [UUID(item["claim_id"]) for item in validated]
        )
        answer_policy = RetrievalPolicy(
            explicit_recall=False,
            allowed_predicates=("preference.personal",),
            max_records=8,
        )
        authorization_at = await self.admin.fetchval(
            "SELECT pg_catalog.clock_timestamp()"
        )
        selected_claims = revalidate_candidates(
            OWNER_A,
            validated,
            authoritative,
            policy=answer_policy,
            predicate_catalog=PREDICATE_CATALOG,
            authorization_at=authorization_at,
        )
        self.assertEqual(len(selected_claims), 1)
        rendered = render_memory_context(
            OWNER_A, selected_claims, max_records=8, max_bytes=32768
        )
        prepared = build_answer_binding(
            owner_user_id=OWNER_A,
            response_id=RESPONSE_ID,
            rendered_context=rendered,
            selected_claims=selected_claims,
            query_sha256=QUERY_SHA256,
            policy=answer_policy,
            renderer_sha256=ANSWER_RENDERER_SHA256,
            prompt_sha256=PROMPT_SHA256,
        )
        escaped = canonical_json_bytes(rendered["content"])
        prefix = b'{"input":"synthetic question","memory":'
        outbound = prefix + escaped + b"}"
        binding = mark_answer_binding_dispatched(
            prepared,
            rendered,
            outbound_request_bytes=outbound,
            escaped_segment_start_utf8=len(prefix),
            escaped_segment_end_utf8=len(prefix) + len(escaped),
        )
        self.assertNotEqual(THREAD_A, THREAD_B)
        async with self.owner_context(self.api, OWNER_A):
            stored_binding = await self.api.fetchrow(
                "SELECT * FROM memory_private.record_answer_binding("
                "$1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::text[],"
                "$7::text[],$8::text[],$9::integer,$10::integer,$11::text,"
                "$12::text,$13::boolean,$14::uuid[],$15::uuid[],$16::text,"
                "$17::text,$18::text,$19::text,$20::text)",
                ANSWER_OPERATION_ID,
                RESPONSE_ID,
                THREAD_B,
                QUERY_SHA256,
                answer_policy.policy_sha256,
                list(answer_policy.allowed_predicates),
                list(answer_policy.domains),
                list(answer_policy.intents),
                answer_policy.max_records,
                answer_policy.policy_revision,
                ANSWER_RENDERER_SHA256,
                PROMPT_SHA256,
                answer_policy.explicit_recall,
                [UUID(value) for value in binding["selected_claim_ids"]],
                [UUID(value) for value in binding["injected_claim_ids"]],
                "exposed",
                binding["selection_manifest_sha256"],
                binding["injection_manifest_sha256"],
                rendered["content"],
                outbound.decode("utf-8"),
            )
        self.assertEqual(
            stored_binding["injection_manifest_sha256"],
            binding["injection_manifest_sha256"],
        )
        try:
            await self.admin.execute(
                "ALTER TABLE memory.answer_binding "
                "DISABLE TRIGGER answer_binding_immutable"
            )
            await self.admin.execute(
                "UPDATE memory.answer_binding "
                "SET created_at="
                "pg_catalog.transaction_timestamp()-interval '91 days', "
                "expires_at="
                "pg_catalog.transaction_timestamp()-interval '1 day' "
                "WHERE binding_id=$1::uuid",
                stored_binding["binding_id"],
            )
        finally:
            await self.admin.execute(
                "ALTER TABLE memory.answer_binding "
                "ENABLE TRIGGER answer_binding_immutable"
            )
        self.assertEqual(
            await self.worker.fetchval(
                "SELECT memory_private.purge_expired_answer_bindings(10)"
            ),
            1,
        )
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT pg_catalog.count(*) FROM memory.answer_binding "
                "WHERE binding_id=$1::uuid",
                stored_binding["binding_id"],
            ),
            0,
        )
        self.assertEqual(
            await self.admin.fetchval(
                "SELECT pg_catalog.count(*) FROM memory.audit_event "
                "WHERE owner_user_id=$1::uuid "
                "AND object_type='answer_binding' AND object_id=$2::uuid "
                "AND transition_code='answer_binding_retention_purged' "
                "AND reason_code='retention_expired' "
                "AND new_state_sha256=event_sha256",
                OWNER_A,
                stored_binding["binding_id"],
            ),
            1,
        )

        replacement = {
            "epistemic_state": "supported",
            "object_display_name": None,
            "object_entity_type": None,
            "object_kind": "literal",
            "object_literal": "amber",
            "sensitivity": "ordinary",
        }
        correction_status, correction = await self.http_request(
            "POST",
            f"/memory/claims/{claim_id}/correct",
            token=self.owner_a_token,
            body={
                "expected_predicate_catalog_sha256": catalog_sha256,
                "expected_revision_sha256": claims[0]["revision_sha256"],
                "expected_state_sha256": claims[0]["state_sha256"],
                "operation_id": str(CORRECTION_OPERATION_ID),
                "replacement": replacement,
            },
        )
        self.assertEqual(correction_status, 200)
        self.assertEqual(correction["outcome"], "correction_pending")
        stale_results = self.qdrant.search(ALIAS, OWNER_A, list(point["vector"]))
        self.assertEqual(len(stale_results), 1)
        self.assertEqual(await self.read_claims(OWNER_A, [claim_id]), [])
        self.assertEqual(
            revalidate_candidates(
                OWNER_A,
                [self.qdrant_candidate(stale_results[0])],
                [],
                policy=answer_policy,
                predicate_catalog=PREDICATE_CATALOG,
                authorization_at=authorization_at,
            ),
            (),
        )
        correction_delete_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'successor_projector',1,120)"
        )
        await self.finish_delete(
            correction_delete_lease, physical=PHYSICAL_A, finalize=False
        )
        proposals_status, proposals = await self.http_request(
            "GET", "/memory/proposals", token=self.owner_a_token
        )
        self.assertEqual(proposals_status, 200)
        correction_proposal = next(
            row
            for row in proposals
            if row["proposal_id"] == correction["proposal_id"]
        )
        corrected_review_status, corrected_review = await self.http_request(
            "POST",
            f"/memory/proposals/{correction['proposal_id']}/review",
            token=self.owner_a_token,
            body={
                "decision": "admit",
                "expected_predicate_catalog_sha256": correction_proposal[
                    "predicate_catalog_sha256"
                ],
                "expected_proposal_sha256": correction_proposal[
                    "proposal_sha256"
                ],
                "expected_selected_sha256": correction_proposal[
                    "selected_sha256"
                ],
                "expected_selection_binding_sha256": correction_proposal[
                    "selection_binding_sha256"
                ],
                "expected_source_sha256": correction_proposal["source_sha256"],
                "operation_id": correction["review_operation_id"],
                "reason_codes": ["explicit_owner_review"],
            },
        )
        self.assertEqual(corrected_review_status, 200)
        self.assertEqual(corrected_review["outcome"], "admitted")
        corrected_claim = (await self.read_claims(OWNER_A, [claim_id]))[0]
        self.assertEqual(corrected_claim["revision_number"], 2)
        corrected_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'successor_projector',1,120)"
        )
        corrected_point = build_projection_point(
            corrected_claim,
            self.projection_outbox(corrected_lease),
            deterministic_vector(),
        )
        self.qdrant.upsert(PHYSICAL_A, corrected_point)
        await self.finish_upsert(corrected_lease, corrected_point, PHYSICAL_A)
        corrected_candidate = self.qdrant_candidate(
            self.qdrant.search(ALIAS, OWNER_A, list(corrected_point["vector"]))[0]
        )
        self.assertEqual(corrected_candidate["revision_number"], 2)

        self.qdrant.create_collection(PHYSICAL_B)
        del corrected_claim
        del corrected_lease
        del corrected_point
        rebuild_rows = await self.worker.fetch(
            "SELECT * FROM memory_private.read_projection_rebuild_batch("
            "NULL::uuid,NULL::uuid,100)"
        )
        self.assertEqual(len(rebuild_rows), 1)
        rebuild_inputs = projection_rebuild_row_to_inputs(dict(rebuild_rows[0]))
        self.assertEqual(rebuild_inputs["applied_physical_collection"], PHYSICAL_A)
        rebuilt_point = build_rebuild_projection_point(
            rebuild_inputs["claim"],
            rebuild_inputs["outbox"],
            deterministic_vector(),
        )
        self.assertEqual(
            rebuilt_point["payload"]["vector_sha256"],
            rebuild_inputs["expected_vector_sha256"],
        )
        self.qdrant.upsert(PHYSICAL_B, rebuilt_point)
        point_a = self.qdrant.retrieve(PHYSICAL_A, claim_id)[0]
        point_b = self.qdrant.retrieve(PHYSICAL_B, claim_id)[0]
        rebuild_manifest_a = canonical_sha256(
            "governed_memory.successor_rebuild_point",
            {
                "point_id": str(point_a["id"]),
                "vector_sha256": vector_sha256(point_a["vector"]),
                "payload": point_a["payload"],
            },
        )
        rebuild_manifest_b = canonical_sha256(
            "governed_memory.successor_rebuild_point",
            {
                "point_id": str(point_b["id"]),
                "vector_sha256": vector_sha256(point_b["vector"]),
                "payload": point_b["payload"],
            },
        )
        self.assertEqual(rebuild_manifest_a, rebuild_manifest_b)
        direct_a = self.qdrant.search(PHYSICAL_A, OWNER_A, list(rebuilt_point["vector"]))
        direct_b = self.qdrant.search(PHYSICAL_B, OWNER_A, list(rebuilt_point["vector"]))
        candidate_manifest_a = [
            {
                "id": str(item["id"]),
                "payload": item["payload"],
                "score_hex": float(item["score"]).hex(),
            }
            for item in direct_a
        ]
        candidate_manifest_b = [
            {
                "id": str(item["id"]),
                "payload": item["payload"],
                "score_hex": float(item["score"]).hex(),
            }
            for item in direct_b
        ]
        self.assertEqual(
            canonical_sha256(
                "governed_memory.successor_candidates", candidate_manifest_a
            ),
            canonical_sha256(
                "governed_memory.successor_candidates", candidate_manifest_b
            ),
        )
        self.qdrant.swap_alias(ALIAS, PHYSICAL_A, PHYSICAL_B)
        self.assertEqual(self.qdrant.aliases().get(ALIAS), PHYSICAL_B)
        corrected_claim = (await self.read_claims(OWNER_A, [claim_id]))[0]

        retraction_body = {
            "expected_revision_sha256": corrected_claim["revision_sha256"],
            "expected_state_sha256": corrected_claim["state_sha256"],
            "operation_id": str(RETRACTION_OPERATION_ID),
        }
        retraction_status, retracted = await self.http_request(
            "POST",
            f"/memory/claims/{claim_id}/retract",
            token=self.owner_a_token,
            body=retraction_body,
        )
        self.assertEqual(retraction_status, 200)
        self.assertEqual(retracted["outcome"], "retracted")
        stale_after_retraction = self.qdrant.search(
            ALIAS, OWNER_A, list(rebuilt_point["vector"])
        )
        self.assertEqual(len(stale_after_retraction), 1)
        self.assertEqual(await self.read_claims(OWNER_A, [claim_id]), [])
        retraction_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'successor_projector',1,120)"
        )
        await self.finish_delete(retraction_lease, physical=PHYSICAL_B, finalize=False)

        retracted_state = (await self.list_claims(OWNER_A))[0]
        deletion_body = {
            "expected_revision_sha256": retracted_state["revision_sha256"],
            "expected_state_sha256": retracted_state["current_state_sha256"],
            "operation_id": str(DELETION_OPERATION_ID),
        }
        deletion_status, deletion = await self.http_request(
            "DELETE",
            f"/memory/claims/{claim_id}",
            token=self.owner_a_token,
            body=deletion_body,
        )
        self.assertEqual(deletion_status, 200)
        self.assertEqual(deletion["outcome"], "deletion_pending")
        deletion_state = (await self.list_claims(OWNER_A))[0]
        deletion_lease = await self.worker.fetchrow(
            "SELECT * FROM memory_private.lease_projection_jobs("
            "'successor_projector',1,120)"
        )
        deletion_receipt = await self.finish_delete(
            deletion_lease,
            physical=PHYSICAL_B,
            finalize=True,
            deletion_state=deletion_state,
        )
        deleted_counts = await self.admin.fetchrow(
            "SELECT "
            "(SELECT pg_catalog.count(*) FROM memory.claim "
            " WHERE owner_user_id=$1::uuid) AS claims,"
            "(SELECT pg_catalog.count(*) FROM memory.claim_revision "
            " WHERE owner_user_id=$1::uuid) AS revisions,"
            "(SELECT pg_catalog.count(*) FROM memory.claim_evidence "
            " WHERE owner_user_id=$1::uuid) AS evidence_links,"
            "(SELECT pg_catalog.count(*) FROM memory.projection_outbox "
            " WHERE owner_user_id=$1::uuid) AS projection_rows,"
            "(SELECT pg_catalog.count(*) FROM memory.proposal "
            " WHERE owner_user_id=$1::uuid) AS proposals,"
            "(SELECT pg_catalog.count(*) FROM memory.provider_call "
            " WHERE owner_user_id=$1::uuid) AS provider_calls,"
            "(SELECT pg_catalog.count(*) FROM memory.extraction_job "
            " WHERE owner_user_id=$1::uuid) AS extraction_jobs,"
            "(SELECT pg_catalog.count(*) FROM memory.evidence "
            " WHERE owner_user_id=$1::uuid) AS evidence_rows,"
            "(SELECT pg_catalog.count(*) FROM memory.entity "
            " WHERE owner_user_id=$1::uuid) AS entities",
            OWNER_A,
        )
        self.assertEqual(set(dict(deleted_counts).values()), {0})
        retained_deletion = await self.admin.fetchrow(
            "SELECT receipt.receipt_sha256,event.event_sha256 "
            "FROM memory.claim_deletion_receipt AS receipt "
            "JOIN memory.audit_event AS event "
            "ON event.event_id=receipt.audit_event_id "
            "AND event.owner_user_id=receipt.owner_user_id "
            "WHERE receipt.owner_user_id=$1::uuid "
            "AND receipt.claim_id=$2::uuid "
            "AND receipt.operation_id=$3::uuid "
            "AND event.transition_code='claim_deleted' "
            "AND event.reason_code='qdrant_delete_verified'",
            OWNER_A,
            claim_id,
            DELETION_OPERATION_ID,
        )
        self.assertIsNotNone(retained_deletion)
        self.assertEqual(
            retained_deletion["receipt_sha256"],
            retained_deletion["event_sha256"],
        )
        self.assertEqual(await self.list_claims(OWNER_A), [])
        self.assertEqual(await self.read_claims(OWNER_A, [claim_id]), [])
        await self.assert_http_failure(
            404,
            "memory_resource_not_found",
            "GET",
            f"/memory/claims/{claim_id}",
            token=self.owner_a_token,
        )
        deletion_replay_status, deletion_replay = await self.http_request(
            "DELETE",
            f"/memory/claims/{claim_id}",
            token=self.owner_a_token,
            body=deletion_body,
        )
        self.assertEqual(deletion_replay_status, 200)
        self.assertEqual(deletion_replay["outcome"], "replayed")
        self.assertFalse(self.qdrant.retrieve(ALIAS, claim_id))
        self.assertGreaterEqual(self.jwks.fetch_count, 1)

        auth_negative_matrix_sha256 = canonical_sha256(
            "governed_memory.successor_http_auth_negative_matrix",
            (
                "anonymous",
                "bad_service_token",
                "bad_signature",
                "explicit_actor_header",
                "expired",
                "forged_user_metadata_ignored",
                "invalid_session_uuid",
                "invalid_subject_uuid",
                "malformed_bearer",
                "missing_bearer",
                "missing_required_claim",
                "missing_service_token",
                "prohibited_algorithm",
                "prohibited_owner_body",
                "prohibited_owner_query",
                "role_denied",
                "unknown_key",
                "wrong_audience",
                "wrong_issuer",
            ),
        )
        http_lifecycle_sha256 = canonical_sha256(
            "governed_memory.successor_http_lifecycle",
            {
                "claim_id": claim_id,
                "correction_outcome": correction["outcome"],
                "corrected_review_outcome": corrected_review["outcome"],
                "delete_outcome": deletion["outcome"],
                "delete_replay_outcome": deletion_replay["outcome"],
                "initial_review_outcome": review["outcome"],
                "retraction_outcome": retracted["outcome"],
            },
        )
        owner_isolation_sha256 = canonical_sha256(
            "governed_memory.successor_http_owner_isolation",
            {
                "owner_b_claim_hidden": True,
                "owner_b_claim_list_empty": True,
                "owner_b_correction_denied": True,
                "owner_b_deletion_denied": True,
                "owner_b_operation_hidden": True,
                "owner_b_proposals_empty": True,
                "owner_b_retraction_denied": True,
                "owner_b_review_denied": True,
                "rls_direct_table_isolation": True,
                "vector_owner_filter": True,
            },
        )

        receipt = {
            "schema": "governed-memory-successor-http-integration-receipt-v1",
            "auth_negative_matrix_sha256": auth_negative_matrix_sha256,
            "claim_id": str(claim_id),
            "initial_revision_id": str(claims[0]["revision_id"]),
            "corrected_revision_id": str(corrected_claim["revision_id"]),
            "chat_a_thread_id": str(THREAD_A),
            "chat_b_thread_id": str(THREAD_B),
            "cold_extraction_lease": True,
            "cold_projection_rebuild": True,
            "http_lifecycle_sha256": http_lifecycle_sha256,
            "http_owner_lifecycle": True,
            "jwks_fetch_count": self.jwks.fetch_count,
            "jwks_manifest_sha256": canonical_sha256(
                "governed_memory.successor_disposable_jwks", self.jwks.document
            ),
            "owner_isolation_sha256": owner_isolation_sha256,
            "rebuild_manifest_sha256": rebuild_manifest_b,
            "review_surface_sha256": canonical_sha256(
                "governed_memory.successor_review_surface",
                _jsonable(review_surface),
            ),
            "answer_binding_sha256": binding["binding_sha256"],
            "deletion_receipt_sha256": deletion_receipt["receipt_sha256"],
            "provider_external_calls": 0,
            "production_data_read": False,
            "production_endpoint_calls": 0,
            "production_service_invoked": False,
            "route_manifest_sha256": route_manifest_sha256(),
            "semantic_threshold_calibrated": False,
            "synthetic_jwt_only": True,
        }
        print(
            "SUCCESSOR_HTTP_VERTICAL_SLICE_RECEIPT="
            + json.dumps(receipt, sort_keys=True)
        )
