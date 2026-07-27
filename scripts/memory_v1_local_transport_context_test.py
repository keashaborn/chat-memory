#!/usr/bin/env python3
import json

from scripts.memory_v1_relational_extraction_v5_local_provider import (
    _http_error_code,
)


def main() -> int:
    context_error = json.dumps(
        {
            "error": {
                "message": (
                    "request (16682 tokens) exceeds the available context "
                    "size (16384 tokens)"
                )
            }
        }
    ).encode("utf-8")
    assert _http_error_code(400, context_error) == (
        "local_transport_context_exceeded"
    )
    assert _http_error_code(
        400, b'{"error":{"message":"invalid request"}}'
    ) == "local_transport_http_rejected"
    assert _http_error_code(400, b"not-json") == (
        "local_transport_http_rejected"
    )
    assert _http_error_code(500, context_error) == (
        "local_transport_server_error"
    )
    print("memory_v1_local_transport_context_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
