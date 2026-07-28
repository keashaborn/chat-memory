#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import hashlib
import json
import os
import uuid

import asyncpg


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--candidate-limit", type=int, default=3)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict[str, object]:
    owner = uuid.UUID(args.owner_user_id)
    if not 1 <= args.candidate_limit <= 5:
        raise ValueError("candidate-limit must be between 1 and 5")
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    rows: list[dict[str, object]] = []
    try:
        async with conn.transaction(readonly=True):
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(owner),
            )
            before_created_at = None
            before_packet_id = None
            while True:
                page = await conn.fetch(
                    """
                    SELECT *
                      FROM memory.list_owner_memory_workbench_v2(
                        'reviewed',25,$1,$2
                      )
                    """,
                    before_created_at,
                    before_packet_id,
                )
                if not page:
                    break
                rows.extend(dict(row) for row in page)
                before_created_at = page[-1]["packet_created_at"]
                before_packet_id = page[-1]["packet_id"]
                if len(page) < 25:
                    break
    finally:
        await conn.close()

    decisions = Counter(str(row.get("feedback_decision")) for row in rows)
    categories = Counter(
        str(row.get("feedback_category") or "uncategorized")
        for row in rows
    )
    routes = Counter(str(row.get("route") or "unrouted") for row in rows)
    selected: dict[str, list[dict[str, object]]] = defaultdict(list)
    structural_counts: Counter[str] = Counter()
    content_hash_counts: Counter[str] = Counter()
    for row in rows:
        packet = row.get("normalized_packet")
        if isinstance(packet, str):
            packet = json.loads(packet)
        if not isinstance(packet, dict):
            packet = {}
        source = str(row.get("source_content") or "")
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        content_hash_counts[source_hash] += 1
        if row.get("feedback_decision") != "not_correct":
            continue
        observation_count = len(packet.get("observations") or [])
        if len(source) >= 800 and observation_count == 0:
            bucket = "long_no_observation"
        elif len(source) >= 800:
            bucket = "long_partial_observation"
        elif len(source) <= 80:
            bucket = "short_context_fragment"
        else:
            bucket = "medium_missed_or_partial"
        structural_counts[bucket] += 1
        if len(selected[bucket]) < args.candidate_limit:
            selected[bucket].append(
                {
                    "packet_id": str(row["packet_id"]),
                    "packet_storage_sha256": str(
                        row["packet_storage_sha256"]
                    ),
                    "job_id": str(row["job_id"]),
                    "evidence_id": str(row["evidence_id"]),
                    "source_content_sha256": source_hash,
                    "source_char_count": len(source),
                    "source_context_char_count": len(
                        str(row.get("source_context_content") or "")
                    ),
                    "source_char_start": row.get("source_char_start"),
                    "source_char_end": row.get("source_char_end"),
                    "entity_count": len(
                        packet.get("entity_mentions") or []
                    ),
                    "observation_count": observation_count,
                    "deferral_count": len(
                        packet.get("deferrals") or []
                    ),
                    "route": str(row.get("route") or "unrouted"),
                    "route_reason_code": str(
                        row.get("route_reason_code") or ""
                    ),
                }
            )
    return {
        "owner_user_id_sha256": hashlib.sha256(
            str(owner).encode()
        ).hexdigest(),
        "reviewed_count": len(rows),
        "decision_counts": dict(sorted(decisions.items())),
        "category_counts": dict(sorted(categories.items())),
        "route_counts": dict(sorted(routes.items())),
        "not_correct_structural_counts": dict(
            sorted(structural_counts.items())
        ),
        "duplicate_content_group_count": sum(
            count > 1 for count in content_hash_counts.values()
        ),
        "duplicate_packet_count": sum(
            count for count in content_hash_counts.values() if count > 1
        ),
        "candidate_limit_per_category": args.candidate_limit,
        "candidates": dict(sorted(selected.items())),
        "source_content_retained": False,
        "feedback_notes_retained": False,
        "writes": 0,
    }


def main() -> int:
    print(
        json.dumps(
            asyncio.run(run(arguments())),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
