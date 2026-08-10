"""Retry-free HTTPS transport tests; no sockets or credentials are used."""

from __future__ import annotations

import ssl
import unittest

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.runtime.https_transport import (
    HttpsOutcomeUnknown,
    HttpsRequest,
    RetryFreeBoundedHttpsTransport,
    canonical_json_request_bytes,
    json_headers,
)


class FakeRawResponse:
    def __init__(self, *, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, amount: int) -> bytes:
        return self._body[:amount]

    def getheaders(self) -> list[tuple[str, str]]:
        return [("Content-Type", "application/json")]


class FakeConnection:
    def __init__(self, response: FakeRawResponse, events: list[object]) -> None:
        self._response = response
        self._events = events

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        self._events.append(("request", method, path, body, headers))

    def getresponse(self) -> FakeRawResponse:
        self._events.append("getresponse")
        return self._response

    def close(self) -> None:
        self._events.append("close")


class RetryFreeBoundedHttpsTransportTests(unittest.TestCase):
    def request(self, *, maximum: int = 1024) -> HttpsRequest:
        return HttpsRequest(
            url="https://api.openai.com/v1/responses",
            headers=json_headers(bearer_token="synthetic-test-token"),
            body=canonical_json_request_bytes({"test": "bounded"}),
            max_response_bytes=maximum,
        )

    def test_one_direct_post_preserves_exact_body_and_does_not_follow_redirect(self) -> None:
        events: list[object] = []
        raw = FakeRawResponse(status=307, body=b'{"redirect":true}')

        def factory(
            host: str,
            port: int,
            timeout: float,
            context: ssl.SSLContext,
        ) -> FakeConnection:
            self.assertEqual((host, port), ("api.openai.com", 443))
            self.assertEqual(timeout, 4.0)
            self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
            events.append("factory")
            return FakeConnection(raw, events)

        transport = RetryFreeBoundedHttpsTransport(
            timeout_seconds=4,
            connection_factory=factory,
        )
        request = self.request()
        response = transport.post(request)
        self.assertEqual(response.status, 307)
        requests = [event for event in events if isinstance(event, tuple)]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][1:3], ("POST", "/v1/responses"))
        self.assertEqual(requests[0][3], request.body)
        self.assertEqual(events[-1], "close")

    def test_response_bound_fails_closed_after_the_single_attempt(self) -> None:
        events: list[object] = []
        raw = FakeRawResponse(status=200, body=b"x" * 9)
        transport = RetryFreeBoundedHttpsTransport(
            connection_factory=lambda *_args: FakeConnection(raw, events)
        )
        with self.assertRaisesRegex(
            HttpsOutcomeUnknown, "https_response_too_large_after_dispatch"
        ):
            transport.post(self.request(maximum=8))
        self.assertEqual(
            len([event for event in events if isinstance(event, tuple)]),
            1,
        )

    def test_non_https_and_noncanonical_numbers_fail_before_connection_factory(self) -> None:
        with self.assertRaises(ContractViolation):
            HttpsRequest(
                url="http://api.openai.com/v1/responses",
                headers=json_headers(bearer_token="synthetic-test-token"),
                body=b"{}",
            )
        with self.assertRaises(ContractViolation):
            HttpsRequest(
                url="https://api.openai.com/v1/responses?redirect=1",
                headers=json_headers(bearer_token="synthetic-test-token"),
                body=b"{}",
            )
        with self.assertRaisesRegex(
            Exception, "canonical_json_failed_before_send"
        ):
            canonical_json_request_bytes({"float": 1.5})

    def test_secret_header_and_body_are_redacted_from_request_repr(self) -> None:
        request = self.request()
        rendered = repr(request)
        self.assertNotIn("synthetic-test-token", rendered)
        self.assertNotIn("bounded", rendered)

    def test_injected_tls_context_must_verify_certificate_and_hostname(self) -> None:
        insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        with self.assertRaisesRegex(
            ContractViolation,
            "insecure_https_tls_context",
        ):
            RetryFreeBoundedHttpsTransport(tls_context=insecure)

        verified = ssl.create_default_context()
        transport = RetryFreeBoundedHttpsTransport(tls_context=verified)
        self.assertIs(transport._tls_context, verified)
        self.assertTrue(verified.check_hostname)
        self.assertEqual(verified.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(verified.minimum_version, ssl.TLSVersion.TLSv1_2)


if __name__ == "__main__":
    unittest.main()
