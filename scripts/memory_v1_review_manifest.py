#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _manifest(path: Path) -> tuple[dict[str, Any], str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("manifest root must be an object")
    digest = hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()
    return value, digest


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--expected-owner-user-id", required=True)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


async def _set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))


def _rows_by_id(rows: list[asyncpg.Record]) -> dict[str, asyncpg.Record]:
    return {str(row["candidate_id"]): row for row in rows}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


async def _preflight(
    conn: asyncpg.Connection,
    manifest: dict[str, Any],
    manifest_sha256: str,
    owner: uuid.UUID,
) -> dict[str, Any]:
    await _set_actor(conn, owner)
    claim_items = manifest.get("claim_candidates") or []
    preference_items = manifest.get("preference_candidates") or []
    project_items = manifest.get("project_candidates") or []
    expected_counts = manifest.get("expected_counts") or {
        "claim_candidates": 18,
        "preference_candidates": 1,
        "project_candidates": 0,
    }
    if set(expected_counts) != {
        "claim_candidates",
        "preference_candidates",
        "project_candidates",
    }:
        raise RuntimeError("expected_counts does not match the review contract")
    if project_items or expected_counts["project_candidates"] != 0:
        raise RuntimeError("this reviewed manifest must not contain project candidates")
    if (
        len(claim_items) != expected_counts["claim_candidates"]
        or len(preference_items) != expected_counts["preference_candidates"]
    ):
        raise RuntimeError("review candidate counts do not match expected_counts")
    if any(item.get("decision") not in {"approve", "reject"} for item in claim_items):
        raise RuntimeError("claim decisions must be approve or reject")
    if any(
        item.get("decision") not in {"accept", "rewrite", "reject", "defer", "split"}
        for item in preference_items
    ):
        raise RuntimeError("invalid preference review decision")

    claim_rows = await conn.fetch(
        """
        SELECT candidate_id, status::text, proposal_hash, extractor, extractor_version,
               comparison, review_reason
        FROM memory.candidate
        WHERE owner_user_id=$1 AND extractor=$2 AND extractor_version=$3
        ORDER BY candidate_id
        """,
        owner,
        manifest["extractor"],
        manifest["extractor_version"],
    )
    claim_by_id = _rows_by_id(claim_rows)
    manifest_claim_ids = {item["candidate_id"] for item in claim_items}
    if set(claim_by_id) != manifest_claim_ids:
        raise RuntimeError(
            "claim candidate set mismatch: "
            + _stable_json(
                {
                    "database_only": sorted(set(claim_by_id) - manifest_claim_ids),
                    "manifest_only": sorted(manifest_claim_ids - set(claim_by_id)),
                }
            )
        )

    status_changes = 0
    review_events = 0
    for item in claim_items:
        row = claim_by_id[item["candidate_id"]]
        if row["proposal_hash"] != item["candidate_hash"]:
            raise RuntimeError(f"claim hash mismatch: {item['candidate_id']}")
        expected_status = "approved" if item["decision"] == "approve" else "rejected"
        fulfilled = row["status"] == expected_status or (
            item["decision"] == "approve" and row["status"] == "applied"
        )
        if not fulfilled:
            if row["status"] not in {"proposed", "review_required", "approved", "rejected"}:
                raise RuntimeError(
                    f"claim cannot transition from {row['status']}: {item['candidate_id']}"
                )
            status_changes += 1
        exists = await conn.fetchval(
            """
            SELECT EXISTS(
              SELECT 1 FROM memory.governance_review_event
              WHERE owner_user_id=$1 AND manifest_sha256=$2 AND lane='claim'
                AND candidate_id=$3 AND candidate_hash=$4 AND decision=$5
            )
            """,
            owner,
            manifest_sha256,
            uuid.UUID(item["candidate_id"]),
            item["candidate_hash"],
            item["decision"],
        )
        if not exists:
            review_events += 1

    preference_rows = await conn.fetch(
        """
        SELECT candidate_id, candidate_hash, extractor, extractor_version
        FROM memory.preference_candidate
        WHERE owner_user_id=$1 AND extractor=$2 AND extractor_version=$3
        ORDER BY candidate_id
        """,
        owner,
        manifest["extractor"],
        manifest["extractor_version"],
    )
    preference_by_id = _rows_by_id(preference_rows)
    manifest_preference_ids = {item["candidate_id"] for item in preference_items}
    if set(preference_by_id) != manifest_preference_ids:
        raise RuntimeError("preference candidate set mismatch")

    specialized_reviews = 0
    for item in preference_items:
        row = preference_by_id[item["candidate_id"]]
        if row["candidate_hash"] != item["candidate_hash"]:
            raise RuntimeError(f"preference hash mismatch: {item['candidate_id']}")
        exists = await conn.fetchval(
            """
            SELECT EXISTS(
              SELECT 1 FROM memory.preference_candidate_review
              WHERE owner_user_id=$1 AND candidate_id=$2
                AND expected_candidate_hash=$3 AND decision=$4
                AND reviewer_ref=$5 AND rationale=$6
            )
            """,
            owner,
            uuid.UUID(item["candidate_id"]),
            item["candidate_hash"],
            item["decision"],
            manifest["reviewer_ref"],
            item["rationale"],
        )
        if not exists:
            specialized_reviews += 1
        event_exists = await conn.fetchval(
            """
            SELECT EXISTS(
              SELECT 1 FROM memory.governance_review_event
              WHERE owner_user_id=$1 AND manifest_sha256=$2 AND lane='preference'
                AND candidate_id=$3 AND candidate_hash=$4 AND decision=$5
            )
            """,
            owner,
            manifest_sha256,
            uuid.UUID(item["candidate_id"]),
            item["candidate_hash"],
            item["decision"],
        )
        if not event_exists:
            review_events += 1

    predicate_inserts = 0
    for item in manifest.get("predicate_registry") or []:
        row = await conn.fetchrow(
            """
            SELECT object_kind, cardinality, description
            FROM memory.predicate WHERE predicate=$1
            """,
            item["predicate"],
        )
        if row is None:
            predicate_inserts += 1
        elif row["object_kind"] != item["object_kind"] or row["cardinality"] != item["cardinality"]:
            raise RuntimeError(f"predicate registry conflict: {item['predicate']}")

    approved = sum(item["decision"] == "approve" for item in claim_items)
    rejected = sum(item["decision"] == "reject" for item in claim_items)
    preference_accepted = sum(
        item["decision"] == "accept" for item in preference_items
    )
    preference_rejected = sum(
        item["decision"] == "reject" for item in preference_items
    )
    return {
        "owner_user_id": str(owner),
        "manifest_sha256": manifest_sha256,
        "claim_candidates": len(claim_items),
        "claim_approved": approved,
        "claim_rejected": rejected,
        "preference_accepted": preference_accepted,
        "preference_rejected": preference_rejected,
        "predicate_inserts_required": predicate_inserts,
        "status_changes_required": status_changes,
        "specialized_reviews_required": specialized_reviews,
        "review_events_required": review_events,
        "writes_required": predicate_inserts + status_changes + specialized_reviews + review_events,
    }


