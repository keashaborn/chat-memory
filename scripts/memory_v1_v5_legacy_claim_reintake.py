#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

import asyncpg

VERSION = "memory_v1_v5_legacy_claim_reintake_v1"
SELECTOR_VERSION = "20260719_v5_legacy_claim_reintake_v1"
APPLY_TOKEN = "memory_v1_v5_legacy_claim_reintake_apply_v1"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or enqueue legacy-only evidence for one V5 reintake"
    )
    parser.add_argument("--owner-user-id", action="append", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report-path", required=True)
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return sha256_text(stable_json(value))


def canonical_owners(values: Sequence[str]) -> list[uuid.UUID]:
    try:
        owners = sorted({uuid.UUID(value) for value in values}, key=str)
    except ValueError as exc:
        raise RuntimeError("owner allowlist contains an invalid UUID") from exc
    if not owners or len(owners) > 6:
        raise RuntimeError("owner allowlist must contain one to six UUIDs")
    return owners


def loopback_dsn(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("database DSN scheme is invalid")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("database DSN must be loopback-only")
    return value


def secure_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
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


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def plan(
    conn: asyncpg.Connection, owner: uuid.UUID, limit: int
) -> list[dict[str, Any]]:
    await set_actor(conn, owner)
    rows = await conn.fetch(
        """
        SELECT evidence_id,evidence_content_sha256,outcome,route,reason_code
        FROM memory.plan_owner_v5_legacy_claim_reintake_v1($1,$2,NULL)
        """,
        SELECTOR_VERSION,
        limit,
    )
    return [dict(row) for row in rows]


async def run_owner(
    conn: asyncpg.Connection, owner: uuid.UUID, limit: int, apply: bool
) -> dict[str, Any]:
    before = await plan(conn, owner, limit)
    if any(
        row["outcome"] != "eligible"
        or row["route"] != "relational_extraction"
        or row["reason_code"] != "eligible_unprocessed"
        for row in before
    ):
        raise RuntimeError("legacy reintake plan contract changed")
    applied = 0
    replayed = 0
    if apply:
        for row in before:
            first = await conn.fetchrow(
                """
                SELECT * FROM memory.enqueue_owner_v5_legacy_claim_reintake_v1(
                  $1,$2,$3,$4,$5
                )
                """,
                row["evidence_id"],
                SELECTOR_VERSION,
                row["evidence_content_sha256"],
                row["route"],
                row["reason_code"],
            )
            second = await conn.fetchrow(
                """
                SELECT * FROM memory.enqueue_owner_v5_legacy_claim_reintake_v1(
                  $1,$2,$3,$4,$5
                )
                """,
                row["evidence_id"],
                SELECTOR_VERSION,
                row["evidence_content_sha256"],
                row["route"],
                row["reason_code"],
            )
            if first["apply_outcome"] != "applied" or second["apply_outcome"] != "replayed":
                raise RuntimeError("legacy reintake enqueue replay failed")
            applied += 1
            replayed += 1
    after = await plan(conn, owner, limit)
    if apply and after:
        raise RuntimeError("legacy reintake plan was not exhausted after apply")
    safe_before = [
        {
            "evidence_id_sha256": sha256_text(str(row["evidence_id"])),
            "evidence_content_sha256": row["evidence_content_sha256"],
            "route": row["route"],
        }
        for row in before
    ]
    return {
        "owner_user_id_sha256": sha256_text(str(owner)),
        "plan_count": len(before),
        "plan_sha256": digest(safe_before),
        "applied": applied,
        "replayed": replayed,
        "after_count": len(after),
    }


async def run() -> int:
    args = arguments()
    if not 1 <= args.limit <= 100:
        raise RuntimeError("limit must be between one and 100")
    if args.apply and os.getenv("MEMORY_V1_V5_LEGACY_REINTAKE_APPLY") != APPLY_TOKEN:
        raise RuntimeError("legacy V5 reintake apply capability is absent")
    owners = canonical_owners(args.owner_user_id)
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=60, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("legacy V5 reintake requires brains_app session")
        async with conn.transaction():
            reports = [
                await run_owner(conn, owner, args.limit, args.apply)
                for owner in owners
            ]
    finally:
        await conn.close()
    report = {
        "contract_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selector_version": SELECTOR_VERSION,
        "mode": "owner_scoped_transactional_apply" if args.apply else "read_only_dry_run",
        "owners": reports,
        "totals": {
            "planned": sum(item["plan_count"] for item in reports),
            "applied": sum(item["applied"] for item in reports),
            "replayed": sum(item["replayed"] for item in reports),
            "after": sum(item["after_count"] for item in reports),
        },
        "claim_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "local_model_calls": 0,
        "prompt_influence": 0,
    }
    output = Path(args.report_path).resolve()
    report_sha256 = secure_write(output, report)
    print(stable_json({
        "version": VERSION,
        "apply": args.apply,
        "owner_count": len(owners),
        "totals": report["totals"],
        "output": str(output),
        "sha256": report_sha256,
        "claim_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "local_model_calls": 0,
        "prompt_influence": 0,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
