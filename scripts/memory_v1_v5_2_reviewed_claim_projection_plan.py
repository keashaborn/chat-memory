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

import asyncpg

from rag_engine.qdrant_compat import make_qdrant_client


CONTRACT = "memory_v1_v5_2_reviewed_claim_projection_plan_v1"
APPLY_CONTRACT = "memory_v1_v5_2_reviewed_claim_apply_result_v1"
MANIFEST_CONTRACT = "memory_v1_v5_2_reviewed_claim_apply_manifest_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_OWNER = uuid.UUID("557ea042-cb82-48f8-9429-472e96c957ef")
EXPECTED_CLAIMS = 19
ALLOWED_PREDICATES = {
    "age.reported",
    "health.user_reported_observation",
    "identity.name",
    "pet.breed",
    "pet.coat_color",
    "pet.eye_color",
    "pet.hearing_status",
    "pet.sex",
    "pet.weight_reported",
    "relationship.sibling_of",
}


class ProjectionPlanError(RuntimeError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def content_sha256(value: dict[str, Any], hash_field: str) -> str:
    return hashlib.sha256(
        stable_json({key: item for key, item in value.items() if key != hash_field}).encode()
    ).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-result", required=True)
    parser.add_argument("--apply-manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--collection", default="memory_claim_v1")
    return parser.parse_args()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ProjectionPlanError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ProjectionPlanError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ProjectionPlanError("input must be a private mode-0600 file")
    return path


def load_hashed(path: Path, contract: str, hash_field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if (
        value.get("contract_version") != contract
        or value.get(hash_field) != content_sha256(value, hash_field)
    ):
        raise ProjectionPlanError("source artifact contract or content hash mismatch")
    return value


async def set_actor(conn: asyncpg.Connection, actor: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(actor))


async def run() -> int:
    args = arguments()
    apply_path = private_path(args.apply_result)
    manifest_path = private_path(args.apply_manifest)
    output = private_path(args.output, output=True)
    apply = load_hashed(apply_path, APPLY_CONTRACT, "result_sha256")
    manifest = load_hashed(manifest_path, MANIFEST_CONTRACT, "manifest_sha256")
    outcomes = apply.get("outcomes")
    manifest_items = manifest.get("items")
    required_head = os.environ.get("MEMORY_V1_REQUIRED_HEAD", "").strip()
    if (
        apply.get("mode") != "apply"
        or apply.get("owner_user_id") != str(OWNER)
        or apply.get("manifest_sha256") != manifest.get("manifest_sha256")
        or apply.get("projection_outbox_rows_written") != 0
        or apply.get("qdrant_writes") != 0
        or manifest.get("owner_user_id") != str(OWNER)
        or not required_head
        or not isinstance(outcomes, list)
        or not isinstance(manifest_items, list)
        or len(outcomes) != EXPECTED_CLAIMS
        or len(manifest_items) != EXPECTED_CLAIMS
    ):
        raise ProjectionPlanError("source artifacts are outside the exact boundary")
    source_by_plan = {item.get("plan_id"): item for item in manifest_items}
    if len(source_by_plan) != EXPECTED_CLAIMS:
        raise ProjectionPlanError("source manifest plan set is not unique")

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    qdrant_url = os.environ.get("QDRANT_URL", "").strip()
    if not dsn or not qdrant_url:
        raise ProjectionPlanError("runtime configuration is missing")
    conn = await asyncpg.connect(dsn, command_timeout=60)
    qdrant = make_qdrant_client(url=qdrant_url, timeout=20.0)
    items: list[dict[str, Any]] = []
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ProjectionPlanError("POSTGRES_DSN must authenticate as brains_app")
        async with conn.transaction(readonly=True, isolation="serializable"):
            await set_actor(conn, OWNER)
            for outcome in outcomes:
                source = source_by_plan.get(outcome.get("plan_id"))
                try:
                    claim_id = uuid.UUID(str(outcome["claim_id"]))
                    plan_id = uuid.UUID(str(outcome["plan_id"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ProjectionPlanError("invalid claim or plan identity") from exc
                predicate = str(outcome.get("predicate") or "")
                canonical_hash = source.get("canonical_text_sha256") if source else None
                if (
                    source is None
                    or predicate not in ALLOWED_PREDICATES
                    or source.get("predicate") != predicate
                    or source.get("target_action") != "create"
                    or outcome.get("target_action") != "create"
                    or outcome.get("outcome") != "applied"
                    or outcome.get("claim_revision_number") != 2
                    or not isinstance(canonical_hash, str)
                    or len(canonical_hash) != 64
                ):
                    raise ProjectionPlanError("claim outcome and source manifest differ")
                row = await conn.fetchrow(
                    """
                    SELECT claim.claim_id,claim.predicate,claim.canonical_text,
                           claim.status::text,
                           COALESCE(max(revision.revision_number),0) AS revision_number
                    FROM memory.claim AS claim
                    LEFT JOIN memory.claim_revision AS revision
                      ON revision.owner_user_id=claim.owner_user_id
                     AND revision.claim_id=claim.claim_id
                    WHERE claim.owner_user_id=$1 AND claim.claim_id=$2
                    GROUP BY claim.claim_id
                    """,
                    OWNER,
                    claim_id,
                )
                outbox_count = await conn.fetchval(
                    """
                    SELECT count(*) FROM memory.projection_outbox
                    WHERE owner_user_id=$1 AND aggregate_type='claim'
                      AND aggregate_id=$2
                    """,
                    OWNER,
                    claim_id,
                )
                if (
                    row is None
                    or row["status"] != "supported"
                    or row["predicate"] != predicate
                    or row["revision_number"] != 2
                    or hashlib.sha256(row["canonical_text"].encode()).hexdigest()
                    != canonical_hash
                    or outbox_count != 0
                ):
                    raise ProjectionPlanError("live claim or outbox baseline differs")
                items.append(
                    {
                        "claim_id": str(claim_id),
                        "plan_id": str(plan_id),
                        "predicate": predicate,
                        "revision_number": 2,
                        "canonical_text_sha256": canonical_hash,
                        "prior_outbox": "absent",
                        "prior_qdrant": "absent",
                    }
                )

        points = qdrant.retrieve(
            collection_name=args.collection,
            ids=[item["claim_id"] for item in items],
            with_payload=True,
            with_vectors=False,
        )
        if points:
            raise ProjectionPlanError("one or more target Qdrant points already exist")

        async with conn.transaction(readonly=True, isolation="serializable"):
            await set_actor(conn, OTHER_OWNER)
            visible = await conn.fetchval(
                """
                SELECT count(*) FROM memory.claim
                WHERE owner_user_id=$1 AND claim_id=ANY($2::uuid[])
                """,
                OWNER,
                [uuid.UUID(item["claim_id"]) for item in items],
            )
        if visible != 0:
            raise ProjectionPlanError("cross-owner claim visibility is not zero")
    finally:
        await conn.close()
        qdrant.close()

    items = sorted(items, key=lambda item: item["claim_id"])
    if len(items) != EXPECTED_CLAIMS:
        raise ProjectionPlanError("exact claim count differs")
    plan = {
        "contract_version": CONTRACT,
        "required_ancestor_commit": required_head,
        "source_required_head_commit": manifest.get("required_head_commit"),
        "owner_user_id": str(OWNER),
        "other_owner_user_id": str(OTHER_OWNER),
        "source": {
            "apply_result_path": str(apply_path),
            "apply_result_file_sha256": file_sha256(apply_path),
            "apply_result_sha256": apply["result_sha256"],
            "apply_manifest_path": str(manifest_path),
            "apply_manifest_file_sha256": file_sha256(manifest_path),
            "apply_manifest_sha256": manifest["manifest_sha256"],
        },
        "projection": {
            "collection": args.collection,
            "embedding_model": "text-embedding-3-large",
            "vector_size": 3072,
            "maximum_embedding_requests": EXPECTED_CLAIMS,
            "automatic_http_retries": 0,
            "answer_generation": False,
            "prompt_influence": False,
        },
        "items": items,
    }
    plan["plan_sha256"] = content_sha256(plan, "plan_sha256")
    output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"claim_count={len(items)}")
    print(f"plan={output}")
    print(f"plan_sha256={plan['plan_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
