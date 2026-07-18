#!/usr/bin/env python3
"""Atomically admit and stage one policy-safe local V5 review artifact."""

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

import asyncpg

from scripts.memory_v1_v5_local_packet_disposition import (
    canonical_owners,
    loopback_dsn,
    sha256_text,
    stable_json,
)
from scripts.memory_v1_v5_local_packet_router import artifact_paths
from scripts.memory_v1_v5_stage_batch import (
    COUNT_KEYS,
    load_bundle,
    returned_counts,
    structural_counts,
)


WORKER_VERSION = "memory_v1_v5_local_auto_stage_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_local_auto_stage_apply_v1"
POLICY_VERSION = "memory_v1_v5_local_auto_stage_policy_v1"
IDENTITY_NAMESPACE = uuid.UUID("8e85b93b-e288-5b74-acb2-058846c5e410")
DEFAULT_REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")


class LocalAutoStageError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Admit and stage at most one blocker-free local V5 artifact."
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
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
        raise LocalAutoStageError("review root must be owner-only mode 0700")
    return root


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


async def plan_owner(
    conn: asyncpg.Connection, owner: uuid.UUID
) -> dict[str, Any] | None:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        rows = await conn.fetch(
            "SELECT * FROM memory.plan_owner_v5_local_auto_stage_v1(1)"
        )
        txid_assigned = await conn.fetchval("SELECT txid_current_if_assigned()")
    if txid_assigned is not None:
        raise LocalAutoStageError("read-only auto-stage planning assigned a txid")
    if len(rows) > 1:
        raise LocalAutoStageError("auto-stage planner exceeded its row budget")
    return dict(rows[0]) if rows else None


