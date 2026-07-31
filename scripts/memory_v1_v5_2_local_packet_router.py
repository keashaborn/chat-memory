#!/usr/bin/env python3
"""Route one immutable V5.2 local packet without staging or promotion."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import Any
import uuid

import asyncpg

from scripts.memory_v1_authenticated_owners import resolve_authenticated_owners
from scripts.memory_v1_v5_local_packet_disposition import (
    loopback_dsn,
    sha256_text,
    stable_json,
)


WORKER_VERSION = "memory_v1_v5_2_local_packet_router_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_2_local_packet_router_apply_v1"
IDENTITY_NAMESPACE = uuid.UUID("9a15e563-8ed6-5d92-9db8-a48679295463")
DEFAULT_BUILDER = Path(__file__).with_name(
    "memory_v1_v5_1_review_local_packet.py"
)
DEFAULT_REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
REVIEW_CONTRACT = "memory_v1_v5_2_local_packet_review_v1"
BUNDLE_CONTRACT = "memory_v1_v5_2_stage_preflight_v1"
RESOLUTION_STATES = (
    "auto_link_eligible",
    "manual_review_required",
    "deferred",
    "rejected",
)
TERMINAL_REASON_CODES = frozenset(
    {
        "structured_domain",
        "question_only",
        "transient_state",
        "insufficient_evidence",
    }
)
REVIEW_UNRESOLVED_REASON_CODES = frozenset(
    {
        "structured_domain",
        "question_only",
        "transient_state",
        "insufficient_evidence",
        "entity_resolution_unresolved",
        "unregistered_predicate",
    }
)
REVIEW_UNRESOLVED_REQUIRED_CODES = frozenset(
    {"entity_resolution_unresolved", "unregistered_predicate"}
)


def rotate_owners(
    owners: list[uuid.UUID],
    minute_slot: int,
) -> list[uuid.UUID]:
    if not owners:
        return []
    offset = minute_slot % len(owners)
    return owners[offset:] + owners[:offset]


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Route at most one exact owner-scoped V5.2 packet. The only "
            "persistent output is one append-only route event."
        )
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--packet-id")
    parser.add_argument("--review-builder", default=str(DEFAULT_BUILDER))
    parser.add_argument("--review-root", default=str(DEFAULT_REVIEW_ROOT))
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def secure_review_root(value: str) -> Path:
    root = Path(value).resolve(strict=True)
    metadata = root.stat()
    if (
        not root.is_dir()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise RuntimeError("review root must be an owner-only mode-0700 directory")
    return root


def artifact_paths(root: Path, packet_id: uuid.UUID) -> tuple[Path, Path]:
    packet_hash = sha256_text(str(packet_id))
    return (
        root / f"v5-2-router-{packet_hash}-review.json",
        root / f"v5-2-router-{packet_hash}-stage.json",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def secure_json(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if (
        not resolved.is_relative_to(root)
        or not resolved.is_file()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise RuntimeError("review artifact security invariant failed")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("review artifact is not a JSON object")
    return value


def repository_commit_valid(commit: Any) -> bool:
    if not isinstance(commit, str) or len(commit) != 40:
        return False
    if any(character not in "0123456789abcdef" for character in commit):
        return False
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def validate_artifacts(
    *,
    root: Path,
    report_path: Path,
    bundle_path: Path,
    owner: uuid.UUID,
    packet_id: uuid.UUID,
) -> dict[str, Any]:
    report = secure_json(report_path, root)
    bundle = secure_json(bundle_path, root)
    report_sha = sha256_file(report_path)
    bundle_sha = sha256_file(bundle_path)
    counts = report.get("resolution_summary")
    if (
        report.get("contract_version") != REVIEW_CONTRACT
        or report.get("mode") != "owner_scoped_local_packet_review_zero_write"
        or report.get("owner_user_id") != str(owner)
        or report.get("packet_id") != str(packet_id)
        or report.get("review_disposition") != "manual_review_required"
        or not repository_commit_valid(report.get("repository_commit"))
        or not isinstance(counts, dict)
        or set(counts) != set(RESOLUTION_STATES)
        or any(not isinstance(counts[state], int) for state in RESOLUTION_STATES)
        or any(counts[state] < 0 for state in RESOLUTION_STATES)
        or not isinstance(report.get("blocking_codes"), list)
        or any(not isinstance(code, str) for code in report["blocking_codes"])
        or report.get("zero_write_proof", {}).get("database_writes") != 0
        or report.get("zero_write_proof", {}).get("qdrant_writes") != 0
        or report.get("zero_write_proof", {}).get("external_model_calls") != 0
    ):
        raise RuntimeError("V5.2 review report contract is invalid")
    if (
        bundle.get("contract_version") != BUNDLE_CONTRACT
        or bundle.get("mode") != "preflight_only_zero_write"
        or bundle.get("owner_user_id") != str(owner)
        or bundle.get("case_id") != f"local-packet-{packet_id}"
        or bundle.get("database_writes") != 0
        or bundle.get("qdrant_writes") != 0
        or bundle.get("external_model_calls") != 0
        or bundle.get("authorized_stage") is not False
        or bundle.get("source_report", {}).get("path") != str(report_path)
        or bundle.get("source_report", {}).get("sha256") != report_sha
        or bundle.get("extraction_packet_sha256")
        != report.get("derived_packet_sha256")
        or bundle.get("resolution_packet_sha256")
        != report.get("resolution_packet_sha256")
        or bundle.get("resolution_summary") != counts
    ):
        raise RuntimeError("V5.2 stage preflight bundle contract is invalid")
    try:
        review_id = uuid.UUID(str(report["review_id"]))
        request_id = uuid.UUID(str(bundle["request_id"]))
    except (KeyError, ValueError) as exc:
        raise RuntimeError("V5.2 review artifact identifiers are invalid") from exc
    return {
        "review_id": review_id,
        "request_id": request_id,
        "report_sha256": report_sha,
        "bundle_sha256": bundle_sha,
        "repository_commit": report["repository_commit"],
        "counts": {state: counts[state] for state in RESOLUTION_STATES},
        "blocking_code_count": len(set(report["blocking_codes"])),
    }


def build_artifacts(
    *,
    builder: Path,
    root: Path,
    report_path: Path,
    bundle_path: Path,
    owner: uuid.UUID,
    packet_id: uuid.UUID,
) -> bool:
    if report_path.exists() != bundle_path.exists():
        raise RuntimeError("partial V5.2 review artifact pair exists")
    if report_path.exists():
        return False
    completed = subprocess.run(
        [
            sys.executable,
            str(builder),
            "--contract-profile",
            "v5_2",
            "--owner-user-id",
            str(owner),
            "--packet-id",
            str(packet_id),
            "--review-report",
            str(report_path),
            "--stage-bundle",
            str(bundle_path),
            "--review-root",
            str(root),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        diagnostic = sha256_text(completed.stdout + "\0" + completed.stderr)
        raise RuntimeError(f"V5.2 review builder failed closed:{diagnostic}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("V5.2 review builder returned invalid output") from exc
    if (
        result.get("contract_version") != REVIEW_CONTRACT
        or result.get("predicate_contract_profile") != "v5_2"
        or result.get("database_writes") != 0
        or result.get("qdrant_writes") != 0
        or result.get("external_model_calls") != 0
    ):
        raise RuntimeError("V5.2 review builder violated the zero-write contract")
    return True


def stable_ids(
    *,
    owner: uuid.UUID,
    packet_id: uuid.UUID,
    routing_basis_sha256: str,
    report_sha256: str | None = None,
    bundle_sha256: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    identity = "|".join(
        (
            str(owner),
            str(packet_id),
            routing_basis_sha256,
            report_sha256 or "terminal",
            bundle_sha256 or "terminal",
        )
    )
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"operation|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"event|{identity}"),
    )


async def plan_owner(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    packet_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        if packet_id is not None:
            rows = await conn.fetch(
                """
                SELECT *
                FROM memory.plan_owner_v5_2_exact_packet_route_v1($1)
                """,
                packet_id,
            )
            if not rows:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM memory.plan_owner_v5_2_zero_atom_deferral_route_v1(
                      $1::uuid
                    )
                    """,
                    packet_id,
                )
        else:
            rows = await conn.fetch(
                "SELECT * FROM memory.plan_owner_v5_2_local_packet_route_v1($1)",
                1,
            )
            if not rows:
                rows = await conn.fetch(
                    "SELECT * FROM "
                    "memory.plan_owner_v5_2_zero_atom_deferral_route_v1("
                    "$1::integer)",
                    1,
                )
    if len(rows) > 1:
        raise RuntimeError("V5.2 route planner exceeded its bound")
    return dict(rows[0]) if rows else None


