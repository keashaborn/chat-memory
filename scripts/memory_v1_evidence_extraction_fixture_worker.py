#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg


WORKER_VERSION = "memory_v1_evidence_extraction_fixture_worker_v1"
FIXTURE_CONTRACT = "memory_v1_evidence_extraction_fixture_v1"
CHECKPOINT_CONTRACT = "memory_v1_relational_extraction_v5"
FINISH_CONTRACT = "memory_v1_evidence_extraction_fixture_result_v1"
DISPOSABLE_DATABASE_COMMENT = "memory_v1_disposable_clone_worker_v1"
SHA256_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class Fixture:
    file_sha256: str
    route: str
    source_content_sha256: str
    checkpoint: dict[str, Any]
    finish_status: str
    finish_payload: dict[str, Any]


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate or apply a deterministic extraction fixture against an "
            "explicitly marked disposable database clone."
        )
    )
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--expected-fixture-sha256", required=True)
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument(
        "--route",
        choices=(
            "relational_extraction",
            "artifact_assessment",
            "structured_projection",
        ),
        default="relational_extraction",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--worker-id")
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-jobs", type=int, default=1)
    parser.add_argument("--apply-fixture", action="store_true")
    parser.add_argument("--verify-replay", action="store_true")
    return parser.parse_args()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in SHA256_HEX for character in value)
    )


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return dict(value)


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RuntimeError(f"{label} keys do not match the contract")


def load_fixture(path_value: str, expected_sha256: str) -> Fixture:
    if not _is_sha256(expected_sha256):
        raise RuntimeError("expected fixture SHA-256 is invalid")
    path = Path(path_value)
    raw = path.read_bytes()
    actual_sha256 = _sha256_bytes(raw)
    if actual_sha256 != expected_sha256:
        raise RuntimeError("fixture SHA-256 mismatch")
    root = _object(json.loads(raw), "fixture")
    _exact_keys(
        root,
        {
            "contract_version",
            "route",
            "source_content_sha256",
            "checkpoint",
            "finish",
        },
        "fixture",
    )
    if root["contract_version"] != FIXTURE_CONTRACT:
        raise RuntimeError("fixture contract version mismatch")
    if root["route"] != "relational_extraction":
        raise RuntimeError("fixture route is not enabled in fixture worker v1")
    source_sha256 = root["source_content_sha256"]
    if not _is_sha256(source_sha256):
        raise RuntimeError("fixture source content SHA-256 is invalid")

    checkpoint = _object(root["checkpoint"], "checkpoint")
    _exact_keys(
        checkpoint,
        {
            "contract_version",
            "source_content_sha256",
            "extraction_mode",
            "entities",
            "observations",
            "relationships",
            "manual_review_required",
            "model_calls",
        },
        "checkpoint",
    )
    if checkpoint["contract_version"] != CHECKPOINT_CONTRACT:
        raise RuntimeError("checkpoint contract version mismatch")
    if checkpoint["source_content_sha256"] != source_sha256:
        raise RuntimeError("checkpoint source hash is not bound to fixture")
    if checkpoint["extraction_mode"] != "deterministic_fixture":
        raise RuntimeError("checkpoint extraction mode is unsafe")
    if checkpoint["model_calls"] != 0:
        raise RuntimeError("fixture checkpoint declares model calls")
    for key in ("entities", "observations", "relationships"):
        if not isinstance(checkpoint[key], list):
            raise RuntimeError(f"checkpoint {key} must be a list")
    if not isinstance(checkpoint["manual_review_required"], int):
        raise RuntimeError("checkpoint manual review count must be an integer")

    finish = _object(root["finish"], "finish")
    _exact_keys(finish, {"status", "payload"}, "finish")
    if finish["status"] != "review_required":
        raise RuntimeError("fixture worker may only finish as review_required")
    finish_payload = _object(finish["payload"], "finish payload")
    _exact_keys(
        finish_payload,
        {
            "contract_version",
            "route",
            "source_content_sha256",
            "fixture_only",
            "model_calls",
            "candidate_writes",
            "claim_writes",
            "staging_writes",
        },
        "finish payload",
    )
    if finish_payload["contract_version"] != FINISH_CONTRACT:
        raise RuntimeError("finish payload contract version mismatch")
    if finish_payload["route"] != root["route"]:
        raise RuntimeError("finish payload route is not bound to fixture")
    if finish_payload["source_content_sha256"] != source_sha256:
        raise RuntimeError("finish payload source hash is not bound to fixture")
    if finish_payload["fixture_only"] is not True:
        raise RuntimeError("finish payload is not fixture-only")
    for key in ("model_calls", "candidate_writes", "claim_writes", "staging_writes"):
        if finish_payload[key] != 0:
            raise RuntimeError(f"finish payload {key} must be zero")

    return Fixture(
        file_sha256=actual_sha256,
        route=root["route"],
        source_content_sha256=source_sha256,
        checkpoint=checkpoint,
        finish_status=finish["status"],
        finish_payload=finish_payload,
    )


