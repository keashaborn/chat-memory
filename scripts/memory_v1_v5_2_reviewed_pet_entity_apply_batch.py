#!/usr/bin/env python3
"""Apply three reviewed pet entity resolutions and bind three observations."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any
import uuid

import asyncpg


MANIFEST_CONTRACT = "memory_v1_v5_2_reviewed_pet_entity_apply_manifest_v1"
PLAN_CONTRACT = "memory_v1_v5_2_reviewed_pet_entity_apply_plan_v1"
AUTHORIZATION_CONTRACT = "memory_v1_v5_2_reviewed_pet_entity_apply_authorization_v1"
REPORT_CONTRACT = "memory_v1_v5_2_reviewed_pet_entity_apply_report_v1"
CONFIRMATION = "APPLY_THREE_REVIEWED_PET_ENTITY_RESOLUTIONS_AND_BIND_ONLY"
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
ITEM_COUNT = 3
NEW_ENTITIES = 1
NEW_ALIASES = 3
TOTAL_BINDINGS = 3
NEW_OPERATION_REQUESTS = 3
NEW_ROWS = 13
REQUEST_NAMESPACE = uuid.UUID("1e68525c-5c88-4256-8e51-12b86f9b902a")
EXPECTED: dict[str, list[dict[str, Any]]] = {
    "c5d6f5cf-c6d6-554c-85d9-02150e8b8ae7": [
        {
            "entity_ref": "e00",
            "resolution_id": "560ecd15-0831-4088-8c64-206fcde78de7",
            "review_id": "1330e048-ae11-48b4-92e7-3074fa210ecb",
            "entity_type": "animal",
            "mention_kind": "named",
            "name_text": "Keasha von Steffen Haus",
            "action": "link_existing",
            "decision_state": "manual_review_required",
            "selected_entity_id": "6db538e2-b7f9-48ff-b2bc-db757708b660",
            "proposed_entity": None,
            "observation_count": 1,
            "expected_bindings": 1,
        },
    ],
    "2be95051-30c8-5f87-8ff9-e0999600b447": [
        {
            "entity_ref": "e00",
            "resolution_id": "4e0759d2-9347-4215-be5b-2dede8fcfbd9",
            "review_id": "b3910819-76f2-4a4f-8266-a1d15c1c0e6e",
            "entity_type": "animal",
            "mention_kind": "named",
            "name_text": "Dahlia",
            "action": "link_existing",
            "decision_state": "manual_review_required",
            "selected_entity_id": "0c620047-b302-44ef-b04a-810de3b311fc",
            "proposed_entity": None,
            "observation_count": 1,
            "expected_bindings": 1,
        },
    ],
    "88161526-0c53-5291-8601-0bd0ba41da53": [
        {
            "entity_ref": "e00",
            "resolution_id": "704947a6-81de-4dd5-ad42-848753588f9d",
            "review_id": "5ec22a19-ce7b-469b-b709-f4aa20fda151",
            "entity_type": "animal",
            "mention_kind": "named",
            "name_text": "Helsing",
            "action": "create_new",
            "decision_state": "manual_review_required",
            "selected_entity_id": None,
            "proposed_entity": {
                "entity_type": "animal",
                "identity_state": "named",
                "canonical_name": "Helsing",
                "display_label": "Helsing",
                "creation_reason": "new_named_entity_no_exact_owner_match",
            },
            "observation_count": 1,
            "expected_bindings": 1,
        },
    ],
}


class EntityApplyError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_valid(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def parse_utc(value: Any, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EntityApplyError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EntityApplyError(f"{field} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def repository_state() -> tuple[Path, str]:
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout
    if status.strip():
        raise EntityApplyError("entity apply requires a clean Git worktree")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    return root, head


def review_root(path_value: str) -> Path:
    root = Path(path_value).resolve(strict=True)
    if not root.is_dir() or stat.S_IMODE(root.stat().st_mode) & 0o022:
        raise EntityApplyError("review root is writable by a peer")
    return root


def secure_input(path_value: str, root: Path) -> tuple[Path, bytes]:
    path = Path(path_value).resolve(strict=True)
    if (
        not path.is_file()
        or stat.S_IMODE(path.stat().st_mode) != 0o600
        or not path.is_relative_to(root)
    ):
        raise EntityApplyError("input must be a mode-0600 file inside review root")
    return path, path.read_bytes()


def secure_write(path_value: str, value: dict[str, Any]) -> tuple[Path, str]:
    path = Path(path_value).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o022:
        raise EntityApplyError("output directory is writable by a peer")
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
    return path, sha256_bytes(payload)


def exact_object(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise EntityApplyError(f"{field} fields differ from the contract")
    return value


def decode_jsonb(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise EntityApplyError(f"{field} did not return an object")
    return value


def normalized_planner_item(
    raw: dict[str, Any], expected: dict[str, Any], evidence_id: str
) -> dict[str, Any]:
    semantic = {
        key: raw.get(key)
        for key in (
            "entity_ref", "resolution_id", "entity_type", "mention_kind",
            "name_text", "action", "decision_state", "selected_entity_id",
            "proposed_entity", "observation_count",
        )
    }
    locked = {
        key: expected[key]
        for key in (
            "entity_ref", "resolution_id", "entity_type", "mention_kind",
            "name_text", "action", "decision_state", "selected_entity_id",
            "proposed_entity", "observation_count",
        )
    }
    if (
        semantic != locked
        or raw.get("latest_review_id") != expected["review_id"]
        or raw.get("latest_review_decision")
           != ("approved" if expected["review_id"] else None)
        or raw.get("existing_apply_count") != 0
    ):
        raise EntityApplyError(
            f"entity apply candidate drifted: {evidence_id}/{expected['entity_ref']}"
        )
    for key in ("resolution_id", "mention_id"):
        uuid.UUID(str(raw.get(key)))
    for key in ("mention_sha256", "candidate_set_sha256", "decision_sha256"):
        if not sha256_valid(raw.get(key)):
            raise EntityApplyError(f"entity apply {key} is invalid")
    return {
        "evidence_id": evidence_id,
        "entity_ref": expected["entity_ref"],
        "resolution_id": expected["resolution_id"],
        "review_id": expected["review_id"],
        "mention_id": raw["mention_id"],
        "entity_type": expected["entity_type"],
        "mention_kind": expected["mention_kind"],
        "name_text": expected["name_text"],
        "action": expected["action"],
        "decision_state": expected["decision_state"],
        "selected_entity_id": expected["selected_entity_id"],
        "proposed_entity": expected["proposed_entity"],
        "mention_sha256": raw["mention_sha256"],
        "candidate_set_sha256": raw["candidate_set_sha256"],
        "decision_sha256": raw["decision_sha256"],
        "observation_count": expected["observation_count"],
        "expected_bindings": expected["expected_bindings"],
    }


async def apply_preflight(
    conn: asyncpg.Connection, item: dict[str, Any]
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT resolution_id,action::text,decision_state::text,review_id,
               prospective_entity_id,entity_state_sha256,apply_manifest_sha256
        FROM memory.preflight_entity_resolution_apply_v5_2($1::uuid,$2::uuid)
        """,
        uuid.UUID(item["resolution_id"]),
        uuid.UUID(item["review_id"]) if item["review_id"] else None,
    )
    if row is None:
        raise EntityApplyError("entity apply preflight returned no row")
    result = {
        "resolution_id": str(row["resolution_id"]),
        "action": row["action"],
        "decision_state": row["decision_state"],
        "review_id": str(row["review_id"]) if row["review_id"] else None,
        "prospective_entity_id": (
            str(row["prospective_entity_id"])
            if row["prospective_entity_id"] else None
        ),
        "entity_state_sha256": row["entity_state_sha256"],
        "apply_manifest_sha256": row["apply_manifest_sha256"],
    }
    if (
        result["resolution_id"] != item["resolution_id"]
        or result["action"] != item["action"]
        or result["decision_state"] != item["decision_state"]
        or result["review_id"] != item["review_id"]
        or (
            item["action"] == "link_existing"
            and result["prospective_entity_id"] != item["selected_entity_id"]
        )
        or (
            item["action"] == "create_new"
            and result["prospective_entity_id"] is not None
        )
        or not sha256_valid(result["entity_state_sha256"])
        or not sha256_valid(result["apply_manifest_sha256"])
    ):
        raise EntityApplyError("entity apply preflight differs from manifest")
    return result


