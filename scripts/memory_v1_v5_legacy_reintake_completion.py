#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import tempfile
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import asyncpg


VERSION = "memory_v1_v5_legacy_reintake_completion_v1"
TERMINAL_STATUSES = {"completed", "review_required", "skipped"}
ACTIVE_STATUSES = {"pending", "leased", "processing", "error"}
SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or compare a sanitized, read-only completion snapshot for one "
            "owner-scoped legacy reintake selector."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--isolation-owner-user-id", required=True)
    parser.add_argument("--selector-version", required=True)
    parser.add_argument("--expected-jobs", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline")
    parser.add_argument(
        "--qdrant-scroll-url",
        default="http://127.0.0.1:6333/collections/memory_claim_v1/points/scroll",
    )
    return parser.parse_args()


def validated(args: argparse.Namespace) -> tuple[uuid.UUID, uuid.UUID]:
    try:
        owner = uuid.UUID(args.owner_user_id)
        isolation_owner = uuid.UUID(args.isolation_owner_user_id)
    except ValueError as exc:
        raise RuntimeError("owner ids must be UUIDs") from exc
    if owner == isolation_owner:
        raise RuntimeError("isolation owner must differ from target owner")
    if not 1 <= args.expected_jobs <= 100:
        raise RuntimeError("expected-jobs must be between 1 and 100")
    if not 3 <= len(args.selector_version) <= 100:
        raise RuntimeError("selector-version length is invalid")
    return owner, isolation_owner


def loopback_dsn(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("database DSN scheme is invalid")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("database DSN must be loopback-only")
    return value


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def owner_snapshot(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    selector_version: str,
) -> dict[str, Any]:
    async with conn.transaction(readonly=True, isolation="repeatable_read"):
        await set_actor(conn, owner)
        jobs = await conn.fetch(
            """
            SELECT job_id,evidence_content_sha256,status::text AS status,attempts,
                   lease_token IS NOT NULL AS has_lease,last_error,
                   result#>>'{final,payload,packet_storage_sha256}'
                     AS packet_storage_sha256,
                   result#>>'{final,payload,provider_output_sha256}'
                     AS provider_output_sha256,
                   result#>>'{final,payload,validator_packet_sha256}'
                     AS validator_packet_sha256,
                   result#>>'{final,payload,policy_compiler_sha256}'
                     AS policy_compiler_sha256,
                   CASE WHEN result#>>'{final,payload,local_model_calls}' ~ '^[0-9]+$'
                     THEN (result#>>'{final,payload,local_model_calls}')::integer
                     ELSE 0 END AS local_model_calls,
                   CASE WHEN result#>>'{final,payload,external_model_calls}' ~ '^[0-9]+$'
                     THEN (result#>>'{final,payload,external_model_calls}')::integer
                     ELSE 0 END AS external_model_calls
              FROM memory.evidence_extraction_job
             WHERE owner_user_id=$1 AND route='relational_extraction'
               AND selector_version=$2
             ORDER BY job_id
            """,
            owner,
            selector_version,
        )
        privileges = {
            table: bool(
                await conn.fetchval(
                    """
                    SELECT bool_or(has_table_privilege('brains_app',$1,privilege))
                      FROM unnest(ARRAY['INSERT','UPDATE','DELETE']::text[]) privilege
                    """,
                    f"memory.{table}",
                )
            )
            for table in ("entity", "candidate", "claim", "claim_revision")
        }

    status_counts: dict[str, int] = {}
    attempt_counts: dict[str, int] = {}
    for row in jobs:
        status = str(row["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
        key = str(int(row["attempts"]))
        attempt_counts[key] = attempt_counts.get(key, 0) + 1
    job_bindings = [
        {
            "job_id_sha256": sha256_text(str(row["job_id"])),
            "evidence_content_sha256": str(row["evidence_content_sha256"]),
            "status": str(row["status"]),
            "attempts": int(row["attempts"]),
            "has_lease": bool(row["has_lease"]),
        }
        for row in jobs
    ]
    def safe_error(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        if SAFE_CODE_RE.fullmatch(text):
            return text
        return f"sha256:{sha256_text(text)}"

    completion_inventory = [
        {
            "job_id_sha256": sha256_text(str(row["job_id"])),
            "status": str(row["status"]),
            "rejection_code": safe_error(row["last_error"]),
            "packet_storage_sha256": (
                str(row["packet_storage_sha256"])
                if row["packet_storage_sha256"] is not None
                else None
            ),
            "provider_output_sha256": (
                str(row["provider_output_sha256"])
                if row["provider_output_sha256"] is not None
                else None
            ),
            "validator_packet_sha256": (
                str(row["validator_packet_sha256"])
                if row["validator_packet_sha256"] is not None
                else None
            ),
            "policy_compiler_sha256": (
                str(row["policy_compiler_sha256"])
                if row["policy_compiler_sha256"] is not None
                else None
            ),
            "local_model_calls": int(row["local_model_calls"]),
            "external_model_calls": int(row["external_model_calls"]),
        }
        for row in jobs
        if str(row["status"]) in TERMINAL_STATUSES
    ]
    return {
        "job_count": len(jobs),
        "status_counts": dict(sorted(status_counts.items())),
        "attempt_counts": dict(sorted(attempt_counts.items())),
        "active_lease_count": sum(bool(row["has_lease"]) for row in jobs),
        "job_bindings": job_bindings,
        "packet_count": sum(
            row["packet_storage_sha256"] is not None
            for row in completion_inventory
        ),
        "completion_inventory": completion_inventory,
        "protected_direct_write_privileges": privileges,
    }


async def cross_owner_visible_count(
    conn: asyncpg.Connection,
    isolation_owner: uuid.UUID,
    selector_version: str,
) -> int:
    async with conn.transaction(readonly=True, isolation="repeatable_read"):
        await set_actor(conn, isolation_owner)
        return int(
            await conn.fetchval(
                """
                SELECT count(*) FROM memory.evidence_extraction_job
                 WHERE selector_version=$1
                """,
                selector_version,
            )
        )


def qdrant_snapshot(url: str) -> dict[str, Any]:
    points: list[dict[str, Any]] = []
    offset: Any = None
    for _ in range(100):
        request_body: dict[str, Any] = {
            "limit": 256,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            request_body["offset"] = offset
        request = urllib.request.Request(
            url,
            data=stable_json(request_body).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("points"), list):
            raise RuntimeError("Qdrant scroll returned an invalid payload")
        points.extend(result["points"])
        offset = result.get("next_page_offset")
        if offset is None:
            break
    else:
        raise RuntimeError("Qdrant scroll exceeded the page bound")
    canonical = sorted(
        (
            {
                "id": point.get("id"),
                "payload": point.get("payload"),
            }
            for point in points
        ),
        key=lambda value: stable_json(value.get("id")),
    )
    return {
        "point_count": len(canonical),
        "inventory_sha256": sha256_text(stable_json(canonical)),
    }


def attempt_total(snapshot: dict[str, Any]) -> int:
    return sum(
        int(row["attempts"])
        for row in snapshot.get("job_bindings", [])
    )


def evaluation(
    *,
    current: dict[str, Any],
    expected_jobs: int,
    cross_owner_count: int,
    qdrant: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> dict[str, Any]:
    statuses = current["status_counts"]
    active_count = sum(int(statuses.get(value, 0)) for value in ACTIVE_STATUSES)
    terminal_count = sum(int(statuses.get(value, 0)) for value in TERMINAL_STATUSES)
    direct_writes_absent = not any(
        current["protected_direct_write_privileges"].values()
    )
    checks: dict[str, bool] = {
        "exact_job_count": current["job_count"] == expected_jobs,
        "account_isolation": cross_owner_count == 0,
        "no_active_leases": current["active_lease_count"] == 0,
        "protected_direct_writes_absent": direct_writes_absent,
        "external_model_calls_zero": all(
            row["external_model_calls"] == 0
            for row in current["completion_inventory"]
        ),
    }
    if baseline is None:
        checks["baseline_has_no_terminal_requirement"] = True
        ready_for_restoration = False
        attempt_delta = 0
    else:
        baseline_current = baseline.get("snapshot") or {}
        baseline_qdrant = baseline.get("qdrant") or {}
        immutable_bindings = [
            {
                "job_id_sha256": row["job_id_sha256"],
                "evidence_content_sha256": row["evidence_content_sha256"],
            }
            for row in current["job_bindings"]
        ]
        baseline_bindings = [
            {
                "job_id_sha256": row["job_id_sha256"],
                "evidence_content_sha256": row["evidence_content_sha256"],
            }
            for row in baseline_current.get("job_bindings", [])
        ]
        attempt_delta = attempt_total(current) - attempt_total(baseline_current)
        checks.update(
            {
                "same_job_bindings": immutable_bindings == baseline_bindings,
                "qdrant_unchanged": qdrant == baseline_qdrant,
                "exact_attempt_delta": attempt_delta == expected_jobs,
                "all_jobs_terminal": terminal_count == expected_jobs
                and active_count == 0,
            }
        )
        ready_for_restoration = all(checks.values())
    return {
        "checks": checks,
        "pass": all(checks.values()),
        "ready_for_restoration": ready_for_restoration,
        "active_job_count": active_count,
        "terminal_job_count": terminal_count,
        "attempt_delta": attempt_delta,
    }


def secure_write(path: Path, packet: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError("completion audit output already exists")
    payload = (json.dumps(packet, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(payload).hexdigest()


async def run() -> int:
    args = arguments()
    owner, isolation_owner = validated(args)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=30, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("completion audit requires brains_app session")
        current = await owner_snapshot(conn, owner, args.selector_version)
        cross_count = await cross_owner_visible_count(
            conn, isolation_owner, args.selector_version
        )
    finally:
        await conn.close()
    qdrant = qdrant_snapshot(args.qdrant_scroll_url)
    baseline = None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        if baseline.get("version") != VERSION:
            raise RuntimeError("baseline contract version changed")
        if baseline.get("owner_user_id_sha256") != sha256_text(str(owner)):
            raise RuntimeError("baseline owner binding changed")
        if baseline.get("selector_version_sha256") != sha256_text(
            args.selector_version
        ):
            raise RuntimeError("baseline selector binding changed")
        if baseline.get("expected_jobs") != args.expected_jobs:
            raise RuntimeError("baseline expected-job binding changed")
    result = evaluation(
        current=current,
        expected_jobs=args.expected_jobs,
        cross_owner_count=cross_count,
        qdrant=qdrant,
        baseline=baseline,
    )
    packet = {
        "version": VERSION,
        "mode": "comparison" if baseline is not None else "baseline",
        "owner_user_id_sha256": sha256_text(str(owner)),
        "isolation_owner_user_id_sha256": sha256_text(str(isolation_owner)),
        "selector_version_sha256": sha256_text(args.selector_version),
        "expected_jobs": args.expected_jobs,
        "snapshot": current,
        "cross_owner_visible_jobs": cross_count,
        "qdrant": qdrant,
        "evaluation": result,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "prompt_influence": 0,
    }
    output = Path(args.output)
    packet_sha256 = secure_write(output, packet)
    print(
        stable_json(
            {
                "version": VERSION,
                "mode": packet["mode"],
                "packet_sha256": packet_sha256,
                "evaluation": result,
                "database_writes": 0,
                "qdrant_writes": 0,
                "external_model_calls": 0,
                "prompt_influence": 0,
            }
        )
    )
    return 0 if result["pass"] else 2


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