def canonical_owners(values: list[str]) -> list[uuid.UUID]:
    if not values:
        raise RuntimeError("at least one explicit owner UUID is required")
    try:
        owners = sorted({uuid.UUID(value) for value in values}, key=str)
    except ValueError as exc:
        raise RuntimeError("owner allowlist contains an invalid UUID") from exc
    if len(owners) > 100:
        raise RuntimeError("owner allowlist exceeds 100 entries")
    return owners


def worker_reference(value: str | None) -> str:
    result = (
        f"{WORKER_VERSION}:{socket.gethostname()}:{os.getpid()}:"
        f"{uuid.uuid4().hex[:12]}"
        if value is None
        else value
    )
    if not result.strip() or len(result) > 500:
        raise RuntimeError("worker id must contain 1 to 500 characters")
    return result


def validate_limits(args: argparse.Namespace, fixture: Fixture) -> uuid.UUID:
    try:
        run_id = uuid.UUID(str(args.run_id))
    except ValueError as exc:
        raise RuntimeError("run id must be a UUID") from exc
    if args.route != fixture.route:
        raise RuntimeError("requested route does not match fixture")
    if not 30 <= int(args.lease_seconds) <= 3600:
        raise RuntimeError("lease-seconds must be between 30 and 3600")
    if not 1 <= int(args.max_attempts) <= 20:
        raise RuntimeError("max-attempts must be between 1 and 20")
    if not 1 <= int(args.max_jobs) <= 10:
        raise RuntimeError("max-jobs must be between 1 and 10")
    if args.verify_replay and not args.apply_fixture:
        raise RuntimeError("replay verification requires --apply-fixture")
    return run_id


def operation_id(run_id: uuid.UUID, label: str) -> uuid.UUID:
    return uuid.uuid5(run_id, label)


async def _set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def assert_disposable_clone(conn: asyncpg.Connection) -> None:
    comment = await conn.fetchval(
        """
        SELECT shobj_description(oid,'pg_database')
        FROM pg_database
        WHERE datname=current_database()
        """
    )
    if comment != DISPOSABLE_DATABASE_COMMENT:
        raise RuntimeError("fixture apply requires an explicitly marked disposable clone")


async def database_json_sha256(
    conn: asyncpg.Connection,
    value: dict[str, Any],
) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    result = await conn.fetchval(
        """
        SELECT encode(
          public.digest(convert_to($1::jsonb::text,'UTF8'),
                        'sha256'),
          'hex'
        )
        """,
        encoded,
    )
    if not _is_sha256(result):
        raise RuntimeError("database returned an invalid JSON hash")
    return result


async def claim_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    claim_operation_id: uuid.UUID,
    route: str,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
) -> dict[str, Any] | None:
    async with conn.transaction():
        await _set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT *
            FROM memory.claim_owner_evidence_extraction_job_v1(
              $1,$2,$3,$4,$5
            )
            """,
            claim_operation_id,
            route,
            worker_id,
            lease_seconds,
            max_attempts,
        )
    return dict(row) if row else None


async def checkpoint_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    checkpoint_operation_id: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    checkpoint: dict[str, Any],
    checkpoint_sequence: int,
    lease_seconds: int,
) -> dict[str, Any]:
    checkpoint_sha256 = await database_json_sha256(conn, checkpoint)
    async with conn.transaction():
        await _set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT *
            FROM memory.checkpoint_owner_evidence_extraction_job_v1(
              $1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9
            )
            """,
            checkpoint_operation_id,
            job["job_id"],
            job["lease_token"],
            worker_id,
            job["evidence_content_sha256"],
            checkpoint_sequence,
            checkpoint_sha256,
            json.dumps(checkpoint, sort_keys=True, separators=(",", ":")),
            lease_seconds,
        )
    if row is None:
        raise RuntimeError("checkpoint function returned no row")
    return dict(row)


