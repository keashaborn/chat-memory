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

from memory_v1_projection_v5_2_contract import (
    owner_manifest_sha256,
    sha256,
    validate_packet,
    validate_projection_schema,
    validate_registry,
)
from memory_v1_v5_2_projection_dispatch import (
    build_reconciled_stance_packet,
    stable_json,
)


MANIFEST_CONTRACT = "memory_v1_v5_2_reconciled_stance_stage_manifest_v1"
AUTHORIZATION_CONTRACT = (
    "memory_v1_v5_2_reconciled_stance_stage_authorization_v1"
)
RESULT_CONTRACT = "memory_v1_v5_2_reconciled_stance_stage_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
ID_NAMESPACE = uuid.UUID("94305950-6ab4-4ef0-8315-ab197564aa91")
EXPECTED_TABLE_ROWS = {
    "projection_plan": 1,
    "projection_plan_item": 1,
    "projection_claim_payload": 1,
    "projection_plan_observation": 2,
}
CONFIRMATION = "STAGE_ONE_OWNER_V5_2_RECONCILED_STANCE_ONLY"


class ReconciledStageError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("manifest")
    manifest.add_argument("--owner", required=True)
    manifest.add_argument("--evidence", required=True)
    manifest.add_argument("--primary", required=True)
    manifest.add_argument("--context", required=True)
    manifest.add_argument("--required-head", required=True)
    manifest.add_argument("--output", required=True)

    authorize = commands.add_parser("authorize")
    authorize.add_argument("--manifest", required=True)
    authorize.add_argument("--output", required=True)

    for command in ("apply", "replay"):
        action = commands.add_parser(command)
        action.add_argument("--manifest", required=True)
        action.add_argument("--authorization", required=True)
        action.add_argument("--output", required=True)
        action.add_argument("--confirm", required=True)

    cross_owner = commands.add_parser("cross-owner")
    cross_owner.add_argument("--manifest", required=True)
    cross_owner.add_argument("--other-owner", required=True)
    cross_owner.add_argument("--output", required=True)
    return parser.parse_args()


def review_path(value: str, *, exists: bool) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ReconciledStageError("artifact is outside the private review root")
    if exists:
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ReconciledStageError("input must be a private mode-0600 file")
    elif path.exists():
        raise ReconciledStageError("output path already exists")
    return path


def write_private(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def load_hashed(path: Path, hash_field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get(hash_field) != sha256(
        {key: item for key, item in value.items() if key != hash_field}
    ):
        raise ReconciledStageError(f"{path.name} content hash mismatch")
    return value


def stable_id(owner: str, evidence: str, primary: str, context: str) -> str:
    return str(
        uuid.uuid5(
            ID_NAMESPACE,
            "|".join(("plan", owner, evidence, primary, context)),
        )
    )


def load_contracts() -> dict[str, dict[str, Any]]:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "specs/memory_v1_projection_plan_v5_2.schema.json").read_text()
    )
    registry = json.loads(
        (root / "specs/memory_v1_predicate_registry_v5_2.json").read_text()
    )
    validate_projection_schema(schema)
    return validate_registry(registry)


def load_manifest(path: Path) -> dict[str, Any]:
    value = load_hashed(path, "manifest_sha256")
    expected_keys = {
        "contract_version",
        "target_server",
        "owner_user_id",
        "evidence_id",
        "primary_observation_id",
        "context_observation_id",
        "required_head_commit",
        "plan_id",
        "packet",
        "packet_text_sha256",
        "packet_sha256",
        "owner_manifest_sha256",
        "projection_sha256",
        "semantic_key_sha256",
        "canonical_text_sha256",
        "expected_new_rows",
        "expected_table_rows",
        "manifest_sha256",
    }
    if set(value) != expected_keys:
        raise ReconciledStageError("manifest fields mismatch")
    for field in (
        "owner_user_id",
        "evidence_id",
        "primary_observation_id",
        "context_observation_id",
        "plan_id",
    ):
        uuid.UUID(value[field])
    packet_text = stable_json(value["packet"])
    projection = value["packet"]["projections"][0]
    if (
        value["contract_version"] != MANIFEST_CONTRACT
        or value["target_server"] != "seebx"
        or value["expected_table_rows"] != EXPECTED_TABLE_ROWS
        or value["expected_new_rows"] != sum(EXPECTED_TABLE_ROWS.values())
        or value["primary_observation_id"]
        == value["context_observation_id"]
        or value["packet_text_sha256"]
        != hashlib.sha256(packet_text.encode()).hexdigest()
        or value["packet_sha256"] != value["packet"]["packet_sha256"]
        or value["owner_manifest_sha256"]
        != owner_manifest_sha256(value["owner_user_id"], value["packet_sha256"])
        or value["projection_sha256"] != sha256(projection)
        or value["semantic_key_sha256"]
        != projection["identity"]["semantic_key_sha256"]
        or value["canonical_text_sha256"]
        != hashlib.sha256(
            projection["payload"]["canonical_text"].encode()
        ).hexdigest()
    ):
        raise ReconciledStageError("manifest boundary or hash mismatch")
    return value


