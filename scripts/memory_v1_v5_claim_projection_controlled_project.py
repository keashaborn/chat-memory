#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import socket
import stat
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from openai import OpenAI

from memory_v1_projection_v5_contract_test import sha256
from rag_engine.memory_v1_projection import (
    ClaimVectorIndex,
    projection_payload,
    render_claim_for_embedding,
)
from rag_engine.memory_v1_v5_shadow_loader import load_v5_shadow_claims
from rag_engine.memory_v1_v5_shadow_trace import build_v5_shadow_trace
from rag_engine.qdrant_compat import make_qdrant_client


CONTRACT = "memory_v1_claim_projection_controlled_project_result_v1"
APPLY_CONTRACT = "memory_v1_claim_projection_apply_batch_result_v1"
ADMISSION_CONTRACT = "memory_v1_v5_deferred_projection_admission_result_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER_OWNER = "557ea042-cb82-48f8-9429-472e96c957ef"
MAX_CLAIMS_PER_CONTROLLED_RUN = 4
QUERY_BY_PREDICATE = {
    "relationship.has_pet": "Do you remember my pet?",
    "relationship.parent_of": "Do you remember who my dad is?",
    "life_event.died": "Have I had any deaths in the family?",
    "identity.name": "What was my pet's name?",
    "pet.sex": "Was Dahlia female or male?",
    "pet.breed": "What breed was Dahlia?",
    "stance.reported": "What have I said about worrying about the future?",
    "relationship.caregiver_for": "Do you remember who I care for?",
    "relationship.spouse_of": "Do you remember who my spouse is?",
    "occupation.works_as": "Do you remember what professions I have worked in?",
}


class ControlledProjectionError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("apply", "replay"), default="apply")
    parser.add_argument("--apply-result", required=True)
    parser.add_argument("--admission-result")
    parser.add_argument("--output", required=True)
    parser.add_argument("--collection", default="memory_claim_v1")
    parser.add_argument("--vector-size", type=int, default=3072)
    return parser.parse_args()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ControlledProjectionError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ControlledProjectionError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ControlledProjectionError("input must be a private regular file")
    return path


