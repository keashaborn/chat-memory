#!/usr/bin/env python3
"""Transactionally apply or replay one reviewed V5 project projection bundle."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any
import uuid

from scripts.memory_v1_projection_v5_contract_test import (
    owner_manifest_sha256,
    sha256,
    stable_json,
    validate_packet,
)
from scripts.memory_v1_v5_project_projection_preflight import load_contract


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_CONTRACT = "memory_v1_project_projection_stage_bundle_v1"
RESULT_CONTRACT = "memory_v1_project_projection_apply_result_v1"
REPLAY_CONTRACT = "memory_v1_project_projection_replay_result_v1"
REVIEWER_REF = "memory_v1_v5_project_projection_preparation_20260718"
REASON = (
    "direct user-endorsed architecture statement, exact trusted component "
    "scope, deterministic entailment accepted; phase-authorized project "
    "projection preparation"
)
REASON_CODES = [
    "direct_user_statement",
    "trusted_component_scope",
    "accepted_predicate_entailment",
    "phase_authorized_projection_preparation",
]


class ProjectProjectionApplyError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("apply", "replay"), required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--prior-result")
    return parser.parse_args()


def repository_state() -> str:
    status = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise ProjectProjectionApplyError("project apply requires a clean worktree")
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def read_private_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ProjectProjectionApplyError(f"private JSON mode mismatch: {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ProjectProjectionApplyError(f"JSON object required: {path}")
    return value


def validate_bundle(value: dict[str, Any]) -> None:
    if value.get("contract_version") != BUNDLE_CONTRACT:
        raise ProjectProjectionApplyError("project bundle contract mismatch")
    if value.get("database_writes") != 0 or value.get("qdrant_writes") != 0:
        raise ProjectProjectionApplyError("project bundle is not zero-write source")
    if value.get("external_model_calls") != 0:
        raise ProjectProjectionApplyError("project bundle used an external model")
    packet = value.get("packet")
    if not isinstance(packet, dict):
        raise ProjectProjectionApplyError("project bundle packet is absent")
    validate_packet(packet, value["owner_user_id"], load_contract())
    if value.get("packet_text") != stable_json(packet):
        raise ProjectProjectionApplyError("project bundle packet text mismatch")
    if value.get("packet_sha256") != packet.get("packet_sha256"):
        raise ProjectProjectionApplyError("project bundle packet hash mismatch")
    expected_manifest = owner_manifest_sha256(
        value["owner_user_id"], value["packet_sha256"]
    )
    if value.get("owner_manifest_sha256") != expected_manifest:
        raise ProjectProjectionApplyError("project owner manifest mismatch")
    expected_bundle_hash = sha256(
        {key: item for key, item in value.items() if key != "bundle_sha256"}
    )
    if value.get("bundle_sha256") != expected_bundle_hash:
        raise ProjectProjectionApplyError("project bundle hash mismatch")
    projection = packet["projections"][0]
    if (
        projection["lane"] != "project_knowledge"
        or projection["target"]["action"] != "create"
        or projection["review"]["state"] != "manual_review_required"
        or projection["payload"]["binding_source"]
        != "trusted_component_registry"
        or projection["payload"]["surface_policy"]
        != "exact_project_scope_only"
    ):
        raise ProjectProjectionApplyError("project bundle policy mismatch")
    uuid.UUID(value["owner_user_id"])
    uuid.UUID(value["plan_id"])


def secure_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(payload).hexdigest()


async def assert_other_owner_denied(
    conn: Any, other_owner: str, observation_id: uuid.UUID
) -> None:
    transaction = conn.transaction(readonly=True)
    await transaction.start()
    try:
        await conn.execute("SELECT set_config('app.user_id',$1,true)", other_owner)
        try:
            await conn.fetchrow(
                "SELECT * FROM memory.preflight_project_projection_source_v5($1)",
                observation_id,
            )
        except Exception as error:
            if getattr(error, "sqlstate", None) != "P0002":
                raise
        else:
            raise ProjectProjectionApplyError(
                "cross-owner project projection source unexpectedly resolved"
            )
    finally:
        await transaction.rollback()


async def apply_bundle(
    conn: Any,
    bundle: dict[str, Any],
    request_id: uuid.UUID,
    commit: str,
) -> dict[str, Any]:
    owner = bundle["owner_user_id"]
    plan_id = uuid.UUID(bundle["plan_id"])
    observation_id = uuid.UUID(bundle["source_snapshot"]["observation_id"])
    other_owner = "557ea042-cb82-48f8-9429-472e96c957ef"
    reason_codes_text = stable_json(REASON_CODES)
    await assert_other_owner_denied(conn, other_owner, observation_id)
    transaction = conn.transaction()
    await transaction.start()
    try:
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        staged = await conn.fetchrow(
            "SELECT * FROM memory.stage_project_projection_plan_v5($1,$2,$3)",
            plan_id,
            bundle["packet_text"],
            bundle["owner_manifest_sha256"],
        )
        if staged["outcome"] != "applied" or staged["rows_written"] != 4:
            raise ProjectProjectionApplyError("project stage did not create four rows")
        review_preflight = await conn.fetchrow(
            """SELECT * FROM memory.preflight_projection_review_v5(
                $1,'p01','authorized'::memory.projection_review_decision_v5,
                'system',$2,$3,$4::jsonb
            )""",
            plan_id,
            REVIEWER_REF,
            REASON,
            reason_codes_text,
        )
        reviewed = await conn.fetchrow(
            """SELECT * FROM memory.review_projection_v5(
                $1,'p01','authorized'::memory.projection_review_decision_v5,
                'system',$2,$3,$4::jsonb,$5
            )""",
            plan_id,
            REVIEWER_REF,
            REASON,
            reason_codes_text,
            review_preflight["authorization_manifest_sha256"],
        )
        if reviewed["outcome"] != "applied" or reviewed["rows_written"] != 1:
            raise ProjectProjectionApplyError("project review did not create one row")
        apply_preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_apply_v5($1,'p01',$2)",
            plan_id,
            reviewed["review_id"],
        )
        applied = await conn.fetchrow(
            "SELECT * FROM memory.apply_projection_v5($1,$2,'p01',$3,$4)",
            request_id,
            plan_id,
            reviewed["review_id"],
            apply_preflight["apply_manifest_sha256"],
        )
        if (
            applied["outcome"] != "applied"
            or str(applied["lane"]) != "project_knowledge"
            or applied["revision_number"] != 1
            or applied["rows_written"] != 6
        ):
            raise ProjectProjectionApplyError("project materialization result drifted")
        applied_result = applied["result"]
        if isinstance(applied_result, str):
            applied_result = json.loads(applied_result)
        review_replay = await conn.fetchrow(
            """SELECT * FROM memory.review_projection_v5(
                $1,'p01','authorized'::memory.projection_review_decision_v5,
                'system',$2,$3,$4::jsonb,$5
            )""",
            plan_id,
            REVIEWER_REF,
            REASON,
            reason_codes_text,
            review_preflight["authorization_manifest_sha256"],
        )
        apply_replay = await conn.fetchrow(
            "SELECT * FROM memory.apply_projection_v5($1,$2,'p01',$3,$4)",
            request_id,
            plan_id,
            reviewed["review_id"],
            apply_preflight["apply_manifest_sha256"],
        )
        if (
            review_replay["outcome"] != "replayed"
            or review_replay["rows_written"] != 0
            or apply_replay["outcome"] != "replayed"
            or apply_replay["rows_written"] != 0
        ):
            raise ProjectProjectionApplyError("in-transaction replay was not zero-write")
        result = {
            "contract_version": RESULT_CONTRACT,
            "mode": "apply",
            "head_commit": commit,
            "owner_user_id": owner,
            "plan_id": str(plan_id),
            "projection_ref": "p01",
            "request_id": str(request_id),
            "bundle_sha256": bundle["bundle_sha256"],
            "packet_sha256": bundle["packet_sha256"],
            "review_id": str(reviewed["review_id"]),
            "review_manifest_sha256": review_preflight[
                "authorization_manifest_sha256"
            ],
            "apply_manifest_sha256": apply_preflight["apply_manifest_sha256"],
            "apply_event_id": str(applied["apply_event_id"]),
            "project_id": str(applied_result["project_id"]),
            "knowledge_id": str(applied["aggregate_id"]),
            "revision_id": str(applied["revision_id"]),
            "revision_number": applied["revision_number"],
            "component_key": applied_result["component_key"],
            "binding_source": applied_result["binding_source"],
            "rows_created": {
                "projection_stage": 4,
                "projection_review": 1,
                "project_materialization": 5,
                "total": 10,
            },
            "apply_rows_touched": applied["rows_written"],
            "in_transaction_zero_write_replay": True,
            "cross_owner_rejected": True,
            "external_model_calls": 0,
            "qdrant_writes": 0,
            "retrieval_activated": False,
            "prompt_influence": False,
        }
        await transaction.commit()
        return result
    except BaseException:
        await transaction.rollback()
        raise


async def replay_bundle(
    conn: Any,
    bundle: dict[str, Any],
    request_id: uuid.UUID,
    prior: dict[str, Any],
    commit: str,
) -> dict[str, Any]:
    if (
        prior.get("contract_version") != RESULT_CONTRACT
        or prior.get("owner_user_id") != bundle["owner_user_id"]
        or prior.get("plan_id") != bundle["plan_id"]
        or prior.get("request_id") != str(request_id)
        or prior.get("bundle_sha256") != bundle["bundle_sha256"]
    ):
        raise ProjectProjectionApplyError("prior project apply result mismatch")
    owner = bundle["owner_user_id"]
    plan_id = uuid.UUID(bundle["plan_id"])
    observation_id = uuid.UUID(bundle["source_snapshot"]["observation_id"])
    transaction = conn.transaction()
    await transaction.start()
    try:
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        staged = await conn.fetchrow(
            "SELECT * FROM memory.stage_project_projection_plan_v5($1,$2,$3)",
            plan_id,
            bundle["packet_text"],
            bundle["owner_manifest_sha256"],
        )
        reviewed = await conn.fetchrow(
            """SELECT * FROM memory.review_projection_v5(
                $1,'p01','authorized'::memory.projection_review_decision_v5,
                'system',$2,$3,$4::jsonb,$5
            )""",
            plan_id,
            REVIEWER_REF,
            REASON,
            stable_json(REASON_CODES),
            prior["review_manifest_sha256"],
        )
        applied = await conn.fetchrow(
            "SELECT * FROM memory.apply_projection_v5($1,$2,'p01',$3,$4)",
            request_id,
            plan_id,
            uuid.UUID(prior["review_id"]),
            prior["apply_manifest_sha256"],
        )
        if (
            staged["outcome"] != "replayed"
            or staged["rows_written"] != 0
            or reviewed["outcome"] != "replayed"
            or reviewed["rows_written"] != 0
            or applied["outcome"] != "replayed"
            or applied["rows_written"] != 0
            or str(applied["apply_event_id"]) != prior["apply_event_id"]
        ):
            raise ProjectProjectionApplyError("cross-transaction replay was not zero-write")
    except BaseException:
        await transaction.rollback()
        raise
    await transaction.rollback()
    await assert_other_owner_denied(
        conn, "557ea042-cb82-48f8-9429-472e96c957ef", observation_id
    )
    return {
        "contract_version": REPLAY_CONTRACT,
        "mode": "replay",
        "head_commit": commit,
        "owner_user_id": owner,
        "plan_id": str(plan_id),
        "request_id": str(request_id),
        "bundle_sha256": bundle["bundle_sha256"],
        "review_id": prior["review_id"],
        "apply_event_id": prior["apply_event_id"],
        "stage_rows_written": 0,
        "review_rows_written": 0,
        "apply_rows_written": 0,
        "cross_owner_rejected": True,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }


async def async_main() -> int:
    import asyncpg

    args = arguments()
    if args.mode == "apply" and os.environ.get(
        "MEMORY_V1_PROJECT_PROJECTION_APPLY"
    ) != "authorized":
        raise ProjectProjectionApplyError(
            "MEMORY_V1_PROJECT_PROJECTION_APPLY=authorized is required"
        )
    if args.mode == "replay" and not args.prior_result:
        raise ProjectProjectionApplyError("replay mode requires --prior-result")
    if args.mode == "apply" and args.prior_result:
        raise ProjectProjectionApplyError("apply mode does not accept --prior-result")
    commit = repository_state()
    bundle = read_private_json(Path(args.bundle).resolve())
    validate_bundle(bundle)
    request_id = uuid.UUID(args.request_id)
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ProjectProjectionApplyError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ProjectProjectionApplyError("POSTGRES_DSN must use brains_app")
        if args.mode == "apply":
            result = await apply_bundle(conn, bundle, request_id, commit)
        else:
            prior = read_private_json(Path(args.prior_result).resolve())
            result = await replay_bundle(conn, bundle, request_id, prior, commit)
    finally:
        await conn.close()
    output = Path(args.output).resolve()
    file_sha = secure_write(output, result)
    print(
        stable_json(
            {
                "contract_version": result["contract_version"],
                "mode": args.mode,
                "output": str(output),
                "file_sha256": file_sha,
                "plan_id": result["plan_id"],
                "request_id": result["request_id"],
                "database_writes": 10 if args.mode == "apply" else 0,
                "qdrant_writes": 0,
                "external_model_calls": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