async def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    _, head = repository_state()
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise EntityApplyError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    items: list[dict[str, Any]] = []
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            if await conn.fetchval("SELECT session_user") != "brains_app":
                raise EntityApplyError("POSTGRES_DSN must authenticate as brains_app")
            await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
            for evidence_id, expected_items in EXPECTED.items():
                raw = await conn.fetchval(
                    "SELECT memory.plan_owner_v5_2_entity_apply_review_v1($1::uuid)",
                    uuid.UUID(evidence_id),
                )
                plan = decode_jsonb(raw, "entity apply planner")
                if (
                    plan.get("contract_version")
                    != "memory_v1_v5_2_entity_apply_review_plan_v1"
                    or plan.get("policy_version")
                    != "memory_v1_v5_2_entity_apply_review_policy_v1"
                    or plan.get("owner_user_id") != OWNER
                    or plan.get("evidence_id") != evidence_id
                    or len(plan.get("items") or []) != len(expected_items)
                ):
                    raise EntityApplyError(f"entity apply plan drifted: {evidence_id}")
                by_ref = {item["entity_ref"]: item for item in plan["items"]}
                if set(by_ref) != {item["entity_ref"] for item in expected_items}:
                    raise EntityApplyError(f"entity apply refs drifted: {evidence_id}")
                for expected in expected_items:
                    items.append(normalized_planner_item(
                        by_ref[expected["entity_ref"]], expected, evidence_id
                    ))
    finally:
        await conn.close()
    manifest = {
        "contract_version": MANIFEST_CONTRACT,
        "target_server": "seebx",
        "owner_user_id": OWNER,
        "required_head_commit": head,
        "expected_item_count": ITEM_COUNT,
        "expected_new_entities": NEW_ENTITIES,
        "expected_total_bindings": TOTAL_BINDINGS,
        "expected_new_rows": NEW_ROWS,
        "items": items,
    }
    path, digest = secure_write(args.output, manifest)
    return {
        "contract_version": "memory_v1_v5_2_reviewed_pet_entity_apply_manifest_report_v1",
        "manifest_path": str(path),
        "manifest_sha256": digest,
        "item_count": ITEM_COUNT,
        "expected_new_rows": NEW_ROWS,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }


