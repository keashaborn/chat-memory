#!/usr/bin/env python3
"""Controlled owner-scoped V5.2 entity review without entity apply."""

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


MANIFEST_CONTRACT = "memory_v1_v5_2_entity_review_only_manifest_v1"
PLAN_CONTRACT = "memory_v1_v5_2_entity_review_only_plan_v1"
AUTHORIZATION_CONTRACT = "memory_v1_v5_2_entity_review_only_authorization_v1"
REPORT_CONTRACT = "memory_v1_v5_2_entity_review_only_apply_report_v1"
CONFIRMATION = "REVIEW_OWNER_V5_2_ENTITY_RESOLUTIONS_WITHOUT_APPLY"
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
REQUEST_NAMESPACE = uuid.UUID("dff746d4-a11c-56fc-87ae-365255f75eb2")
EXPECTED: dict[str, list[dict[str, Any]]] = {
    "fea59e7e-30f5-4139-b634-97b291c88e14": [
        {
            "entity_ref": "e01",
            "entity_type": "person",
            "name_text": "Monika",
            "proposed_entity": {
                "entity_type": "person",
                "identity_state": "named",
                "canonical_name": "Monika",
                "display_label": "Monika",
                "creation_reason": "new_named_entity_no_exact_owner_match",
            },
            "review_reason":
                "User-authorized review of the exact named person Monika; "
                "this phase stops before entity apply.",
        }
    ],
    "dcf5ece1-0e22-574f-8ac9-f3d0acc4e8f5": [
        {
            "entity_ref": "e01",
            "entity_type": "concept",
            "name_text": "clinical psychologist",
            "proposed_entity": {
                "entity_type": "concept",
                "identity_state": "named",
                "canonical_name": "clinical psychologist",
                "display_label": "clinical psychologist",
                "creation_reason": "new_named_entity_no_exact_owner_match",
            },
            "review_reason":
                "User-authorized review of the exact historical occupation "
                "concept clinical psychologist; this phase stops before entity apply.",
        },
        {
            "entity_ref": "e02",
            "entity_type": "concept",
            "name_text": "BCBA",
            "proposed_entity": {
                "entity_type": "concept",
                "identity_state": "named",
                "canonical_name": "BCBA",
                "display_label": "BCBA",
                "creation_reason": "new_named_entity_no_exact_owner_match",
            },
            "review_reason":
                "User-authorized review of the exact historical occupation "
                "concept BCBA; this phase stops before entity apply.",
        },
    ],
}


class ReviewOnlyError(RuntimeError):
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
        raise ReviewOnlyError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReviewOnlyError(f"{field} must include a timezone")
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
        raise ReviewOnlyError("entity review requires a clean Git worktree")
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
        raise ReviewOnlyError("review root is writable by a peer")
    return root


def secure_input(path_value: str, root: Path) -> tuple[Path, bytes]:
    path = Path(path_value).resolve(strict=True)
    if (
        not path.is_file()
        or stat.S_IMODE(path.stat().st_mode) != 0o600
        or not path.is_relative_to(root)
    ):
        raise ReviewOnlyError("input must be a mode-0600 file inside review root")
    return path, path.read_bytes()


def secure_write(path_value: str, value: dict[str, Any]) -> tuple[Path, str]:
    path = Path(path_value).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o022:
        raise ReviewOnlyError("output directory is writable by a peer")
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
        raise ReviewOnlyError(f"{field} fields differ from the contract")
    return value


