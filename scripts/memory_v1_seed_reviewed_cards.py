#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from collections import defaultdict
from typing import Any, Dict

import asyncpg

from rag_engine.memory_v1_seed import APPROVED_SEED_CARD_IDS, build_seed_mapping
from rag_engine.memory_v1_store import apply_candidate, propose_candidate, record_evidence


SENSITIVITY_RANK = {"low": 0, "medium": 1, "high": 2, "restricted": 3}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-owner", default="")
    parser.add_argument("--confirm-card-ids", default="")
    return parser.parse_args()


async def load_cards(conn: asyncpg.Connection) -> list[Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT card_id, vantage_id, kind, topic_key, status::text,
               strength, confidence, summary, payload
        FROM vantage_card.card_head
        WHERE card_id = ANY($1::bigint[])
        ORDER BY card_id
        """,
        list(APPROVED_SEED_CARD_IDS),
    )
    if [int(row["card_id"]) for row in rows] != list(APPROVED_SEED_CARD_IDS):
        raise RuntimeError("approved legacy seed card set is incomplete")
    return [dict(row) for row in rows]


async def load_sources(
    conn: asyncpg.Connection, source_ids: list[uuid.UUID]
) -> Dict[uuid.UUID, Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, user_id, source, text, thread_id, request_id, created_at
        FROM public.chat_log
        WHERE id = ANY($1::uuid[])
        """,
        source_ids,
    )
    return {uuid.UUID(str(row["id"])): dict(row) for row in rows}


def max_sensitivity(values: list[str]) -> str:
    return max(values, key=lambda value: SENSITIVITY_RANK[value])


async def main() -> int:
    args = parse_args()
    dsn = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    conn = await asyncpg.connect(dsn)
    try:
        cards = await load_cards(conn)
        mappings = [build_seed_mapping(card) for card in cards]
        owners = {mapping["owner_user_id"] for mapping in mappings}
        if len(owners) != 1:
            raise RuntimeError("reviewed seed cards do not share one owner")
        owner = uuid.UUID(next(iter(owners)))

        all_source_ids = sorted(
            {
                uuid.UUID(source_id)
                for mapping in mappings
                for source_id in mapping["source_ids"]
            },
            key=str,
        )
        sources = await load_sources(conn, all_source_ids)
        if set(sources) != set(all_source_ids):
            missing = sorted(str(value) for value in set(all_source_ids) - set(sources))
            raise RuntimeError(f"missing authoritative source rows: {missing}")

        for source in sources.values():
            if uuid.UUID(str(source["user_id"])) != owner:
                raise RuntimeError("source owner does not match card owner")
            if source["source"] != "frontend/chat:user":
                raise RuntimeError("source is not a user-authored chat row")

        source_sensitivities: dict[uuid.UUID, list[str]] = defaultdict(list)
        for mapping in mappings:
            sensitivity = mapping["proposal"]["sensitivity"]
            for source_id in mapping["source_ids"]:
                source_sensitivities[uuid.UUID(source_id)].append(sensitivity)

        print("=== memory V1 reviewed seed preview ===")
        print("mode:", "APPLY" if args.apply else "DRY_RUN")
        print("owner_user_id:", owner)
        print("card_count:", len(mappings))
        print("source_count:", len(sources))
        for mapping in mappings:
            policy = mapping["proposal"]["retrieval_policy"]
            print(
                json.dumps(
                    {
                        "card_id": mapping["card_id"],
                        "source_ids": mapping["source_ids"],
                        "predicate": mapping["proposal"]["predicate"],
                        "canonical_text": mapping["proposal"]["canonical_text"],
                        "domains": policy.get("domains"),
                        "intents": policy.get("intents"),
                        "surface": policy.get("surface"),
                        "sensitivity": mapping["proposal"]["sensitivity"],
                        "proposal_hash": mapping["proposal_hash"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )

        if not args.apply:
            print("dry_run_no_writes")
            return 0

        expected_ids = ",".join(str(value) for value in APPROVED_SEED_CARD_IDS)
        if args.confirm_owner != str(owner):
            raise SystemExit("REFUSING APPLY: --confirm-owner does not match")
        if args.confirm_card_ids != expected_ids:
            raise SystemExit(
                f"REFUSING APPLY: --confirm-card-ids must equal {expected_ids}"
            )

        applied: list[Dict[str, Any]] = []
        async with conn.transaction():
            for source_id, source in sources.items():
                await record_evidence(
                    conn,
                    owner,
                    kind="user_statement",
                    source_system="public.chat_log",
                    external_id=f"chat_log:{source_id}",
                    content=source["text"],
                    observed_at=source["created_at"],
                    directness=1.0,
                    independence_key=f"chat_log:{source_id}",
                    sensitivity=max_sensitivity(source_sensitivities[source_id]),
                    metadata={
                        "chat_log_id": str(source_id),
                        "thread_id": str(source["thread_id"])
                        if source["thread_id"]
                        else None,
                        "request_id": source["request_id"],
                    },
                )

            for mapping in mappings:
                for source_id_text in mapping["source_ids"]:
                    evidence_id = await conn.fetchval(
                        """
                        SELECT evidence_id
                        FROM memory.evidence
                        WHERE owner_user_id=$1
                          AND source_system='public.chat_log'
                          AND external_id=$2
                        """,
                        owner,
                        f"chat_log:{source_id_text}",
                    )
                    candidate = await propose_candidate(
                        conn,
                        owner,
                        evidence_id=evidence_id,
                        proposal=mapping["proposal"],
                        extractor="reviewed_seed_v1",
                        extractor_version="v1",
                        comparison={
                            "legacy_card_id": mapping["card_id"],
                            "review_status": "approved",
                        },
                        auto_approve=True,
                    )
                    if candidate["status"] == "applied":
                        applied.append(
                            {
                                "card_id": mapping["card_id"],
                                "claim_id": str(candidate["applied_claim_id"]),
                                "existing": True,
                            }
                        )
                        continue
                    result = await apply_candidate(
                        conn,
                        owner,
                        candidate_id=candidate["candidate_id"],
                        expected_proposal_hash=candidate["proposal_hash"],
                        actor_type="admin",
                        actor_ref="reviewed_seed_v1",
                    )
                    result["card_id"] = mapping["card_id"]
                    applied.append(result)

        print("applied_count:", len(applied))
        for result in applied:
            print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