MANIFEST_KEYS = {
    "contract_version", "target_server", "owner_user_id",
    "required_head_commit", "expected_item_count", "expected_new_entities",
    "expected_total_bindings", "expected_new_rows", "items",
}
ITEM_KEYS = {
    "evidence_id", "entity_ref", "resolution_id", "review_id", "mention_id",
    "entity_type", "mention_kind", "name_text", "action", "decision_state",
    "selected_entity_id", "proposed_entity", "mention_sha256",
    "candidate_set_sha256", "decision_sha256", "observation_count",
    "expected_bindings",
}


def load_manifest(path_value: str, root: Path, head: str) -> tuple[dict[str, Any], str]:
    _, raw = secure_input(path_value, root)
    manifest = exact_object(json.loads(raw), MANIFEST_KEYS, "entity apply manifest")
    if (
        manifest["contract_version"] != MANIFEST_CONTRACT
        or manifest["target_server"] != "seebx"
        or manifest["owner_user_id"] != OWNER
        or manifest["required_head_commit"] != head
        or manifest["expected_item_count"] != ITEM_COUNT
        or manifest["expected_new_entities"] != NEW_ENTITIES
        or manifest["expected_total_bindings"] != TOTAL_BINDINGS
        or manifest["expected_new_rows"] != NEW_ROWS
        or not isinstance(manifest["items"], list)
        or len(manifest["items"]) != ITEM_COUNT
    ):
        raise EntityApplyError("entity apply manifest is stale or invalid")
    seen: set[str] = set()
    for item in manifest["items"]:
        exact_object(item, ITEM_KEYS, "entity apply manifest item")
        resolution_id = str(uuid.UUID(item["resolution_id"]))
        if resolution_id in seen:
            raise EntityApplyError("entity apply manifest repeats a resolution")
        seen.add(resolution_id)
        if any(not sha256_valid(item[key]) for key in (
            "mention_sha256", "candidate_set_sha256", "decision_sha256"
        )):
            raise EntityApplyError("entity apply manifest hash is invalid")
    if (
        sum(item["expected_bindings"] for item in manifest["items"])
        != TOTAL_BINDINGS
    ):
        raise EntityApplyError("reviewed pet entity apply binding budget is invalid")
    return manifest, sha256_bytes(raw)


async def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    _, head = repository_state()
    root = review_root(args.review_root)
    manifest, manifest_sha = load_manifest(args.manifest, root, head)
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise EntityApplyError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    planned: list[dict[str, Any]] = []
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
            for item in manifest["items"]:
                planned.append({
                    "manifest_item": item,
                    "preflight": await apply_preflight(conn, item),
                })
    finally:
        await conn.close()
    plan = {
        "contract_version": PLAN_CONTRACT,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "owner_scoped_entity_apply_zero_write_plan",
        "target_server": "seebx",
        "owner_user_id": OWNER,
        "required_head_commit": head,
        "review_root": str(root),
        "manifest_path": str(Path(args.manifest).resolve()),
        "manifest_sha256": manifest_sha,
        "item_count": ITEM_COUNT,
        "expected_new_entities": NEW_ENTITIES,
        "expected_total_bindings": TOTAL_BINDINGS,
        "expected_new_rows": NEW_ROWS,
        "items": planned,
        "apply_authorized": False,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }
    path, digest = secure_write(args.output, plan)
    return {"plan_path": str(path), "plan_sha256": digest, **plan}


