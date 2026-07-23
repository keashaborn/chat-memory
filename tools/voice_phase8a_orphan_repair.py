#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import asyncpg

from rag_engine.qdrant_compat import make_qdrant_client
from rag_engine.thread_orphan_repair_v1 import (
    OrphanThreadIdentityV1,
    OrphanThreadRepairCountsV1,
    discover_orphan_thread_identities_v1,
    repair_orphan_thread_v1,
)


APPLY_GATE = "VOICE_PHASE8A_ORPHAN_REPAIR_APPLY"


def _load_manifest(path: str) -> tuple[OrphanThreadIdentityV1, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("contract_version") != "thread_orphan_repair_manifest_v1":
        raise RuntimeError("invalid orphan repair manifest contract")
    identities = []
    for item in payload.get("identities", []):
        identities.append(
            OrphanThreadIdentityV1(
                owner_user_id=uuid.UUID(str(item["owner_user_id"])),
                thread_id=uuid.UUID(str(item["thread_id"])),
            )
        )
    if len(set(identities)) != len(identities):
        raise RuntimeError("duplicate orphan repair identity")
    return tuple(sorted(identities, key=lambda item: (item.owner_user_id, item.thread_id)))


async def _inventory_for_identities(
    conn: Any,
    identities: tuple[OrphanThreadIdentityV1, ...],
) -> dict[str, int]:
    totals = {
        "attestations": 0,
        "answer_bindings": 0,
        "retrieval_traces": 0,
        "telemetry_events": 0,
        "active_evidence": 0,
    }
    for identity in identities:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true)",
                str(identity.owner_user_id),
            )
            row = await conn.fetchrow(
                """
                SELECT
                  (SELECT count(*)
                     FROM memory.assistant_transcript_attestation_v1
                    WHERE owner_user_id=$1 AND thread_id=$2) AS attestations,
                  (SELECT count(*)
                     FROM memory.final_answer_memory_binding_v1
                    WHERE owner_user_id=$1 AND thread_id=$2) AS answer_bindings,
                  (SELECT count(*)
                     FROM memory.retrieval_trace
                    WHERE owner_user_id=$1 AND thread_id=$2) AS retrieval_traces,
                  (SELECT count(*)
                     FROM public.telemetry_event
                    WHERE actor_user_id=$3 AND thread_id=$4) AS telemetry_events,
                  (SELECT count(*)
                     FROM memory.evidence
                    WHERE owner_user_id=$1
                      AND source_system='public.chat_log'
                      AND metadata->>'thread_id'=$4
                      AND status='active') AS active_evidence
                """,
                identity.owner_user_id,
                identity.thread_id,
                str(identity.owner_user_id),
                str(identity.thread_id),
            )
        if row is None:
            raise RuntimeError("orphan identity inventory returned no row")
        for key in totals:
            totals[key] += int(row[key])
    return totals


async def _run(
    *,
    apply: bool,
    manifest_path: str | None,
    discover: bool,
) -> dict[str, Any]:
    dsn = (os.getenv("POSTGRES_DSN") or "").strip()
    qdrant_url = (os.getenv("QDRANT_URL") or "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    if apply and not qdrant_url:
        raise RuntimeError("QDRANT_URL is required for apply")
    if apply and os.getenv(APPLY_GATE) != "authorized":
        raise RuntimeError(f"{APPLY_GATE}=authorized is required for apply")

    conn = await asyncpg.connect(dsn, command_timeout=90)
    try:
        if manifest_path:
            identities = _load_manifest(manifest_path)
        elif discover:
            identities = await discover_orphan_thread_identities_v1(conn)
        else:
            raise RuntimeError("--manifest or --discover is required")
        before = await _inventory_for_identities(conn, identities)
        result: dict[str, Any] = {
            "contract_version": "thread_orphan_repair_v1",
            "mode": "apply" if apply else "report",
            "orphan_thread_identities": len(identities),
            "before": before,
        }
        if not apply:
            return result

        qdrant = make_qdrant_client(url=qdrant_url, timeout=60)
        totals = OrphanThreadRepairCountsV1()
        for identity in identities:
            repaired = await repair_orphan_thread_v1(
                conn,
                qdrant,
                owner_user_id=identity.owner_user_id,
                thread_id=identity.thread_id,
            )
            totals = totals.plus(repaired)
        after = await _inventory_for_identities(conn, identities)
        if any(after.values()):
            raise RuntimeError("orphan repair postcondition failed")
        result["repaired"] = totals.as_dict()
        result["after"] = after
        result["qdrant_verified"] = True
        result["retained_audit_tombstones"] = (
            totals.governed_evidence_tombstoned > 0
        )
        return result
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report or repair deleted-thread voice lifecycle orphans."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=f"repair after requiring {APPLY_GATE}=authorized",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--manifest",
        help="0600 JSON manifest generated by a privileged read-only inventory",
    )
    source.add_argument(
        "--discover",
        action="store_true",
        help="discover identities using the current database role",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                _run(
                    apply=args.apply,
                    manifest_path=args.manifest,
                    discover=args.discover,
                )
            ),
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