def load_authorization(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    value = load_hashed(path, "authorization_sha256")
    if (
        set(value)
        != {
            "contract_version",
            "owner_user_id",
            "required_head_commit",
            "manifest_sha256",
            "authorized_action",
            "expected_new_rows",
            "authorization_sha256",
        }
        or value["contract_version"] != AUTHORIZATION_CONTRACT
        or value["owner_user_id"] != manifest["owner_user_id"]
        or value["required_head_commit"] != manifest["required_head_commit"]
        or value["manifest_sha256"] != manifest["manifest_sha256"]
        or value["authorized_action"] != CONFIRMATION
        or value["expected_new_rows"] != manifest["expected_new_rows"]
    ):
        raise ReconciledStageError("authorization mismatch")
    return value


def normalize_source(row: Any) -> dict[str, Any]:
    value = dict(row)
    for field in ("object_literal", "project_scope", "temporal"):
        if isinstance(value.get(field), str):
            value[field] = json.loads(value[field])
    return value


def validate_source(
    source: dict[str, Any], owner: str, evidence: str, observation: str
) -> None:
    if (
        str(source["owner_user_id"]) != owner
        or str(source["evidence_id"]) != evidence
        or str(source["observation_id"]) != observation
        or source["predicate_registry_version"]
        != "memory_predicate_registry_v5_2"
        or source["predicate"] != "stance.reported"
        or source["projection_class"] != "reported_stance"
        or source["surface_policy"]
        != "relevant_recall_or_explicit_recall"
        or source["polarity"] != "affirmed"
        or source["modality"] != "reported_belief"
        or source["object_kind"] != "literal"
        or source["subject_entity_type"] != "self"
        or source["subject_entity_status"] != "active"
        or source["evidence_status"] != "active"
    ):
        raise ReconciledStageError("source is outside reconciled stance boundary")


async def build_manifest(args: argparse.Namespace, conn: Any) -> dict[str, Any]:
    owner = str(uuid.UUID(args.owner))
    evidence = str(uuid.UUID(args.evidence))
    primary_id = str(uuid.UUID(args.primary))
    context_id = str(uuid.UUID(args.context))
    if primary_id == context_id:
        raise ReconciledStageError("primary and context observations must differ")
    if (
        len(args.required_head) != 40
        or any(character not in "0123456789abcdef" for character in args.required_head)
    ):
        raise ReconciledStageError("required head must be a full commit")
    registry = load_contracts()
    plan_id = stable_id(owner, evidence, primary_id, context_id)

    transaction = conn.transaction(readonly=True, isolation="repeatable_read")
    await transaction.start()
    try:
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        primary_row = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
            uuid.UUID(primary_id),
        )
        context_row = await conn.fetchrow(
            "SELECT * FROM memory.preflight_projection_source_v5_2($1)",
            uuid.UUID(context_id),
        )
        if primary_row is None or context_row is None:
            raise ReconciledStageError("owner-scoped source not found")
        primary = normalize_source(primary_row)
        context = normalize_source(context_row)
        validate_source(primary, owner, evidence, primary_id)
        validate_source(context, owner, evidence, context_id)
        packet = build_reconciled_stance_packet(owner, primary, context)
        validate_packet(packet, owner, registry)
        packet_text = stable_json(packet)
        preflight = await conn.fetchrow(
            "SELECT * FROM memory.preflight_reconciled_stance_projection_v5_2($1,$2)",
            uuid.UUID(plan_id),
            packet_text,
        )
        if (
            preflight is None
            or preflight["existing_aggregates"] != 0
            or preflight["existing_plans"] != 0
            or str(preflight["primary_observation_id"]) != primary_id
            or str(preflight["context_observation_id"]) != context_id
        ):
            raise ReconciledStageError("reconciled projection is not empty and stageable")
    finally:
        await transaction.rollback()

    projection = packet["projections"][0]
    manifest = {
        "contract_version": MANIFEST_CONTRACT,
        "target_server": "seebx",
        "owner_user_id": owner,
        "evidence_id": evidence,
        "primary_observation_id": primary_id,
        "context_observation_id": context_id,
        "required_head_commit": args.required_head,
        "plan_id": plan_id,
        "packet": packet,
        "packet_text_sha256": hashlib.sha256(packet_text.encode()).hexdigest(),
        "packet_sha256": packet["packet_sha256"],
        "owner_manifest_sha256": preflight["owner_manifest_sha256"],
        "projection_sha256": preflight["projection_sha256"],
        "semantic_key_sha256": preflight["semantic_key_sha256"],
        "canonical_text_sha256": hashlib.sha256(
            projection["payload"]["canonical_text"].encode()
        ).hexdigest(),
        "expected_new_rows": sum(EXPECTED_TABLE_ROWS.values()),
        "expected_table_rows": EXPECTED_TABLE_ROWS,
    }
    manifest["manifest_sha256"] = sha256(manifest)
    return manifest


