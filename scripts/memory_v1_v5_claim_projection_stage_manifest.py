#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import uuid
from pathlib import Path

from memory_v1_projection_v5_contract_test import sha256, stable_json
from memory_v1_v5_claim_projection_preflight import load_source
from memory_v1_v5_claim_projection_stage_batch import (
    EXPECTED_TABLE_ROWS,
    MANIFEST_CONTRACT,
    file_sha256,
    private_review_path,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--required-head", required=True)
    parser.add_argument("--assessor-ref", required=True)
    parser.add_argument("--bundle", action="append", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


async def run() -> int:
    import asyncpg

    args = arguments()
    owner = str(uuid.UUID(args.owner))
    if len(args.bundle) != 4:
        raise RuntimeError("exactly four bundles are required")
    if not args.assessor_ref.strip() or len(args.assessor_ref) > 500:
        raise RuntimeError("assessor reference is invalid")
    output = private_review_path(args.output, must_exist=False)
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    items = []
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        transaction = conn.transaction(isolation="repeatable_read", readonly=True)
        await transaction.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        for bundle_arg in args.bundle:
            path = private_review_path(bundle_arg, must_exist=True)
            bundle = json.loads(path.read_text())
            observation_id = str(uuid.UUID(
                bundle["source_snapshot"]["observation_id"]
            ))
            plan_id = str(uuid.UUID(bundle["plan_id"]))
            source = await load_source(conn, uuid.UUID(observation_id))
            preflight = await conn.fetchrow(
                """SELECT * FROM memory.preflight_observation_entailment_v5(
                    $1,'accepted'::memory.observation_entailment_decision_v5,
                    'predicate_entailment_v5_1_accepted',$2::jsonb,
                    'system',$3
                )""",
                uuid.UUID(observation_id),
                stable_json(source["source_spans"]),
                args.assessor_ref,
            )
            packet_preflight = await conn.fetchrow(
                "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
                uuid.UUID(plan_id),
                bundle["packet_text"],
            )
            if (
                packet_preflight["existing_claims"] != 0
                or packet_preflight["existing_plans"] != 0
            ):
                raise RuntimeError("bundle is not a clean projection target")
            request_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                "memory-v5-1-live-entailment:"
                + observation_id
                + ":"
                + bundle["bundle_sha256"],
            )
            items.append(
                {
                    "bundle_path": str(path),
                    "bundle_file_sha256": file_sha256(path),
                    "bundle_sha256": bundle["bundle_sha256"],
                    "observation_id": observation_id,
                    "plan_id": plan_id,
                    "entailment_request_id": str(request_id),
                    "entailment_authorization_manifest_sha256": preflight[
                        "authorization_manifest_sha256"
                    ],
                }
            )
        await transaction.rollback()
    finally:
        await conn.close()
    items.sort(key=lambda item: item["observation_id"])
    manifest = {
        "contract_version": MANIFEST_CONTRACT,
        "owner_user_id": owner,
        "required_head_commit": args.required_head,
        "assessor_type": "system",
        "assessor_ref": args.assessor_ref,
        "expected_new_rows": sum(EXPECTED_TABLE_ROWS.values()),
        "expected_table_rows": EXPECTED_TABLE_ROWS,
        "items": items,
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = sha256(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"manifest={output}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    print(f"expected_new_rows={manifest['expected_new_rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
