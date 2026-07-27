#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from pathlib import Path

import asyncpg


CONTRACT = "memory_v1_v5_2_terminal_reconciliation_manifest_v1"
POLICY = "memory_v1_v5_2_terminal_reconciliation_policy_v1"
OP_NAMESPACE = uuid.UUID("4e8a459f-832a-5e61-9cce-2c0c1570d82d")


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def operation_id(kind: str, owner: str, basis: str) -> str:
    return str(uuid.uuid5(OP_NAMESPACE, f"{kind}|{owner}|{basis}"))


async def set_actor(conn: asyncpg.Connection, owner: str) -> None:
    await conn.execute(
        "SELECT set_config('app.user_id', $1, true)",
        owner,
    )


async def plan_async(args: argparse.Namespace) -> None:
    migration_sha = file_sha256(args.migration)
    conn = await asyncpg.connect(args.dsn)
    try:
        async with conn.transaction():
            await set_actor(conn, args.owner)
            rows = await conn.fetch(
                    """
                    SELECT
                      job_id::text,
                      original_event_id::text,
                      superseding_event_id::text,
                      original_event_sha256,
                      superseding_event_sha256,
                      supersession_basis_sha256
                    FROM memory.plan_owner_v5_local_inference_supersession_v1()
                    ORDER BY original_event_id
                    """
            )
            supersessions = []
            for row in rows:
                item = {
                    "job_id": row[0],
                    "original_event_id": row[1],
                    "superseding_event_id": row[2],
                    "original_event_sha256": row[3],
                    "superseding_event_sha256": row[4],
                    "supersession_basis_sha256": row[5],
                }
                item["operation_id"] = operation_id(
                    "outcome_supersession", args.owner, row[5]
                )
                supersessions.append(item)

            rows = await conn.fetch(
                    """
                    SELECT
                      packet_id::text,
                      job_id::text,
                      evidence_id::text,
                      source_completion_event_id::text,
                      source_route_event_id::text,
                      source_disposition_id::text,
                      inventory_set,
                      target_disposition,
                      normalized_reason_code,
                      precedence_rank,
                      precedence_code,
                      evidence_content_sha256,
                      validator_packet_sha256,
                      packet_storage_sha256,
                      source_completion_event_sha256,
                      source_classification_sha256,
                      reconciliation_basis_sha256
                    FROM memory.plan_owner_v5_local_terminal_reconciliation_v1(
                      500
                    )
                    ORDER BY precedence_rank, packet_id
                    """
            )
            terminal_items = []
            for row in rows:
                item = {
                    "packet_id": row[0],
                    "job_id": row[1],
                    "evidence_id": row[2],
                    "source_completion_event_id": row[3],
                    "source_route_event_id": row[4],
                    "source_disposition_id": row[5],
                    "inventory_set": row[6],
                    "target_disposition": row[7],
                    "normalized_reason_code": row[8],
                    "precedence_rank": row[9],
                    "precedence_code": row[10],
                    "evidence_content_sha256": row[11],
                    "validator_packet_sha256": row[12],
                    "packet_storage_sha256": row[13],
                    "source_completion_event_sha256": row[14],
                    "source_classification_sha256": row[15],
                    "reconciliation_basis_sha256": row[16],
                }
                item["operation_id"] = operation_id(
                    "terminal_reconciliation", args.owner, row[16]
                )
                terminal_items.append(item)
    finally:
        await conn.close()

    inventory_counts: dict[str, int] = {}
    disposition_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for item in terminal_items:
        for output, key in (
            (inventory_counts, item["inventory_set"]),
            (disposition_counts, item["target_disposition"]),
            (reason_counts, item["normalized_reason_code"]),
        ):
            output[key] = output.get(key, 0) + 1

    expected = {
        "supersessions": 10,
        "terminal_transitions": 69,
        "inventory_counts": {
            "deferral_only": 1,
            "terminal_no_stage_verified": 62,
            "terminal_skipped": 6,
        },
        "disposition_counts": {"deferred": 1, "skipped": 68},
        "reason_counts": {
            "context_missing": 1,
            "empty_packet": 3,
            "insufficient_evidence": 35,
            "question_only": 2,
            "structured_domain": 26,
            "transient_state": 2,
        },
        "review_required_before": 147,
        "review_required_after": 78,
        "skipped_before": 58,
        "skipped_after": 127,
        "genuine_manual_remaining": 78,
    }
    actual = {
        "supersessions": len(supersessions),
        "terminal_transitions": len(terminal_items),
        "inventory_counts": inventory_counts,
        "disposition_counts": disposition_counts,
        "reason_counts": reason_counts,
    }
    for key in (
        "supersessions",
        "terminal_transitions",
        "inventory_counts",
        "disposition_counts",
        "reason_counts",
    ):
        if actual[key] != expected[key]:
            raise SystemExit(
                f"plan count mismatch for {key}: "
                f"expected={expected[key]!r} actual={actual[key]!r}"
            )

    manifest = {
        "contract_version": CONTRACT,
        "policy_version": POLICY,
        "owner_user_id": args.owner,
        "source_production_commit": args.production_commit,
        "migration_sha256": migration_sha,
        "expected": expected,
        "supersessions": supersessions,
        "terminal_items": terminal_items,
    }
    args.output.write_bytes(canonical_bytes(manifest))
    print(
        json.dumps(
            {
                "manifest": str(args.output),
                "manifest_sha256": file_sha256(args.output),
                "supersessions": len(supersessions),
                "terminal_transitions": len(terminal_items),
                "inventory_counts": inventory_counts,
                "disposition_counts": disposition_counts,
                "reason_counts": reason_counts,
            },
            sort_keys=True,
        )
    )


