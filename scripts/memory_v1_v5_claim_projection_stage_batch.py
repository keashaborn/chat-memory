#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from memory_v1_projection_v5_contract_test import (
    owner_manifest_sha256,
    sha256,
    stable_json,
    validate_packet,
)
from memory_v1_v5_claim_projection_preflight import (
    build_packet,
    build_projection,
    load_contract,
    load_source,
)


MANIFEST_CONTRACT = "memory_v1_claim_projection_stage_batch_manifest_v1"
RESULT_CONTRACT = "memory_v1_claim_projection_stage_batch_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
EXPECTED_TABLE_ROWS = {
    "observation_entailment_v5": 4,
    "relational_operation_request": 4,
    "projection_plan": 4,
    "projection_plan_item": 4,
    "projection_claim_payload": 4,
    "projection_plan_observation": 4,
}


class StageBatchError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "apply", "replay"), required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def private_review_path(value: str, *, must_exist: bool) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise StageBatchError("artifact is outside the review root")
    if must_exist:
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise StageBatchError("review input must be a private regular file")
    elif path.exists():
        raise StageBatchError("output path already exists")
    return path


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text())
    expected_keys = {
        "contract_version",
        "owner_user_id",
        "required_head_commit",
        "assessor_type",
        "assessor_ref",
        "expected_new_rows",
        "expected_table_rows",
        "items",
        "manifest_sha256",
    }
    if set(manifest) != expected_keys:
        raise StageBatchError("manifest fields mismatch")
    if manifest["contract_version"] != MANIFEST_CONTRACT:
        raise StageBatchError("manifest contract mismatch")
    uuid.UUID(manifest["owner_user_id"])
    if manifest["assessor_type"] != "system":
        raise StageBatchError("only the reviewed system assessor is supported")
    assessor = manifest["assessor_ref"]
    if not isinstance(assessor, str) or not assessor.strip() or len(assessor) > 500:
        raise StageBatchError("assessor reference is invalid")
    if manifest["expected_new_rows"] != 24:
        raise StageBatchError("batch row budget mismatch")
    if manifest["expected_table_rows"] != EXPECTED_TABLE_ROWS:
        raise StageBatchError("table row budget mismatch")
    if not isinstance(manifest["items"], list) or len(manifest["items"]) != 4:
        raise StageBatchError("batch must contain exactly four items")
    if manifest["manifest_sha256"] != sha256(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    ):
        raise StageBatchError("manifest hash mismatch")
    return manifest


