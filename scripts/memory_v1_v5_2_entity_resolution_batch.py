#!/usr/bin/env python3
"""Controlled owner-scoped reconciliation/review/apply for V5.2 entities."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import sys
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_v5_stage_batch import (
    exact_object,
    parse_utc,
    repository_state,
    review_root,
    secure_input,
    secure_write,
    sha256_bytes,
)


MANIFEST_CONTRACT = "memory_v1_v5_2_entity_resolution_batch_manifest_v1"
PLAN_CONTRACT = "memory_v1_v5_2_entity_resolution_batch_plan_v1"
AUTHORIZATION_CONTRACT = "memory_v1_v5_2_entity_resolution_batch_authorization_v1"
REPORT_CONTRACT = "memory_v1_v5_2_entity_resolution_batch_apply_report_v1"
CONFIRMATION = "RECONCILE_REVIEW_AND_APPLY_OWNER_V5_2_ENTITY_RESOLUTIONS_ONLY"
DEFAULT_REVIEW_ROOT = "/home/ubuntu/memory-v1-reviews"
REQUEST_NAMESPACE = uuid.UUID("1c19d198-a701-5633-a7ae-24f60996582f")
MAX_ITEMS = 32
MAX_NEW_ROWS = 500
OPERATIONS = {
    "auto_apply",
    "manual_link_existing_and_apply",
    "reconcile_existing_and_apply",
}


class EntityResolutionBatchError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or apply one reviewed owner-scoped V5.2 entity batch."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--manifest", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument("--review-root", default=DEFAULT_REVIEW_ROOT)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--authorization", required=True)
    apply.add_argument("--output", required=True)
    apply.add_argument("--review-root", default=DEFAULT_REVIEW_ROOT)
    apply.add_argument("--confirm", required=True)
    return parser.parse_args()


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise EntityResolutionBatchError(f"{field} must be a UUID") from exc


def _digest(value: Any, field: str) -> str:
    if not (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    ):
        raise EntityResolutionBatchError(f"{field} must be a lowercase SHA-256")
    return value


def _expected_base_rows(items: list[dict[str, Any]]) -> int:
    rows_by_operation = {
        "auto_apply": 2,
        "manual_link_existing_and_apply": 4,
        "reconcile_existing_and_apply": 8,
    }
    return sum(rows_by_operation[item["operation"]] for item in items)


def load_manifest(
    path_value: str, *, root: Path
) -> tuple[dict[str, Any], Path, str]:
    path = secure_input(path_value, root=root)
    raw = path.read_bytes()
    value = exact_object(
        json.loads(raw),
        {
            "contract_version",
            "target_server",
            "owner_user_id",
            "expected_total_bindings",
            "expected_new_rows",
            "items",
        },
        "entity resolution batch manifest",
    )
    owner = _uuid(value["owner_user_id"], "owner_user_id")
    if value["contract_version"] != MANIFEST_CONTRACT or value["target_server"] != "seebx":
        raise EntityResolutionBatchError("manifest contract or target server is invalid")
    if not isinstance(value["items"], list) or not 1 <= len(value["items"]) <= MAX_ITEMS:
        raise EntityResolutionBatchError("manifest must contain 1 to 32 items")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(value["items"]):
        if not isinstance(raw_item, dict):
            raise EntityResolutionBatchError(
                f"manifest item {index} fields do not match the contract"
            )
        operation = raw_item.get("operation")
        item_keys = {
            "resolution_id",
            "operation",
            "expected_action",
            "expected_decision_state",
            "review_reason",
        }
        if operation == "reconcile_existing_and_apply":
            item_keys.update({"expected_entity_id", "successor_resolution_id"})
        elif operation == "manual_link_existing_and_apply":
            item_keys.add("expected_entity_id")
        item = exact_object(
            raw_item,
            item_keys,
            f"manifest item {index}",
        )
        resolution_id = _uuid(item["resolution_id"], f"items[{index}].resolution_id")
        if resolution_id in seen:
            raise EntityResolutionBatchError("manifest contains duplicate resolution IDs")
        seen.add(resolution_id)
        operation = item["operation"]
        if operation not in OPERATIONS:
            raise EntityResolutionBatchError("manifest operation is invalid")
        if operation == "auto_apply":
            if (
                item["expected_action"] != "link_existing"
                or item["expected_decision_state"] != "auto_link_eligible"
                or item["review_reason"] is not None
            ):
                raise EntityResolutionBatchError("auto-apply item contract is invalid")
        elif operation == "manual_link_existing_and_apply":
            reason = item["review_reason"]
            if (
                item["expected_action"] != "link_existing"
                or item["expected_decision_state"] != "manual_review_required"
                or not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 500
            ):
                raise EntityResolutionBatchError(
                    "manual-link-existing-and-apply item contract is invalid"
                )
            item["expected_entity_id"] = _uuid(
                item["expected_entity_id"],
                f"items[{index}].expected_entity_id",
            )
        else:
            reason = item["review_reason"]
            if (
                item["expected_action"] != "create_new"
                or item["expected_decision_state"] != "manual_review_required"
                or not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 500
            ):
                raise EntityResolutionBatchError(
                    "reconcile-existing-and-apply item contract is invalid"
                )
            item["expected_entity_id"] = _uuid(
                item["expected_entity_id"],
                f"items[{index}].expected_entity_id",
            )
            item["successor_resolution_id"] = _uuid(
                item["successor_resolution_id"],
                f"items[{index}].successor_resolution_id",
            )
            if item["successor_resolution_id"] in seen:
                raise EntityResolutionBatchError(
                    "manifest contains a reused successor resolution ID"
                )
            seen.add(item["successor_resolution_id"])
        items.append({**item, "resolution_id": resolution_id})
    bindings = value["expected_total_bindings"]
    new_rows = value["expected_new_rows"]
    if not isinstance(bindings, int) or bindings < 0 or bindings > MAX_NEW_ROWS:
        raise EntityResolutionBatchError("expected_total_bindings is invalid")
    calculated = _expected_base_rows(items) + bindings
    if not isinstance(new_rows, int) or new_rows != calculated or new_rows > MAX_NEW_ROWS:
        raise EntityResolutionBatchError("expected_new_rows does not match the item budget")
    return (
        {
            "owner_user_id": owner,
            "expected_total_bindings": bindings,
            "expected_new_rows": new_rows,
            "items": items,
        },
        path,
        sha256_bytes(raw),
    )


async def _review_preflight(
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
        raise EntityResolutionBatchError("review preflight returned no row")
    value = dict(row)
    output = {
        "resolution_id": str(value["resolution_id"]),
        "action": value["action"],
        "decision_state": value["decision_state"],
        "mention_sha256": _digest(value["mention_sha256"], "mention_sha256"),
        "candidate_set_sha256": _digest(
            value["candidate_set_sha256"], "candidate_set_sha256"
        ),
        "decision_sha256": _digest(value["decision_sha256"], "decision_sha256"),
        "authorization_manifest_sha256": _digest(
            value["authorization_manifest_sha256"],
            "authorization_manifest_sha256",
        ),
    }
    if (
        output["resolution_id"] != item["resolution_id"]
        or output["action"] != item["expected_action"]
        or output["decision_state"] != item["expected_decision_state"]
    ):
        raise EntityResolutionBatchError("review preflight differs from manifest")
    return output


async def _apply_preflight(
    conn: asyncpg.Connection,
    item: dict[str, Any],
    review_id: uuid.UUID | None,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT resolution_id,action::text,decision_state::text,review_id,
               prospective_entity_id,entity_state_sha256,apply_manifest_sha256
          FROM memory.preflight_entity_resolution_apply_v5_2($1::uuid,$2::uuid)
        """,
        uuid.UUID(item["resolution_id"]),
        review_id,
    )
    if row is None:
        raise EntityResolutionBatchError("apply preflight returned no row")
    value = dict(row)
    output = {
        "resolution_id": str(value["resolution_id"]),
        "action": value["action"],
        "decision_state": value["decision_state"],
        "review_id": str(value["review_id"]) if value["review_id"] else None,
        "prospective_entity_id": (
            str(value["prospective_entity_id"])
            if value["prospective_entity_id"]
            else None
        ),
        "entity_state_sha256": _digest(
            value["entity_state_sha256"], "entity_state_sha256"
        ),
        "apply_manifest_sha256": _digest(
            value["apply_manifest_sha256"], "apply_manifest_sha256"
        ),
    }
    if (
        output["resolution_id"] != item["resolution_id"]
        or output["action"] != item["expected_action"]
        or output["decision_state"] != item["expected_decision_state"]
        or output["review_id"] != (str(review_id) if review_id else None)
    ):
        raise EntityResolutionBatchError("apply preflight differs from manifest")
    if item["operation"] == "auto_apply" and output["prospective_entity_id"] is None:
        raise EntityResolutionBatchError("auto-apply has no prospective entity")
    if (
        item["operation"] == "reconcile_existing_and_apply"
        and output["prospective_entity_id"] != item["expected_entity_id"]
    ):
        raise EntityResolutionBatchError(
            "reviewed existing-entity preflight selected a different entity"
        )
    return output


