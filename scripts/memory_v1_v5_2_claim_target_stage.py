#!/usr/bin/env python3
"""Stage reviewed V5.2 create/reinforce claim targets without creating claims."""

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
from scripts.memory_v1_v5_2_claim_target_resolver import (
    resolve_claim_projection_target,
)
from scripts.memory_v1_v5_2_claim_target_review import load_source, stable_plan_id
from scripts.memory_v1_v5_2_projection_dispatch import stable_json
from scripts.memory_v1_v5_2_projection_stage_batch import load_contracts


MANIFEST_CONTRACT = "memory_v1_v5_2_claim_target_stage_manifest_v1"
AUTHORIZATION_CONTRACT = "memory_v1_v5_2_claim_target_stage_authorization_v1"
RESULT_CONTRACT = "memory_v1_v5_2_claim_target_stage_result_v1"
REVIEW_CONTRACT = "memory_v1_v5_2_claim_target_review_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
STAGEABLE_ACTIONS = {"create", "reinforce"}
CONFIRMATION = "STAGE_REVIEWED_V5_2_CLAIM_TARGETS_ONLY"
APPLY_ENV = "MEMORY_V1_V5_2_CLAIM_TARGET_STAGE_APPLY"
CLONE_COMMENT = "memory_v1_v5_2_claim_target_review_clone_v1"
ROWS_PER_STAGE = 4


class ClaimTargetStageError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--owner", required=True)
    manifest.add_argument("--review-report", required=True)
    manifest.add_argument("--required-head", required=True)
    manifest.add_argument("--output", required=True)

    authorize = subparsers.add_parser("authorize")
    authorize.add_argument("--manifest", required=True)
    authorize.add_argument("--output", required=True)

    for command in ("apply", "replay"):
        action = subparsers.add_parser(command)
        action.add_argument("--manifest", required=True)
        action.add_argument("--authorization", required=True)
        action.add_argument("--output", required=True)
        action.add_argument("--confirm", required=True)

    cross_owner = subparsers.add_parser("cross-owner")
    cross_owner.add_argument("--manifest", required=True)
    cross_owner.add_argument("--other-owner", required=True)
    cross_owner.add_argument("--output", required=True)
    return parser.parse_args()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ClaimTargetStageError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ClaimTargetStageError("output already exists")
        if not path.parent.is_dir() or stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            raise ClaimTargetStageError("output parent must be private")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ClaimTargetStageError("input must be a mode-0600 regular file")
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


def load_hashed(path: Path, hash_field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    expected = value.get(hash_field)
    if expected != sha256(
        {key: item for key, item in value.items() if key != hash_field}
    ):
        raise ClaimTargetStageError(f"{path.name} content hash mismatch")
    return value


def valid_head(value: str) -> str:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ClaimTargetStageError("required head must be a full lowercase commit")
    return value


def report_item_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = report.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 32:
        raise ClaimTargetStageError("review item count is invalid")
    mapped: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ClaimTargetStageError("review item is invalid")
        observation = str(uuid.UUID(str(item.get("observation_id"))))
        if observation in mapped:
            raise ClaimTargetStageError("duplicate review observation")
        mapped[observation] = item
    return mapped


def validate_review(report: dict[str, Any], owner: str) -> dict[str, dict[str, Any]]:
    if (
        report.get("contract_version") != REVIEW_CONTRACT
        or report.get("owner_user_id") != owner
        or report.get("item_count") != len(report.get("items", []))
        or report.get("proofs", {}).get("database_writes") != 0
        or report.get("proofs", {}).get("claim_writes") != 0
        or report.get("proofs", {}).get("qdrant_writes") != 0
        or report.get("proofs", {}).get("disposable_clone_verified") is not True
    ):
        raise ClaimTargetStageError("claim-target review boundary mismatch")
    mapped = report_item_map(report)
    action_counts: dict[str, int] = {}
    for item in mapped.values():
        action = item.get("action")
        if action not in STAGEABLE_ACTIONS | {"manual_review"}:
            raise ClaimTargetStageError("unsupported reviewed target action")
        action_counts[action] = action_counts.get(action, 0) + 1
    if report.get("action_counts") != dict(sorted(action_counts.items())):
        raise ClaimTargetStageError("review action counts mismatch")
    return mapped


def load_manifest(path: Path) -> dict[str, Any]:
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
    if set(value) != required or value["contract_version"] != MANIFEST_CONTRACT:
        raise ClaimTargetStageError("manifest fields mismatch")
    uuid.UUID(value["owner_user_id"])
    valid_head(value["required_head_commit"])
    stage_items = value["stage_items"]
    held_items = value["held_items"]
    if (
        not isinstance(stage_items, list)
        or not isinstance(held_items, list)
        or not 1 <= len(stage_items) <= 32
        or value["stage_item_count"] != len(stage_items)
        or value["held_item_count"] != len(held_items)
        or value["expected_rows"] != ROWS_PER_STAGE * len(stage_items)
    ):
        raise ClaimTargetStageError("manifest item counts mismatch")
    seen: set[str] = set()
    for item in stage_items:
        observation = str(uuid.UUID(item["observation_id"]))
        if observation in seen or item["action"] not in STAGEABLE_ACTIONS:
            raise ClaimTargetStageError("manifest stage identity mismatch")
        seen.add(observation)
        uuid.UUID(item["plan_id"])
        packet_text = stable_json(item["packet"])
        if (
            hashlib.sha256(packet_text.encode()).hexdigest()
            != item["packet_text_sha256"]
            or item["packet"]["packet_sha256"] != item["packet_sha256"]
            or item["expected_rows"] != ROWS_PER_STAGE
        ):
            raise ClaimTargetStageError("manifest packet hash mismatch")
    for item in held_items:
        observation = str(uuid.UUID(item["observation_id"]))
        if observation in seen or item["action"] != "manual_review":
            raise ClaimTargetStageError("manifest held identity mismatch")
        seen.add(observation)
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
            "expected_rows",
            "authorization_sha256",
        }
        or value["contract_version"] != AUTHORIZATION_CONTRACT
        or value["owner_user_id"] != manifest["owner_user_id"]
        or value["required_head_commit"] != manifest["required_head_commit"]
        or value["manifest_sha256"] != manifest["manifest_sha256"]
        or value["authorized_action"] != CONFIRMATION
        or value["expected_rows"] != manifest["expected_rows"]
    ):
        raise ClaimTargetStageError("authorization mismatch")
    return value


