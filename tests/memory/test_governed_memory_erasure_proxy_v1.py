"""Successor erasure proxy boundary tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag_engine.governed_memory_erasure_proxy_v1 import (
    ERASURE_COLLECTION_PATH,
    MAX_REQUEST_BODY_BYTES,
    MAX_RESPONSE_BODY_BYTES,
    ProxyResult,
    SUCCESSOR_SOCKET_PATH,
    create_governed_memory_erasure_proxy_router_v1,
)


TOKEN = "synthetic-machine-token"
AUTHORIZATION = "Bearer aaa.bbb.ccc"
OPERATION = "11111111-1111-4111-8111-111111111111"


@dataclass
class RecordingTransport:
    result: ProxyResult = field(
        default_factory=lambda: ProxyResult(
            status_code=202,
            headers={
                "content-type": "application/json; charset=utf-8",
                "location": ERASURE_COLLECTION_PATH + "/" + OPERATION,
                "retry-after": "2",
            },
            body=b'{"state":"fenced"}',
        )
    )
    failure: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def request(self, **kwargs: object) -> ProxyResult:
        self.calls.append(dict(kwargs))
        if self.failure is not None:
            raise self.failure
        return self.result


def client(transport: RecordingTransport, *, token: str | None = TOKEN) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_governed_memory_erasure_proxy_router_v1(
            service_token=token,
            transport=transport,
        )
    )
    return TestClient(app)


class GovernedMemoryErasureProxyV1Tests(unittest.TestCase):
    def test_post_forwards_only_exact_auth_token_and_body(self) -> None:
        transport = RecordingTransport()
        response = client(transport).post(
            ERASURE_COLLECTION_PATH,
            content=json.dumps({"contract_version": "v1"}),
            headers={
                "authorization": AUTHORIZATION,
                "content-type": "application/json; charset=utf-8",
                "cookie": "must-not-forward=1",
                "x-vs-actor-user-id": "must-not-forward",
                "x-governed-memory-service-token": "attacker-token",
            },
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["path"], ERASURE_COLLECTION_PATH)
        self.assertEqual(call["authorization"], AUTHORIZATION)
        self.assertEqual(call["service_token"], TOKEN)
        self.assertEqual(json.loads(call["body"]), {"contract_version": "v1"})
        self.assertNotIn("cookie", call)
        self.assertEqual(response.headers["location"], ERASURE_COLLECTION_PATH + "/" + OPERATION)
        self.assertEqual(response.headers["retry-after"], "2")

    def test_duplicate_missing_or_malformed_authorization_is_rejected(self) -> None:
        for headers in (
            [],
            [("authorization", "Basic value")],
            [("authorization", "Bearer not-a-jwt")],
            [("authorization", AUTHORIZATION), ("authorization", AUTHORIZATION)],
        ):
            with self.subTest(headers=headers):
                transport = RecordingTransport()
                response = client(transport).request(
                    "POST",
                    ERASURE_COLLECTION_PATH,
                    content="{}",
                    headers=headers + [("content-type", "application/json")],
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(transport.calls, [])

        lower = RecordingTransport()
        response = client(lower).post(
            ERASURE_COLLECTION_PATH,
            content="{}",
            headers={
                "authorization": "bearer aaa.bbb.ccc",
                "content-type": "application/json",
            },
        )
        self.assertEqual(response.status_code, 202)

    def test_query_bad_media_type_get_body_and_bad_operation_are_rejected(self) -> None:
        cases = (
            ("POST", ERASURE_COLLECTION_PATH + "?owner=x", "{}", "application/json"),
            ("POST", ERASURE_COLLECTION_PATH, "{}", "text/plain"),
            ("GET", ERASURE_COLLECTION_PATH + "/" + OPERATION, "{}", "application/json"),
            ("GET", ERASURE_COLLECTION_PATH + "/not-a-uuid", None, None),
        )
        for method, path, body, media_type in cases:
            with self.subTest(method=method, path=path):
                transport = RecordingTransport()
                headers = {"authorization": AUTHORIZATION}
                if media_type is not None:
                    headers["content-type"] = media_type
                response = client(transport).request(
                    method,
                    path,
                    content=body,
                    headers=headers,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(transport.calls, [])

    def test_request_and_response_limits_fail_closed(self) -> None:
        request_transport = RecordingTransport()
        request = client(request_transport).post(
            ERASURE_COLLECTION_PATH,
            content=b'"' + (b"a" * MAX_REQUEST_BODY_BYTES) + b'"',
            headers={
                "authorization": AUTHORIZATION,
                "content-type": "application/json",
            },
        )
        self.assertEqual(request.status_code, 400)
        self.assertEqual(request_transport.calls, [])

        response_transport = RecordingTransport(
            result=ProxyResult(
                200,
                {"content-type": "application/json"},
                b'"' + (b"a" * MAX_RESPONSE_BODY_BYTES) + b'"',
            )
        )
        response = client(response_transport).get(
            ERASURE_COLLECTION_PATH + "/" + OPERATION,
            headers={"authorization": AUTHORIZATION},
        )
        self.assertEqual(response.status_code, 503)

    def test_transport_failure_maps_post_unknown_and_get_unavailable(self) -> None:
        transport = RecordingTransport(failure=TimeoutError())
        post = client(transport).post(
            ERASURE_COLLECTION_PATH,
            content="{}",
            headers={
                "authorization": AUTHORIZATION,
                "content-type": "application/json",
            },
        )
        self.assertEqual(post.status_code, 503)
        self.assertEqual(post.json()["error"]["code"], "memory_operation_outcome_unknown")
        get = client(transport).get(
            ERASURE_COLLECTION_PATH + "/" + OPERATION,
            headers={"authorization": AUTHORIZATION},
        )
        self.assertEqual(get.status_code, 503)
        self.assertEqual(get.json()["error"]["code"], "memory_successor_unavailable")

    def test_unconfigured_malformed_or_redirect_response_fails_closed(self) -> None:
        transport = RecordingTransport()
        unconfigured = client(transport, token=None).post(
            ERASURE_COLLECTION_PATH,
            content="{}",
            headers={
                "authorization": AUTHORIZATION,
                "content-type": "application/json",
            },
        )
        self.assertEqual(unconfigured.status_code, 503)
        self.assertEqual(transport.calls, [])
        for result in (
            ProxyResult(302, {"content-type": "application/json"}, b"{}"),
            ProxyResult(200, {"content-type": "text/html"}, b"{}"),
            ProxyResult(200, {"content-type": "application/json"}, b"[]"),
            ProxyResult(200, {"content-type": "application/json"}, b"not-json"),
            ProxyResult(200, {"content-type": "application/json"}, b'{"x":NaN}'),
            ProxyResult(
                200,
                {"content-type": "application/json"},
                b'{"state":"completed","state":"receiving"}',
            ),
        ):
            with self.subTest(result=result):
                failing = RecordingTransport(result=result)
                response = client(failing).get(
                    ERASURE_COLLECTION_PATH + "/" + OPERATION,
                    headers={"authorization": AUTHORIZATION},
                )
                self.assertEqual(response.status_code, 503)

    def test_fixed_socket_identity_and_exact_route_surface(self) -> None:
        self.assertEqual(SUCCESSOR_SOCKET_PATH, "/run/governed-memory/http.sock")
        application = FastAPI()
        application.include_router(
            create_governed_memory_erasure_proxy_router_v1(
                service_token=TOKEN,
                transport=RecordingTransport(),
            )
        )
        methods_by_path = {
            route.path: frozenset(route.methods or ())
            for route in application.routes
            if route.path.startswith(ERASURE_COLLECTION_PATH)
        }
        self.assertEqual(
            methods_by_path,
            {
                ERASURE_COLLECTION_PATH: frozenset({"POST"}),
                ERASURE_COLLECTION_PATH + "/{operation_id}": frozenset({"GET"}),
            },
        )

        arbitrary = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        transport = RecordingTransport()
        response = client(transport).get(
            ERASURE_COLLECTION_PATH + "/" + arbitrary,
            headers={"authorization": AUTHORIZATION},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            transport.calls[0]["path"],
            ERASURE_COLLECTION_PATH + "/" + arbitrary,
        )

    def test_invalid_machine_token_configuration_never_forwards(self) -> None:
        for token in (" token", "token ", "line\nfeed", "nonascii-\N{SNOWMAN}"):
            with self.subTest(token=token):
                transport = RecordingTransport()
                response = client(transport, token=token).post(
                    ERASURE_COLLECTION_PATH,
                    content="{}",
                    headers={
                        "authorization": AUTHORIZATION,
                        "content-type": "application/json",
                    },
                )
                self.assertEqual(response.status_code, 503)
                self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