async def _reconciliation_preflight(
    conn: asyncpg.Connection, item: dict[str, Any]
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT source_resolution_id,successor_resolution_id,target_entity_id,
               source_action::text,source_decision_state::text,match_basis,
               mention_sha256,target_state_sha256,candidate_set_sha256,
               successor_decision_sha256,reconciliation_manifest_sha256
          FROM memory.preflight_entity_resolution_reconciliation_v5_2(
            $1::uuid,$2::uuid,$3::uuid,$4
          )
        """,
        uuid.UUID(item["resolution_id"]),
        uuid.UUID(item["successor_resolution_id"]),
        uuid.UUID(item["expected_entity_id"]),
        item["review_reason"],
    )
    if row is None:
        raise EntityResolutionBatchError("reconciliation preflight returned no row")
    value = dict(row)
    output = {
        "source_resolution_id": str(value["source_resolution_id"]),
        "successor_resolution_id": str(value["successor_resolution_id"]),
        "target_entity_id": str(value["target_entity_id"]),
        "source_action": value["source_action"],
        "source_decision_state": value["source_decision_state"],
        "match_basis": value["match_basis"],
        "mention_sha256": _digest(value["mention_sha256"], "mention_sha256"),
        "target_state_sha256": _digest(
            value["target_state_sha256"], "target_state_sha256"
        ),
        "candidate_set_sha256": _digest(
            value["candidate_set_sha256"], "candidate_set_sha256"
        ),
        "successor_decision_sha256": _digest(
            value["successor_decision_sha256"], "successor_decision_sha256"
        ),
        "reconciliation_manifest_sha256": _digest(
            value["reconciliation_manifest_sha256"],
            "reconciliation_manifest_sha256",
        ),
    }
    if (
        output["source_resolution_id"] != item["resolution_id"]
        or output["successor_resolution_id"] != item["successor_resolution_id"]
        or output["target_entity_id"] != item["expected_entity_id"]
        or output["source_action"] != item["expected_action"]
        or output["source_decision_state"] != item["expected_decision_state"]
        or output["match_basis"] != "unique_owner_role_history"
    ):
        raise EntityResolutionBatchError("reconciliation preflight differs from manifest")
    return output


def _effective_item(item: dict[str, Any]) -> dict[str, Any]:
    if item["operation"] != "reconcile_existing_and_apply":
        return item
    return {
        "resolution_id": item["successor_resolution_id"],
        "operation": item["operation"],
        "expected_action": "link_existing",
        "expected_decision_state": "manual_review_required",
        "review_reason": item["review_reason"],
        "expected_entity_id": item["expected_entity_id"],
    }


async def create_plan(args: argparse.Namespace) -> dict[str, Any]:
    _, head = repository_state()
    root = review_root(args.review_root)
    metadata, manifest_path, manifest_sha = load_manifest(args.manifest, root=root)
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise EntityResolutionBatchError("POSTGRES_DSN is required")
    owner = uuid.UUID(metadata["owner_user_id"])
    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            planned: list[dict[str, Any]] = []
            for item in metadata["items"]:
                if item["operation"] == "auto_apply":
                    preflight = await _apply_preflight(conn, item, None)
                elif item["operation"] == "manual_link_existing_and_apply":
                    preflight = await _review_preflight(conn, item)
                else:
                    preflight = await _reconciliation_preflight(conn, item)
                planned.append({"manifest_item": item, "preflight": preflight})
    finally:
        await conn.close()
    return {
        "contract_version": PLAN_CONTRACT,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "preflight_only_zero_write",
        "target_server": "seebx",
        "owner_user_id": str(owner),
        "required_head_commit": head,
        "review_root": str(root),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "item_count": len(planned),
        "expected_total_bindings": metadata["expected_total_bindings"],
        "expected_new_rows": metadata["expected_new_rows"],
        "items": planned,
        "apply_authorized": False,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
    }


def load_plan(
    path_value: str, *, root: Path, head: str
) -> tuple[dict[str, Any], str]:
    path = secure_input(path_value, root=root)
    raw = path.read_bytes()
    plan = exact_object(
        json.loads(raw),
        {
            "contract_version",
            "generated_at",
            "mode",
            "target_server",
            "owner_user_id",
            "required_head_commit",
            "review_root",
            "manifest_path",
            "manifest_sha256",
            "item_count",
            "expected_total_bindings",
            "expected_new_rows",
            "items",
            "apply_authorized",
            "database_writes",
            "qdrant_writes",
            "external_model_calls",
        },
        "entity resolution batch plan",
    )
    if (
        plan["contract_version"] != PLAN_CONTRACT
        or plan["mode"] != "preflight_only_zero_write"
        or plan["target_server"] != "seebx"
        or plan["required_head_commit"] != head
        or plan["review_root"] != str(root)
        or plan["apply_authorized"] is not False
        or any(
            plan[key] != 0
            for key in ("database_writes", "qdrant_writes", "external_model_calls")
        )
    ):
        raise EntityResolutionBatchError("entity resolution batch plan is stale or invalid")
    metadata, manifest_path, manifest_sha = load_manifest(
        plan["manifest_path"], root=root
    )
    if (
        str(manifest_path) != plan["manifest_path"]
        or manifest_sha != plan["manifest_sha256"]
        or metadata["owner_user_id"] != plan["owner_user_id"]
        or len(metadata["items"]) != plan["item_count"]
        or metadata["expected_total_bindings"] != plan["expected_total_bindings"]
        or metadata["expected_new_rows"] != plan["expected_new_rows"]
        or [entry["manifest_item"] for entry in plan["items"]] != metadata["items"]
    ):
        raise EntityResolutionBatchError("manifest changed after plan creation")
    return plan, sha256_bytes(raw)


def load_authorization(
    path_value: str, *, plan: dict[str, Any], plan_sha: str, head: str, root: Path
) -> tuple[dict[str, Any], str]:
    path = secure_input(path_value, root=root)
    raw = path.read_bytes()
    value = exact_object(
        json.loads(raw),
        {
            "contract_version",
            "authorization_id",
            "authorized",
            "authorized_by",
            "authorized_at",
            "expires_at",
            "expected_head_commit",
            "target_server",
            "scope",
            "owner_user_id",
            "plan_sha256",
            "expected_item_count",
            "expected_total_bindings",
            "expected_new_rows",
            "confirmation",
        },
        "entity resolution batch authorization",
    )
    if (
        value["contract_version"] != AUTHORIZATION_CONTRACT
        or value["authorized"] is not True
        or value["authorized_by"] != "Eric Lund"
        or value["expected_head_commit"] != head
        or value["target_server"] != "seebx"
        or value["scope"]
        != "reconcile_review_and_apply_owner_v5_2_entity_resolutions_only"
        or value["owner_user_id"] != plan["owner_user_id"]
        or value["plan_sha256"] != plan_sha
        or value["expected_item_count"] != plan["item_count"]
        or value["expected_total_bindings"] != plan["expected_total_bindings"]
        or value["expected_new_rows"] != plan["expected_new_rows"]
        or value["confirmation"] != CONFIRMATION
    ):
        raise EntityResolutionBatchError("entity resolution batch authorization is invalid")
    _uuid(value["authorization_id"], "authorization_id")
    authorized_at = parse_utc(value["authorized_at"], "authorized_at")
    expires_at = parse_utc(value["expires_at"], "expires_at")
    now = dt.datetime.now(dt.timezone.utc)
    if (
        expires_at <= authorized_at
        or expires_at - authorized_at > dt.timedelta(minutes=30)
        or now < authorized_at - dt.timedelta(seconds=30)
        or now >= expires_at
    ):
        raise EntityResolutionBatchError("entity resolution authorization expired")
    return value, sha256_bytes(raw)


def _request_id(plan_sha: str, resolution_id: str, operation: str) -> uuid.UUID:
    return uuid.uuid5(REQUEST_NAMESPACE, f"{plan_sha}|{resolution_id}|{operation}")


async def _review(
    conn: asyncpg.Connection,
    item: dict[str, Any],
    request_id: uuid.UUID,
    manifest_sha: str,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT review_id,outcome,result
          FROM memory.review_entity_resolution_v5_2(
            $1::uuid,$2::uuid,'approved'::memory.entity_review_decision,$3,$4
          )
        """,
        request_id,
        uuid.UUID(item["resolution_id"]),
        item["review_reason"],
        manifest_sha,
    )
    if row is None:
        raise EntityResolutionBatchError("review returned no row")
    return {
        "review_id": str(row["review_id"]),
        "outcome": row["outcome"],
        "result": row["result"],
    }