def decode_jsonb(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ReviewOnlyError(f"{field} did not return an object")
    return value


def normalized_item(raw: dict[str, Any], expected: dict[str, Any], evidence: str) -> dict[str, Any]:
    if (
        raw.get("entity_ref") != expected["entity_ref"]
        or raw.get("entity_type") != expected["entity_type"]
        or raw.get("name_text") != expected["name_text"]
        or raw.get("action") != "create_new"
        or raw.get("decision_state") != "manual_review_required"
        or raw.get("selected_entity_id") is not None
        or raw.get("proposed_entity") != expected["proposed_entity"]
        or raw.get("review_reason_codes") != ["new_named_entity_requires_review"]
        or raw.get("existing_review_count") != 0
        or raw.get("existing_apply_count") != 0
    ):
        raise ReviewOnlyError(
            f"manual entity review candidate drifted: {evidence}/{expected['entity_ref']}"
        )
    for key in ("resolution_id", "mention_id"):
        uuid.UUID(str(raw.get(key)))
    for key in ("mention_sha256", "candidate_set_sha256", "decision_sha256"):
        if not sha256_valid(raw.get(key)):
            raise ReviewOnlyError(f"manual entity review {key} is invalid")
    return {
        "evidence_id": evidence,
        "entity_ref": expected["entity_ref"],
        "entity_type": expected["entity_type"],
        "name_text": expected["name_text"],
        "resolution_id": raw["resolution_id"],
        "mention_id": raw["mention_id"],
        "action": raw["action"],
        "decision_state": raw["decision_state"],
        "proposed_entity": raw["proposed_entity"],
        "mention_sha256": raw["mention_sha256"],
        "candidate_set_sha256": raw["candidate_set_sha256"],
        "decision_sha256": raw["decision_sha256"],
        "review_reason": expected["review_reason"],
    }


async def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    _, head = repository_state()
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ReviewOnlyError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    items: list[dict[str, Any]] = []
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            if await conn.fetchval("SELECT session_user") != "brains_app":
                raise ReviewOnlyError("POSTGRES_DSN must authenticate as brains_app")
            await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
            for evidence, expected_items in EXPECTED.items():
                raw = await conn.fetchval(
                    "SELECT memory.plan_owner_v5_2_entity_resolution_review_v1($1::uuid)",
                    uuid.UUID(evidence),
                )
                plan = decode_jsonb(raw, "entity review planner")
                if (
                    plan.get("contract_version")
                    != "memory_v1_v5_2_entity_resolution_review_plan_v1"
                    or plan.get("policy_version")
                    != "memory_v1_v5_2_entity_resolution_review_policy_v1"
                    or plan.get("owner_user_id") != OWNER
                    or plan.get("evidence_id") != evidence
                    or len(plan.get("items") or []) != len(expected_items)
                ):
                    raise ReviewOnlyError(f"entity review plan drifted: {evidence}")
                by_ref = {item["entity_ref"]: item for item in plan["items"]}
                if set(by_ref) != {item["entity_ref"] for item in expected_items}:
                    raise ReviewOnlyError(f"entity review reference set drifted: {evidence}")
                for expected in expected_items:
                    items.append(normalized_item(
                        by_ref[expected["entity_ref"]], expected, evidence
                    ))
    finally:
        await conn.close()
    manifest = {
        "contract_version": MANIFEST_CONTRACT,
        "target_server": "seebx",
        "owner_user_id": OWNER,
        "required_head_commit": head,
        "expected_item_count": 3,
        "expected_new_rows": 6,
        "items": items,
    }
    path, digest = secure_write(args.output, manifest)
    return {
        "contract_version": "memory_v1_v5_2_entity_review_only_manifest_report_v1",
        "manifest_path": str(path),
        "manifest_sha256": digest,
        "item_count": len(items),
        "expected_new_rows": 6,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }


def load_manifest(path_value: str, root: Path, head: str) -> tuple[dict[str, Any], str]:
    path, raw = secure_input(path_value, root)
    manifest = exact_object(json.loads(raw), {
        "contract_version", "target_server", "owner_user_id",
        "required_head_commit", "expected_item_count", "expected_new_rows", "items",
    }, "entity review manifest")
    if (
        manifest["contract_version"] != MANIFEST_CONTRACT
        or manifest["target_server"] != "seebx"
        or manifest["owner_user_id"] != OWNER
        or manifest["required_head_commit"] != head
        or manifest["expected_item_count"] != 3
        or manifest["expected_new_rows"] != 6
        or not isinstance(manifest["items"], list)
        or len(manifest["items"]) != 3
    ):
        raise ReviewOnlyError("entity review manifest is stale or invalid")
    seen: set[str] = set()
    for item in manifest["items"]:
        exact_object(item, {
            "evidence_id", "entity_ref", "entity_type", "name_text",
            "resolution_id", "mention_id", "action", "decision_state",
            "proposed_entity", "mention_sha256", "candidate_set_sha256",
            "decision_sha256", "review_reason",
        }, "entity review manifest item")
        resolution_id = str(uuid.UUID(item["resolution_id"]))
        if resolution_id in seen:
            raise ReviewOnlyError("entity review manifest repeats a resolution")
        seen.add(resolution_id)
        if (
            item["action"] != "create_new"
            or item["decision_state"] != "manual_review_required"
            or not item["review_reason"]
            or any(not sha256_valid(item[key]) for key in (
                "mention_sha256", "candidate_set_sha256", "decision_sha256"
            ))
        ):
            raise ReviewOnlyError("entity review manifest item is invalid")
    return manifest, sha256_bytes(raw)


async def preflight(
    conn: asyncpg.Connection, item: dict[str, Any]
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT resolution_id,action::text,decision_state::text,mention_sha256,
               candidate_set_sha256,decision_sha256,authorization_manifest_sha256
        FROM memory.preflight_entity_resolution_review_v5_2(
          $1::uuid,'approved'::memory.entity_review_decision,$2
        )
        """,
        uuid.UUID(item["resolution_id"]),
        item["review_reason"],
    )
    if row is None:
        raise ReviewOnlyError("entity review preflight returned no row")
    result = {key: (str(value) if isinstance(value, uuid.UUID) else value)
              for key, value in dict(row).items()}
    expected = {
        "resolution_id": item["resolution_id"],
        "action": item["action"],
        "decision_state": item["decision_state"],
        "mention_sha256": item["mention_sha256"],
        "candidate_set_sha256": item["candidate_set_sha256"],
        "decision_sha256": item["decision_sha256"],
    }
    if any(result.get(key) != value for key, value in expected.items()) or not sha256_valid(
        result.get("authorization_manifest_sha256")
    ):
        raise ReviewOnlyError("entity review preflight differs from manifest")
    return result


async def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    _, head = repository_state()
    root = review_root(args.review_root)
    manifest, manifest_sha = load_manifest(args.manifest, root, head)
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ReviewOnlyError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    planned: list[dict[str, Any]] = []
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
            for item in manifest["items"]:
                planned.append({
                    "manifest_item": item,
                    "preflight": await preflight(conn, item),
                })
    finally:
        await conn.close()
    plan = {
        "contract_version": PLAN_CONTRACT,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "owner_scoped_review_only_zero_write",
        "target_server": "seebx",
        "owner_user_id": OWNER,
        "required_head_commit": head,
        "review_root": str(root),
        "manifest_path": str(Path(args.manifest).resolve()),
        "manifest_sha256": manifest_sha,
        "item_count": 3,
        "expected_new_rows": 6,
        "items": planned,
        "apply_authorized": False,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }
    path, digest = secure_write(args.output, plan)
    return {"plan_path": str(path), "plan_sha256": digest, **plan}


def load_plan(path_value: str, root: Path, head: str) -> tuple[dict[str, Any], str]:
    _, raw = secure_input(path_value, root)
    plan = exact_object(json.loads(raw), {
        "contract_version", "generated_at", "mode", "target_server",
        "owner_user_id", "required_head_commit", "review_root", "manifest_path",
        "manifest_sha256", "item_count", "expected_new_rows", "items",
        "apply_authorized", "database_writes", "qdrant_writes",
        "external_model_calls",
    }, "entity review plan")
    if (
        plan["contract_version"] != PLAN_CONTRACT
        or plan["mode"] != "owner_scoped_review_only_zero_write"
        or plan["target_server"] != "seebx"
        or plan["owner_user_id"] != OWNER
        or plan["required_head_commit"] != head
        or plan["review_root"] != str(root)
        or plan["item_count"] != 3
        or plan["expected_new_rows"] != 6
        or plan["apply_authorized"] is not False
        or any(plan[key] != 0 for key in (
            "database_writes", "qdrant_writes", "external_model_calls"
        ))
    ):
        raise ReviewOnlyError("entity review plan is stale or invalid")
    manifest, manifest_sha = load_manifest(plan["manifest_path"], root, head)
    if (
        manifest_sha != plan["manifest_sha256"]
        or [entry["manifest_item"] for entry in plan["items"]] != manifest["items"]
    ):
        raise ReviewOnlyError("entity review manifest changed after planning")
    return plan, sha256_bytes(raw)


def load_authorization(
    path_value: str, root: Path, plan: dict[str, Any], plan_sha: str, head: str
) -> tuple[dict[str, Any], str]:
    _, raw = secure_input(path_value, root)
    authorization = exact_object(json.loads(raw), {
        "contract_version", "authorization_id", "authorized", "authorized_by",
        "authorized_at", "expires_at", "expected_head_commit", "target_server",
        "scope", "owner_user_id", "plan_sha256", "expected_item_count",
        "expected_new_rows", "confirmation",
    }, "entity review authorization")
    if (
        authorization["contract_version"] != AUTHORIZATION_CONTRACT
        or authorization["authorized"] is not True
        or authorization["authorized_by"] != "Eric Lund"
        or authorization["expected_head_commit"] != head
        or authorization["target_server"] != "seebx"
        or authorization["scope"] != "review_owner_v5_2_entity_resolutions_without_apply"
        or authorization["owner_user_id"] != OWNER
        or authorization["plan_sha256"] != plan_sha
        or authorization["expected_item_count"] != 3
        or authorization["expected_new_rows"] != 6
        or authorization["confirmation"] != CONFIRMATION
    ):
        raise ReviewOnlyError("entity review authorization is invalid")
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
        raise ReviewOnlyError("entity review authorization is expired or overbroad")
    return authorization, sha256_bytes(raw)


def request_id(plan_sha: str, resolution_id: str) -> uuid.UUID:
    return uuid.uuid5(REQUEST_NAMESPACE, f"{plan_sha}|{resolution_id}|review_only")


async def call_review(
    conn: asyncpg.Connection, item: dict[str, Any], preflight_row: dict[str, Any],
    plan_sha: str
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT review_id,outcome,result
        FROM memory.review_entity_resolution_v5_2(
          $1::uuid,$2::uuid,'approved'::memory.entity_review_decision,$3,$4
        )
        """,
        request_id(plan_sha, item["resolution_id"]),
        uuid.UUID(item["resolution_id"]),
        item["review_reason"],
        preflight_row["authorization_manifest_sha256"],
    )
    if row is None:
        raise ReviewOnlyError("entity review returned no row")
    return {
        "resolution_id": item["resolution_id"],
        "review_id": str(row["review_id"]),
        "outcome": row["outcome"],
    }


async def apply_plan(args: argparse.Namespace) -> dict[str, Any]:
    if os.getenv("MEMORY_V1_V5_2_ENTITY_REVIEW_ONLY_APPLY") != "authorized":
        raise ReviewOnlyError("entity review apply environment gate is closed")
    if args.confirm != CONFIRMATION:
        raise ReviewOnlyError(f"--confirm must equal {CONFIRMATION}")
    _, head = repository_state()
    root = review_root(args.review_root)
    plan, plan_sha = load_plan(args.plan, root, head)
    authorization, authorization_sha = load_authorization(
        args.authorization, root, plan, plan_sha, head
    )
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ReviewOnlyError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    first: list[dict[str, Any]] = []
    replay: list[dict[str, Any]] = []
    try:
        async with conn.transaction(isolation="serializable"):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"{OWNER}|entity_review_only_v5_2",
            )
            for entry in plan["items"]:
                fresh = await preflight(conn, entry["manifest_item"])
                if fresh != entry["preflight"]:
                    raise ReviewOnlyError("entity review preflight changed before apply")
                result = await call_review(
                    conn, entry["manifest_item"], fresh, plan_sha
                )
                if result["outcome"] != "applied":
                    raise ReviewOnlyError("first entity review did not apply")
                first.append(result)
        async with conn.transaction(isolation="serializable"):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", OWNER)
            for entry in plan["items"]:
                result = await call_review(
                    conn, entry["manifest_item"], entry["preflight"], plan_sha
                )
                if result["outcome"] != "replayed":
                    raise ReviewOnlyError("entity review replay wrote data")
                replay.append(result)
    finally:
        await conn.close()
    report = {
        "contract_version": REPORT_CONTRACT,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "owner_scoped_review_only_applied",
        "target_server": "seebx",
        "owner_user_id": OWNER,
        "required_head_commit": head,
        "plan_sha256": plan_sha,
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": authorization_sha,
        "item_count": 3,
        "new_review_rows": 3,
        "new_operation_request_rows": 3,
        "new_rows": 6,
        "first_results": first,
        "replay_results": replay,
        "zero_write_replay": True,
        "entity_apply_calls": 0,
        "claim_writes": 0,
        "qdrant_writes": 0,
        "retrieval_changes": 0,
        "prompt_changes": 0,
        "hard_stop": "before_entity_apply_claims_projection_or_retrieval",
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