def validate_bundle(
    *, root: Path, owner: uuid.UUID, target: dict[str, Any]
) -> dict[str, Any]:
    packet_id = uuid.UUID(str(target["packet_id"]))
    report_path, bundle_path = artifact_paths(root, packet_id)
    for path in (report_path, bundle_path):
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
        if (
            not resolved.is_relative_to(root)
            or not resolved.is_file()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise LocalAutoStageError("review artifact security invariant failed")
    if sha256_file(report_path) != target["review_report_sha256"]:
        raise LocalAutoStageError("review report differs from append-only ledger")
    if sha256_file(bundle_path) != target["stage_bundle_sha256"]:
        raise LocalAutoStageError("stage bundle differs from append-only ledger")
    raw = json.loads(bundle_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise LocalAutoStageError("stage bundle is not a JSON object")
    try:
        extraction = json.loads(raw["extraction_packet_text"])
        resolution = json.loads(raw["resolution_packet_text"])
        counts = structural_counts(extraction, resolution)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LocalAutoStageError("stage bundle structure is invalid") from exc
    bundle = load_bundle(
        {
            "path": str(bundle_path),
            "sha256": target["stage_bundle_sha256"],
            "expected_outcome": "applied",
            "expected_counts": counts,
        },
        owner=owner,
        root=root,
    )
    summary = raw["resolution_summary"]
    if (
        raw["source_report"]
        != {
            "path": str(report_path),
            "sha256": target["review_report_sha256"],
        }
        or raw["request_id"] != str(target["stage_request_id"])
        or raw["evidence_id"] != str(target["evidence_id"])
        or raw["extractor_version"] != target["repository_commit"]
        or not repository_commit_valid(target["repository_commit"])
        or summary.get("auto_link_eligible") != target["auto_link_count"]
        or summary.get("manual_review_required") != 0
        or summary.get("deferred") != 0
        or summary.get("rejected") != 0
        or counts["mentions"] != target["entity_mention_count"]
        or counts["observations"] != target["observation_count"]
    ):
        raise LocalAutoStageError("bundle and auto-stage ledger differ")
    return bundle


def admission_ids(
    owner: uuid.UUID, artifact_id: uuid.UUID, bundle_sha256: str
) -> tuple[uuid.UUID, uuid.UUID]:
    identity = f"{owner}|{artifact_id}|{bundle_sha256}|{POLICY_VERSION}"
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"operation|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"admission|{identity}"),
    )


async def apply_once(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    target: dict[str, Any],
    bundle: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact_id = uuid.UUID(str(target["artifact_id"]))
    operation_id, admission_id = admission_ids(
        owner, artifact_id, target["stage_bundle_sha256"]
    )
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"{owner}|local_auto_stage_v1",
        )
        admission = await conn.fetchrow(
            """
            SELECT * FROM memory.register_owner_v5_local_auto_stage_v1(
              $1,$2,$3,$4,$5,$6,$7
            )
            """,
            operation_id,
            admission_id,
            artifact_id,
            target["review_report_sha256"],
            target["stage_bundle_sha256"],
            target["packet_storage_sha256"],
            POLICY_VERSION,
        )
        staged = await conn.fetchrow(
            """
            SELECT batch_id,outcome,mentions_inserted,resolutions_inserted,
                   candidates_inserted,observations_inserted,
                   temporals_inserted,result
            FROM memory.stage_relational_packet_v5(
              $1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8
            )
            """,
            bundle["request_id"],
            bundle["evidence_id"],
            bundle["extractor"],
            bundle["extractor_version"],
            bundle["extraction_packet_text"],
            bundle["resolution_packet_text"],
            bundle["extraction_packet_sha256"],
            bundle["resolution_packet_sha256"],
        )
    if admission is None or staged is None:
        raise LocalAutoStageError("controlled auto-stage API returned no row")
    return dict(admission), dict(staged)


async def run() -> int:
    args = arguments()
    owners = canonical_owners(args.owner_user_id)
    if args.apply and os.getenv("MEMORY_V1_V5_LOCAL_AUTO_STAGE_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise LocalAutoStageError("local auto-stage apply capability is absent")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise LocalAutoStageError("POSTGRES_DSN is required")
    root = secure_review_root(args.review_root)
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=60, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise LocalAutoStageError("local auto-stage requires brains_app session")
        plans = [(owner, await plan_owner(conn, owner)) for owner in owners]
        selected = next(((owner, row) for owner, row in plans if row), None)
        sanitized_plans = [
            {
                "owner_user_id_sha256": sha256_text(str(owner)),
                "artifact_id_sha256": (
                    sha256_text(str(row["artifact_id"])) if row else None
                ),
                "route": "auto_stage_eligible" if row else "no_work",
                "counts": (
                    {
                        "entity_mentions": row["entity_mention_count"],
                        "observations": row["observation_count"],
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
                        "qdrant_writes": 0,
                        "external_model_calls": 0,
                        "prompt_influence": 0,
                    }
                )
            )
            return 0
        if selected is None:
            outcome = "no_work"
            created_rows = 0
            stage_counts = {key: 0 for key in COUNT_KEYS}
            replay_proved = True
        else:
            owner, target = selected
            bundle = validate_bundle(root=root, owner=owner, target=target)
            admission, staged = await apply_once(
                conn, owner=owner, target=target, bundle=bundle
            )
            replay_admission, replay_stage = await apply_once(
                conn, owner=owner, target=target, bundle=bundle
            )
            stage_counts = returned_counts(staged)
            replay_counts = returned_counts(replay_stage)
            if (
                admission["apply_outcome"] != "applied"
                or staged["outcome"] != "applied"
                or stage_counts != bundle["expected_counts"]
                or replay_admission["apply_outcome"] != "replayed"
                or replay_stage["outcome"] != "replayed"
                or any(replay_counts.values())
            ):
                raise LocalAutoStageError("auto-stage apply or replay invariant failed")
            outcome = "staged_for_entity_resolution_review"
            created_rows = sum(stage_counts.values()) + 3
            replay_proved = True
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": True,
                    "outcome": outcome,
                    "plans": sanitized_plans,
                    "stage_counts": stage_counts,
                    "database_rows_created": created_rows,
                    "write_counts": {
                        "admission": int(outcome != "no_work"),
                        "stage_and_request": 2 if outcome != "no_work" else 0,
                        "claims": 0,
                        "projections": 0,
                        "qdrant": 0,
                        "prompt_influence": 0,
                    },
                    "zero_write_replay_proved": replay_proved,
                    "filesystem_writes": 0,
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
                    "outcome": "auto_stage_error",
                    "error_class": type(exc).__name__,
                    "error_sha256": sha256_text(str(exc)),
                    "external_model_calls": 0,
                    "claims": 0,
                    "projections": 0,
                    "qdrant": 0,
                    "prompt_influence": 0,
                }
            ),
            file=os.sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main())