async def _apply(
    conn: asyncpg.Connection,
    item: dict[str, Any],
    request_id: uuid.UUID,
    review_id: uuid.UUID | None,
    manifest_sha: str,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT applied_entity_id,outcome,bindings_created,result
          FROM memory.apply_entity_resolution_v5_2($1::uuid,$2::uuid,$3::uuid,$4)
        """,
        request_id,
        uuid.UUID(item["resolution_id"]),
        review_id,
        manifest_sha,
    )
    if row is None:
        raise EntityResolutionBatchError("apply returned no row")
    return {
        "applied_entity_id": str(row["applied_entity_id"]),
        "outcome": row["outcome"],
        "bindings_created": int(row["bindings_created"]),
        "result": row["result"],
    }


async def _reconcile(
    conn: asyncpg.Connection,
    item: dict[str, Any],
    request_id: uuid.UUID,
    manifest_sha: str,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT reconciliation_id,successor_resolution_id,target_entity_id,
               outcome,result
          FROM memory.reconcile_entity_resolution_v5_2(
            $1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6
          )
        """,
        request_id,
        uuid.UUID(item["resolution_id"]),
        uuid.UUID(item["successor_resolution_id"]),
        uuid.UUID(item["expected_entity_id"]),
        item["review_reason"],
        manifest_sha,
    )
    if row is None:
        raise EntityResolutionBatchError("reconciliation returned no row")
    return {
        "reconciliation_id": str(row["reconciliation_id"]),
        "successor_resolution_id": str(row["successor_resolution_id"]),
        "target_entity_id": str(row["target_entity_id"]),
        "outcome": row["outcome"],
        "result": row["result"],
    }


