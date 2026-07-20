#!/usr/bin/env python3
"""Preflight, apply, or replay a bounded V5.1 epistemic snapshot manifest."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import uuid

import asyncpg


CONTRACT = "memory_v1_pattern_salience_snapshot_apply_manifest_v5_1"
OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def secure_write(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "contract_version", "owner_user_id_sha256", "source_report_path",
        "source_report_file_sha256", "source_report_sha256", "policy_version",
        "items", "expected_new_rows", "write_budget", "manifest_sha256",
    }
    if set(value) != required or value.get("contract_version") != CONTRACT:
        raise RuntimeError("apply manifest envelope is invalid")
    expected = sha256_text(stable_json({
        key: item for key, item in value.items() if key != "manifest_sha256"
    }))
    if value.get("manifest_sha256") != expected:
        raise RuntimeError("apply manifest hash mismatch")
    if value.get("owner_user_id_sha256") != sha256_text(str(OWNER)):
        raise RuntimeError("apply manifest owner mismatch")
    source = Path(value["source_report_path"])
    if sha256_file(source) != value["source_report_file_sha256"]:
        raise RuntimeError("source report file hash mismatch")
    report = json.loads(source.read_text(encoding="utf-8"))
    if report.get("report_sha256") != value["source_report_sha256"]:
        raise RuntimeError("source report semantic hash mismatch")
    if report.get("target_snapshot_candidates") != [
        {"request_id": item["request_id"], "packet": item["packet"]}
        for item in value["items"]
    ]:
        raise RuntimeError("manifest items differ from the reviewed report")
    if not 1 <= len(value["items"]) <= 32:
        raise RuntimeError("manifest item count is outside the bounded range")
    link_count = sum(
        len(item["packet"]["evidence_assessment"][key])
        for item in value["items"]
        for key in (
            "supporting_observation_ids", "opposing_observation_ids",
            "qualifying_observation_ids", "corrective_observation_ids",
        )
    )
    count = len(value["items"])
    expected_rows = {
        "epistemic_target_binding_v5_1": count,
        "epistemic_assessment_snapshot_v5_1": count,
        "epistemic_assessment_observation_link_v5_1": link_count,
        "salience_feature_snapshot_v5_1": count,
        "epistemic_operation_request_v5_1": count,
        "retrieval_outcome_signal_v5_1": 0,
        "pattern_hypothesis_v5_1": 0,
        "pattern_hypothesis_revision_v5_1": 0,
        "pattern_observation_link_v5_1": 0,
        "pattern_review_v5_1": 0,
        "pattern_review_observation_v5_1": 0,
        "pattern_operation_request_v5_1": 0,
        "pattern_apply_event_v5_1": 0,
    }
    if value["expected_new_rows"] != expected_rows:
        raise RuntimeError("manifest row bounds mismatch")
    if value["write_budget"] != {
        "maximum_total_rows": count * 4 + link_count,
        "other_owners": 0,
        "pattern_heads_or_revisions": 0,
        "retrieval_outcomes": 0,
        "qdrant": 0,
        "prompt_influence": 0,
    }:
        raise RuntimeError("manifest write budget mismatch")
    return value


async def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("preflight", "apply", "replay"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--apply-result", type=Path)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    if args.mode == "apply" and os.getenv(
        "MEMORY_V1_PATTERN_SALIENCE_SNAPSHOT_APPLY"
    ) != "authorized":
        raise RuntimeError("durable apply sentinel is absent")
    prior: dict[str, dict[str, str]] = {}
    if args.mode == "replay":
        if args.apply_result is None:
            raise RuntimeError("replay requires --apply-result")
        applied = json.loads(args.apply_result.read_text(encoding="utf-8"))
        if applied.get("manifest_sha256") != manifest["manifest_sha256"]:
            raise RuntimeError("apply result manifest mismatch")
        prior = {item["request_id"]: item for item in applied["results"]}

    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    results: list[dict[str, str]] = []
    transaction = conn.transaction(isolation="serializable")
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("apply runner requires brains_app")
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(OWNER))
        expected_outcome = "replayed" if args.mode == "replay" else "applied"
        for item in manifest["items"]:
            if item["packet_sha256"] != item["packet"]["packet_sha256"]:
                raise RuntimeError("manifest packet hash binding mismatch")
            row = await conn.fetchrow(
                "SELECT * FROM memory.persist_epistemic_snapshot_packet_v5_1($1,$2::jsonb)",
                uuid.UUID(item["request_id"]), stable_json(item["packet"]),
            )
            if row["apply_outcome"] != expected_outcome:
                raise RuntimeError(
                    f"unexpected persistence outcome: {row['apply_outcome']}"
                )
            result = {
                "request_id": item["request_id"],
                "target_binding_id": str(row["target_binding_id"]),
                "assessment_snapshot_id": str(row["assessment_snapshot_id"]),
                "feature_snapshot_id": str(row["feature_snapshot_id"]),
                "apply_outcome": row["apply_outcome"],
            }
            if args.mode == "replay":
                expected = prior.get(item["request_id"])
                if expected is None or any(
                    result[key] != expected[key]
                    for key in (
                        "target_binding_id", "assessment_snapshot_id",
                        "feature_snapshot_id",
                    )
                ):
                    raise RuntimeError("replay identifiers differ from apply result")
            results.append(result)
        if args.mode == "preflight":
            for item, first in zip(manifest["items"], results, strict=True):
                replay = await conn.fetchrow(
                    "SELECT * FROM memory.persist_epistemic_snapshot_packet_v5_1($1,$2::jsonb)",
                    uuid.UUID(item["request_id"]), stable_json(item["packet"]),
                )
                if replay["apply_outcome"] != "replayed" or any(
                    str(replay[key]) != first[key]
                    for key in (
                        "target_binding_id", "assessment_snapshot_id",
                        "feature_snapshot_id",
                    )
                ):
                    raise RuntimeError("preflight replay was not zero-write")
            await transaction.rollback()
        else:
            await transaction.commit()
    except BaseException:
        if conn.is_in_transaction():
            await transaction.rollback()
        raise
    finally:
        await conn.close()

    secure_write(args.output, {
        "contract_version": "memory_v1_pattern_salience_snapshot_apply_result_v5_1",
        "mode": args.mode,
        "owner_user_id_sha256": manifest["owner_user_id_sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
        "results": results,
        "persistent_writes": 0 if args.mode == "preflight" else (
            manifest["write_budget"]["maximum_total_rows"]
            if args.mode == "apply" else 0
        ),
    })
    print(stable_json({
        "outcome": f"{args.mode}_passed",
        "manifest_sha256": manifest["manifest_sha256"],
        "items": len(results),
    }))


if __name__ == "__main__":
    asyncio.run(run())
