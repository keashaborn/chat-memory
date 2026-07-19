#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from memory_v1_projection_v5_contract_test import sha256


MANIFEST_CONTRACT = "memory_v1_claim_projection_apply_batch_manifest_v1"
RESULT_CONTRACT = "memory_v1_claim_projection_apply_batch_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
EXPECTED_ROWS_PER_ITEM = {
    "claim": 1,
    "claim_revision": 2,
    "claim_observation": 1,
    "projection_apply_event": 1,
    "projection_dispatch_v5": 1,
    "claim_assessment_review_v5": 1,
    "claim_assessment": 1,
    "claim_assessment_apply_v5": 1,
    "relational_operation_request": 2,
    "projection_outbox": 1,
}


class ApplyBatchError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "apply", "replay"), required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply-result")
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ApplyBatchError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ApplyBatchError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ApplyBatchError("input must be a private regular file")
    return path


def load_hashed(path: Path, hash_field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get(hash_field) != sha256(
        {key: item for key, item in value.items() if key != hash_field}
    ):
        raise ApplyBatchError(f"{path.name} content hash mismatch")
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    value = load_hashed(path, "manifest_sha256")
    keys = {
        "contract_version", "owner_user_id", "required_head_commit",
        "review_manifest_path", "review_manifest_file_sha256", "review_manifest_sha256",
        "review_result_path", "review_result_file_sha256", "review_result_sha256",
        "assessment", "expected_insert_rows", "expected_mutated_rows",
        "expected_table_rows", "items", "manifest_sha256",
    }
    if set(value) != keys or value.get("contract_version") != MANIFEST_CONTRACT:
        raise ApplyBatchError("manifest contract or fields mismatch")
    uuid.UUID(value["owner_user_id"])
    item_count = len(value["items"]) if isinstance(value["items"], list) else 0
    expected_table_rows = {
        table: rows * item_count
        for table, rows in EXPECTED_ROWS_PER_ITEM.items()
    }
    if (
        not 1 <= item_count <= 32
        or value["expected_table_rows"] != expected_table_rows
        or value["expected_insert_rows"] != sum(expected_table_rows.values())
        or value["expected_mutated_rows"]
        != sum(expected_table_rows.values()) + item_count
    ):
        raise ApplyBatchError("manifest row budget mismatch")
    for field in ("review_manifest_path", "review_result_path"):
        source = private_path(value[field])
        if file_sha256(source) != value[field.replace("_path", "_file_sha256")]:
            raise ApplyBatchError("source review artifact file hash mismatch")
    return value


def validate_items(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    keys = {
        "plan_id", "projection_ref", "predicate", "canonical_text_sha256",
        "projection_sha256", "semantic_key_sha256", "review_id",
        "projection_request_id", "projection_apply_manifest_sha256",
        "assessment_review_request_id", "assessment_apply_request_id",
    }
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for item in manifest["items"]:
        if not isinstance(item, dict) or set(item) != keys:
            raise ApplyBatchError("manifest item fields mismatch")
        plan_id = str(uuid.UUID(item["plan_id"]))
        if plan_id in seen or item["projection_ref"] != "p01":
            raise ApplyBatchError("duplicate or invalid apply identity")
        seen.add(plan_id)
        for field in (
            "review_id", "projection_request_id", "assessment_review_request_id",
            "assessment_apply_request_id",
        ):
            uuid.UUID(item[field])
        for field in (
            "canonical_text_sha256", "projection_sha256", "semantic_key_sha256",
            "projection_apply_manifest_sha256",
        ):
            if len(item[field]) != 64 or any(c not in "0123456789abcdef" for c in item[field]):
                raise ApplyBatchError("invalid item hash")
        items.append(item)
    return sorted(items, key=lambda item: item["plan_id"])


def validate_replay_result(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    value = load_hashed(path, "result_sha256")
    if (
        value.get("contract_version") != RESULT_CONTRACT
        or value.get("mode") != "apply"
        or value.get("manifest_sha256") != manifest["manifest_sha256"]
        or value.get("insert_rows") != manifest["expected_insert_rows"]
        or value.get("mutated_rows") != manifest["expected_mutated_rows"]
        or len(value.get("outcomes", [])) != len(manifest["items"])
    ):
        raise ApplyBatchError("apply result cannot authorize replay")
    return value


async def projection_preflight(conn: Any, item: dict[str, Any]) -> Any:
    row = await conn.fetchrow(
        "SELECT * FROM memory.preflight_projection_apply_v5($1,$2,$3)",
        uuid.UUID(item["plan_id"]), item["projection_ref"], uuid.UUID(item["review_id"]),
    )
    if (
        row["lane"] != "claim"
        or row["target_action"] != "create"
        or row["review_state"] != "manual_review_required"
        or row["current_revision_number"] != 0
        or str(row["review_id"]) != item["review_id"]
        or row["apply_manifest_sha256"] != item["projection_apply_manifest_sha256"]
    ):
        raise ApplyBatchError("projection apply preflight drifted")
    return row


async def insert_outbox(conn: Any, owner: str, claim_id: str, revision: int) -> tuple[str, int]:
    payload = json.dumps(
        {"claim_id": claim_id, "revision_number": revision},
        sort_keys=True, separators=(",", ":"),
    )
    inserted = await conn.fetchrow(
        """INSERT INTO memory.projection_outbox(
             owner_user_id,aggregate_type,aggregate_id,operation,payload
           ) VALUES($1,'claim',$2,'upsert',$3::jsonb)
           ON CONFLICT(owner_user_id,aggregate_type,aggregate_id,operation) DO NOTHING
           RETURNING outbox_id""",
        uuid.UUID(owner), uuid.UUID(claim_id), payload,
    )
    if inserted:
        return str(inserted["outbox_id"]), 1
    existing = await conn.fetchrow(
        """SELECT outbox_id,payload FROM memory.projection_outbox
           WHERE owner_user_id=$1 AND aggregate_type='claim'
             AND aggregate_id=$2 AND operation='upsert'""",
        uuid.UUID(owner), uuid.UUID(claim_id),
    )
    existing_payload = existing["payload"] if existing else None
    if isinstance(existing_payload, str):
        existing_payload = json.loads(existing_payload)
    if not existing or existing_payload != {"claim_id": claim_id, "revision_number": revision}:
        raise ApplyBatchError("existing projection outbox payload mismatch")
    return str(existing["outbox_id"]), 0


async def run() -> int:
    import asyncpg

    args = arguments()
    manifest_path = private_path(args.manifest)
    output = private_path(args.output, output=True)
    manifest = load_manifest(manifest_path)
    items = validate_items(manifest)
    if os.environ.get("MEMORY_V1_REQUIRED_HEAD", "") != manifest["required_head_commit"]:
        raise ApplyBatchError("runtime head does not match manifest")
    if args.mode == "apply" and os.environ.get(
        "MEMORY_V1_CLAIM_PROJECTION_APPLY_BATCH", ""
    ) != "authorized":
        raise ApplyBatchError("apply mode requires the bounded authorization gate")
    prior: dict[str, dict[str, Any]] = {}
    prior_path: Path | None = None
    if args.mode == "replay":
        if not args.apply_result:
            raise ApplyBatchError("replay requires the immutable apply result")
        prior_path = private_path(args.apply_result)
        prior_value = validate_replay_result(prior_path, manifest)
        prior = {item["plan_id"]: item for item in prior_value["outcomes"]}
        if prior.keys() != {item["plan_id"] for item in items}:
            raise ApplyBatchError("replay result plan set mismatch")
    elif args.apply_result:
        raise ApplyBatchError("apply result is accepted only in replay mode")

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ApplyBatchError("POSTGRES_DSN is required")
    assessment = manifest["assessment"]
    reason_codes_text = json.dumps(assessment["reason_codes"], separators=(",", ":"))
    conn = await asyncpg.connect(dsn, command_timeout=90)
    outcomes: list[dict[str, Any]] = []
    insert_rows = 0
    mutated_rows = 0
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ApplyBatchError("POSTGRES_DSN must authenticate as brains_app")
        tx = conn.transaction(readonly=args.mode == "preflight", isolation="serializable")
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", manifest["owner_user_id"])
        if args.mode != "replay":
            for item in items:
                await projection_preflight(conn, item)
        if args.mode in {"apply", "replay"}:
            for item in items:
                projected = await conn.fetchrow(
                    "SELECT * FROM memory.apply_projection_v5($1,$2,$3,$4,$5)",
                    uuid.UUID(item["projection_request_id"]), uuid.UUID(item["plan_id"]),
                    item["projection_ref"], uuid.UUID(item["review_id"]),
                    item["projection_apply_manifest_sha256"],
                )
                expected_projection = "applied" if args.mode == "apply" else "replayed"
                if projected["outcome"] != expected_projection or projected["lane"] != "claim":
                    raise ApplyBatchError("projection apply outcome mismatch")
                claim_id = str(projected["aggregate_id"])
                if args.mode == "apply":
                    if projected["rows_written"] != 5 or projected["revision_number"] != 1:
                        raise ApplyBatchError("initial claim materialization row budget mismatch")
                    review_preflight = await conn.fetchrow(
                        """SELECT * FROM memory.preflight_claim_assessment_review_v5(
                          $1,'promote_supported'::memory.claim_assessment_action_v5,
                          $2::numeric,$3::numeric,$4::numeric,$5::numeric,
                          $6::jsonb,$7,$8,$9)""",
                        uuid.UUID(claim_id), assessment["support_score"],
                        assessment["opposition_score"], assessment["claim_confidence"],
                        assessment["assessment_confidence"], reason_codes_text,
                        assessment["rationale"], assessment["reviewer_type"],
                        assessment["reviewer_ref"],
                    )
                    if review_preflight["from_status"] != "candidate" or review_preflight["target_status"] != "supported":
                        raise ApplyBatchError("claim assessment transition mismatch")
                    reviewed = await conn.fetchrow(
                        """SELECT * FROM memory.review_claim_assessment_v5(
                          $1,$2,'promote_supported'::memory.claim_assessment_action_v5,
                          $3::numeric,$4::numeric,$5::numeric,$6::numeric,
                          $7::jsonb,$8,$9,$10,$11)""",
                        uuid.UUID(item["assessment_review_request_id"]), uuid.UUID(claim_id),
                        assessment["support_score"], assessment["opposition_score"],
                        assessment["claim_confidence"], assessment["assessment_confidence"],
                        reason_codes_text, assessment["rationale"], assessment["reviewer_type"],
                        assessment["reviewer_ref"], review_preflight["authorization_manifest_sha256"],
                    )
                    if reviewed["outcome"] != "applied":
                        raise ApplyBatchError("claim assessment review was not applied")
                    assessment_review_id = str(reviewed["review_id"])
                    assessment_preflight = await conn.fetchrow(
                        "SELECT * FROM memory.preflight_claim_assessment_apply_v5($1,$2)",
                        uuid.UUID(claim_id), uuid.UUID(assessment_review_id),
                    )
                    assessment_apply_manifest = assessment_preflight["apply_manifest_sha256"]
                else:
                    previous = prior[item["plan_id"]]
                    if previous.get("claim_id") != claim_id:
                        raise ApplyBatchError("replayed claim identity mismatch")
                    assessment_review_id = str(uuid.UUID(previous["assessment_review_id"]))
                    assessment_apply_manifest = previous["assessment_apply_manifest_sha256"]
                assessed = await conn.fetchrow(
                    "SELECT * FROM memory.apply_claim_assessment_v5($1,$2,$3,$4)",
                    uuid.UUID(item["assessment_apply_request_id"]), uuid.UUID(claim_id),
                    uuid.UUID(assessment_review_id), assessment_apply_manifest,
                )
                expected_assessment = "applied" if args.mode == "apply" else "replayed"
                if (
                    assessed["outcome"] != expected_assessment
                    or assessed["resulting_revision_number"] != 2
                    or assessed["rows_written"] != (5 if args.mode == "apply" else 0)
                ):
                    raise ApplyBatchError("claim assessment apply outcome mismatch")
                outbox_id, outbox_rows = await insert_outbox(
                    conn, manifest["owner_user_id"], claim_id, 2
                )
                if outbox_rows != (1 if args.mode == "apply" else 0):
                    raise ApplyBatchError("projection outbox row budget mismatch")
                if args.mode == "apply":
                    insert_rows += 12
                    mutated_rows += 13
                outcomes.append(
                    {
                        "plan_id": item["plan_id"], "predicate": item["predicate"],
                        "claim_id": claim_id, "claim_revision_number": 2,
                        "projection_apply_event_id": str(projected["apply_event_id"]),
                        "projection_dispatch_created": args.mode == "apply",
                        "assessment_review_id": assessment_review_id,
                        "assessment_review_manifest_sha256": (
                            review_preflight["authorization_manifest_sha256"]
                            if args.mode == "apply" else prior[item["plan_id"]]["assessment_review_manifest_sha256"]
                        ),
                        "assessment_apply_manifest_sha256": assessment_apply_manifest,
                        "assessment_id": str(assessed["assessment_id"]),
                        "assessment_event_id": str(assessed["event_id"]),
                        "outbox_id": outbox_id,
                        "outcome": "applied" if args.mode == "apply" else "replayed",
                    }
                )
        if args.mode == "preflight":
            await tx.rollback()
        else:
            expected_insert = manifest["expected_insert_rows"] if args.mode == "apply" else 0
            expected_mutated = manifest["expected_mutated_rows"] if args.mode == "apply" else 0
            if insert_rows != expected_insert or mutated_rows != expected_mutated:
                raise ApplyBatchError("aggregate row budget mismatch")
            await tx.commit()
    finally:
        await conn.close()

    result = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.mode,
        "owner_user_id": manifest["owner_user_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "apply_result_file_sha256": file_sha256(prior_path) if prior_path else None,
        "item_count": len(items),
        "insert_rows": insert_rows,
        "mutated_rows": mutated_rows,
        "outcomes": outcomes,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "claims_supported": len(items) if args.mode == "apply" else 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }
    result["result_sha256"] = sha256(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"mode={args.mode}")
    print(f"insert_rows={insert_rows}")
    print(f"mutated_rows={mutated_rows}")
    print(f"result={output}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
