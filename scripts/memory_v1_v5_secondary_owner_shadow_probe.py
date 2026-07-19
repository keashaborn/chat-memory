#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_engine.memory_v1_v5_shadow_trace import run_memory_v1_v5_shadow_trace


VERSION = "memory_v1_v5_secondary_owner_shadow_probe_v1"
SAFE_TRACE_FIELDS = {
    "version",
    "status",
    "outcome_code",
    "owner_user_id_sha256",
    "request_id_sha256",
    "thread_id_sha256",
    "request_binding_sha256",
    "query_sha256",
    "persistable",
    "intent",
    "domain",
    "candidate_set_sha256",
    "selection_set_sha256",
    "candidate_count",
    "visible_candidate_count",
    "selected_count",
    "token_estimate",
    "rejected_counts",
    "candidate_limit",
    "max_claims",
    "max_tokens",
    "max_sensitivity",
    "database_transaction",
    "database_writes",
    "qdrant_writes",
    "trace_writes",
    "prompt_injection",
    "answer_model_exposure",
    "retrieval_activation",
}


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a read-only, zero-influence V5 shadow probe for one explicitly "
            "bound owner using an existing owner-scoped claim vector."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--request-classification", required=True)
    parser.add_argument("--expected-selected", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--qdrant-scroll-url",
        default="http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll",
    )
    return parser.parse_args()


def validate(args: argparse.Namespace) -> uuid.UUID:
    try:
        owner = uuid.UUID(args.owner_user_id)
    except ValueError as exc:
        raise RuntimeError("owner-user-id must be a UUID") from exc
    if not 0 <= args.expected_selected <= 20:
        raise RuntimeError("expected-selected must be between 0 and 20")
    if not str(args.query).strip() or len(args.query) > 16000:
        raise RuntimeError("query is required and bounded")
    if not str(args.request_classification).strip():
        raise RuntimeError("request classification is required")
    return owner


def owner_seed_vector(url: str, owner: uuid.UUID) -> tuple[list[float], str]:
    body = {
        "limit": 1,
        "filter": {
            "must": [
                {
                    "key": "owner_user_id",
                    "match": {"value": str(owner)},
                }
            ]
        },
        "with_payload": False,
        "with_vector": True,
    }
    request = urllib.request.Request(
        url,
        data=stable_json(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    points = payload.get("result", {}).get("points")
    if not isinstance(points, list) or len(points) != 1:
        raise RuntimeError("owner seed vector is absent or ambiguous")
    point = points[0]
    if not isinstance(point, dict) or "id" not in point:
        raise RuntimeError("owner seed point is invalid")
    vector = point.get("vector")
    if not isinstance(vector, list) or not 1 <= len(vector) <= 16384:
        raise RuntimeError("owner seed vector is invalid")
    try:
        values = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("owner seed vector contains invalid values") from exc
    return values, sha256_text(stable_json(point["id"]))


@contextmanager
def temporary_owner_allowlist(owner: uuid.UUID) -> Iterator[None]:
    key = "MEMORY_V1_V5_SHADOW_USER_IDS"
    original = os.environ.get(key)
    values: set[str] = set()
    for item in str(original or "").split(","):
        try:
            values.add(str(uuid.UUID(item.strip())))
        except (ValueError, AttributeError):
            continue
    values.add(str(owner))
    os.environ[key] = ",".join(sorted(values))
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original


def sanitized_trace(trace: dict[str, Any]) -> dict[str, Any]:
    if trace.get("status") != "ok":
        raise RuntimeError(f"shadow probe failed closed:{trace.get('status')}")
    if any(
        trace.get(field) is not False
        for field in ("prompt_injection", "answer_model_exposure", "retrieval_activation")
    ):
        raise RuntimeError("shadow probe unexpectedly influenced the answer path")
    if trace.get("database_writes") != 0 or trace.get("qdrant_writes") != 0:
        raise RuntimeError("shadow probe unexpectedly wrote state")
    if trace.get("trace_writes") != 0 or trace.get("persistable") is not False:
        raise RuntimeError("shadow probe unexpectedly became persistable")
    return {
        key: trace[key]
        for key in sorted(SAFE_TRACE_FIELDS)
        if key in trace
    }


def secure_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError("shadow probe output already exists")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    args = arguments()
    owner = validate(args)
    vector, point_id_sha256 = owner_seed_vector(args.qdrant_scroll_url, owner)
    with temporary_owner_allowlist(owner):
        raw_trace = run_memory_v1_v5_shadow_trace(
            str(owner),
            query=args.query,
            request_classification=args.request_classification,
            request_id=None,
            thread_id=None,
            query_vector=vector,
        )
    trace = sanitized_trace(raw_trace)
    if int(trace.get("selected_count") or 0) != args.expected_selected:
        raise RuntimeError("shadow probe selected count changed")
    report = {
        "version": VERSION,
        "mode": "read_only_zero_influence",
        "owner_user_id_sha256": sha256_text(str(owner)),
        "seed_point_id_sha256": point_id_sha256,
        "seed_vector_dimension": len(vector),
        "trace": trace,
        "database_writes": 0,
        "qdrant_reads": 2,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "trace_writes": 0,
        "prompt_influence": 0,
    }
    packet_sha256 = secure_write(Path(args.output), report)
    print(
        stable_json(
            {
                "version": VERSION,
                "packet_sha256": packet_sha256,
                "owner_user_id_sha256": report["owner_user_id_sha256"],
                "selected_count": trace["selected_count"],
                "rejected_counts": trace["rejected_counts"],
                "database_writes": 0,
                "qdrant_reads": 2,
                "qdrant_writes": 0,
                "external_model_calls": 0,
                "trace_writes": 0,
                "prompt_influence": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
