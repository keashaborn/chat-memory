#!/usr/bin/env python3
"""Apply a hash-locked, owner-scoped batch of governed V5.1 entailment decisions."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid


CONTRACT_VERSION = "memory_v1_observation_entailment_batch_v5_1"
RESULT_VERSION = "memory_v1_observation_entailment_batch_result_v5_1"
POLICY_VERSION = "memory_v1_predicate_entailment_v5_1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class EntailmentBatchError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256(value: Any) -> str:
    payload = value if isinstance(value, str) else stable_json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "apply", "replay"), required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def private_path(value: str, *, must_exist: bool) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise EntailmentBatchError("artifact is outside the private review root")
    if must_exist:
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise EntailmentBatchError("review input must be a mode-0600 regular file")
    elif path.exists():
        raise EntailmentBatchError("output path already exists")
    return path


def expected_reason(decision: str, reason: str) -> bool:
    if decision == "accepted":
        return reason == "predicate_entailment_v5_1_accepted"
    return decision == "deferred" and reason in {
        "source_contradicts_predicate",
        "predicate_semantics_unresolved",
    }


def validate_source_spans(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise EntailmentBatchError("source_spans must be a non-empty list")
    for span in value:
        if not isinstance(span, dict) or set(span) != {"start", "end", "span_sha256"}:
            raise EntailmentBatchError("source span fields mismatch")
        if (
            not isinstance(span["start"], int)
            or not isinstance(span["end"], int)
            or span["start"] < 0
            or span["end"] <= span["start"]
            or not isinstance(span["span_sha256"], str)
            or not SHA_RE.fullmatch(span["span_sha256"])
        ):
            raise EntailmentBatchError("source span is invalid")


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected_keys = {
        "contract_version",
        "owner_user_id",
        "required_head_commit",
        "assessor_type",
        "assessor_ref",
        "review_sha256",
        "expected_new_rows",
        "expected_table_rows",
        "items",
        "manifest_sha256",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise EntailmentBatchError("manifest fields mismatch")
    if manifest["contract_version"] != CONTRACT_VERSION:
        raise EntailmentBatchError("manifest contract mismatch")
    uuid.UUID(manifest["owner_user_id"])
    if not HEAD_RE.fullmatch(str(manifest["required_head_commit"])):
        raise EntailmentBatchError("required head is invalid")
    if manifest["assessor_type"] not in {"system", "admin"}:
        raise EntailmentBatchError("assessor_type is invalid")
    assessor = manifest["assessor_ref"]
    if not isinstance(assessor, str) or assessor.strip() != assessor or not assessor or len(assessor) > 500:
        raise EntailmentBatchError("assessor_ref is invalid")
    if not SHA_RE.fullmatch(str(manifest["review_sha256"])):
        raise EntailmentBatchError("review hash is invalid")
    items = manifest["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise EntailmentBatchError("batch size must be between one and twenty")
    expected_rows = len(items) * 2
    if manifest["expected_new_rows"] != expected_rows or manifest["expected_table_rows"] != {
        "observation_entailment_v5": len(items),
        "relational_operation_request": len(items),
    }:
        raise EntailmentBatchError("batch row budget mismatch")
    item_keys = {
        "observation_id",
        "request_id",
        "decision",
        "reason_code",
        "source_spans",
        "authorization_manifest_sha256",
    }
    observations: set[str] = set()
    requests: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != item_keys:
            raise EntailmentBatchError("manifest item fields mismatch")
        observation_id = str(uuid.UUID(item["observation_id"]))
        request_id = str(uuid.UUID(item["request_id"]))
        if observation_id in observations or request_id in requests:
            raise EntailmentBatchError("duplicate batch identity")
        observations.add(observation_id)
        requests.add(request_id)
        if not expected_reason(item["decision"], item["reason_code"]):
            raise EntailmentBatchError("decision and reason_code mismatch")
        validate_source_spans(item["source_spans"])
        if not SHA_RE.fullmatch(str(item["authorization_manifest_sha256"])):
            raise EntailmentBatchError("authorization manifest hash is invalid")
    calculated = sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    if manifest["manifest_sha256"] != calculated:
        raise EntailmentBatchError("manifest hash mismatch")
    return manifest


async def validate_item(conn: Any, manifest: dict[str, Any], item: dict[str, Any]) -> None:
    observation_id = uuid.UUID(item["observation_id"])
    preflight = await conn.fetchrow(
        """
        SELECT * FROM memory.preflight_observation_entailment_v5(
          $1,$2::memory.observation_entailment_decision_v5,$3,$4::jsonb,$5,$6
        )
        """,
        observation_id,
        item["decision"],
        item["reason_code"],
        stable_json(item["source_spans"]),
        manifest["assessor_type"],
        manifest["assessor_ref"],
    )
    if preflight is None or preflight["authorization_manifest_sha256"] != item["authorization_manifest_sha256"]:
        raise EntailmentBatchError("entailment authorization manifest drifted")


async def run() -> int:
    import asyncpg

    args = arguments()
    manifest_path = private_path(args.manifest, must_exist=True)
    output_path = private_path(args.output, must_exist=False)
    manifest = load_manifest(manifest_path)
    if os.environ.get("MEMORY_V1_REQUIRED_HEAD") != manifest["required_head_commit"]:
        raise EntailmentBatchError("runtime head does not match manifest")
    if args.mode == "apply" and os.environ.get("MEMORY_V1_V5_1_ENTAILMENT_APPLY") != "authorized":
        raise EntailmentBatchError("apply capability is absent")
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise EntailmentBatchError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    rows_written = 0
    outcomes: list[dict[str, Any]] = []
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise EntailmentBatchError("POSTGRES_DSN must authenticate as brains_app")
        transaction = conn.transaction(isolation="serializable", readonly=args.mode == "preflight")
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", manifest["owner_user_id"])
        if args.mode in {"apply", "replay"}:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"{manifest['owner_user_id']}|{CONTRACT_VERSION}",
            )
        for item in manifest["items"]:
            await validate_item(conn, manifest, item)
        if args.mode in {"apply", "replay"}:
            for item in manifest["items"]:
                result = await conn.fetchrow(
                    """
                    SELECT * FROM memory.record_observation_entailment_v5(
                      $1,$2,$3::memory.observation_entailment_decision_v5,
                      $4,$5::jsonb,$6,$7,$8
                    )
                    """,
                    uuid.UUID(item["request_id"]),
                    uuid.UUID(item["observation_id"]),
                    item["decision"],
                    item["reason_code"],
                    stable_json(item["source_spans"]),
                    manifest["assessor_type"],
                    manifest["assessor_ref"],
                    item["authorization_manifest_sha256"],
                )
                rows_written += int(result["rows_written"])
                outcomes.append({
                    "observation_id": item["observation_id"],
                    "decision": item["decision"],
                    "outcome": result["outcome"],
                    "rows_written": int(result["rows_written"]),
                })
        if args.mode == "preflight":
            await transaction.rollback()
        else:
            expected = manifest["expected_new_rows"] if args.mode == "apply" else 0
            if rows_written != expected:
                raise EntailmentBatchError(f"batch wrote {rows_written} rows; expected {expected}")
            await transaction.commit()
    finally:
        await conn.close()
    result = {
        "contract_version": RESULT_VERSION,
        "mode": args.mode,
        "owner_user_id": manifest["owner_user_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "item_count": len(manifest["items"]),
        "rows_written": rows_written,
        "outcomes": outcomes,
        "external_model_calls": 0,
        "local_model_calls": 0,
        "claims_written": 0,
        "qdrant_writes": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }
    result["result_sha256"] = sha256(result)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_path.chmod(0o600)
    print(f"mode={args.mode}")
    print(f"rows_written={rows_written}")
    print(f"result={output_path}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
