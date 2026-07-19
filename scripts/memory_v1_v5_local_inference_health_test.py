#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from memory_v1_v5_local_inference_health import (
    fetch_models,
    load_credential,
    loopback_endpoint,
)


class Response(io.BytesIO):
    status = 200

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        del args


def main() -> None:
    endpoint = "http://127.0.0.1:18080/v1/models"
    assert loopback_endpoint(endpoint)
    for invalid in (
        "https://127.0.0.1:18080/v1/models",
        "http://10.0.0.1:18080/v1/models",
        "http://127.0.0.1:18080/health",
    ):
        try:
            loopback_endpoint(invalid)
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid health endpoint was accepted")

    with tempfile.TemporaryDirectory() as directory:
        credential = Path(directory) / "local_api_key"
        credential.write_text("x" * 32, encoding="utf-8")
        original = os.environ.get("CREDENTIALS_DIRECTORY")
        os.environ["CREDENTIALS_DIRECTORY"] = directory
        try:
            assert load_credential("local_api_key") == "x" * 32
        finally:
            if original is None:
                os.environ.pop("CREDENTIALS_DIRECTORY", None)
            else:
                os.environ["CREDENTIALS_DIRECTORY"] = original

    payload = json.dumps(
        {"object": "list", "data": [{"id": "qwen3-8b-local-extractor"}]}
    ).encode("utf-8")
    with patch("urllib.request.urlopen", return_value=Response(payload)) as mocked:
        assert fetch_models(endpoint, "x" * 32, 5.0) == [
            "qwen3-8b-local-extractor"
        ]
        request = mocked.call_args.args[0]
        assert request.full_url == "http://127.0.0.1:18080/v1/models"
        assert request.get_header("Authorization") == "Bearer " + "x" * 32

    print("memory_v1_v5_local_inference_health: PASS")


if __name__ == "__main__":
    main()
