#!/usr/bin/env python3
"""Dry-run, apply, or replay one hash-locked reviewed parent-role resolution."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any
import uuid

from memory_v1_role_only_family_manifest_v5_1 import CONTRACT
from memory_v1_v5_1_entailment_batch import private_path, sha256


RESULT_CONTRACT = "memory_v1_role_only_family_apply_result_v5_1"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "apply", "replay"), required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply-result")
    return parser.parse_args()


def load_manifest(value: str) -> dict[str, Any]:
    path = private_path(value, must_exist=True)
    manifest = json.loads(path.read_text())
    expected = {
        "contract_version", "owner_user_id", "required_head_commit",
        "source_resolution_id", "successor_resolution_id",
        "reconcile_request_id", "review_request_id", "apply_request_id",
        "reconcile_reason", "review_reason", "expected_relationship_role",
        "expected_successor_action", "expected_bindings", "expected_new_rows",
        "expected_table_rows", "mention_sha256", "candidate_set_sha256",
        "successor_decision_sha256", "reconciliation_manifest_sha256",
        "database_writes", "external_model_calls", "qdrant_writes",
        "prompt_influence", "manifest_sha256",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise RuntimeError("family-role manifest fields mismatch")
    if manifest["contract_version"] != CONTRACT:
        raise RuntimeError("family-role manifest contract mismatch")
    for field in (
        "owner_user_id", "source_resolution_id", "successor_resolution_id",
        "reconcile_request_id", "review_request_id", "apply_request_id",
    ):
        uuid.UUID(manifest[field])
    if manifest["expected_successor_action"] == "create_new":
        expected_tables = {
            "entity": 1,
            "entity_resolution_plan": 1,
            "entity_resolution_review": 1,
            "entity_resolution_apply": 1,
            "entity_role_resolution_v5_1": 1,
            "observation_entity_binding": manifest["expected_bindings"],
            "relational_operation_request": 2,
        }
    elif manifest["expected_successor_action"] == "link_existing":
        expected_tables = {
            "entity": 0,
            "entity_resolution_candidate": 1,
            "entity_resolution_plan": 1,
            "entity_resolution_review": 1,
            "entity_resolution_apply": 1,
            "entity_role_resolution_v5_1": 1,
            "observation_entity_binding": manifest["expected_bindings"],
            "relational_operation_request": 2,
        }
    else:
        raise RuntimeError("family-role successor action is invalid")
    if (
        manifest["expected_relationship_role"] not in {"family:mother", "family:father"}
        or manifest["expected_table_rows"] != expected_tables
        or manifest["expected_new_rows"] != sum(expected_tables.values())
        or any(manifest[field] != 0 for field in (
            "database_writes", "external_model_calls", "qdrant_writes", "prompt_influence"
        ))
        or manifest["manifest_sha256"] != sha256({
            key: item for key, item in manifest.items() if key != "manifest_sha256"
        })
    ):
        raise RuntimeError("family-role manifest boundary or hash mismatch")
    return manifest


def load_apply_result(value: str, manifest: dict[str, Any]) -> dict[str, Any]:
    path = private_path(value, must_exist=True)
    result = json.loads(path.read_text())
    required = {
        "contract_version", "mode", "owner_user_id", "manifest_sha256",
        "outcome", "rows_written", "bindings_created", "review_id",
        "review_authorization_manifest_sha256", "apply_manifest_sha256",
        "applied_entity_id", "external_model_calls", "qdrant_writes",
        "retrieval_activated", "prompt_influence_activated", "result_sha256",
    }
    if (
        not isinstance(result, dict) or set(result) != required
        or result["contract_version"] != RESULT_CONTRACT
        or result["mode"] != "apply"
        or result["owner_user_id"] != manifest["owner_user_id"]
        or result["manifest_sha256"] != manifest["manifest_sha256"]
        or result["result_sha256"] != sha256({
            key: item for key, item in result.items() if key != "result_sha256"
        })
    ):
        raise RuntimeError("family-role apply result mismatch")
    return result


async def run() -> int:
    import asyncpg

    args = arguments()
    manifest = load_manifest(args.manifest)
    output = private_path(args.output, must_exist=False)
    if os.environ.get("MEMORY_V1_REQUIRED_HEAD") != manifest["required_head_commit"]:
        raise RuntimeError("runtime head does not match family-role manifest")
    if args.mode == "apply" and os.environ.get("MEMORY_V1_ROLE_ONLY_FAMILY_APPLY") != "authorized":
        raise RuntimeError("family-role apply capability is absent")
    prior = None
    if args.mode == "replay":
        if not args.apply_result:
            raise RuntimeError("replay requires the apply result")
        prior = load_apply_result(args.apply_result, manifest)
    elif args.apply_result:
        raise RuntimeError("apply result is accepted only for replay")
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    review_id = None
    review_auth = None
    apply_manifest = None
    applied_entity = None
    bindings = 0
    checked_target_entity = None
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must authenticate as brains_app")
        tx = conn.transaction(isolation="serializable")
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", manifest["owner_user_id"])
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"{manifest['owner_user_id']}|{CONTRACT}",
        )
        if args.mode != "replay":
            checked = await conn.fetchrow(
                "SELECT * FROM memory.preflight_role_only_family_resolution_v5_1($1,$2,$3)",
                uuid.UUID(manifest["source_resolution_id"]),
                uuid.UUID(manifest["successor_resolution_id"]),
                manifest["reconcile_reason"],
            )
            if any((
                checked["relationship_role"] != manifest["expected_relationship_role"],
                str(checked["successor_action"]) != manifest["expected_successor_action"],
                checked["mention_sha256"] != manifest["mention_sha256"],
                checked["candidate_set_sha256"] != manifest["candidate_set_sha256"],
                checked["successor_decision_sha256"] != manifest["successor_decision_sha256"],
                checked["reconciliation_manifest_sha256"] != manifest["reconciliation_manifest_sha256"],
            )):
                raise RuntimeError("family-role source drifted after manifest creation")
            checked_target_entity = (
                str(checked["target_entity_id"])
                if checked["target_entity_id"] is not None else None
            )
            if (
                manifest["expected_successor_action"] == "link_existing"
                and checked_target_entity is None
            ):
                raise RuntimeError("family-role link target disappeared")
        reconciled = await conn.fetchrow(
            "SELECT * FROM memory.reconcile_role_only_family_resolution_v5_1($1,$2,$3,$4,$5)",
            uuid.UUID(manifest["reconcile_request_id"]),
            uuid.UUID(manifest["source_resolution_id"]),
            uuid.UUID(manifest["successor_resolution_id"]),
            manifest["reconcile_reason"], manifest["reconciliation_manifest_sha256"],
        )
        expected_outcome = "replayed" if args.mode == "replay" else "applied"
        if reconciled["outcome"] != expected_outcome:
            raise RuntimeError("family-role reconciliation outcome drifted")
        if args.mode == "replay":
            review_id = uuid.UUID(prior["review_id"])
            review_auth = prior["review_authorization_manifest_sha256"]
            apply_manifest = prior["apply_manifest_sha256"]
        else:
            review_checked = await conn.fetchrow(
                "SELECT * FROM memory.preflight_entity_resolution_review_v5_1($1,'approved',$2)",
                uuid.UUID(manifest["successor_resolution_id"]), manifest["review_reason"],
            )
            review_auth = review_checked["authorization_manifest_sha256"]
        reviewed = await conn.fetchrow(
            "SELECT * FROM memory.review_entity_resolution_v5_1($1,$2,'approved',$3,$4)",
            uuid.UUID(manifest["review_request_id"]),
            uuid.UUID(manifest["successor_resolution_id"]),
            manifest["review_reason"], review_auth,
        )
        if reviewed["outcome"] != expected_outcome:
            raise RuntimeError("family-role review outcome drifted")
        if args.mode != "replay":
            review_id = reviewed["review_id"]
            apply_checked = await conn.fetchrow(
                "SELECT * FROM memory.preflight_role_only_family_apply_v5_1($1,$2)",
                uuid.UUID(manifest["successor_resolution_id"]), review_id,
            )
            apply_manifest = apply_checked["apply_manifest_sha256"]
        applied = await conn.fetchrow(
            "SELECT * FROM memory.apply_role_only_family_resolution_v5_1($1,$2,$3,$4)",
            uuid.UUID(manifest["apply_request_id"]),
            uuid.UUID(manifest["successor_resolution_id"]),
            review_id, apply_manifest,
        )
        if applied["outcome"] != expected_outcome:
            raise RuntimeError("family-role apply outcome drifted")
        bindings = int(applied["bindings_created"])
        if bindings != (0 if args.mode == "replay" else manifest["expected_bindings"]):
            raise RuntimeError("family-role binding budget drifted")
        applied_entity = str(applied["applied_entity_id"])
        expected_applied_entity = (
            prior["applied_entity_id"]
            if args.mode == "replay"
            else checked_target_entity
        )
        if (
            manifest["expected_successor_action"] == "link_existing"
            and applied_entity != expected_applied_entity
        ):
            raise RuntimeError("family-role apply selected a different entity")
        if args.mode == "preflight":
            await tx.rollback()
        else:
            await tx.commit()
    finally:
        await conn.close()
    rows_written = manifest["expected_new_rows"] if args.mode == "apply" else 0
    result = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.mode,
        "owner_user_id": manifest["owner_user_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "outcome": "preflight" if args.mode == "preflight" else expected_outcome,
        "rows_written": rows_written,
        "bindings_created": bindings,
        "review_id": str(review_id),
        "review_authorization_manifest_sha256": review_auth,
        "apply_manifest_sha256": apply_manifest,
        "applied_entity_id": applied_entity,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }
    result["result_sha256"] = sha256(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"mode={args.mode}")
    print(f"outcome={result['outcome']}")
    print(f"rows_written={rows_written}")
    print(f"bindings_created={bindings}")
    print(f"result={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