def load_items(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    item_keys = {
        "bundle_path",
        "bundle_file_sha256",
        "bundle_sha256",
        "observation_id",
        "plan_id",
        "entailment_request_id",
        "entailment_authorization_manifest_sha256",
    }
    seen_observations: set[str] = set()
    seen_plans: set[str] = set()
    seen_requests: set[str] = set()
    loaded: list[dict[str, Any]] = []
    for item in manifest["items"]:
        if not isinstance(item, dict) or set(item) != item_keys:
            raise StageBatchError("manifest item fields mismatch")
        observation = str(uuid.UUID(item["observation_id"]))
        plan = str(uuid.UUID(item["plan_id"]))
        request = str(uuid.UUID(item["entailment_request_id"]))
        if observation in seen_observations or plan in seen_plans or request in seen_requests:
            raise StageBatchError("duplicate batch identity")
        seen_observations.add(observation)
        seen_plans.add(plan)
        seen_requests.add(request)
        bundle_path = private_review_path(item["bundle_path"], must_exist=True)
        if file_sha256(bundle_path) != item["bundle_file_sha256"]:
            raise StageBatchError("bundle file hash mismatch")
        bundle = json.loads(bundle_path.read_text())
        bundle_hash = bundle.get("bundle_sha256")
        if bundle_hash != item["bundle_sha256"] or bundle_hash != sha256(
            {key: value for key, value in bundle.items() if key != "bundle_sha256"}
        ):
            raise StageBatchError("bundle content hash mismatch")
        if (
            bundle.get("contract_version")
            != "memory_v1_claim_projection_stage_bundle_v5_1"
            or bundle.get("owner_user_id") != manifest["owner_user_id"]
            or bundle.get("plan_id") != plan
            or bundle.get("source_snapshot", {}).get("observation_id") != observation
            or bundle.get("database_writes") != 0
            or bundle.get("external_model_calls") != 0
            or bundle.get("qdrant_writes") != 0
        ):
            raise StageBatchError("bundle boundary mismatch")
        loaded.append({**item, "bundle_path": bundle_path, "bundle": bundle})
    return loaded


async def validate_item(
    conn: Any,
    manifest: dict[str, Any],
    item: dict[str, Any],
    *,
    replay: bool,
) -> dict[str, Any]:
    owner = manifest["owner_user_id"]
    bundle = item["bundle"]
    source = await load_source(conn, uuid.UUID(item["observation_id"]))
    projection = build_projection(owner, source)
    packet = build_packet(projection)
    validate_packet(packet, owner, load_contract())
    if packet != bundle["packet"] or stable_json(packet) != bundle["packet_text"]:
        raise StageBatchError("bundle no longer matches deterministic source projection")
    if bundle["packet_sha256"] != packet["packet_sha256"]:
        raise StageBatchError("bundle packet hash mismatch")
    expected_owner_manifest = owner_manifest_sha256(owner, packet["packet_sha256"])
    if bundle["owner_manifest_sha256"] != expected_owner_manifest:
        raise StageBatchError("bundle owner manifest mismatch")
    packet_preflight = await conn.fetchrow(
        "SELECT * FROM memory.preflight_claim_projection_packet_v5_1($1,$2)",
        uuid.UUID(item["plan_id"]),
        bundle["packet_text"],
    )
    expected_existing = 1 if replay else 0
    if (
        packet_preflight["owner_manifest_sha256"] != expected_owner_manifest
        or packet_preflight["existing_claims"] != 0
        or packet_preflight["existing_plans"] != expected_existing
    ):
        raise StageBatchError("database projection preflight is stale")
    spans_text = stable_json(source["source_spans"])
    entailment = await conn.fetchrow(
        """SELECT * FROM memory.preflight_observation_entailment_v5(
            $1,'accepted'::memory.observation_entailment_decision_v5,
            'predicate_entailment_v5_1_accepted',$2::jsonb,$3,$4
        )""",
        uuid.UUID(item["observation_id"]),
        spans_text,
        manifest["assessor_type"],
        manifest["assessor_ref"],
    )
    if (
        entailment["authorization_manifest_sha256"]
        != item["entailment_authorization_manifest_sha256"]
    ):
        raise StageBatchError("entailment authorization manifest drifted")
    return {
        "source_spans_text": spans_text,
        "owner_manifest_sha256": expected_owner_manifest,
    }


async def run() -> int:
    import asyncpg

    args = arguments()
    manifest_path = private_review_path(args.manifest, must_exist=True)
    output = private_review_path(args.output, must_exist=False)
    manifest = load_manifest(manifest_path)
    if os.environ.get("MEMORY_V1_REQUIRED_HEAD", "") != manifest["required_head_commit"]:
        raise StageBatchError("runtime head does not match manifest")
    if args.mode == "apply" and os.environ.get(
        "MEMORY_V1_CLAIM_PROJECTION_STAGE_BATCH_APPLY", ""
    ) != "authorized":
        raise StageBatchError("apply mode requires explicit environment authorization")
    items = load_items(manifest)
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise StageBatchError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    rows_written = 0
    outcomes: list[dict[str, Any]] = []
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise StageBatchError("POSTGRES_DSN must authenticate as brains_app")
        transaction = conn.transaction(
            isolation="serializable", readonly=args.mode == "preflight"
        )
        await transaction.start()
        await conn.execute(
            "SELECT set_config('app.user_id',$1,true)", manifest["owner_user_id"]
        )
        prepared: list[dict[str, Any]] = []
        for item in items:
            values = await validate_item(
                conn, manifest, item, replay=args.mode == "replay"
            )
            prepared.append({**item, **values})
        if args.mode in {"apply", "replay"}:
            for item in prepared:
                entailment = await conn.fetchrow(
                    """SELECT * FROM memory.record_observation_entailment_v5(
                        $1,$2,'accepted'::memory.observation_entailment_decision_v5,
                        'predicate_entailment_v5_1_accepted',$3::jsonb,$4,$5,$6
                    )""",
                    uuid.UUID(item["entailment_request_id"]),
                    uuid.UUID(item["observation_id"]),
                    item["source_spans_text"],
                    manifest["assessor_type"],
                    manifest["assessor_ref"],
                    item["entailment_authorization_manifest_sha256"],
                )
                staged = await conn.fetchrow(
                    "SELECT * FROM memory.stage_claim_projection_plan_v5_1($1,$2,$3)",
                    uuid.UUID(item["plan_id"]),
                    item["bundle"]["packet_text"],
                    item["owner_manifest_sha256"],
                )
                rows_written += entailment["rows_written"] + staged["rows_written"]
                outcomes.append(
                    {
                        "observation_id": item["observation_id"],
                        "plan_id": item["plan_id"],
                        "entailment_outcome": entailment["outcome"],
                        "projection_outcome": staged["outcome"],
                        "rows_written": (
                            entailment["rows_written"] + staged["rows_written"]
                        ),
                    }
                )
                await conn.execute("SET CONSTRAINTS ALL DEFERRED")
        if args.mode == "preflight":
            await transaction.rollback()
        else:
            expected_rows = manifest["expected_new_rows"] if args.mode == "apply" else 0
            if rows_written != expected_rows:
                raise StageBatchError(
                    f"batch wrote {rows_written} rows; expected {expected_rows}"
                )
            await transaction.commit()
    finally:
        await conn.close()
    result = {
        "contract_version": RESULT_CONTRACT,
        "mode": args.mode,
        "owner_user_id": manifest["owner_user_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "item_count": len(items),
        "rows_written": rows_written,
        "outcomes": outcomes,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "claims_written": 0,
        "retrieval_activated": False,
        "prompt_influence_activated": False,
    }
    result["result_sha256"] = sha256(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"mode={args.mode}")
    print(f"rows_written={rows_written}")
    print(f"result={output}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