async def existing_checkpoint_sequence(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    checkpoint_operation_id: uuid.UUID,
) -> int | None:
    async with conn.transaction(readonly=True):
        await _set_actor(conn, owner)
        value = await conn.fetchval(
            """
            SELECT (details->>'checkpoint_sequence')::integer
            FROM memory.evidence_extraction_event
            WHERE owner_user_id=$1
              AND operation_id=$2
              AND event_type='checkpointed'
            """,
            owner,
            checkpoint_operation_id,
        )
    return int(value) if value is not None else None


async def finish_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    finish_operation_id: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    status: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    result_sha256 = await database_json_sha256(conn, payload)
    async with conn.transaction():
        await _set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT *
            FROM memory.finish_owner_evidence_extraction_job_v1(
              $1,$2,$3,$4,$5,$6,$7,$8::jsonb
            )
            """,
            finish_operation_id,
            job["job_id"],
            job["lease_token"],
            worker_id,
            job["evidence_content_sha256"],
            status,
            result_sha256,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )
    if row is None:
        raise RuntimeError("finish function returned no row")
    return dict(row)


async def fail_binding_mismatch(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    run_id: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    max_attempts: int,
) -> None:
    if job["status"] != "processing":
        return
    async with conn.transaction():
        await _set_actor(conn, owner)
        await conn.fetchrow(
            """
            SELECT *
            FROM memory.fail_owner_evidence_extraction_job_v1(
              $1,$2,$3,$4,$5,$6,$7,$8
            )
            """,
            operation_id(run_id, f"{job['job_id']}:binding-failure"),
            job["job_id"],
            job["lease_token"],
            worker_id,
            job["evidence_content_sha256"],
            "FixtureBindingMismatch",
            "claimed evidence does not match the hash-locked fixture",
            max_attempts,
        )


async def process_owner(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    run_id: uuid.UUID,
    worker_id: str,
    fixture: Fixture,
    lease_seconds: int,
    max_attempts: int,
    max_jobs: int,
    verify_replay: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "claimed": 0,
        "checkpointed": 0,
        "review_required": 0,
        "claim_replayed": 0,
        "checkpoint_replayed": 0,
        "finish_replayed": 0,
        "jobs": [],
    }
    for index in range(max_jobs):
        claim_operation_id = operation_id(
            run_id,
            f"{owner}:claim:{index}",
        )
        job = await claim_job(
            conn,
            owner=owner,
            claim_operation_id=claim_operation_id,
            route=fixture.route,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
        if job is None:
            break
        if job["apply_outcome"] == "applied":
            result["claimed"] += 1
        else:
            result["claim_replayed"] += 1
        if (
            job["route"] != fixture.route
            or job["evidence_content_sha256"] != fixture.source_content_sha256
        ):
            await fail_binding_mismatch(
                conn,
                owner=owner,
                run_id=run_id,
                job=job,
                worker_id=worker_id,
                max_attempts=max_attempts,
            )
            raise RuntimeError("claimed evidence does not match fixture binding")

        checkpoint_operation_id = operation_id(
            run_id,
            f"{job['job_id']}:checkpoint",
        )
        checkpoint_sequence = await existing_checkpoint_sequence(
            conn,
            owner=owner,
            checkpoint_operation_id=checkpoint_operation_id,
        )
        if checkpoint_sequence is None:
            checkpoint_sequence = int(job["checkpoint_sequence"]) + 1
        checkpoint = await checkpoint_job(
            conn,
            owner=owner,
            checkpoint_operation_id=checkpoint_operation_id,
            job=job,
            worker_id=worker_id,
            checkpoint=fixture.checkpoint,
            checkpoint_sequence=checkpoint_sequence,
            lease_seconds=lease_seconds,
        )
        if checkpoint["apply_outcome"] == "applied":
            result["checkpointed"] += 1
        else:
            result["checkpoint_replayed"] += 1

        finish_operation_id = operation_id(
            run_id,
            f"{job['job_id']}:finish",
        )
        finished = await finish_job(
            conn,
            owner=owner,
            finish_operation_id=finish_operation_id,
            job=job,
            worker_id=worker_id,
            status=fixture.finish_status,
            payload=fixture.finish_payload,
        )
        if finished["apply_outcome"] == "applied":
            result["review_required"] += 1
        else:
            result["finish_replayed"] += 1

        if verify_replay:
            checkpoint_replay = await checkpoint_job(
                conn,
                owner=owner,
                checkpoint_operation_id=checkpoint_operation_id,
                job=job,
                worker_id=worker_id,
                checkpoint=fixture.checkpoint,
                checkpoint_sequence=checkpoint_sequence,
                lease_seconds=lease_seconds,
            )
            finish_replay = await finish_job(
                conn,
                owner=owner,
                finish_operation_id=finish_operation_id,
                job=job,
                worker_id=worker_id,
                status=fixture.finish_status,
                payload=fixture.finish_payload,
            )
            if checkpoint_replay["apply_outcome"] != "replayed":
                raise RuntimeError("checkpoint replay wrote a second transition")
            if finish_replay["apply_outcome"] != "replayed":
                raise RuntimeError("finish replay wrote a second transition")
            result["checkpoint_replayed"] += 1
            result["finish_replayed"] += 1

        result["jobs"].append(
            {
                "job_id": str(job["job_id"]),
                "evidence_id": str(job["evidence_id"]),
                "content_sha256": job["evidence_content_sha256"],
                "claim_operation_id": str(claim_operation_id),
                "checkpoint_operation_id": str(checkpoint_operation_id),
                "finish_operation_id": str(finish_operation_id),
            }
        )
    return result


def plan_report(
    *,
    fixture: Fixture,
    owners: list[uuid.UUID],
    run_id: uuid.UUID,
    worker_id: str,
) -> dict[str, Any]:
    return {
        "contract_version": "memory_v1_evidence_extraction_fixture_worker_report_v1",
        "worker_version": WORKER_VERSION,
        "apply": False,
        "fixture_only": True,
        "fixture_sha256": fixture.file_sha256,
        "source_content_sha256": fixture.source_content_sha256,
        "route": fixture.route,
        "run_id": str(run_id),
        "worker_id": worker_id,
        "owners": [str(owner) for owner in owners],
        "owner_count": len(owners),
        "model_calls": 0,
        "candidate_writes": 0,
        "claim_writes": 0,
        "staging_writes": 0,
    }


async def apply_fixture(
    args: argparse.Namespace,
    *,
    fixture: Fixture,
    owners: list[uuid.UUID],
    run_id: uuid.UUID,
    worker_id: str,
) -> dict[str, Any]:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required for fixture apply")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        await assert_disposable_clone(conn)
        owner_results: dict[str, dict[str, Any]] = {}
        for owner in owners:
            owner_results[str(owner)] = await process_owner(
                conn,
                owner=owner,
                run_id=run_id,
                worker_id=worker_id,
                fixture=fixture,
                lease_seconds=int(args.lease_seconds),
                max_attempts=int(args.max_attempts),
                max_jobs=int(args.max_jobs),
                verify_replay=bool(args.verify_replay),
            )
    finally:
        await conn.close()
    return {
        **plan_report(
            fixture=fixture,
            owners=owners,
            run_id=run_id,
            worker_id=worker_id,
        ),
        "apply": True,
        "disposable_clone_verified": True,
        "owners": owner_results,
        "claimed": sum(item["claimed"] for item in owner_results.values()),
        "checkpointed": sum(
            item["checkpointed"] for item in owner_results.values()
        ),
        "review_required": sum(
            item["review_required"] for item in owner_results.values()
        ),
        "claim_replayed": sum(
            item["claim_replayed"] for item in owner_results.values()
        ),
        "checkpoint_replayed": sum(
            item["checkpoint_replayed"] for item in owner_results.values()
        ),
        "finish_replayed": sum(
            item["finish_replayed"] for item in owner_results.values()
        ),
    }


def main() -> int:
    args = arguments()
    fixture = load_fixture(args.fixture, args.expected_fixture_sha256)
    owners = canonical_owners(args.owner_user_id)
    run_id = validate_limits(args, fixture)
    worker_id = worker_reference(args.worker_id)
    if args.apply_fixture:
        report = asyncio.run(
            apply_fixture(
                args,
                fixture=fixture,
                owners=owners,
                run_id=run_id,
                worker_id=worker_id,
            )
        )
    else:
        report = plan_report(
            fixture=fixture,
            owners=owners,
            run_id=run_id,
            worker_id=worker_id,
        )
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
