#!/usr/bin/env python3
"""Dry-run or finalize one hash-locked owner-scoped V5.2 review deferral."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

import asyncpg

from scripts.memory_v1_relational_extraction_v5_provider import canonical_sha256
from scripts.memory_v1_v5_build_local_review_deferral import CONTRACT, REASON, decision_body


APPLY_TOKEN = "memory_v1_v5_2_local_review_deferral_apply_v1"
IDENTITY_NAMESPACE = uuid.UUID("84ea62ce-3075-52ec-8ac0-f65e86e95762")
TOP_KEYS = {
    "contract_version", "decision_sha256", "owner_user_id", "packet_id",
    "packet_storage_sha256", "evidence_content_sha256",
    "validator_packet_sha256", "review_decision", "reason_code",
    "promotion_eligible", "reviewer_type", "reviewer_ref",
    "source_prose_included", "external_model_calls", "database_writes",
    "qdrant_writes", "prompt_influence",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--packet-id", required=True)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def load_decision(path_value: str) -> tuple[dict, str]:
    path = Path(path_value).resolve(strict=True)
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("review decision must be a mode-0600 regular file")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != TOP_KEYS:
        raise RuntimeError("review decision fields differ from contract")
    if (
        value["contract_version"] != CONTRACT
        or value["review_decision"] != "deferred"
        or value["reason_code"] != REASON
        or value["promotion_eligible"] is not False
        or value["reviewer_type"] != "owner_authorized_operator"
        or value["source_prose_included"] is not False
        or any(value[key] != 0 for key in (
            "external_model_calls", "database_writes", "qdrant_writes",
            "prompt_influence",
        ))
        or value["decision_sha256"] != canonical_sha256(decision_body(value))
    ):
        raise RuntimeError("review decision failed validation")
    return value, hashlib.sha256(raw).hexdigest()


def operation_ids(owner: uuid.UUID, packet: uuid.UUID, decision_sha256: str) -> tuple[uuid.UUID, uuid.UUID]:
    identity = f"{owner}|{packet}|{decision_sha256}"
    return (
        uuid.uuid5(IDENTITY_NAMESPACE, f"operation|{identity}"),
        uuid.uuid5(IDENTITY_NAMESPACE, f"disposition|{identity}"),
    )


async def main() -> int:
    args = arguments()
    owner = uuid.UUID(args.owner_user_id)
    packet_id = uuid.UUID(args.packet_id)
    decision, file_sha256 = load_decision(args.decision)
    if decision["owner_user_id"] != str(owner) or decision["packet_id"] != str(packet_id):
        raise RuntimeError("review decision owner or packet differs")
    if args.apply and os.getenv("MEMORY_V1_V5_2_REVIEW_DEFERRAL_APPLY") != APPLY_TOKEN:
        raise RuntimeError("review deferral apply capability is absent")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("review deferral requires brains_app session")
        async with conn.transaction(readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
            rows = await conn.fetch(
                "SELECT * FROM memory.read_owner_v5_local_packet_review_v1($1)",
                packet_id,
            )
        if len(rows) != 1:
            raise RuntimeError("owner-scoped packet is absent")
        row = dict(rows[0])
        for field in (
            "packet_storage_sha256", "evidence_content_sha256",
            "validator_packet_sha256",
        ):
            if decision[field] != row[field]:
                raise RuntimeError(f"review decision {field} differs")
        if row["exact_stage_batch_count"] or row["evidence_stage_batch_count"]:
            raise RuntimeError("packet evidence already entered staging")
        writes = 0
        replay_proved = False
        if args.apply:
            operation_id, disposition_id = operation_ids(
                owner, packet_id, decision["decision_sha256"]
            )

            async def invoke() -> dict:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))
                    value = await conn.fetchrow(
                        """SELECT * FROM memory.finalize_owner_v5_2_review_deferral_v1(
                          $1,$2,$3,$4,$5,$6
                        )""",
                        operation_id, disposition_id, packet_id,
                        decision["packet_storage_sha256"], REASON, file_sha256,
                    )
                if value is None:
                    raise RuntimeError("review deferral returned no row")
                return dict(value)

            applied = await invoke()
            replayed = await invoke()
            if (
                applied["apply_outcome"] not in {"applied", "replayed"}
                or replayed["apply_outcome"] != "replayed"
                or applied["review_decision"] != "deferred"
                or applied["reason_code"] != REASON
                or applied["promotion_eligible"] is not False
            ):
                raise RuntimeError("review deferral replay proof failed")
            writes = int(applied["apply_outcome"] == "applied")
            replay_proved = True
        print(json.dumps({
            "contract_version": CONTRACT,
            "apply": args.apply,
            "outcome": "deferred" if args.apply else "eligible",
            "owner_user_id_sha256": canonical_sha256(str(owner)),
            "packet_id_sha256": canonical_sha256(str(packet_id)),
            "reason_code": REASON,
            "promotion_eligible": False,
            "write_counts": {
                "dispositions": writes, "staging": 0, "claims": 0,
                "qdrant": 0, "prompt_influence": 0,
            },
            "zero_write_replay_proved": replay_proved,
            "external_model_calls": 0,
        }, sort_keys=True, separators=(",", ":")))
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