async def _apply(
    conn: asyncpg.Connection,
    manifest: dict[str, Any],
    manifest_sha256: str,
    owner: uuid.UUID,
) -> dict[str, Any]:
    async with conn.transaction():
        preflight = await _preflight(conn, manifest, manifest_sha256, owner)
        await _set_actor(conn, owner)

        for item in manifest["claim_candidates"]:
            row = await conn.fetchrow(
                """
                SELECT status::text, comparison
                FROM memory.candidate
                WHERE owner_user_id=$1 AND candidate_id=$2 AND proposal_hash=$3
                FOR UPDATE
                """,
                owner,
                uuid.UUID(item["candidate_id"]),
                item["candidate_hash"],
            )
            if row is None:
                raise RuntimeError(f"claim disappeared: {item['candidate_id']}")
            target = "approved" if item["decision"] == "approve" else "rejected"
            fulfilled = row["status"] == target or (
                item["decision"] == "approve" and row["status"] == "applied"
            )
            if not fulfilled:
                comparison = _json_object(row["comparison"])
                comparison["review"] = {
                    "decision": target,
                    "reviewer_ref": manifest["reviewer_ref"],
                    "reason": item["rationale"],
                    "reason_codes": item["reason_codes"],
                    "manifest_sha256": manifest_sha256,
                }
                await conn.execute(
                    """
                    UPDATE memory.candidate
                    SET status=$4::memory.candidate_status,
                        comparison=$5::jsonb,
                        review_reason=$6,
                        updated_at=clock_timestamp()
                    WHERE owner_user_id=$1 AND candidate_id=$2 AND proposal_hash=$3
                    """,
                    owner,
                    uuid.UUID(item["candidate_id"]),
                    item["candidate_hash"],
                    target,
                    _stable_json(comparison),
                    item["rationale"],
                )
            await conn.execute(
                """
                INSERT INTO memory.governance_review_event(
                  owner_user_id, manifest_sha256, lane, candidate_id,
                  candidate_hash, decision, reviewer_ref, rationale,
                  reason_codes, metadata
                ) VALUES($1,$2,'claim',$3,$4,$5,$6,$7,$8,$9::jsonb)
                ON CONFLICT DO NOTHING
                """,
                owner,
                manifest_sha256,
                uuid.UUID(item["candidate_id"]),
                item["candidate_hash"],
                item["decision"],
                manifest["reviewer_ref"],
                item["rationale"],
                item["reason_codes"],
                _stable_json({"extractor_version": manifest["extractor_version"]}),
            )

        for item in manifest["preference_candidates"]:
            existing = await conn.fetchval(
                """
                SELECT review_id
                FROM memory.preference_candidate_review
                WHERE owner_user_id=$1 AND candidate_id=$2
                  AND expected_candidate_hash=$3 AND decision=$4
                  AND reviewer_ref=$5 AND rationale=$6
                ORDER BY reviewed_at DESC, review_id DESC
                LIMIT 1
                """,
                owner,
                uuid.UUID(item["candidate_id"]),
                item["candidate_hash"],
                item["decision"],
                manifest["reviewer_ref"],
                item["rationale"],
            )
            if existing is None:
                request_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"memory-v1-review:{manifest_sha256}:preference:{item['candidate_id']}",
                )
                await conn.fetchval(
                    """
                    SELECT memory.review_preference_candidate(
                      $1,$2,$3,$4,'admin',$5,$6,$7::text[],$8::uuid[],$9::jsonb
                    )
                    """,
                    uuid.UUID(item["candidate_id"]),
                    request_id,
                    item["candidate_hash"],
                    item["decision"],
                    manifest["reviewer_ref"],
                    item["rationale"],
                    item["reason_codes"],
                    [],
                    _stable_json({"manifest_sha256": manifest_sha256}),
                )
            await conn.execute(
                """
                INSERT INTO memory.governance_review_event(
                  owner_user_id, manifest_sha256, lane, candidate_id,
                  candidate_hash, decision, reviewer_ref, rationale,
                  reason_codes, metadata
                ) VALUES($1,$2,'preference',$3,$4,$5,$6,$7,$8,$9::jsonb)
                ON CONFLICT DO NOTHING
                """,
                owner,
                manifest_sha256,
                uuid.UUID(item["candidate_id"]),
                item["candidate_hash"],
                item["decision"],
                manifest["reviewer_ref"],
                item["rationale"],
                item["reason_codes"],
                _stable_json({"extractor_version": manifest["extractor_version"]}),
            )

        after = await _preflight(conn, manifest, manifest_sha256, owner)
        if after["writes_required"] != 0:
            raise RuntimeError("review apply did not converge to zero-write replay")
        return {**preflight, "replay_writes_required": 0}


async def _main() -> None:
    args = _args()
    manifest, manifest_sha256 = _manifest(args.manifest)
    owner = uuid.UUID(str(manifest["owner_user_id"]))
    expected_owner = uuid.UUID(args.expected_owner_user_id)
    if owner != expected_owner:
        raise RuntimeError("manifest owner does not match --expected-owner-user-id")
    if args.apply:
        if not args.expected_manifest_sha256:
            raise RuntimeError("--apply requires --expected-manifest-sha256")
        if args.expected_manifest_sha256 != manifest_sha256:
            raise RuntimeError("manifest SHA-256 does not match the authorized hash")

    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn)
    try:
        if args.apply:
            result = await _apply(conn, manifest, manifest_sha256, owner)
            mode = "applied"
        else:
            async with conn.transaction(readonly=True):
                result = await _preflight(conn, manifest, manifest_sha256, owner)
            mode = "dry_run"
    finally:
        await conn.close()
    print(_stable_json({"mode": mode, **result}))


if __name__ == "__main__":
    asyncio.run(_main())