def select_plans(
    rows: list[Any],
    packet_id: uuid.UUID | None,
) -> list[Any]:
    if packet_id is None:
        return rows
    return [row for row in rows if row["packet_id"] == packet_id]


async def finalize_terminal(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    target: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    packet_id = uuid.UUID(str(target["packet_id"]))
    operation_id, event_id = stable_ids(
        owner=owner,
        packet_id=packet_id,
        routing_basis_sha256=target["routing_basis_sha256"],
    )
    reason_codes = list(target["source_deferral_reason_codes"])
    route_reason = target["reason_code"]
    reason_set = set(reason_codes)
    if route_reason == "deferral_only_review_unresolved_v5_2":
        if (
            not reason_set
            or not reason_set.issubset(REVIEW_UNRESOLVED_REASON_CODES)
            or not reason_set.intersection(REVIEW_UNRESOLVED_REQUIRED_CODES)
        ):
            raise RuntimeError(
                "planner returned an invalid unresolved-review terminal reason"
            )
    elif (
        route_reason != "deferral_only_no_stage_v5_2"
        or not reason_set
        or not reason_set.issubset(TERMINAL_REASON_CODES)
    ):
        raise RuntimeError("planner returned a non-allowlisted terminal reason")

    async def invoke() -> dict[str, Any]:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            if route_reason == "deferral_only_review_unresolved_v5_2":
                row = await conn.fetchrow(
                    """
                    SELECT * FROM
                      memory.finalize_owner_v5_2_zero_atom_deferral_route_v1(
                        $1,$2,$3,$4,$5,$6,$7
                      )
                    """,
                    operation_id,
                    event_id,
                    packet_id,
                    target["packet_storage_sha256"],
                    route_reason,
                    target["routing_basis_sha256"],
                    reason_codes,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT * FROM memory.finalize_owner_v5_2_terminal_route_v1(
                      $1,$2,$3,$4,$5,$6
                    )
                    """,
                    operation_id,
                    event_id,
                    packet_id,
                    target["packet_storage_sha256"],
                    target["routing_basis_sha256"],
                    reason_codes,
                )
        if row is None:
            raise RuntimeError("V5.2 terminal route returned no row")
        return dict(row)

    return await invoke(), await invoke()


async def record_review(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    target: dict[str, Any],
    artifact: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    packet_id = uuid.UUID(str(target["packet_id"]))
    operation_id, event_id = stable_ids(
        owner=owner,
        packet_id=packet_id,
        routing_basis_sha256=target["routing_basis_sha256"],
        report_sha256=artifact["report_sha256"],
        bundle_sha256=artifact["bundle_sha256"],
    )

    async def invoke() -> dict[str, Any]:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            row = await conn.fetchrow(
                """
                SELECT * FROM memory.record_owner_v5_2_review_route_v1(
                  $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15
                )
                """,
                operation_id,
                event_id,
                packet_id,
                target["packet_storage_sha256"],
                target["routing_basis_sha256"],
                artifact["review_id"],
                artifact["request_id"],
                artifact["report_sha256"],
                artifact["bundle_sha256"],
                artifact["repository_commit"],
                artifact["counts"]["auto_link_eligible"],
                artifact["counts"]["manual_review_required"],
                artifact["counts"]["deferred"],
                artifact["counts"]["rejected"],
                artifact["blocking_code_count"],
            )
        if row is None:
            raise RuntimeError("V5.2 review route returned no row")
        return dict(row)

    return await invoke(), await invoke()


async def run() -> int:
    args = arguments()
    try:
        target_packet_id = uuid.UUID(args.packet_id) if args.packet_id else None
    except ValueError as exc:
        raise RuntimeError("exact packet ID is invalid") from exc
    if args.apply and os.getenv("MEMORY_V1_V5_2_LOCAL_PACKET_ROUTER_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise RuntimeError("V5.2 local packet router apply capability is absent")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    owners = await resolve_authenticated_owners(dsn, args.owner_user_id)
    if not args.owner_user_id:
        owners = rotate_owners(owners, int(time.time() // 60))
    if target_packet_id is not None and len(owners) != 1:
        raise RuntimeError("exact packet routing requires exactly one owner")
    root = secure_review_root(args.review_root)
    builder = Path(args.review_builder).resolve(strict=True)
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=30, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("V5.2 local packet router requires brains_app session")
        plans = [
            (owner, await plan_owner(conn, owner, target_packet_id))
            for owner in owners
        ]
        selected = next(((owner, row) for owner, row in plans if row), None)
        sanitized_plans = [
            {
                "owner_user_id_sha256": sha256_text(str(owner)),
                "packet_id_sha256": (
                    sha256_text(str(row["packet_id"])) if row else None
                ),
                "route": row["route"] if row else "no_work",
                "reason_code": row["reason_code"] if row else None,
                "source_deferral_reason_codes": (
                    list(row["source_deferral_reason_codes"]) if row else []
                ),
                "counts": (
                    {
                        "entity_mentions": row["entity_mention_count"],
                        "observations": row["observation_count"],
                        "comparison_hints": row["comparison_hint_count"],
                        "deferrals": row["deferral_count"],
                    }
                    if row
                    else None
                ),
            }
            for owner, row in plans
        ]
        if not args.apply:
            print(
                stable_json(
                    {
                        "worker_version": WORKER_VERSION,
                        "apply": False,
                        "plans": sanitized_plans,
                        "database_writes": 0,
                        "filesystem_writes": 0,
                        "stage_writes": 0,
                        "claim_writes": 0,
                        "qdrant_writes": 0,
                        "external_model_calls": 0,
                        "prompt_influence": 0,
                    }
                )
            )
            return 0

        if selected is None:
            outcome = "no_work"
            database_writes = 0
            filesystem_writes = 0
            review_counts = None
        else:
            owner, target = selected
            route = target["route"]
            if route == "terminal_no_stage":
                applied, replayed = await finalize_terminal(conn, owner, target)
                if (
                    applied["apply_outcome"] not in {"applied", "replayed"}
                    or replayed["apply_outcome"] != "replayed"
                ):
                    raise RuntimeError("V5.2 terminal route replay failed")
                outcome = "terminal_no_stage"
                database_writes = int(applied["apply_outcome"] == "applied")
                filesystem_writes = 0
                review_counts = None
            elif route == "manual_review_artifact_ready":
                packet_id = uuid.UUID(str(target["packet_id"]))
                report_path, bundle_path = artifact_paths(root, packet_id)
                created = build_artifacts(
                    builder=builder,
                    root=root,
                    report_path=report_path,
                    bundle_path=bundle_path,
                    owner=owner,
                    packet_id=packet_id,
                )
                artifact = validate_artifacts(
                    root=root,
                    report_path=report_path,
                    bundle_path=bundle_path,
                    owner=owner,
                    packet_id=packet_id,
                )
                applied, replayed = await record_review(
                    conn, owner=owner, target=target, artifact=artifact
                )
                if (
                    applied["apply_outcome"] not in {"applied", "replayed"}
                    or replayed["apply_outcome"] != "replayed"
                ):
                    raise RuntimeError("V5.2 review route replay failed")
                outcome = "manual_review_artifact_ready"
                database_writes = int(applied["apply_outcome"] == "applied")
                filesystem_writes = 2 if created else 0
                review_counts = artifact["counts"]
            else:
                raise RuntimeError("V5.2 router received an unknown route")

        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": True,
                    "outcome": outcome,
                    "plans": sanitized_plans,
                    "review_resolution_counts": review_counts,
                    "write_counts": {
                        "route_events": database_writes,
                        "restricted_review_artifacts": filesystem_writes,
                        "stage": 0,
                        "claims": 0,
                        "qdrant": 0,
                        "prompt_influence": 0,
                    },
                    "zero_write_replay_proved": True,
                    "external_model_calls": 0,
                }
            )
        )
        return 0
    finally:
        await conn.close()


def main() -> int:
    return asyncio.run(run())


def guarded_main() -> int:
    try:
        return main()
    except Exception as exc:
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": False,
                    "outcome": "router_error",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "external_model_calls": 0,
                    "stage_writes": 0,
                    "claim_writes": 0,
                    "qdrant_writes": 0,
                    "prompt_influence": 0,
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