async def apply_stage(
    args: argparse.Namespace, conn: Any, manifest: dict[str, Any]
) -> dict[str, Any]:
    authorization_path = review_path(args.authorization, exists=True)
    load_authorization(authorization_path, manifest)
    if args.confirm != CONFIRMATION:
        raise ReconciledStageError("exact stage confirmation is required")
    if os.environ.get("MEMORY_V1_REQUIRED_HEAD") != manifest["required_head_commit"]:
        raise ReconciledStageError("runtime head does not match manifest")
    output = review_path(args.output, exists=False)
    transaction = conn.transaction(isolation="serializable")
    await transaction.start()
    try:
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)", manifest["owner_user_id"]
        )
        row = await conn.fetchrow(
            "SELECT * FROM memory.stage_reconciled_stance_projection_v5_2($1,$2,$3)",
            uuid.UUID(manifest["plan_id"]),
            stable_json(manifest["packet"]),
            manifest["owner_manifest_sha256"],
        )
        result_value = row["result"] if row is not None else None
        if isinstance(result_value, str):
            result_value = json.loads(result_value)
        expected_outcome = "applied" if args.command == "apply" else "replayed"
        expected_rows = manifest["expected_new_rows"] if args.command == "apply" else 0
        if (
            row is None
            or row["outcome"] != expected_outcome
            or row["rows_written"] != expected_rows
            or result_value.get("observation_count") != 2
        ):
            raise ReconciledStageError("stage outcome or row budget mismatch")
        await transaction.commit()
    except BaseException:
        await transaction.rollback()
        raise
    result = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.command,
        "owner_user_id": manifest["owner_user_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "plan_id": manifest["plan_id"],
        "outcome": row["outcome"],
        "rows_written": row["rows_written"],
        "claims_written": 0,
        "qdrant_writes": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }
    result["result_sha256"] = sha256(result)
    write_private(output, result)
    return result


async def run() -> int:
    import asyncpg

    args = arguments()
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ReconciledStageError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ReconciledStageError("POSTGRES_DSN must authenticate as brains_app")
        if args.command == "manifest":
            output = review_path(args.output, exists=False)
            value = await build_manifest(args, conn)
            write_private(output, value)
            print(f"manifest={output}")
            print(f"manifest_sha256={value['manifest_sha256']}")
        elif args.command == "authorize":
            manifest_path = review_path(args.manifest, exists=True)
            manifest = load_manifest(manifest_path)
            output = review_path(args.output, exists=False)
            value = {
                "contract_version": AUTHORIZATION_CONTRACT,
                "owner_user_id": manifest["owner_user_id"],
                "required_head_commit": manifest["required_head_commit"],
                "manifest_sha256": manifest["manifest_sha256"],
                "authorized_action": CONFIRMATION,
                "expected_new_rows": manifest["expected_new_rows"],
            }
            value["authorization_sha256"] = sha256(value)
            write_private(output, value)
            print(f"authorization={output}")
            print(f"authorization_sha256={value['authorization_sha256']}")
        elif args.command in {"apply", "replay"}:
            manifest = load_manifest(review_path(args.manifest, exists=True))
            value = await apply_stage(args, conn, manifest)
            print(f"mode={args.command}")
            print(f"rows_written={value['rows_written']}")
            print(f"result_sha256={value['result_sha256']}")
        else:
            manifest = load_manifest(review_path(args.manifest, exists=True))
            output = review_path(args.output, exists=False)
            other = str(uuid.UUID(args.other_owner))
            if other == manifest["owner_user_id"]:
                raise ReconciledStageError("cross-owner probe requires another owner")
            transaction = conn.transaction(readonly=True, isolation="serializable")
            await transaction.start()
            rejected = False
            try:
                await conn.execute("SELECT set_config('app.user_id',$1,true)", other)
                try:
                    await conn.fetchrow(
                        "SELECT * FROM memory.preflight_reconciled_stance_projection_v5_2($1,$2)",
                        uuid.UUID(manifest["plan_id"]),
                        stable_json(manifest["packet"]),
                    )
                except Exception:
                    rejected = True
            finally:
                await transaction.rollback()
            if not rejected:
                raise ReconciledStageError("cross-owner preflight unexpectedly succeeded")
            value = {
                "contract_version": "memory_v1_v5_2_reconciled_stance_isolation_v1",
                "owner_user_id": manifest["owner_user_id"],
                "other_owner_user_id": other,
                "manifest_sha256": manifest["manifest_sha256"],
                "cross_owner_rejected": True,
            }
            value["result_sha256"] = sha256(value)
            write_private(output, value)
            print("cross_owner_rejected=true")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