async def apply_async(args: argparse.Namespace) -> None:
    actual_sha = file_sha256(args.manifest)
    if actual_sha != args.expected_manifest_sha256:
        raise SystemExit(
            "manifest hash mismatch: "
            f"expected={args.expected_manifest_sha256} actual={actual_sha}"
        )
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("contract_version") != CONTRACT:
        raise SystemExit("manifest contract mismatch")
    if manifest.get("policy_version") != POLICY:
        raise SystemExit("manifest policy mismatch")
    if manifest.get("owner_user_id") != args.owner:
        raise SystemExit("manifest owner mismatch")

    outcomes = {"supersession": {}, "terminal": {}}
    conn = await asyncpg.connect(args.dsn)
    try:
        async with conn.transaction():
            await set_actor(conn, args.owner)
            for item in manifest["supersessions"]:
                row = await conn.fetchrow(
                        """
                        SELECT supersession_id::text, apply_outcome
                        FROM memory.apply_owner_v5_local_inference_supersession_v1(
                          $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                          $5, $6, $7
                        )
                        """,
                        item["operation_id"],
                        item["job_id"],
                        item["original_event_id"],
                        item["superseding_event_id"],
                        item["original_event_sha256"],
                        item["superseding_event_sha256"],
                        item["supersession_basis_sha256"],
                    )
                outcome = row[1]
                outcomes["supersession"][outcome] = (
                    outcomes["supersession"].get(outcome, 0) + 1
                )

            for item in manifest["terminal_items"]:
                row = await conn.fetchrow(
                        """
                        SELECT
                          reconciliation_id::text,
                          job_id::text,
                          status,
                          disposition,
                          normalized_reason_code,
                          apply_outcome
                        FROM memory.finalize_owner_v5_local_terminal_reconciliation_v1(
                          $1::uuid, $2::uuid, $3, $4, $5
                        )
                        """,
                        item["operation_id"],
                        item["packet_id"],
                        item["reconciliation_basis_sha256"],
                        item["target_disposition"],
                        item["normalized_reason_code"],
                    )
                if row[2] != "skipped":
                    raise RuntimeError(
                        f"unexpected job status for {item['packet_id']}"
                    )
                outcome = row[5]
                outcomes["terminal"][outcome] = (
                    outcomes["terminal"].get(outcome, 0) + 1
                )
    finally:
        await conn.close()

    print(
        json.dumps(
            {
                "manifest_sha256": actual_sha,
                "outcomes": outcomes,
            },
            sort_keys=True,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    planner = sub.add_parser("plan")
    planner.add_argument("--dsn", required=True)
    planner.add_argument("--owner", required=True)
    planner.add_argument("--production-commit", required=True)
    planner.add_argument("--migration", required=True, type=Path)
    planner.add_argument("--output", required=True, type=Path)
    planner.set_defaults(func=plan_async)

    applier = sub.add_parser("apply")
    applier.add_argument("--dsn", required=True)
    applier.add_argument("--owner", required=True)
    applier.add_argument("--manifest", required=True, type=Path)
    applier.add_argument("--expected-manifest-sha256", required=True)
    applier.set_defaults(func=apply_async)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