def load_apply(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get("result_sha256") != sha256(
        {key: item for key, item in value.items() if key != "result_sha256"}
    ):
        raise ControlledProjectionError("apply result content hash mismatch")
    item_count = len(value.get("outcomes", [])) if isinstance(
        value.get("outcomes"), list
    ) else 0
    deferred = value.get("projection_outbox_deferred") is True
    expected_insert_rows = (11 if deferred else 12) * item_count
    expected_mutated_rows = (12 if deferred else 13) * item_count
    if (
        value.get("contract_version") != APPLY_CONTRACT
        or value.get("mode") != "apply"
        or value.get("owner_user_id") != OWNER
        or not 1 <= item_count <= MAX_CLAIMS_PER_CONTROLLED_RUN
        or value.get("insert_rows") != expected_insert_rows
        or value.get("mutated_rows") != expected_mutated_rows
    ):
        raise ControlledProjectionError("apply result is outside the bounded claim boundary")
    return value


def load_admission(
    path: Path, apply: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    value = json.loads(path.read_text())
    if value.get("result_sha256") != sha256(
        {key: item for key, item in value.items() if key != "result_sha256"}
    ):
        raise ControlledProjectionError("admission result content hash mismatch")
    outcomes = value.get("outcomes")
    apply_outcomes = apply["outcomes"]
    if (
        value.get("contract_version") != ADMISSION_CONTRACT
        or value.get("mode") != "apply"
        or value.get("owner_user_id") != OWNER
        or value.get("source_apply_result_sha256") != apply["result_sha256"]
        or value.get("rows_written") != len(apply_outcomes)
        or value.get("qdrant_writes") != 0
        or not isinstance(outcomes, list)
        or len(outcomes) != len(apply_outcomes)
    ):
        raise ControlledProjectionError(
            "admission result is outside the bounded claim boundary"
        )
    by_claim: dict[str, dict[str, Any]] = {}
    for item in outcomes:
        try:
            claim_id = str(uuid.UUID(item["claim_id"]))
            outbox_id = str(uuid.UUID(item["outbox_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ControlledProjectionError(
                "admission result has invalid identifiers"
            ) from exc
        if (
            claim_id in by_claim
            or item.get("status") != "pending"
            or item.get("predicate") not in QUERY_BY_PREDICATE
        ):
            raise ControlledProjectionError("admission result contains an invalid job")
        by_claim[claim_id] = {**item, "claim_id": claim_id, "outbox_id": outbox_id}
    if by_claim.keys() != {item["claim_id"] for item in apply_outcomes}:
        raise ControlledProjectionError("admission and apply claim sets differ")
    return by_claim


async def set_actor(conn: asyncpg.Connection, actor: str) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", actor)


async def claim_job(
    conn: asyncpg.Connection, item: dict[str, Any], worker_id: str
) -> dict[str, Any]:
    async with conn.transaction(isolation="serializable"):
        await set_actor(conn, OWNER)
        row = await conn.fetchrow(
            """UPDATE memory.projection_outbox SET
                 status='processing'::memory.outbox_status,
                 attempts=attempts+1,last_error=NULL,
                 lease_token=gen_random_uuid(),
                 lease_expires_at=clock_timestamp()+interval '10 minutes',
                 worker_id=$4,updated_at=clock_timestamp()
               WHERE owner_user_id=$1 AND outbox_id=$2
                 AND aggregate_type='claim' AND aggregate_id=$3
                 AND operation='upsert' AND status='pending' AND attempts=0
               RETURNING outbox_id,aggregate_id,payload,attempts,lease_token""",
            uuid.UUID(OWNER), uuid.UUID(item["outbox_id"]),
            uuid.UUID(item["claim_id"]), worker_id,
        )
        job_payload = row["payload"] if row else None
        if isinstance(job_payload, str):
            job_payload = json.loads(job_payload)
        if not row or row["attempts"] != 1 or job_payload != {
            "claim_id": item["claim_id"], "revision_number": 2
        }:
            raise ControlledProjectionError("exact projection outbox job is not pending")
        snapshot = await conn.fetchrow(
            """SELECT claim.claim_id,claim.canonical_text,claim.predicate,
                      claim.qualifiers,claim.status::text,claim.sensitivity::text,
                      claim.retrieval_policy,claim.updated_at,
                      COALESCE(max(revision.revision_number),0) AS revision_number
               FROM memory.claim AS claim
               LEFT JOIN memory.claim_revision AS revision
                 ON revision.owner_user_id=claim.owner_user_id
                AND revision.claim_id=claim.claim_id
               WHERE claim.owner_user_id=$1 AND claim.claim_id=$2
               GROUP BY claim.claim_id""",
            uuid.UUID(OWNER), uuid.UUID(item["claim_id"]),
        )
        if (
            not snapshot
            or snapshot["status"] != "supported"
            or snapshot["revision_number"] != 2
            or snapshot["predicate"] != item["predicate"]
            or hashlib.sha256(snapshot["canonical_text"].encode()).hexdigest()
            != item.get("canonical_text_sha256")
        ):
            raise ControlledProjectionError("supported claim snapshot mismatch")
        return {
            "outbox_id": str(row["outbox_id"]),
            "claim_id": str(row["aggregate_id"]),
            "lease_token": str(row["lease_token"]),
            "snapshot": dict(snapshot),
        }


async def finish_job(
    conn: asyncpg.Connection, job: dict[str, Any], *, error: str | None = None
) -> None:
    async with conn.transaction():
        await set_actor(conn, OWNER)
        if error is None:
            command = await conn.execute(
                """UPDATE memory.projection_outbox SET
                     status='done'::memory.outbox_status,last_error=NULL,
                     lease_token=NULL,lease_expires_at=NULL,worker_id=NULL,
                     updated_at=clock_timestamp()
                   WHERE owner_user_id=$1 AND outbox_id=$2
                     AND aggregate_id=$3 AND status='processing' AND lease_token=$4""",
                uuid.UUID(OWNER), uuid.UUID(job["outbox_id"]),
                uuid.UUID(job["claim_id"]), uuid.UUID(job["lease_token"]),
            )
        else:
            command = await conn.execute(
                """UPDATE memory.projection_outbox SET
                     status='error'::memory.outbox_status,attempts=8,
                     last_error=left($5,2000),available_at=clock_timestamp()+interval '100 years',
                     lease_token=NULL,lease_expires_at=NULL,worker_id=NULL,
                     updated_at=clock_timestamp()
                   WHERE owner_user_id=$1 AND outbox_id=$2
                     AND aggregate_id=$3 AND status='processing' AND lease_token=$4""",
                uuid.UUID(OWNER), uuid.UUID(job["outbox_id"]),
                uuid.UUID(job["claim_id"]), uuid.UUID(job["lease_token"]), error,
            )
        if command != "UPDATE 1":
            raise ControlledProjectionError("projection job lease completion failed")


async def freeze_remaining(conn: asyncpg.Connection, items: list[dict[str, Any]], reason: str) -> None:
    async with conn.transaction():
        await set_actor(conn, OWNER)
        ids = [uuid.UUID(item["outbox_id"]) for item in items]
        await conn.execute(
            """UPDATE memory.projection_outbox SET
                 status='error'::memory.outbox_status,attempts=8,
                 last_error=left($3,2000),available_at=clock_timestamp()+interval '100 years',
                 lease_token=NULL,lease_expires_at=NULL,worker_id=NULL,
                 updated_at=clock_timestamp()
               WHERE owner_user_id=$1 AND outbox_id=ANY($2::uuid[])
                 AND status IN ('pending','error')""",
            uuid.UUID(OWNER), ids, reason,
        )


async def load_replay_state(
    conn: asyncpg.Connection,
    qdrant: Any,
    *,
    collection: str,
    vector_size: int,
    items: list[dict[str, Any]],
) -> tuple[dict[str, list[float]], list[dict[str, Any]]]:
    points = qdrant.retrieve(
        collection_name=collection,
        ids=[item["claim_id"] for item in items],
        with_payload=True,
        with_vectors=True,
    )
    point_by_id = {str(point.id): point for point in points}
    if point_by_id.keys() != {item["claim_id"] for item in items}:
        raise ControlledProjectionError("replay target Qdrant point set differs")

    vectors: dict[str, list[float]] = {}
    completed: list[dict[str, Any]] = []
    async with conn.transaction(readonly=True, isolation="serializable"):
        await set_actor(conn, OWNER)
        for item in items:
            row = await conn.fetchrow(
                """SELECT claim.claim_id,claim.canonical_text,claim.predicate,
                          claim.qualifiers,claim.status::text,claim.sensitivity::text,
                          claim.retrieval_policy,claim.updated_at,
                          COALESCE(max(revision.revision_number),0) AS revision_number,
                          outbox.outbox_id,outbox.status::text AS outbox_status,
                          outbox.attempts,outbox.payload
                   FROM memory.claim AS claim
                   LEFT JOIN memory.claim_revision AS revision
                     ON revision.owner_user_id=claim.owner_user_id
                    AND revision.claim_id=claim.claim_id
                   JOIN memory.projection_outbox AS outbox
                     ON outbox.owner_user_id=claim.owner_user_id
                    AND outbox.aggregate_type='claim'
                    AND outbox.aggregate_id=claim.claim_id
                    AND outbox.operation='upsert'
                   WHERE claim.owner_user_id=$1 AND claim.claim_id=$2
                     AND outbox.outbox_id=$3
                   GROUP BY claim.claim_id,outbox.outbox_id""",
                uuid.UUID(OWNER),
                uuid.UUID(item["claim_id"]),
                uuid.UUID(item["outbox_id"]),
            )
            payload = row["payload"] if row else None
            if isinstance(payload, str):
                payload = json.loads(payload)
            if (
                not row
                or row["status"] != "supported"
                or row["revision_number"] != 2
                or row["predicate"] != item["predicate"]
                or hashlib.sha256(row["canonical_text"].encode()).hexdigest()
                != item["canonical_text_sha256"]
                or row["outbox_status"] != "done"
                or row["attempts"] != 1
                or payload
                != {"claim_id": item["claim_id"], "revision_number": 2}
            ):
                raise ControlledProjectionError(
                    "replay Postgres claim or outbox state differs"
                )

            point = point_by_id[item["claim_id"]]
            point_payload = point.payload or {}
            expected_payload = projection_payload(uuid.UUID(OWNER), dict(row))
            vector = point.vector
            if (
                any(
                    point_payload.get(key) != value
                    for key, value in expected_payload.items()
                )
                or not isinstance(vector, list)
                or len(vector) != vector_size
                or any(not math.isfinite(float(value)) for value in vector)
            ):
                raise ControlledProjectionError("replay Qdrant projection differs")
            vectors[item["claim_id"]] = [float(value) for value in vector]
            completed.append(
                {
                    "claim_id": item["claim_id"],
                    "predicate": item["predicate"],
                    "outbox_id": item["outbox_id"],
                    "revision_number": 2,
                    "embedding_input_sha256": hashlib.sha256(
                        render_claim_for_embedding(dict(row)).encode()
                    ).hexdigest(),
                    "replay_verified": True,
                }
            )
    return vectors, completed


async def shadow_tests(
    conn: asyncpg.Connection,
    index: ClaimVectorIndex,
    items: list[dict[str, Any]],
    vectors: dict[str, list[float]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in items:
        vector = vectors[item["claim_id"]]
        owner_hits = index.search_claims(OWNER, vector, limit=24)
        if item["claim_id"] not in {hit["claim_id"] for hit in owner_hits}:
            raise ControlledProjectionError("owner shadow candidate search missed projected claim")
        other_hits = index.search_claims(OTHER_OWNER, vector, limit=24)
        other_owner_target_present = item["claim_id"] in {
            hit["claim_id"] for hit in other_hits
        }
        if other_owner_target_present:
            raise ControlledProjectionError("cross-owner Qdrant search exposed projected claim")
        other_records = await load_v5_shadow_claims(conn, OTHER_OWNER, [item["claim_id"]])
        if other_records:
            raise ControlledProjectionError("cross-owner V5 reader exposed projected claim")
        trace = await build_v5_shadow_trace(
            conn, index, OWNER,
            query=QUERY_BY_PREDICATE[item["predicate"]],
            query_vector=vector,
            context={
                "eligible": True,
                "reason": "owner_only_controlled_shadow_canary",
                "intent": "specific_recall",
                "domain": "personal",
                "allowed_predicate_prefixes": [item["predicate"]],
                "explicit_recall": True,
            },
            request_id=None, thread_id=None,
            candidate_limit=24,max_claims=4,max_tokens=500,
            max_sensitivity="restricted",
        )
        if (
            trace["status"] != "ok"
            or trace["selected_count"] < 1
            or trace["prompt_injection"]
            or trace["answer_model_exposure"]
            or trace["retrieval_activation"]
            or trace["database_writes"] != 0
            or trace["qdrant_writes"] != 0
        ):
            raise ControlledProjectionError("owner shadow trace did not remain read-only")
        results.append(
            {
                "claim_id": item["claim_id"],
                "predicate": item["predicate"],
                "owner_candidate_count": len(owner_hits),
                "other_owner_candidate_count": len(other_hits),
                "other_owner_target_present": other_owner_target_present,
                "other_owner_database_record_count": len(other_records),
                "selected_count": trace["selected_count"],
                "candidate_set_sha256": trace["candidate_set_sha256"],
                "selection_set_sha256": trace["selection_set_sha256"],
                "prompt_influence": False,
            }
        )
    return results


async def run() -> int:
    args = arguments()
    apply_path = private_path(args.apply_result)
    admission_path = (
        private_path(args.admission_result) if args.admission_result else None
    )
    output = private_path(args.output, output=True)
    apply = load_apply(apply_path)
    items = sorted(apply["outcomes"], key=lambda item: item["claim_id"])
    if apply.get("projection_outbox_deferred") is True:
        if admission_path is None:
            raise ControlledProjectionError(
                "deferred projection requires an admission result"
            )
        admission = load_admission(admission_path, apply)
        for item in items:
            item["outbox_id"] = admission[item["claim_id"]]["outbox_id"]
    elif admission_path is not None:
        raise ControlledProjectionError(
            "admission result is accepted only for deferred projection"
        )
    for item in items:
        if item.get("claim_revision_number") != 2 or item.get("predicate") not in QUERY_BY_PREDICATE:
            raise ControlledProjectionError("apply outcome cannot be projected")
    # Canonical text hashes are recovered from the apply manifest, not from claim prose.
    manifest_sha = apply["manifest_sha256"]
    manifest_candidates = list(REVIEW_ROOT.rglob("*apply*manifest*.json"))
    manifests = []
    for candidate in manifest_candidates:
        if stat.S_IMODE(candidate.stat().st_mode) != 0o600:
            continue
        value = json.loads(candidate.read_text())
        if (
            value.get("contract_version")
            == "memory_v1_claim_projection_apply_batch_manifest_v1"
            and value.get("manifest_sha256") == manifest_sha
        ):
            manifests.append((candidate, value))
    if len(manifests) != 1:
        raise ControlledProjectionError("exact immutable apply manifest was not found")
    manifest_path, manifest = manifests[0]
    source_by_plan = {item["plan_id"]: item for item in manifest["items"]}
    for item in items:
        source = source_by_plan.get(item["plan_id"])
        if not source or source["predicate"] != item["predicate"]:
            raise ControlledProjectionError("apply outcome and manifest differ")
        item["canonical_text_sha256"] = source["canonical_text_sha256"]

    gate = (
        "MEMORY_V1_CONTROLLED_PROJECTION"
        if args.mode == "apply"
        else "MEMORY_V1_CONTROLLED_PROJECTION_REPLAY"
    )
    if os.environ.get(gate, "") != "authorized":
        raise ControlledProjectionError(
            f"controlled projection {args.mode} authorization gate is closed"
        )
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    qdrant_url = os.environ.get("QDRANT_URL", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not dsn or not qdrant_url or (args.mode == "apply" and not api_key):
        raise ControlledProjectionError("required runtime configuration is missing")
    if args.vector_size != 3072 or not 1 <= len(items) <= MAX_CLAIMS_PER_CONTROLLED_RUN:
        raise ControlledProjectionError("projection call or vector budget mismatch")
    model = os.environ.get("EMBED_MODEL", "text-embedding-3-large").strip()
    if model != "text-embedding-3-large":
        raise ControlledProjectionError("controlled projection requires text-embedding-3-large")

    qdrant = make_qdrant_client(url=qdrant_url, timeout=20.0)
    index = ClaimVectorIndex(qdrant, collection_name=args.collection, vector_size=args.vector_size)
    info = qdrant.get_collection(args.collection)
    if getattr(info.config.params.vectors, "size", None) != args.vector_size:
        raise ControlledProjectionError("Qdrant collection vector size mismatch")
    existing = qdrant.retrieve(
        collection_name=args.collection,
        ids=[item["claim_id"] for item in items],
        with_payload=True,with_vectors=False,
    )
    if args.mode == "apply" and existing:
        raise ControlledProjectionError("one or more target claim points already exist")
    if args.mode == "replay" and len(existing) != len(items):
        raise ControlledProjectionError("one or more replay target points are missing")

    conn = await asyncpg.connect(dsn, command_timeout=60)
    calls = 0
    writes = 0
    vectors: dict[str, list[float]] = {}
    completed: list[dict[str, Any]] = []
    try:
        if args.mode == "replay":
            vectors, completed = await load_replay_state(
                conn,
                qdrant,
                collection=args.collection,
                vector_size=args.vector_size,
                items=items,
            )
            shadows = await shadow_tests(conn, index, items, vectors)
        else:
            provider = OpenAI(
                api_key=api_key,
                base_url=os.environ.get("OPENAI_BASE_URL")
                or "https://api.openai.com/v1",
                max_retries=0,
                timeout=30.0,
            )
            worker_id = f"controlled-v5:{socket.gethostname()}:{os.getpid()}"
            active_job: dict[str, Any] | None = None
            try:
                for item in items:
                    active_job = await claim_job(conn, item, worker_id)
                    text = render_claim_for_embedding(active_job["snapshot"])
                    if calls >= len(items):
                        raise ControlledProjectionError(
                            "embedding request budget exhausted"
                        )
                    calls += 1
                    response = provider.embeddings.create(model=model, input=text)
                    vector = [
                        float(value) for value in response.data[0].embedding
                    ]
                    if len(vector) != args.vector_size or any(
                        not math.isfinite(value) for value in vector
                    ):
                        raise ControlledProjectionError(
                            "provider returned an invalid embedding vector"
                        )
                    index.upsert_claim(OWNER, active_job["snapshot"], vector)
                    writes += 1
                    await finish_job(conn, active_job)
                    vectors[item["claim_id"]] = vector
                    completed.append(
                        {
                            "claim_id": item["claim_id"],
                            "predicate": item["predicate"],
                            "outbox_id": item["outbox_id"],
                            "revision_number": 2,
                            "embedding_input_sha256": hashlib.sha256(
                                text.encode()
                            ).hexdigest(),
                        }
                    )
                    active_job = None
                if calls != len(items) or writes != len(items):
                    raise ControlledProjectionError(
                        "controlled projection did not consume exact budget"
                    )
                shadows = await shadow_tests(conn, index, items, vectors)
            except Exception as exc:
                reason = f"controlled projection stopped: {type(exc).__name__}"
                if active_job is not None:
                    try:
                        await finish_job(conn, active_job, error=reason)
                    except Exception:
                        pass
                await freeze_remaining(conn, items, reason)
                raise
    finally:
        await conn.close()
        qdrant.close()

    result = {
        "contract_version": CONTRACT,
        "mode": args.mode,
        "owner_user_id": OWNER,
        "apply_result_file_sha256": hashlib.sha256(apply_path.read_bytes()).hexdigest(),
        "apply_result_sha256": apply["result_sha256"],
        "admission_result_file_sha256": (
            hashlib.sha256(admission_path.read_bytes()).hexdigest()
            if admission_path is not None else None
        ),
        "admission_result_sha256": (
            json.loads(admission_path.read_text())["result_sha256"]
            if admission_path is not None else None
        ),
        "apply_manifest_file_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "apply_manifest_sha256": manifest_sha,
        "collection": args.collection,
        "embedding_model": model,
        "embedding_requests": calls,
        "automatic_http_retries": 0,
        "qdrant_writes": writes,
        "completed": completed,
        "shadow_tests": shadows,
        "database_claim_writes": 0,
        "prompt_influence_activated": False,
        "general_account_activation": False,
    }
    result["result_sha256"] = sha256(result)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"embedding_requests={calls}")
    print(f"qdrant_writes={writes}")
    print(f"shadow_tests={len(shadows)}")
    print(f"result={output}")
    print(f"result_sha256={result['result_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
