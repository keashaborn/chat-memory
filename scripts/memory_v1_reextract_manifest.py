#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import asyncpg


PIPELINE_VERSION = "20260714_v2"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_manifest(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(payload) != {
        "manifest_version",
        "owner_user_id",
        "pipeline_version",
        "reason",
        "sources",
    }:
        raise RuntimeError("manifest keys do not match the v1 contract")
    if payload["manifest_version"] != "memory_v1_reextract_v1":
        raise RuntimeError("unsupported manifest_version")
    if payload["pipeline_version"] != PIPELINE_VERSION:
        raise RuntimeError("manifest pipeline_version mismatch")
    uuid.UUID(payload["owner_user_id"])
    sources = payload["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 50:
        raise RuntimeError("manifest sources must contain 1 to 50 rows")
    seen: set[str] = set()
    for item in sources:
        if not isinstance(item, dict) or set(item) != {
            "source_external_id",
            "source_sha256",
        }:
            raise RuntimeError("invalid source row")
        source_id = str(uuid.UUID(item["source_external_id"]))
        if source_id in seen:
            raise RuntimeError(f"duplicate source_external_id: {source_id}")
        seen.add(source_id)
        digest = str(item["source_sha256"]).lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise RuntimeError(f"invalid source_sha256: {source_id}")
    return payload


async def verify_sources(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    sources: list[dict[str, str]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in sources:
        source_id = uuid.UUID(item["source_external_id"])
        row = await conn.fetchrow(
            """
            SELECT id, text, created_at, source
            FROM public.chat_log
            WHERE owner_user_id=$1 AND id=$2
            """,
            owner,
            source_id,
        )
        if row is None or row["source"] != "frontend/chat:user":
            raise RuntimeError(f"owner-scoped source not found: {source_id}")
        actual = hashlib.sha256(str(row["text"] or "").encode("utf-8")).hexdigest()
        if actual != item["source_sha256"]:
            raise RuntimeError(f"source hash mismatch: {source_id}")
        existing = await conn.fetchval(
            """
            SELECT count(*)
            FROM memory.consolidation_job
            WHERE owner_user_id=$1
              AND source_system='public.chat_log'
              AND source_external_id=$2
              AND pipeline_version=$3
            """,
            owner,
            str(source_id),
            PIPELINE_VERSION,
        )
        output.append(
            {
                "source_external_id": str(source_id),
                "source_sha256": actual,
                "source_recorded_at": row["created_at"].isoformat(),
                "v2_job_exists": bool(existing),
            }
        )
    return output


async def main() -> int:
    args = arguments()
    manifest = load_manifest(args.manifest)
    owner = uuid.UUID(manifest["owner_user_id"])
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is not configured")
    if args.apply and os.getenv("MEMORY_V1_REEXTRACT_APPLY", "0").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise RuntimeError("apply requires MEMORY_V1_REEXTRACT_APPLY=1")

    conn = await asyncpg.connect(dsn)
    try:
        if await conn.fetchval("SELECT current_user") != "brains_app":
            raise RuntimeError("manifest runner requires brains_app")
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))
            verified = await verify_sources(conn, owner, manifest["sources"])
            result: dict[str, Any] = {
                "mode": "apply" if args.apply else "dry_run",
                "owner_user_id": str(owner),
                "pipeline_version": PIPELINE_VERSION,
                "manifest_sha256": hashlib.sha256(
                    stable_json(manifest).encode("utf-8")
                ).hexdigest(),
                "verified_sources": verified,
            }
            if args.apply:
                applied = await conn.fetchrow(
                    "SELECT * FROM memory.enqueue_consolidation_reextract($1,$2::jsonb)",
                    PIPELINE_VERSION,
                    stable_json(manifest["sources"]),
                )
                result["enqueued_count"] = int(applied["enqueued_count"])
                result["existing_count"] = int(applied["existing_count"])
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