async def apply_plan(args: argparse.Namespace) -> dict[str, Any]:
    if os.getenv("MEMORY_V1_V5_2_ENTITY_RESOLUTION_BATCH_APPLY") != "authorized":
        raise EntityResolutionBatchError("apply environment gate is not authorized")
    if args.confirm != CONFIRMATION:
        raise EntityResolutionBatchError(f"--confirm must equal {CONFIRMATION}")
    _, head = repository_state()
    root = review_root(args.review_root)
    plan, plan_sha = load_plan(args.plan, root=root, head=head)
    authorization, authorization_sha = load_authorization(
        args.authorization, plan=plan, plan_sha=plan_sha, head=head, root=root
    )
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise EntityResolutionBatchError("POSTGRES_DSN is required")
    owner = uuid.UUID(plan["owner_user_id"])
    conn = await asyncpg.connect(dsn, command_timeout=60)
    applied: list[dict[str, Any]] = []
    replayed: list[dict[str, Any]] = []
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"{owner}|entity_resolution_batch_v5_2",
            )
            for entry in plan["items"]:
                item = entry["manifest_item"]
                effective = _effective_item(item)
                review_id: uuid.UUID | None = None
                review_result: dict[str, Any] | None = None
                reconciliation_result: dict[str, Any] | None = None
                reconciliation_request: uuid.UUID | None = None
                if item["operation"] == "reconcile_existing_and_apply":
                    current_reconciliation = await _reconciliation_preflight(conn, item)
                    if current_reconciliation != entry["preflight"]:
                        raise EntityResolutionBatchError(
                            "reconciliation preflight drifted"
                        )
                    reconciliation_request = _request_id(
                        plan_sha, item["resolution_id"], "reconcile"
                    )
                    reconciliation_result = await _reconcile(
                        conn,
                        item,
                        reconciliation_request,
                        current_reconciliation["reconciliation_manifest_sha256"],
                    )
                    if (
                        reconciliation_result["outcome"] != "applied"
                        or reconciliation_result["successor_resolution_id"]
                        != item["successor_resolution_id"]
                        or reconciliation_result["target_entity_id"]
                        != item["expected_entity_id"]
                    ):
                        raise EntityResolutionBatchError(
                            "reconciliation outcome is not the authorized successor"
                        )
                    current_review = await _review_preflight(conn, effective)
                    review_request = _request_id(
                        plan_sha, effective["resolution_id"], "review"
                    )
                    review_result = await _review(
                        conn,
                        effective,
                        review_request,
                        current_review["authorization_manifest_sha256"],
                    )
                    if review_result["outcome"] != "applied":
                        raise EntityResolutionBatchError("review outcome is not applied")
                    review_id = uuid.UUID(review_result["review_id"])
                    apply_preflight = await _apply_preflight(conn, effective, review_id)
                elif item["operation"] == "manual_link_existing_and_apply":
                    current_review = await _review_preflight(conn, effective)
                    if current_review != entry["preflight"]:
                        raise EntityResolutionBatchError("review preflight drifted")
                    review_request = _request_id(
                        plan_sha, effective["resolution_id"], "review"
                    )
                    review_result = await _review(
                        conn,
                        effective,
                        review_request,
                        current_review["authorization_manifest_sha256"],
                    )
                    if review_result["outcome"] != "applied":
                        raise EntityResolutionBatchError("review outcome is not applied")
                    review_id = uuid.UUID(review_result["review_id"])
                    apply_preflight = await _apply_preflight(conn, effective, review_id)
                else:
                    apply_preflight = await _apply_preflight(conn, effective, None)
                    if apply_preflight != entry["preflight"]:
                        raise EntityResolutionBatchError("apply preflight drifted")
                apply_request = _request_id(plan_sha, effective["resolution_id"], "apply")
                apply_result = await _apply(
                    conn,
                    effective,
                    apply_request,
                    review_id,
                    apply_preflight["apply_manifest_sha256"],
                )
                if apply_result["outcome"] != "applied":
                    raise EntityResolutionBatchError("resolution outcome is not applied")
                if (
                    item["operation"]
                    in {
                        "manual_link_existing_and_apply",
                        "reconcile_existing_and_apply",
                    }
                    and apply_result["applied_entity_id"]
                    != item["expected_entity_id"]
                ):
                    raise EntityResolutionBatchError(
                        "reviewed resolution applied to a different entity"
                    )
                applied.append(
                    {
                        "source_resolution_id": item["resolution_id"],
                        "resolution_id": effective["resolution_id"],
                        "operation": item["operation"],
                        "reconciliation_request_id": (
                            str(reconciliation_request)
                            if reconciliation_request
                            else None
                        ),
                        "reconciliation_id": (
                            reconciliation_result["reconciliation_id"]
                            if reconciliation_result
                            else None
                        ),
                        "reconciliation_manifest_sha256": (
                            entry["preflight"]["reconciliation_manifest_sha256"]
                            if reconciliation_result
                            else None
                        ),
                        "review_request_id": (
                            str(_request_id(plan_sha, effective["resolution_id"], "review"))
                            if review_result
                            else None
                        ),
                        "review_id": str(review_id) if review_id else None,
                        "review_manifest_sha256": (
                            current_review["authorization_manifest_sha256"]
                            if review_result
                            else None
                        ),
                        "apply_request_id": str(apply_request),
                        "apply_manifest_sha256": apply_preflight[
                            "apply_manifest_sha256"
                        ],
                        "applied_entity_id": apply_result["applied_entity_id"],
                        "bindings_created": apply_result["bindings_created"],
                    }
                )
            total_bindings = sum(item["bindings_created"] for item in applied)
            if total_bindings != plan["expected_total_bindings"]:
                raise EntityResolutionBatchError("binding count differs from manifest")
            for entry, prior in zip(plan["items"], applied, strict=True):
                item = entry["manifest_item"]
                effective = _effective_item(item)
                replay_review: dict[str, Any] | None = None
                review_id = uuid.UUID(prior["review_id"]) if prior["review_id"] else None
                replay_reconciliation: dict[str, Any] | None = None
                if item["operation"] == "reconcile_existing_and_apply":
                    replay_reconciliation = await _reconcile(
                        conn,
                        item,
                        uuid.UUID(prior["reconciliation_request_id"]),
                        prior["reconciliation_manifest_sha256"],
                    )
                    if (
                        replay_reconciliation["outcome"] != "replayed"
                        or replay_reconciliation["reconciliation_id"]
                        != prior["reconciliation_id"]
                    ):
                        raise EntityResolutionBatchError(
                            "reconciliation replay is not zero-write"
                        )
                    replay_review = await _review(
                        conn,
                        effective,
                        uuid.UUID(prior["review_request_id"]),
                        prior["review_manifest_sha256"],
                    )
                    if (
                        replay_review["outcome"] != "replayed"
                        or replay_review["review_id"] != prior["review_id"]
                    ):
                        raise EntityResolutionBatchError("review replay is not zero-write")
                elif item["operation"] == "manual_link_existing_and_apply":
                    replay_review = await _review(
                        conn,
                        effective,
                        uuid.UUID(prior["review_request_id"]),
                        prior["review_manifest_sha256"],
                    )
                    if (
                        replay_review["outcome"] != "replayed"
                        or replay_review["review_id"] != prior["review_id"]
                    ):
                        raise EntityResolutionBatchError("review replay is not zero-write")
                replay_apply = await _apply(
                    conn,
                    effective,
                    uuid.UUID(prior["apply_request_id"]),
                    review_id,
                    prior["apply_manifest_sha256"],
                )
                if (
                    replay_apply["outcome"] != "replayed"
                    or replay_apply["bindings_created"] != 0
                    or replay_apply["applied_entity_id"] != prior["applied_entity_id"]
                ):
                    raise EntityResolutionBatchError("apply replay is not zero-write")
                replayed.append(
                    {
                        "source_resolution_id": item["resolution_id"],
                        "resolution_id": effective["resolution_id"],
                        "reconciliation_outcome": (
                            replay_reconciliation["outcome"]
                            if replay_reconciliation
                            else None
                        ),
                        "review_outcome": (
                            replay_review["outcome"] if replay_review else None
                        ),
                        "apply_outcome": replay_apply["outcome"],
                        "bindings_created": replay_apply["bindings_created"],
                    }
                )
    finally:
        await conn.close()
    return {
        "contract_version": REPORT_CONTRACT,
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "transactional_review_apply_and_zero_write_replay",
        "target_server": "seebx",
        "owner_user_id": str(owner),
        "head_commit": head,
        "plan_sha256": plan_sha,
        "authorization_id": authorization["authorization_id"],
        "authorization_sha256": authorization_sha,
        "item_count": plan["item_count"],
        "database_rows_created": plan["expected_new_rows"],
        "bindings_created": plan["expected_total_bindings"],
        "applied": applied,
        "replayed": replayed,
        "checks": {
            "one_owner_per_transaction": True,
            "owner_advisory_lock": True,
            "atomic_batch": True,
            "exact_row_budget": True,
            "review_replay_rows_written": 0,
            "reconciliation_replay_rows_written": 0,
            "apply_replay_rows_written": 0,
            "external_model_calls": 0,
            "qdrant_calls": 0,
            "projection_invoked": False,
            "retrieval_invoked": False,
            "prompt_influence": False,
        },
        "hard_stop": "before_projection_preflight_or_apply",
    }


async def async_main() -> int:
    args = arguments()
    value = await create_plan(args) if args.command == "plan" else await apply_plan(args)
    output, digest = secure_write(args.output, value)
    print(
        json.dumps(
            {
                "contract_version": value["contract_version"],
                "mode": value["mode"],
                "output": str(output),
                "sha256": digest,
                "owner_user_id": value["owner_user_id"],
                "item_count": value["item_count"],
                "database_writes": value.get("database_rows_created", 0),
                "qdrant_calls": 0,
                "external_model_calls": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main()))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}:{exc}", file=sys.stderr)
        raise SystemExit(1)
