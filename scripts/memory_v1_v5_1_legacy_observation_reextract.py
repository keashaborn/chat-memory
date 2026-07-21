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


VERSION = "memory_v1_v5_1_legacy_observation_reextract_v1"
SELECTOR_VERSION = "20260721_v5_1_legacy_observation_reextract_v1"
APPLY_TOKEN = "memory_v1_v5_1_legacy_observation_reextract_apply_v1"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or enqueue exact owner-scoped V5 observation evidence for "
            "V5.1 re-extraction"
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--evidence-id", action="append", required=True)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report-path", required=True)
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return sha256_text(stable_json(value))


def one_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise RuntimeError(f"{field} must be a UUID") from exc


def evidence_ids(values: Sequence[str]) -> list[uuid.UUID]:
    result = sorted({one_uuid(value, "evidence id") for value in values}, key=str)
    if not 1 <= len(result) <= 12:
        raise RuntimeError("one to twelve unique evidence ids are required")
    return result


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


async def plan_one(
    conn: asyncpg.Connection, owner: uuid.UUID, evidence_id: uuid.UUID
) -> dict[str, Any]:
    await set_actor(conn, owner)
    row = await conn.fetchrow(
        """
        SELECT evidence_id,evidence_content_sha256,outcome,route,reason_code,
               legacy_observation_count
        FROM memory.plan_owner_v5_1_legacy_observation_reextract_v1($1,1,$2)
        """,
        SELECTOR_VERSION,
        evidence_id,
    )
    if row is None:
        raise RuntimeError("exact evidence is not eligible for V5.1 re-extraction")
    result = dict(row)
    if (
        result["evidence_id"] != evidence_id
        or result["outcome"] != "eligible"
        or result["route"] != "relational_extraction"
        or result["reason_code"] != "eligible_unprocessed"
        or int(result["legacy_observation_count"]) < 1
    ):
        raise RuntimeError("V5.1 re-extraction plan contract changed")
    return result


def safe_plan(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id_sha256": sha256_text(str(row["evidence_id"])),
            "evidence_content_sha256": row["evidence_content_sha256"],
            "route": row["route"],
            "legacy_observation_count": int(row["legacy_observation_count"]),
        }
        for row in rows
    ]


async def enqueue(
    conn: asyncpg.Connection, row: dict[str, Any]
) -> tuple[str, str]:
    values = (
        row["evidence_id"],
        SELECTOR_VERSION,
        row["evidence_content_sha256"],
        row["route"],
        row["reason_code"],
        int(row["legacy_observation_count"]),
    )
    first = await conn.fetchrow(
        """
        SELECT *
        FROM memory.enqueue_owner_v5_1_legacy_observation_reextract_v1(
          $1,$2,$3,$4,$5,$6
        )
        """,
        *values,
    )
    second = await conn.fetchrow(
        """
        SELECT *
        FROM memory.enqueue_owner_v5_1_legacy_observation_reextract_v1(
          $1,$2,$3,$4,$5,$6
        )
        """,
        *values,
    )
    if first is None or second is None:
        raise RuntimeError("V5.1 re-extraction enqueue returned no row")
    if first["apply_outcome"] not in {"applied", "replayed"}:
        raise RuntimeError("V5.1 re-extraction apply outcome is invalid")
    if second["apply_outcome"] != "replayed":
        raise RuntimeError("V5.1 re-extraction replay wrote again")
    return str(first["apply_outcome"]), str(second["apply_outcome"])


async def run() -> int:
    args = arguments()
    owner = one_uuid(args.owner_user_id, "owner user id")
    targets = evidence_ids(args.evidence_id)
    if args.apply and args.expected_plan_sha256 is None:
        raise RuntimeError("apply requires --expected-plan-sha256")
    if args.expected_plan_sha256 is not None and (
        len(args.expected_plan_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in args.expected_plan_sha256)
    ):
        raise RuntimeError("expected plan SHA-256 is invalid")
    if args.apply and os.getenv("MEMORY_V1_V5_1_LEGACY_OBSERVATION_REEXTRACT_APPLY") != APPLY_TOKEN:
        raise RuntimeError("V5.1 legacy observation re-extraction apply capability is absent")

    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=60, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("V5.1 re-extraction requires brains_app session")
        async with conn.transaction():
            rows = [await plan_one(conn, owner, target) for target in targets]
            plan = safe_plan(rows)
            plan_sha256 = digest(plan)
            if args.expected_plan_sha256 is not None and plan_sha256 != args.expected_plan_sha256:
                raise RuntimeError("V5.1 re-extraction plan SHA-256 changed")
            applied = 0
            replayed = 0
            if args.apply:
                for row in rows:
                    first, second = await enqueue(conn, row)
                    applied += int(first == "applied")
                    replayed += int(first == "replayed") + int(second == "replayed")
    finally:
        await conn.close()

    report = {
        "contract_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selector_version": SELECTOR_VERSION,
        "mode": "owner_scoped_transactional_apply" if args.apply else "read_only_dry_run",
        "owner_user_id_sha256": sha256_text(str(owner)),
        "plan": plan,
        "plan_count": len(plan),
        "plan_sha256": plan_sha256,
        "applied": applied,
        "replayed": replayed,
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
        "plan_count": len(plan),
        "plan_sha256": plan_sha256,
        "applied": applied,
        "replayed": replayed,
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
