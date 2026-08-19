from __future__ import annotations

import unittest
from uuid import UUID

from starlette.requests import Request

from seebx.core.request_ids import get_request_id, sanitize_request_id


def request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
        }
    )


class RequestIdTests(unittest.TestCase):
    def test_sanitizer_preserves_bounded_nonempty_value(self) -> None:
        self.assertEqual(sanitize_request_id("  request-1  "), "request-1")
        self.assertIsNone(sanitize_request_id(None))
        self.assertIsNone(sanitize_request_id("  "))
        self.assertIsNone(sanitize_request_id("x" * 129))

    def test_primary_header_precedes_correlation_fallback(self) -> None:
        value = request(
            [
                (b"x-request-id", b"primary"),
                (b"x-correlation-id", b"fallback"),
            ]
        )
        self.assertEqual(get_request_id(value), "primary")

    def test_invalid_or_missing_headers_generate_uuid(self) -> None:
        value = request([(b"x-request-id", b"x" * 129)])
        generated = get_request_id(value)
        self.assertEqual(str(UUID(generated)), generated)


if __name__ == "__main__":
    unittest.main()