PLAN_KEYS = {
    "contract_version", "generated_at", "mode", "target_server",
    "owner_user_id", "required_head_commit", "review_root", "manifest_path",
    "manifest_sha256", "item_count", "expected_new_entities",
    "expected_total_bindings", "expected_new_rows", "items",
    "apply_authorized", "database_writes", "qdrant_writes",
    "external_model_calls",
}


def load_plan(path_value: str, root: Path, head: str) -> tuple[dict[str, Any], str]:
    _, raw = secure_input(path_value, root)
    plan = exact_object(json.loads(raw), PLAN_KEYS, "entity apply plan")
    if (
        plan["contract_version"] != PLAN_CONTRACT
        or plan["mode"] != "owner_scoped_entity_apply_zero_write_plan"
        or plan["target_server"] != "seebx"
        or plan["owner_user_id"] != OWNER
        or plan["required_head_commit"] != head
        or plan["review_root"] != str(root)
        or plan["item_count"] != ITEM_COUNT
        or plan["expected_new_entities"] != NEW_ENTITIES
        or plan["expected_total_bindings"] != TOTAL_BINDINGS
        or plan["expected_new_rows"] != NEW_ROWS
        or plan["apply_authorized"] is not False
        or any(plan[key] != 0 for key in (
            "database_writes", "qdrant_writes", "external_model_calls"
        ))
    ):
        raise EntityApplyError("entity apply plan is stale or invalid")
    manifest, manifest_sha = load_manifest(plan["manifest_path"], root, head)
    if (
        manifest_sha != plan["manifest_sha256"]
        or [entry["manifest_item"] for entry in plan["items"]] != manifest["items"]
    ):
        raise EntityApplyError("entity apply manifest changed after planning")
    return plan, sha256_bytes(raw)


AUTH_KEYS = {
    "contract_version", "authorization_id", "authorized", "authorized_by",
    "authorized_at", "expires_at", "expected_head_commit", "target_server",
    "scope", "owner_user_id", "plan_sha256", "expected_item_count",
    "expected_new_entities", "expected_total_bindings", "expected_new_rows",
    "confirmation",
}


def load_authorization(
    path_value: str, root: Path, plan: dict[str, Any], plan_sha: str, head: str
) -> tuple[dict[str, Any], str]:
    _, raw = secure_input(path_value, root)
    authorization = exact_object(json.loads(raw), AUTH_KEYS, "entity apply authorization")
    if (
        authorization["contract_version"] != AUTHORIZATION_CONTRACT
        or authorization["authorized"] is not True
        or authorization["authorized_by"] != "Eric Lund"
        or authorization["expected_head_commit"] != head
        or authorization["target_server"] != "seebx"
        or authorization["scope"]
        != "apply_reviewed_pet_entities_and_bind_only"
        or authorization["owner_user_id"] != OWNER
        or authorization["plan_sha256"] != plan_sha
        or authorization["expected_item_count"] != ITEM_COUNT
        or authorization["expected_new_entities"] != NEW_ENTITIES
        or authorization["expected_total_bindings"] != TOTAL_BINDINGS
        or authorization["expected_new_rows"] != NEW_ROWS
        or authorization["confirmation"] != CONFIRMATION
    ):
        raise EntityApplyError("entity apply authorization is invalid")
    uuid.UUID(authorization["authorization_id"])
    authorized_at = parse_utc(authorization["authorized_at"], "authorized_at")
    expires_at = parse_utc(authorization["expires_at"], "expires_at")
    now = dt.datetime.now(dt.timezone.utc)
    if (
        expires_at <= authorized_at
        or expires_at - authorized_at > dt.timedelta(minutes=30)
        or now < authorized_at - dt.timedelta(seconds=30)
        or now >= expires_at
    ):
        raise EntityApplyError("entity apply authorization is expired or overbroad")
    return authorization, sha256_bytes(raw)


def request_id(plan_sha: str, resolution_id: str) -> uuid.UUID:
    return uuid.uuid5(REQUEST_NAMESPACE, f"{plan_sha}|{resolution_id}|apply")


