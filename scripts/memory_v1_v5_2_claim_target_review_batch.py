#!/usr/bin/env python3
"""Review staged V5.2 create/reinforce projections without applying claims."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_projection_v5_2_contract import sha256


STAGE_CONTRACT = "memory_v1_v5_2_claim_target_stage_manifest_v1"
MANIFEST_CONTRACT = "memory_v1_v5_2_claim_target_review_manifest_v1"
AUTHORIZATION_CONTRACT = (
    "memory_v1_v5_2_claim_target_review_authorization_v1"
)
RESULT_CONTRACT = "memory_v1_v5_2_claim_target_review_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
REVIEWER_TYPE = "system"
REVIEWER_REF = "memory_v1_v5_2_claim_target_review_v1"
CONFIRMATION = "REVIEW_EXACT_STAGED_V5_2_CLAIM_TARGETS_ONLY"
APPLY_ENV = "MEMORY_V1_V5_2_CLAIM_TARGET_REVIEW_APPLY"
VALID_ACTIONS = {"create", "reinforce"}


class ClaimTargetReviewError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("manifest")
    manifest.add_argument("--stage-manifest", required=True)
    manifest.add_argument("--required-head", required=True)
    manifest.add_argument("--output", required=True)

    authorize = commands.add_parser("authorize")
    authorize.add_argument("--manifest", required=True)
    authorize.add_argument("--output", required=True)

    for command in ("apply", "replay"):
        action = commands.add_parser(command)
        action.add_argument("--manifest", required=True)
        action.add_argument("--authorization", required=True)
        action.add_argument("--confirm", required=True)
        action.add_argument("--output", required=True)

    cross_owner = commands.add_parser("cross-owner")
    cross_owner.add_argument("--manifest", required=True)
    cross_owner.add_argument("--other-owner", required=True)
    cross_owner.add_argument("--output", required=True)
    return parser.parse_args()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ClaimTargetReviewError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ClaimTargetReviewError("output already exists")
        if (
            not path.parent.is_dir()
            or stat.S_IMODE(path.parent.stat().st_mode) & 0o077
        ):
            raise ClaimTargetReviewError("output parent must be private")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ClaimTargetReviewError("input must be a mode-0600 regular file")
    return path


def write_private(path: Path, value: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def valid_head(value: str) -> str:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ClaimTargetReviewError("required head must be a full lowercase commit")
    return value


def load_hashed(path: Path, field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get(field) != sha256(
        {key: item for key, item in value.items() if key != field}
    ):
        raise ClaimTargetReviewError(f"{path.name} content hash mismatch")
    return value


def load_stage(path: Path) -> dict[str, Any]:
    value = load_hashed(path, "manifest_sha256")
    required = {
        "contract_version",
        "owner_user_id",
        "required_head_commit",
        "review_report_file_sha256",
        "review_report_sha256",
        "stage_item_count",
        "held_item_count",
        "expected_rows",
        "stage_items",
        "held_items",
        "manifest_sha256",
    }
    if set(value) != required or value["contract_version"] != STAGE_CONTRACT:
        raise ClaimTargetReviewError("stage manifest fields mismatch")
    uuid.UUID(value["owner_user_id"])
    valid_head(value["required_head_commit"])
    items = value["stage_items"]
    if (
        not isinstance(items, list)
        or not 1 <= len(items) <= 32
        or value["stage_item_count"] != len(items)
        or value["expected_rows"] != 4 * len(items)
    ):
        raise ClaimTargetReviewError("stage item count mismatch")
    seen: set[tuple[str, str]] = set()
    for item in items:
        plan = str(uuid.UUID(item["plan_id"]))
        observation = str(uuid.UUID(item["observation_id"]))
        identity = (plan, observation)
        projections = item.get("packet", {}).get("projections")
        if (
            identity in seen
            or item.get("action") not in VALID_ACTIONS
            or item.get("expected_rows") != 4
            or not isinstance(projections, list)
            or len(projections) != 1
        ):
            raise ClaimTargetReviewError("stage item boundary mismatch")
        seen.add(identity)
        projection = projections[0]
        if (
            projection.get("projection_ref") != "p01"
            or projection.get("lane") != "claim"
            or projection.get("target", {}).get("action") != item["action"]
            or projection.get("review", {}).get("state")
            != "manual_review_required"
            or projection.get("review", {}).get("authorization_required") is not True
            or projection.get("identity", {}).get("predicate")
            != item.get("predicate")
            or projection.get("identity", {}).get("semantic_key_sha256")
            != item.get("semantic_key_sha256")
        ):
            raise ClaimTargetReviewError("stage projection boundary mismatch")
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    value = load_hashed(path, "manifest_sha256")
    required = {
        "contract_version",
        "owner_user_id",
        "required_head_commit",
        "source_stage_required_head_commit",
        "source_stage_manifest",
        "source_stage_manifest_file_sha256",
        "source_stage_manifest_sha256",
        "reviewer_type",
        "reviewer_ref",
        "expected_new_rows",
        "held_item_count",
        "items",
        "manifest_sha256",
    }
    if set(value) != required or value["contract_version"] != MANIFEST_CONTRACT:
        raise ClaimTargetReviewError("review manifest fields mismatch")
    uuid.UUID(value["owner_user_id"])
    valid_head(value["required_head_commit"])
    valid_head(value["source_stage_required_head_commit"])
    if (
        value["reviewer_type"] != REVIEWER_TYPE
        or value["reviewer_ref"] != REVIEWER_REF
        or value["expected_new_rows"] != len(value["items"])
        or not 1 <= len(value["items"]) <= 32
    ):
        raise ClaimTargetReviewError("review manifest boundary mismatch")
    item_fields = {
        "plan_id",
        "projection_ref",
        "observation_id",
        "observation_sha256",
        "predicate",
        "target_action",
        "expected_revision_number",
        "canonical_text_sha256",
        "projection_sha256",
        "semantic_key_sha256",
        "review_number",
        "decision",
        "reason",
        "reason_codes",
        "authorization_manifest_sha256",
    }
    seen: set[str] = set()
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != item_fields:
            raise ClaimTargetReviewError("review manifest item fields mismatch")
        plan = str(uuid.UUID(item["plan_id"]))
        uuid.UUID(item["observation_id"])
        if (
            plan in seen
            or item["projection_ref"] != "p01"
            or item["target_action"] not in VALID_ACTIONS
            or (
                item["target_action"] == "create"
                and item["expected_revision_number"] is not None
            )
            or (
                item["target_action"] == "reinforce"
                and (
                    not isinstance(item["expected_revision_number"], int)
                    or item["expected_revision_number"] < 1
                )
            )
            or item["review_number"] != 1
            or item["decision"] != "authorized"
            or len(item["projection_sha256"]) != 64
            or len(item["semantic_key_sha256"]) != 64
            or len(item["canonical_text_sha256"]) != 64
            or not item["reason_codes"]
        ):
            raise ClaimTargetReviewError("review manifest item boundary mismatch")
        seen.add(plan)
    stage_path = private_path(value["source_stage_manifest"])
    if (
        file_sha256(stage_path) != value["source_stage_manifest_file_sha256"]
        or load_stage(stage_path)["manifest_sha256"]
        != value["source_stage_manifest_sha256"]
    ):
        raise ClaimTargetReviewError("source stage manifest drifted")
    return value


def load_authorization(
    path: Path, manifest: dict[str, Any]
) -> dict[str, Any]:
    value = load_hashed(path, "authorization_sha256")
    required = {
        "contract_version",
        "owner_user_id",
        "required_head_commit",
        "manifest_sha256",
        "authorized_action",
        "expected_new_rows",
        "authorization_sha256",
    }
    if (
        set(value) != required
        or value["contract_version"] != AUTHORIZATION_CONTRACT
        or value["owner_user_id"] != manifest["owner_user_id"]
        or value["required_head_commit"] != manifest["required_head_commit"]
        or value["manifest_sha256"] != manifest["manifest_sha256"]
        or value["authorized_action"] != CONFIRMATION
        or value["expected_new_rows"] != manifest["expected_new_rows"]
    ):
        raise ClaimTargetReviewError("review authorization mismatch")
    return value


def review_reason(action: str) -> tuple[str, list[str]]:
    common = [
        "reviewed_source_observation_revalidated",
        "deterministic_projection_revalidated",
        "governed_target_action_reviewed",
    ]
    if action == "create":
        return (
            "The owner-scoped staged projection is supported by its reviewed "
            "source observation and had no governed semantic aggregate at staging.",
            common + ["no_existing_semantic_aggregate"],
        )
    return (
        "The owner-scoped staged projection supplies additional support to the "
        "exact supported governed semantic aggregate selected at staging.",
        common + ["exact_supported_semantic_aggregate"],
    )


async def set_actor(connection: Any, owner: str) -> None:
    await connection.execute("SELECT set_config('app.user_id',$1,true)", owner)


async def build_manifest(
    args: argparse.Namespace, connection: Any
) -> dict[str, Any]:
    stage_path = private_path(args.stage_manifest)
    stage = load_stage(stage_path)
    owner = stage["owner_user_id"]
    required_head = valid_head(args.required_head)
    transaction = connection.transaction(readonly=True, isolation="repeatable_read")
    await transaction.start()
    items: list[dict[str, Any]] = []
    try:
        await set_actor(connection, owner)
        for staged in sorted(
            stage["stage_items"], key=lambda item: item["plan_id"]
        ):
            projection = staged["packet"]["projections"][0]
            reason, reason_codes = review_reason(staged["action"])
            preflight = await connection.fetchrow(
                """
                SELECT * FROM memory.preflight_projection_review_v5(
                  $1,'p01','authorized'::memory.projection_review_decision_v5,
                  $2,$3,$4,$5::jsonb
                )
                """,
                uuid.UUID(staged["plan_id"]),
                REVIEWER_TYPE,
                REVIEWER_REF,
                reason,
                json.dumps(reason_codes, separators=(",", ":")),
            )
            if (
                preflight["review_number"] != 1
                or preflight["projection_sha256"] != sha256(projection)
                or preflight["semantic_key_sha256"]
                != staged["semantic_key_sha256"]
            ):
                raise ClaimTargetReviewError("projection review preflight drifted")
            items.append(
                {
                    "plan_id": staged["plan_id"],
                    "projection_ref": "p01",
                    "observation_id": staged["observation_id"],
                    "observation_sha256": staged["observation_sha256"],
                    "predicate": staged["predicate"],
                    "target_action": staged["action"],
                    "expected_revision_number": staged[
                        "expected_revision_number"
                    ],
                    "canonical_text_sha256": hashlib.sha256(
                        projection["payload"]["canonical_text"].encode()
                    ).hexdigest(),
                    "projection_sha256": preflight["projection_sha256"],
                    "semantic_key_sha256": preflight["semantic_key_sha256"],
                    "review_number": preflight["review_number"],
                    "decision": "authorized",
                    "reason": reason,
                    "reason_codes": reason_codes,
                    "authorization_manifest_sha256": preflight[
                        "authorization_manifest_sha256"
                    ],
                }
            )
        if await connection.fetchval("SELECT txid_current_if_assigned()") is not None:
            raise ClaimTargetReviewError("review manifest assigned a transaction ID")
    finally:
        await transaction.rollback()
    manifest: dict[str, Any] = {
        "contract_version": MANIFEST_CONTRACT,
        "owner_user_id": owner,
        "required_head_commit": required_head,
        "source_stage_required_head_commit": stage["required_head_commit"],
        "source_stage_manifest": str(stage_path),
        "source_stage_manifest_file_sha256": file_sha256(stage_path),
        "source_stage_manifest_sha256": stage["manifest_sha256"],
        "reviewer_type": REVIEWER_TYPE,
        "reviewer_ref": REVIEWER_REF,
        "expected_new_rows": len(items),
        "held_item_count": stage["held_item_count"],
        "items": items,
    }
    manifest["manifest_sha256"] = sha256(manifest)
    return manifest


def build_authorization(manifest: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "contract_version": AUTHORIZATION_CONTRACT,
        "owner_user_id": manifest["owner_user_id"],
        "required_head_commit": manifest["required_head_commit"],
        "manifest_sha256": manifest["manifest_sha256"],
        "authorized_action": CONFIRMATION,
        "expected_new_rows": manifest["expected_new_rows"],
    }
    value["authorization_sha256"] = sha256(value)
    return value


async def apply_or_replay(
    args: argparse.Namespace,
    connection: Any,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    load_authorization(private_path(args.authorization), manifest)
    if args.confirm != CONFIRMATION:
        raise ClaimTargetReviewError("confirmation phrase mismatch")
    if os.getenv("MEMORY_V1_REQUIRED_HEAD") != manifest["required_head_commit"]:
        raise ClaimTargetReviewError("runtime head differs from manifest")
    if os.getenv(APPLY_ENV) != "authorized":
        raise ClaimTargetReviewError("review apply authorization is absent")
    replay = args.command == "replay"
    transaction = connection.transaction(isolation="serializable")
    await transaction.start()
    rows_written = 0
    outcomes: list[dict[str, Any]] = []
    try:
        await set_actor(connection, manifest["owner_user_id"])
        for item in manifest["items"]:
            if not replay:
                preflight = await connection.fetchrow(
                    """
                    SELECT * FROM memory.preflight_projection_review_v5(
                      $1,'p01','authorized'::memory.projection_review_decision_v5,
                      $2,$3,$4,$5::jsonb
                    )
                    """,
                    uuid.UUID(item["plan_id"]),
                    manifest["reviewer_type"],
                    manifest["reviewer_ref"],
                    item["reason"],
                    json.dumps(item["reason_codes"], separators=(",", ":")),
                )
                if (
                    preflight["review_number"] != item["review_number"]
                    or preflight["projection_sha256"] != item["projection_sha256"]
                    or preflight["semantic_key_sha256"]
                    != item["semantic_key_sha256"]
                    or preflight["authorization_manifest_sha256"]
                    != item["authorization_manifest_sha256"]
                ):
                    raise ClaimTargetReviewError("review preflight drifted")
            row = await connection.fetchrow(
                """
                SELECT * FROM memory.review_projection_v5(
                  $1,'p01','authorized'::memory.projection_review_decision_v5,
                  $2,$3,$4,$5::jsonb,$6
                )
                """,
                uuid.UUID(item["plan_id"]),
                manifest["reviewer_type"],
                manifest["reviewer_ref"],
                item["reason"],
                json.dumps(item["reason_codes"], separators=(",", ":")),
                item["authorization_manifest_sha256"],
            )
            wanted_outcome = "replayed" if replay else "applied"
            wanted_rows = 0 if replay else 1
            if row["outcome"] != wanted_outcome or row["rows_written"] != wanted_rows:
                raise ClaimTargetReviewError("review outcome mismatch")
            rows_written += row["rows_written"]
            outcomes.append(
                {
                    "plan_id": item["plan_id"],
                    "target_action": item["target_action"],
                    "review_id": str(row["review_id"]),
                    "outcome": row["outcome"],
                    "rows_written": row["rows_written"],
                }
            )
        expected = 0 if replay else manifest["expected_new_rows"]
        if rows_written != expected:
            raise ClaimTargetReviewError("review transaction row budget mismatch")
        await transaction.commit()
    except BaseException:
        await transaction.rollback()
        raise
    value: dict[str, Any] = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.command,
        "owner_user_id": manifest["owner_user_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "item_count": len(outcomes),
        "rows_written": rows_written,
        "outcomes": outcomes,
        "claims_written": 0,
        "projection_apply_events_written": 0,
        "qdrant_writes": 0,
        "retrieval_changes": 0,
        "prompt_influence": 0,
    }
    value["result_sha256"] = sha256(value)
    return value


async def cross_owner(
    args: argparse.Namespace,
    connection: Any,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    other = str(uuid.UUID(args.other_owner))
    if other == manifest["owner_user_id"]:
        raise ClaimTargetReviewError("cross-owner probe requires another owner")
    item = manifest["items"][0]
    transaction = connection.transaction(readonly=True, isolation="repeatable_read")
    await transaction.start()
    rejected = False
    sqlstate = None
    try:
        await set_actor(connection, other)
        probe = connection.transaction()
        await probe.start()
        try:
            await connection.fetchrow(
                """
                SELECT * FROM memory.preflight_projection_review_v5(
                  $1,'p01','authorized'::memory.projection_review_decision_v5,
                  $2,$3,$4,$5::jsonb
                )
                """,
                uuid.UUID(item["plan_id"]),
                manifest["reviewer_type"],
                manifest["reviewer_ref"],
                item["reason"],
                json.dumps(item["reason_codes"], separators=(",", ":")),
            )
        except asyncpg.PostgresError as error:
            sqlstate = error.sqlstate
            rejected = error.sqlstate == "P0002"
            await probe.rollback()
        else:
            await probe.commit()
        if not rejected:
            raise ClaimTargetReviewError("cross-owner review was not rejected")
        if await connection.fetchval("SELECT txid_current_if_assigned()") is not None:
            raise ClaimTargetReviewError("cross-owner probe assigned a transaction ID")
    finally:
        await transaction.rollback()
    value: dict[str, Any] = {
        "contract_version": RESULT_CONTRACT,
        "mode": "cross-owner",
        "owner_user_id": manifest["owner_user_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "cross_owner_rejected": rejected,
        "sqlstate": sqlstate,
        "rows_written": 0,
        "claims_written": 0,
        "qdrant_writes": 0,
    }
    value["result_sha256"] = sha256(value)
    return value


async def run() -> int:
    args = arguments()
    output = private_path(args.output, output=True)
    if args.command == "authorize":
        value = build_authorization(load_manifest(private_path(args.manifest)))
        write_private(output, value)
        print(f"authorization={output}")
        print(f"authorization_sha256={value['authorization_sha256']}")
        return 0
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ClaimTargetReviewError("POSTGRES_DSN is required")
    connection = await asyncpg.connect(dsn, command_timeout=60)
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise ClaimTargetReviewError(
                "POSTGRES_DSN must authenticate as brains_app"
            )
        if args.command == "manifest":
            value = await build_manifest(args, connection)
        else:
            manifest = load_manifest(private_path(args.manifest))
            if args.command in {"apply", "replay"}:
                value = await apply_or_replay(args, connection, manifest)
            else:
                value = await cross_owner(args, connection, manifest)
    finally:
        await connection.close()
    write_private(output, value)
    hash_field = (
        "manifest_sha256"
        if args.command == "manifest"
        else "result_sha256"
    )
    print(f"mode={args.command}")
    print(f"rows_written={value.get('rows_written', 0)}")
    print(f"output={output}")
    print(f"{hash_field}={value[hash_field]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
