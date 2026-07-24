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

from memory_v1_projection_v5_contract_test import sha256


CONTRACT = "memory_v1_v5_deferred_projection_admission_result_v1"
APPLY_CONTRACT = "memory_v1_claim_projection_apply_batch_result_v1"
MANIFEST_CONTRACT = "memory_v1_claim_projection_apply_batch_manifest_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
MAX_CLAIMS = 4


class DeferredProjectionAdmissionError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "apply", "replay"), required=True)
    parser.add_argument("--apply-result", required=True)
    parser.add_argument("--apply-manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--prior-result")
    return parser.parse_args()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise DeferredProjectionAdmissionError(
            "artifact is outside the private review root"
        )
    if output:
        if path.exists():
            raise DeferredProjectionAdmissionError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise DeferredProjectionAdmissionError(
            "input must be a private mode-0600 file"
        )
    return path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_hashed(path: Path, contract: str, hash_field: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if value.get("contract_version") != contract or value.get(hash_field) != sha256(
        {key: item for key, item in value.items() if key != hash_field}
    ):
        raise DeferredProjectionAdmissionError(
            "source artifact contract or content hash mismatch"
        )
    return value


def validate_sources(
    apply_path: Path, manifest_path: Path
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    apply = load_hashed(apply_path, APPLY_CONTRACT, "result_sha256")
    manifest = load_hashed(manifest_path, MANIFEST_CONTRACT, "manifest_sha256")
    outcomes = apply.get("outcomes")
    items = manifest.get("items")
    if (
        apply.get("mode") != "apply"
        or apply.get("projection_outbox_deferred") is not True
        or apply.get("qdrant_writes") != 0
        or not isinstance(outcomes, list)
        or not 1 <= len(outcomes) <= MAX_CLAIMS
        or manifest.get("defer_projection_outbox") is not True
        or not isinstance(items, list)
        or len(items) != len(outcomes)
        or apply.get("owner_user_id") != manifest.get("owner_user_id")
        or apply.get("manifest_sha256") != manifest.get("manifest_sha256")
        or manifest.get("expected_table_rows", {}).get("projection_outbox") != 0
    ):
        raise DeferredProjectionAdmissionError(
            "source artifacts are outside the deferred projection boundary"
        )
    by_plan = {item.get("plan_id"): item for item in items}
    if len(by_plan) != len(items):
        raise DeferredProjectionAdmissionError("projection plan set is not unique")
    normalized: list[dict[str, Any]] = []
    for outcome in outcomes:
        item = by_plan.get(outcome.get("plan_id"))
        if (
            item is None
            or outcome.get("outcome") != "applied"
            or outcome.get("claim_revision_number") != 2
            or outcome.get("outbox_id") is not None
            or outcome.get("predicate") != item.get("predicate")
            or not isinstance(item.get("canonical_text_sha256"), str)
            or len(item["canonical_text_sha256"]) != 64
        ):
            raise DeferredProjectionAdmissionError(
                "deferred claim outcome and manifest differ"
            )
        normalized.append(
            {
                "plan_id": str(uuid.UUID(outcome["plan_id"])),
                "claim_id": str(uuid.UUID(outcome["claim_id"])),
                "predicate": outcome["predicate"],
                "canonical_text_sha256": item["canonical_text_sha256"],
            }
        )
    return apply, manifest, sorted(normalized, key=lambda item: item["claim_id"])


def validate_prior(
    path: Path,
    *,
    owner: str,
    apply_sha: str,
    manifest_sha: str,
    claims: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    prior = load_hashed(path, CONTRACT, "result_sha256")
    outcomes = prior.get("outcomes")
    if (
        prior.get("mode") != "apply"
        or prior.get("owner_user_id") != owner
        or prior.get("source_apply_result_sha256") != apply_sha
        or prior.get("source_apply_manifest_sha256") != manifest_sha
        or prior.get("rows_written") != len(claims)
        or not isinstance(outcomes, list)
        or len(outcomes) != len(claims)
    ):
        raise DeferredProjectionAdmissionError("prior admission result does not match")
    by_claim = {item.get("claim_id"): item for item in outcomes}
    if by_claim.keys() != {item["claim_id"] for item in claims}:
        raise DeferredProjectionAdmissionError("prior admission claim set differs")
    return by_claim


async def run() -> int:
    import asyncpg

    args = arguments()
    apply_path = private_path(args.apply_result)
    manifest_path = private_path(args.apply_manifest)
    output = private_path(args.output, output=True)
    apply, manifest, claims = validate_sources(apply_path, manifest_path)
    owner = str(uuid.UUID(apply["owner_user_id"]))
    required_head = os.environ.get("MEMORY_V1_REQUIRED_HEAD", "")
    if required_head != manifest.get("required_head_commit"):
        raise DeferredProjectionAdmissionError("runtime head does not match manifest")
    if args.mode == "replay":
        if not args.prior_result:
            raise DeferredProjectionAdmissionError("replay requires prior result")
        prior = validate_prior(
            private_path(args.prior_result),
            owner=owner,
            apply_sha=apply["result_sha256"],
            manifest_sha=manifest["manifest_sha256"],
            claims=claims,
        )
    elif args.prior_result:
        raise DeferredProjectionAdmissionError(
            "prior result is accepted only in replay mode"
        )
    else:
        prior = {}
    if args.mode == "apply" and os.environ.get(
        "MEMORY_V1_DEFERRED_PROJECTION_ADMISSION", ""
    ) != "authorized":
        raise DeferredProjectionAdmissionError("admission authorization gate is closed")
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise DeferredProjectionAdmissionError("POSTGRES_DSN is required")

    connection = await asyncpg.connect(dsn, command_timeout=60)
    rows_written = 0
    outcomes: list[dict[str, Any]] = []
    try:
        if await connection.fetchval("SELECT session_user") != "brains_app":
            raise DeferredProjectionAdmissionError(
                "POSTGRES_DSN must authenticate as brains_app"
            )
        transaction = connection.transaction(
            readonly=args.mode == "preflight", isolation="serializable"
        )
        await transaction.start()
        await connection.execute("SELECT set_config('app.user_id',$1,true)", owner)
        for item in claims:
            claim = await connection.fetchrow(
                """SELECT claim.claim_id,claim.predicate,claim.canonical_text,
                          claim.status::text,
                          COALESCE(max(revision.revision_number),0) AS revision_number
                   FROM memory.claim AS claim
                   LEFT JOIN memory.claim_revision AS revision
                     ON revision.owner_user_id=claim.owner_user_id
                    AND revision.claim_id=claim.claim_id
                   WHERE claim.owner_user_id=$1 AND claim.claim_id=$2
                   GROUP BY claim.claim_id""",
                uuid.UUID(owner),
                uuid.UUID(item["claim_id"]),
            )
            if (
                claim is None
                or claim["status"] != "supported"
                or claim["revision_number"] != 2
                or claim["predicate"] != item["predicate"]
                or hashlib.sha256(claim["canonical_text"].encode()).hexdigest()
                != item["canonical_text_sha256"]
            ):
                raise DeferredProjectionAdmissionError(
                    "supported claim snapshot differs from immutable manifest"
                )
            payload = {
                "claim_id": item["claim_id"],
                "revision_number": 2,
            }
            existing = await connection.fetchrow(
                """SELECT outbox_id,payload,status::text,attempts,
                          available_at='infinity'::timestamptz AS held
                   FROM memory.projection_outbox
                   WHERE owner_user_id=$1 AND aggregate_type='claim'
                     AND aggregate_id=$2 AND operation='upsert'""",
                uuid.UUID(owner),
                uuid.UUID(item["claim_id"]),
            )
            if args.mode in {"preflight", "apply"} and existing is not None:
                raise DeferredProjectionAdmissionError(
                    "claim already has a projection outbox row"
                )
            if args.mode == "apply":
                inserted = await connection.fetchrow(
                    """INSERT INTO memory.projection_outbox(
                         owner_user_id,aggregate_type,aggregate_id,operation,
                         payload,available_at
                       ) VALUES(
                         $1,'claim',$2,'upsert',$3::jsonb,'infinity'::timestamptz
                       )
                       RETURNING outbox_id,status::text,attempts,
                         available_at='infinity'::timestamptz AS held""",
                    uuid.UUID(owner),
                    uuid.UUID(item["claim_id"]),
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                )
                if (
                    inserted is None
                    or inserted["status"] != "pending"
                    or inserted["attempts"] != 0
                    or inserted["held"] is not True
                ):
                    raise DeferredProjectionAdmissionError(
                        "projection outbox admission did not create a pending job"
                    )
                outbox_id = str(inserted["outbox_id"])
                rows_written += 1
            elif args.mode == "replay":
                expected = prior[item["claim_id"]]
                existing_payload = existing["payload"] if existing else None
                if isinstance(existing_payload, str):
                    existing_payload = json.loads(existing_payload)
                if (
                    existing is None
                    or str(existing["outbox_id"]) != expected.get("outbox_id")
                    or existing_payload != payload
                    or existing["status"] != "pending"
                    or existing["attempts"] != 0
                    or existing["held"] is not True
                ):
                    raise DeferredProjectionAdmissionError(
                        "projection outbox replay state differs"
                    )
                outbox_id = str(existing["outbox_id"])
            else:
                outbox_id = None
            outcomes.append(
                {
                    **item,
                    "outbox_id": outbox_id,
                    "status": "pending" if outbox_id else "eligible",
                }
            )
        if args.mode == "preflight":
            await transaction.rollback()
        else:
            expected_rows = len(claims) if args.mode == "apply" else 0
            if rows_written != expected_rows:
                raise DeferredProjectionAdmissionError(
                    "projection outbox row budget mismatch"
                )
            await transaction.commit()
    finally:
        await connection.close()

    result = {
        "contract_version": CONTRACT,
        "mode": args.mode,
        "owner_user_id": owner,
        "source_apply_result_file_sha256": file_sha256(apply_path),
        "source_apply_result_sha256": apply["result_sha256"],
        "source_apply_manifest_file_sha256": file_sha256(manifest_path),
        "source_apply_manifest_sha256": manifest["manifest_sha256"],
        "item_count": len(claims),
        "rows_written": rows_written,
        "outcomes": outcomes,
        "qdrant_writes": 0,
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
