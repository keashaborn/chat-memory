#!/usr/bin/env python3
"""Build a private hash-locked manifest for one reviewed parent-role resolution."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import uuid

from memory_v1_v5_1_entailment_batch import private_path, sha256


CONTRACT = "memory_v1_role_only_family_apply_manifest_v5_1"
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--source-resolution-id", required=True)
    parser.add_argument("--successor-resolution-id", required=True)
    parser.add_argument("--reconcile-request-id", required=True)
    parser.add_argument("--review-request-id", required=True)
    parser.add_argument("--apply-request-id", required=True)
    parser.add_argument("--reconcile-reason", required=True)
    parser.add_argument("--review-reason", required=True)
    parser.add_argument(
        "--expected-role", choices=("family:mother", "family:father"), required=True
    )
    parser.add_argument("--required-head", required=True)
    parser.add_argument("--expected-bindings", type=int, required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def clean_reason(value: str, label: str) -> str:
    if not value or value.strip() != value or len(value) > 500:
        raise RuntimeError(f"{label} is invalid")
    return value


async def run() -> int:
    import asyncpg

    args = arguments()
    owner = str(uuid.UUID(args.owner))
    ids = {
        name: str(uuid.UUID(value))
        for name, value in {
            "source_resolution_id": args.source_resolution_id,
            "successor_resolution_id": args.successor_resolution_id,
            "reconcile_request_id": args.reconcile_request_id,
            "review_request_id": args.review_request_id,
            "apply_request_id": args.apply_request_id,
        }.items()
    }
    if len(set(ids.values())) != len(ids):
        raise RuntimeError("role resolution identities must be distinct")
    if not HEAD_RE.fullmatch(args.required_head):
        raise RuntimeError("required head is invalid")
    if args.expected_bindings < 1 or args.expected_bindings > 32:
        raise RuntimeError("expected binding budget is invalid")
    reconcile_reason = clean_reason(args.reconcile_reason, "reconcile reason")
    review_reason = clean_reason(args.review_reason, "review reason")
    output = private_path(args.output, must_exist=False)
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must authenticate as brains_app")
        tx = conn.transaction(isolation="repeatable_read", readonly=True)
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        checked = await conn.fetchrow(
            "SELECT * FROM memory.preflight_role_only_family_resolution_v5_1($1,$2,$3)",
            uuid.UUID(ids["source_resolution_id"]),
            uuid.UUID(ids["successor_resolution_id"]),
            reconcile_reason,
        )
        txid = await conn.fetchval("SELECT txid_current_if_assigned()")
        await tx.rollback()
    finally:
        await conn.close()
    proposed = checked["proposed_entity"]
    if isinstance(proposed, str):
        proposed = json.loads(proposed)
    if (
        checked["relationship_role"] != args.expected_role
        or str(checked["successor_action"]) != "create_new"
        or checked["target_entity_id"] is not None
        or proposed.get("identity_state") != "role_only"
        or proposed.get("entity_type") != "person"
        or proposed.get("canonical_name") is not None
        or txid is not None
    ):
        raise RuntimeError("parent-role preflight is outside the approved boundary")
    expected_tables = {
        "entity": 1,
        "entity_resolution_plan": 1,
        "entity_resolution_review": 1,
        "entity_resolution_apply": 1,
        "entity_role_resolution_v5_1": 1,
        "observation_entity_binding": args.expected_bindings,
        "relational_operation_request": 2,
    }
    manifest = {
        "contract_version": CONTRACT,
        "owner_user_id": owner,
        "required_head_commit": args.required_head,
        **ids,
        "reconcile_reason": reconcile_reason,
        "review_reason": review_reason,
        "expected_relationship_role": args.expected_role,
        "expected_successor_action": "create_new",
        "expected_bindings": args.expected_bindings,
        "expected_new_rows": sum(expected_tables.values()),
        "expected_table_rows": expected_tables,
        "mention_sha256": checked["mention_sha256"],
        "candidate_set_sha256": checked["candidate_set_sha256"],
        "successor_decision_sha256": checked["successor_decision_sha256"],
        "reconciliation_manifest_sha256": checked[
            "reconciliation_manifest_sha256"
        ],
        "database_writes": 0,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "prompt_influence": 0,
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = sha256(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"manifest={output}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    print("database_writes=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
