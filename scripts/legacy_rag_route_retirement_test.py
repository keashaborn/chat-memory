#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path("/opt/chat-memory")
BASE_URL = (os.getenv("BRAINS_URL") or "http://127.0.0.1:8088").rstrip("/")


def load_service_token() -> str:
    token = (os.getenv("VS_SERVICE_TOKEN") or "").strip()
    if token:
        return token
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("VS_SERVICE_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("VS_SERVICE_TOKEN is missing")


def request_status(method: str, path: str, token: str, body: dict | None = None) -> int:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={
            "content-type": "application/json",
            "x-vs-service-token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code


def main() -> int:
    token = load_service_token()

    with urllib.request.urlopen(f"{BASE_URL}/openapi.json", timeout=10) as response:
        schema = json.load(response)
    paths = set((schema.get("paths") or {}).keys())

    retired_paths = {"/rag/query", "/rag/feedback", "/retrieve", "/vantage/feedback"}
    assert retired_paths.isdisjoint(paths), retired_paths & paths
    assert "/vantage/query" in paths

    random_owner = "00000000-0000-4000-8000-000000000001"
    probes = (
        ("/rag/query", {"query": "route retirement probe", "user_id": random_owner}),
        ("/rag/feedback", {"user_id": random_owner, "message": "route retirement probe"}),
        ("/retrieve", {"query": "route retirement probe"}),
        ("/vantage/feedback", {"user_id": random_owner, "message": "route retirement probe"}),
    )
    for path, body in probes:
        assert request_status("POST", path, token, body) == 404, path
    assert request_status("GET", "/healthz", token) == 200

    vantage_source = (ROOT / "rag_engine" / "vantage_router.py").read_text(encoding="utf-8")
    for symbol in (
        "retrieve_personal_memory(",
        "unified_retrieve(",
        "run_memory_v1_runtime(",
        "run_preference_project_runtime(",
    ):
        assert symbol in vantage_source, symbol

    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "include_router(vantage_router, prefix=\"/vantage\")" in app_source
    assert "include_router(rag_router" not in app_source

    print("legacy_rag_route_retirement_test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
