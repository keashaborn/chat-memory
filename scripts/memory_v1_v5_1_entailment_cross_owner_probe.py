#!/usr/bin/env python3
"""Prove a reviewed entailment manifest is unreadable under another owner."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid

from memory_v1_v5_1_entailment_batch import (
    load_manifest,
    private_path,
    sha256,
    validate_item,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--other-owner", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


async def run() -> int:
    import asyncpg

    args = arguments()
    manifest = load_manifest(private_path(args.manifest, must_exist=True))
    other_owner = str(uuid.UUID(args.other_owner))
    if other_owner == manifest["owner_user_id"]:
        raise RuntimeError("cross-owner probe requires a different owner")
    output = private_path(args.output, must_exist=False)
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    rejected = 0
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must authenticate as brains_app")
        transaction = conn.transaction(isolation="repeatable_read", readonly=True)
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", other_owner)
        for item in manifest["items"]:
            try:
                async with conn.transaction():
                    await validate_item(conn, manifest, item)
            except asyncpg.PostgresError as exc:
                if exc.sqlstate != "P0002":
                    raise
                rejected += 1
            else:
                raise RuntimeError("cross-owner observation was visible")
        txid = await conn.fetchval("SELECT txid_current_if_assigned()")
        await transaction.rollback()
    finally:
        await conn.close()
    if rejected != len(manifest["items"]) or txid is not None:
        raise RuntimeError("cross-owner rejection proof failed")
    result = {
        "contract_version": "memory_v1_observation_entailment_cross_owner_probe_v5_1",
        "manifest_sha256": manifest["manifest_sha256"],
        "target_owner_sha256": sha256(manifest["owner_user_id"]),
        "other_owner_sha256": sha256(other_owner),
        "items_rejected": rejected,
        "database_writes": 0,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "prompt_influence": 0,
    }
    result["result_sha256"] = sha256(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)
    print(f"items_rejected={rejected}")
    print(f"result={output}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
