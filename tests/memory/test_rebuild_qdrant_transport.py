from __future__ import annotations

import json
import unittest

from rag_engine.governed_memory.runtime.qdrant_transport import QDRANT_RUNTIME_URL
from rag_engine.governed_memory.runtime.rebuild_qdrant_transport import (
    LoopbackRebuildQdrantTransport,
    RebuildQdrantOutcomeUnknown,
    RebuildQdrantTransportFailure,
)


COLLECTION = "governed_memory_9a54cf123493_000002"


class FakeResponse:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self, _limit: int) -> bytes:
        return self._body

    def getheader(self, name: str, default: str = "") -> str:
        return "application/json" if name.lower() == "content-type" else default


class FakeConnection:
    def __init__(self, response: FakeResponse, *, fail: bool = False) -> None:
        self.response = response
        self.fail = fail
        self.calls: list[tuple[str, str, object, object]] = []

    def request(self, method: str, path: str, *, body: object, headers: object) -> None:
        self.calls.append((method, path, body, headers))
        if self.fail:
            raise OSError("synthetic failure")

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        return None


class RebuildQdrantTransportTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, connection: FakeConnection) -> LoopbackRebuildQdrantTransport:
        return LoopbackRebuildQdrantTransport(
            base_url=QDRANT_RUNTIME_URL,
            api_key="synthetic-test-key",
            connection_factory=lambda *_args, **_kwargs: connection,
        )

    async def test_bounded_get_accepts_explicit_404_without_mutation_ambiguity(self) -> None:
        connection = FakeConnection(FakeResponse(404, {"status": "not_found"}))
        response = await self.transport(connection).request(
            "GET",
            f"/collections/{COLLECTION}",
            accepted_statuses=frozenset({200, 404}),
        )
        self.assertEqual(response.status, 404)
        self.assertEqual(connection.calls[0][0:2], ("GET", f"/collections/{COLLECTION}"))

    async def test_exact_create_is_canonical_json_and_api_key_is_header_only(self) -> None:
        connection = FakeConnection(FakeResponse(200, {"result": True, "status": "ok"}))
        await self.transport(connection).request(
            "PUT",
            f"/collections/{COLLECTION}",
            {"vectors": {"size": 3072, "distance": "Dot"}},
        )
        method, path, body, headers = connection.calls[0]
        self.assertEqual((method, path), ("PUT", f"/collections/{COLLECTION}"))
        self.assertEqual(
            body,
            b'{"vectors":{"distance":"Dot","size":3072}}',
        )
        self.assertEqual(headers["api-key"], "synthetic-test-key")
        self.assertNotIn(b"synthetic-test-key", body)

    async def test_mutation_transport_failure_is_outcome_unknown(self) -> None:
        connection = FakeConnection(FakeResponse(500, {}), fail=True)
        with self.assertRaisesRegex(
            RebuildQdrantOutcomeUnknown, "rebuild_qdrant_write_outcome_unknown"
        ):
            await self.transport(connection).request(
                "POST",
                "/collections/aliases",
                {"actions": []},
            )

    async def test_legacy_or_unbounded_paths_are_rejected_before_connection(self) -> None:
        connection = FakeConnection(FakeResponse(200, {}))
        with self.assertRaisesRegex(
            RebuildQdrantTransportFailure,
            "rebuild_qdrant_request_rejected_before_send",
        ):
            await self.transport(connection).request(
                "GET", "/collections/memory_raw"
            )
        self.assertEqual(connection.calls, [])


if __name__ == "__main__":
    unittest.main()