async def set_actor(conn: Any, owner: str) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)


async def resolve_current(
    conn: Any,
    owner: str,
    observation: str,
    registry: dict[str, dict[str, Any]],
    *,
    allow_existing_plan: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = await load_source(conn, observation)
    resolved = await resolve_claim_projection_target(
        conn,
        owner_user_id=owner,
        source=source,
        plan_id=stable_plan_id(owner, observation),
        registry=registry,
        allow_existing_plan=allow_existing_plan,
    )
    return source, resolved


def compare_review(
    reviewed: dict[str, Any],
    source: dict[str, Any],
    resolved: dict[str, Any],
) -> None:
    packet = resolved["packet"]
    projection = packet["projections"][0] if packet is not None else None
    canonical_text = (
        projection["payload"]["canonical_text"] if projection is not None else None
    )
    checks = {
        "observation_sha256": source["observation_sha256"],
        "evidence_id": str(source["evidence_id"]),
        "predicate": source["predicate"],
        "action": resolved["action"],
        "reason_codes": resolved["reason_codes"],
        "semantic_key_sha256": resolved["semantic_key_sha256"],
        "target_claim_id": resolved["target_claim_id"],
        "expected_revision_number": resolved["expected_revision_number"],
        "existing_claim_status": resolved["existing_claim_status"],
        "canonical_text": canonical_text,
        "packet_sha256": resolved["packet_sha256"],
        "owner_manifest_sha256": resolved["owner_manifest_sha256"],
    }
    for field, expected in checks.items():
        if reviewed.get(field) != expected:
            raise ClaimTargetStageError(f"review drifted at {field}")


async def build_manifest(
    args: argparse.Namespace, conn: Any
) -> dict[str, Any]:
    owner = str(uuid.UUID(args.owner))
    required_head = valid_head(args.required_head)
    report_path = private_path(args.review_report)
    report = load_hashed(report_path, "report_sha256")
    reviewed = validate_review(report, owner)
    _, registry = load_contracts()
    transaction = conn.transaction(readonly=True, isolation="repeatable_read")
    await transaction.start()
    stage_items: list[dict[str, Any]] = []
    held_items: list[dict[str, Any]] = []
    try:
        await set_actor(conn, owner)
        for observation, review_item in sorted(reviewed.items()):
            source, resolved = await resolve_current(
                conn,
                owner,
                observation,
                registry,
                allow_existing_plan=False,
            )
            compare_review(review_item, source, resolved)
            if resolved["action"] == "manual_review":
                held_items.append(
                    {
                        "observation_id": observation,
                        "action": "manual_review",
                        "reason_codes": resolved["reason_codes"],
                        "semantic_key_sha256": resolved["semantic_key_sha256"],
                        "target_claim_id": resolved["target_claim_id"],
                        "expected_revision_number": resolved[
                            "expected_revision_number"
                        ],
                    }
                )
                continue
            packet = resolved["packet"]
            packet_text = stable_json(packet)
            stage_items.append(
                {
                    "observation_id": observation,
                    "observation_sha256": source["observation_sha256"],
                    "evidence_id": str(source["evidence_id"]),
                    "predicate": source["predicate"],
                    "plan_id": stable_plan_id(owner, observation),
                    "action": resolved["action"],
                    "reason_codes": resolved["reason_codes"],
                    "semantic_key_sha256": resolved["semantic_key_sha256"],
                    "target_claim_id": resolved["target_claim_id"],
                    "expected_revision_number": resolved[
                        "expected_revision_number"
                    ],
                    "canonical_text": packet["projections"][0]["payload"][
                        "canonical_text"
                    ],
                    "packet": packet,
                    "packet_text_sha256": hashlib.sha256(
                        packet_text.encode()
                    ).hexdigest(),
                    "packet_sha256": resolved["packet_sha256"],
                    "owner_manifest_sha256": resolved[
                        "owner_manifest_sha256"
                    ],
                    "expected_rows": ROWS_PER_STAGE,
                }
            )
        if await conn.fetchval("SELECT txid_current_if_assigned()") is not None:
            raise ClaimTargetStageError("manifest review assigned a transaction ID")
    finally:
        await transaction.rollback()
    if not stage_items:
        raise ClaimTargetStageError("review contains no stageable targets")
    manifest: dict[str, Any] = {
        "contract_version": MANIFEST_CONTRACT,
        "owner_user_id": owner,
        "required_head_commit": required_head,
        "review_report_file_sha256": file_sha256(report_path),
        "review_report_sha256": report["report_sha256"],
        "stage_item_count": len(stage_items),
        "held_item_count": len(held_items),
        "expected_rows": ROWS_PER_STAGE * len(stage_items),
        "stage_items": stage_items,
        "held_items": held_items,
    }
    manifest["manifest_sha256"] = sha256(manifest)
    return manifest


async def validate_live_item(
    conn: Any,
    manifest: dict[str, Any],
    item: dict[str, Any],
    registry: dict[str, dict[str, Any]],
    *,
    replay: bool,
) -> tuple[dict[str, Any], str]:
    source, resolved = await resolve_current(
        conn,
        manifest["owner_user_id"],
        item["observation_id"],
        registry,
        allow_existing_plan=replay,
    )
    synthetic_review = {
        "observation_sha256": item["observation_sha256"],
        "evidence_id": item["evidence_id"],
        "predicate": item["predicate"],
        "action": item["action"],
        "reason_codes": item["reason_codes"],
        "semantic_key_sha256": item["semantic_key_sha256"],
        "target_claim_id": item["target_claim_id"],
        "expected_revision_number": item["expected_revision_number"],
        "existing_claim_status": (
            None if item["action"] == "create" else "supported"
        ),
        "canonical_text": item["canonical_text"],
        "packet_sha256": item["packet_sha256"],
        "owner_manifest_sha256": item["owner_manifest_sha256"],
    }
    compare_review(synthetic_review, source, resolved)
    packet_text = stable_json(resolved["packet"])
    if (
        resolved["packet"] != item["packet"]
        or hashlib.sha256(packet_text.encode()).hexdigest()
        != item["packet_text_sha256"]
    ):
        raise ClaimTargetStageError("live projection packet drifted")
    return resolved, packet_text


async def apply_or_replay(
    args: argparse.Namespace,
    conn: Any,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    load_authorization(private_path(args.authorization), manifest)
    if args.confirm != CONFIRMATION:
        raise ClaimTargetStageError("confirmation phrase mismatch")
    if os.getenv("MEMORY_V1_REQUIRED_HEAD") != manifest["required_head_commit"]:
        raise ClaimTargetStageError("runtime head differs from manifest")
    if os.getenv(APPLY_ENV) != "authorized":
        raise ClaimTargetStageError("apply environment authorization is absent")
    _, registry = load_contracts()
    replay = args.command == "replay"
    transaction = conn.transaction(isolation="serializable")
    await transaction.start()
    outcomes: list[dict[str, Any]] = []
    rows_written = 0
    try:
        await set_actor(conn, manifest["owner_user_id"])
        prepared = []
        for item in manifest["stage_items"]:
            resolved, packet_text = await validate_live_item(
                conn, manifest, item, registry, replay=replay
            )
            prepared.append((item, resolved, packet_text))
        for item, resolved, packet_text in prepared:
            function = (
                "memory.stage_projection_plan_v5_2"
                if item["action"] == "create"
                else "memory.stage_projection_reinforcement_v5_2"
            )
            row = await conn.fetchrow(
                f"SELECT * FROM {function}($1,$2,$3)",
                uuid.UUID(item["plan_id"]),
                packet_text,
                resolved["owner_manifest_sha256"],
            )
            if row is None:
                raise ClaimTargetStageError("stage function returned no result")
            expected_outcome = "replayed" if replay else "applied"
            expected_rows = 0 if replay else ROWS_PER_STAGE
            if row["outcome"] != expected_outcome or row["rows_written"] != expected_rows:
                raise ClaimTargetStageError("stage outcome or row budget mismatch")
            rows_written += row["rows_written"]
            outcomes.append(
                {
                    "observation_id": item["observation_id"],
                    "plan_id": item["plan_id"],
                    "action": item["action"],
                    "outcome": row["outcome"],
                    "rows_written": row["rows_written"],
                }
            )
            await conn.execute("SET CONSTRAINTS ALL DEFERRED")
        expected_total = 0 if replay else manifest["expected_rows"]
        if rows_written != expected_total:
            raise ClaimTargetStageError("transaction row budget mismatch")
        await transaction.commit()
    except BaseException:
        await transaction.rollback()
        raise
    result: dict[str, Any] = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.command,
        "owner_user_id": manifest["owner_user_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "stage_item_count": len(outcomes),
        "held_item_count": manifest["held_item_count"],
        "rows_written": rows_written,
        "outcomes": outcomes,
        "claims_written": 0,
        "qdrant_writes": 0,
        "retrieval_changes": 0,
        "prompt_influence": 0,
    }
    result["result_sha256"] = sha256(result)
    return result


async def cross_owner(
    args: argparse.Namespace,
    conn: Any,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    other = str(uuid.UUID(args.other_owner))
    if other == manifest["owner_user_id"]:
        raise ClaimTargetStageError("cross-owner probe requires another owner")
    transaction = conn.transaction(readonly=True, isolation="repeatable_read")
    await transaction.start()
    rejected = False
    try:
        await set_actor(conn, other)
        probe = conn.transaction()
        await probe.start()
        try:
            await load_source(conn, manifest["stage_items"][0]["observation_id"])
        except Exception:
            rejected = True
            await probe.rollback()
        else:
            await probe.commit()
        if await conn.fetchval("SELECT txid_current_if_assigned()") is not None:
            raise ClaimTargetStageError("cross-owner probe assigned a transaction ID")
    finally:
        await transaction.rollback()
    if not rejected:
        raise ClaimTargetStageError("cross-owner observation was exposed")
    result: dict[str, Any] = {
        "contract_version": RESULT_CONTRACT,
        "mode": "cross-owner",
        "owner_user_id": manifest["owner_user_id"],
        "other_owner_user_id": other,
        "manifest_sha256": manifest["manifest_sha256"],
        "cross_owner_rejected": True,
        "rows_written": 0,
    }
    result["result_sha256"] = sha256(result)
    return result


async def run() -> int:
    args = arguments()
    output = private_path(args.output, output=True)
    if args.command == "authorize":
        manifest = load_manifest(private_path(args.manifest))
        result: dict[str, Any] = {
            "contract_version": AUTHORIZATION_CONTRACT,
            "owner_user_id": manifest["owner_user_id"],
            "required_head_commit": manifest["required_head_commit"],
            "manifest_sha256": manifest["manifest_sha256"],
            "authorized_action": CONFIRMATION,
            "expected_rows": manifest["expected_rows"],
        }
        result["authorization_sha256"] = sha256(result)
        write_private(output, result)
        return 0

    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ClaimTargetStageError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ClaimTargetStageError("POSTGRES_DSN must authenticate as brains_app")
        if args.command == "manifest":
            result = await build_manifest(args, conn)
        else:
            manifest = load_manifest(private_path(args.manifest))
            if args.command in {"apply", "replay"}:
                result = await apply_or_replay(args, conn, manifest)
            else:
                result = await cross_owner(args, conn, manifest)
    finally:
        await conn.close()
    write_private(output, result)
    print(
        json.dumps(
            {
                "contract_version": result["contract_version"],
                "mode": result.get("mode", args.command),
                "rows_written": result.get("rows_written", 0),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