async def call_apply(
    conn: asyncpg.Connection, item: dict[str, Any],
    preflight_row: dict[str, Any], plan_sha: str
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT applied_entity_id,outcome,bindings_created,result
        FROM memory.apply_entity_resolution_v5_2(
          $1::uuid,$2::uuid,$3::uuid,$4
        )
        """,
        request_id(plan_sha, item["resolution_id"]),
        uuid.UUID(item["resolution_id"]),
        uuid.UUID(item["review_id"]) if item["review_id"] else None,
        preflight_row["apply_manifest_sha256"],
    )
    if row is None:
        raise EntityApplyError("entity apply returned no row")
    return {
        "resolution_id": item["resolution_id"],
        "review_id": item["review_id"],
        "applied_entity_id": str(row["applied_entity_id"]),
        "outcome": row["outcome"],
        "bindings_created": int(row["bindings_created"]),
    }


async def apply_plan(args: argparse.Namespace) -> dict[str, Any]:
    if os.getenv("MEMORY_V1_V5_2_REVIEWED_PET_ENTITY_APPLY") != "authorized":
        raise EntityApplyError("reviewed pet entity apply environment gate is closed")
    if args.confirm != CONFIRMATION:
        raise EntityApplyError(f"--confirm must equal {CONFIRMATION}")
    _, head = repository_state()
    root = review_root(args.review_root)
    plan, plan_sha = load_plan(args.plan, root, head)
    authorization, authorization_sha = load_authorization(
        args.authorization, root, plan, plan_sha, head
    )
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise EntityApplyError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    first: list[dict[str, Any]] = []
    replay: list[dict[str, Any]] = []
    try:
        async with conn.transaction(isolation="serializable"):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"{OWNER}|reviewed_pet_entity_apply",
            )
            for entry in plan["items"]:
                fresh = await apply_preflight(conn, entry["manifest_item"])
                if fresh != entry["preflight"]:
                    raise EntityApplyError("entity apply preflight changed before apply")
                result = await call_apply(
                    conn, entry["manifest_item"], fresh, plan_sha
                )
                if (
                    result["outcome"] != "applied"
                    or result["bindings_created"]
                       != entry["manifest_item"]["expected_bindings"]
                    or (
                        entry["manifest_item"]["action"] == "link_existing"
                        and result["applied_entity_id"]
                        != entry["manifest_item"]["selected_entity_id"]
                    )
                ):
                    raise EntityApplyError("entity apply result differs from plan")
                first.append(result)
        async with conn.transaction(isolation="serializable"):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
            for entry in plan["items"]:
                result = await call_apply(
                    conn, entry["manifest_item"], entry["preflight"], plan_sha
                )
                if result["outcome"] != "replayed" or result["bindings_created"] != 0:
                    raise EntityApplyError("entity apply replay wrote data")
                replay.append(result)
    finally:
        await conn.close()
    if sum(item["bindings_created"] for item in first) != TOTAL_BINDINGS:
        raise EntityApplyError("reviewed pet entity apply binding total is invalid")
    report = {
        "contract_version": REPORT_CONTRACT,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "owner_scoped_transactional_entity_apply_and_binding",
        "target_server": "seebx",
        "owner_user_id": OWNER,
        "required_head_commit": head,
        "plan_sha256": plan_sha,
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": authorization_sha,
        "item_count": ITEM_COUNT,
        "new_entities": NEW_ENTITIES,
        "new_entity_resolution_applies": ITEM_COUNT,
        "new_alias_observations": NEW_ALIASES,
        "new_observation_bindings": TOTAL_BINDINGS,
        "new_operation_requests": NEW_OPERATION_REQUESTS,
        "new_rows": NEW_ROWS,
        "first_results": first,
        "replay_results": replay,
        "zero_write_replay": True,
        "claim_writes": 0,
        "projection_writes": 0,
        "qdrant_writes": 0,
        "retrieval_changes": 0,
        "prompt_changes": 0,
        "hard_stop": "before_claims_projection_qdrant_retrieval_or_prompt_influence",
    }
    path, digest = secure_write(args.output, report)
    return {"report_path": str(path), "report_sha256": digest, **report}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--output", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--manifest", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument("--review-root", required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--authorization", required=True)
    apply.add_argument("--output", required=True)
    apply.add_argument("--review-root", required=True)
    apply.add_argument("--confirm", required=True)
    return parser.parse_args()


async def main() -> int:
    args = arguments()
    if args.command == "manifest":
        result = await build_manifest(args)
    elif args.command == "plan":
        result = await build_plan(args)
    else:
        result = await apply_plan(args)
    print(stable_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
