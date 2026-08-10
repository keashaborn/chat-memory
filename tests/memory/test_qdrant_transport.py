from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.runtime.qdrant_adapter import (
    QDRANT_PHYSICAL_COLLECTION,
    QdrantWriteOutcomeUnknown,
)
from rag_engine.governed_memory.runtime.qdrant_transport import (
    LoopbackQdrantTransport,
    QDRANT_RUNTIME_URL,
    QdrantTransportFailure,
)


class FakeResponse:
    def __init__(
        self,
        *,
        status: int,
        body: bytes,
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self._body = body
        self._content_type = content_type

    def read(self, _maximum: int) -> bytes:
        return self._body

    def getheader(self, name: str, default: str = "") -> str:
        return self._content_type if name.lower() == "content-type" else default


class FakeConnection:
    response = FakeResponse(status=200, body=b"{}")
    calls: list[tuple[str, str, bytes | None, dict[str, str]]]

    def __init__(self, host: str, *, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = []

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        headers: dict[str, str],
    ) -> None:
        self.calls.append((method, path, body, headers))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        return None


class RequestFailureConnection(FakeConnection):
    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        headers: dict[str, str],
    ) -> None:
        raise ConnectionResetError("synthetic-partial-send")


def transport() -> LoopbackQdrantTransport:
    return LoopbackQdrantTransport(
        base_url=QDRANT_RUNTIME_URL,
        api_key="synthetic-qdrant-key",
    )


class QdrantRuntimeTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_only_exact_loopback_target_is_constructible(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "qdrant_runtime_target_mismatch"):
            LoopbackQdrantTransport(
                base_url="http://127.0.0.1:6333",
                api_key="synthetic-qdrant-key",
            )

    async def test_read_only_post_failure_is_not_classified_as_write(self) -> None:
        FakeConnection.response = FakeResponse(
            status=503,
            body=json.dumps({"status": "unavailable"}).encode("ascii"),
        )
        with patch(
            "rag_engine.governed_memory.runtime.qdrant_transport.http.client.HTTPConnection",
            FakeConnection,
        ):
            with self.assertRaisesRegex(
                QdrantTransportFailure,
                "qdrant_read_response_not_successful",
            ):
                await transport().request(
                    "POST",
                    f"/collections/{QDRANT_PHYSICAL_COLLECTION}/points",
                    {"ids": ["11111111-1111-4111-8111-111111111111"]},
                )

    async def test_dispatched_write_with_invalid_response_is_outcome_unknown(self) -> None:
        FakeConnection.response = FakeResponse(
            status=200,
            body=b"not-json",
        )
        with patch(
            "rag_engine.governed_memory.runtime.qdrant_transport.http.client.HTTPConnection",
            FakeConnection,
        ):
            with self.assertRaisesRegex(
                QdrantWriteOutcomeUnknown,
                "qdrant_write_outcome_unknown",
            ):
                await transport().request(
                    "PUT",
                    (
                        f"/collections/{QDRANT_PHYSICAL_COLLECTION}"
                        "/points?wait=true"
                    ),
                    {"points": []},
                )

    async def test_write_request_failure_is_ambiguous_not_safe_to_retry(self) -> None:
        with patch(
            "rag_engine.governed_memory.runtime.qdrant_transport.http.client.HTTPConnection",
            RequestFailureConnection,
        ):
            with self.assertRaisesRegex(
                QdrantWriteOutcomeUnknown,
                "qdrant_write_outcome_unknown",
            ):
                await transport().request(
                    "PUT",
                    (
                        f"/collections/{QDRANT_PHYSICAL_COLLECTION}"
                        "/points?wait=true"
                    ),
                    {"points": []},
                )

    async def test_read_request_failure_is_retryable_unavailable(self) -> None:
        with patch(
            "rag_engine.governed_memory.runtime.qdrant_transport.http.client.HTTPConnection",
            RequestFailureConnection,
        ):
            with self.assertRaisesRegex(
                QdrantTransportFailure,
                "qdrant_unavailable",
            ):
                await transport().request(
                    "GET",
                    f"/collections/{QDRANT_PHYSICAL_COLLECTION}",
                )


if __name__ == "__main__":
    unittest.main()
