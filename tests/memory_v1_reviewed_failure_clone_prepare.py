#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

import asyncpg

from rag_engine.memory_v1_contextual_span_splitter_v2 import (
    SPLITTER_VERSION,
    contextual_span_plan_v2,
)


OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
PARENTS = (
    (
        uuid.UUID("78fc7204-d211-4206-8570-9d3efacd6654"),
        "44bace4a00083fa6d68b27965487f1f5237028b6e02dab7d6d6235d3096e38e6",
        "long_dog_history",
    ),
    (
        uuid.UUID("8f986090-19db-45a4-96ca-c0ec3ec5c6c0"),
        "370c4069be5387dcf2916510aaf2c5537f326c739f35d2c69d75c2e02e8288cf",
        "name_and_question",
    ),
)
EXISTING = (
    uuid.UUID("5480aacd-e5f3-5060-8c6a-95ef0e46c9fe"),
    "8e4cfbd41156ccc92fd4392372a220d3e5802597c6d5756f8d48748503d0166b",
    "context_fragment",
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", required=True)
    return parser.parse_args()


def secure_write(path: Path, value: dict[str, object]) -> str:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(payload).hexdigest()


async def run() -> dict[str, object]:
    conn = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    targets: list[dict[str, object]] = []
    try:
        database = str(await conn.fetchval("SELECT current_database()"))
        if not database.startswith("memory_reviewed_failure_v2_"):
            raise RuntimeError("preparation requires reviewed-failure clone")
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id',$1,true)",
                str(OWNER),
            )
            for evidence_id, expected_hash, case in PARENTS:
                parent = await conn.fetchrow(
                    """
                    SELECT evidence_id,content,content_sha256
                      FROM memory.evidence
                     WHERE owner_user_id=$1
                       AND evidence_id=$2
                       AND content_sha256=$3
                       AND status='active'
                    """,
                    OWNER,
                    evidence_id,
                    expected_hash,
                )
                if parent is None:
                    raise RuntimeError(f"{case} parent is unavailable")
                plan = contextual_span_plan_v2(parent["content"])
                preflight = await conn.fetchrow(
                    """
                    SELECT *
                      FROM memory.preflight_owner_contextual_split_v2(
                        $1,$2,$3,$4::jsonb
                      )
                    """,
                    evidence_id,
                    expected_hash,
                    SPLITTER_VERSION,
                    json.dumps(plan, sort_keys=True, separators=(",", ":")),
                )
                applied = await conn.fetch(
                    """
                    SELECT *
                      FROM memory.apply_owner_contextual_split_v2(
                        $1,$2,$3,$4::jsonb,$5
                      )
                    """,
                    evidence_id,
                    expected_hash,
                    SPLITTER_VERSION,
                    json.dumps(plan, sort_keys=True, separators=(",", ":")),
                    preflight["plan_sha256"],
                )
                if len(applied) != len(plan):
                    raise RuntimeError(f"{case} split count changed")
                for item, result in zip(plan, applied, strict=True):
                    targets.append(
                        {
                            "case": case,
                            "ordinal": int(item["ordinal"]),
                            "context_needed": bool(
                                item["context_needed"]
                            ),
                            "evidence_id": str(
                                result["child_evidence_id"]
                            ),
                            "content_sha256": str(
                                result["child_content_sha256"]
                            ),
                            "content_char_count": len(item["content"]),
                        }
                    )
            existing_id, existing_hash, existing_case = EXISTING
            if not await conn.fetchval(
                """
                SELECT EXISTS(
                  SELECT 1
                    FROM memory.evidence
                   WHERE owner_user_id=$1
                     AND evidence_id=$2
                     AND content_sha256=$3
                     AND status='active'
                )
                """,
                OWNER,
                existing_id,
                existing_hash,
            ):
                raise RuntimeError("existing fragment is unavailable")
            targets.append(
                {
                    "case": existing_case,
                    "ordinal": 0,
                    "context_needed": True,
                    "evidence_id": str(existing_id),
                    "content_sha256": existing_hash,
                    "content_char_count": 54,
                }
            )
        return {
            "contract_version": (
                "memory_v1_reviewed_failure_clone_targets_v1"
            ),
            "owner_user_id_sha256": hashlib.sha256(
                str(OWNER).encode()
            ).hexdigest(),
            "target_count": len(targets),
            "targets": targets,
            "production_writes": 0,
            "qdrant_writes": 0,
            "prompt_influence": False,
        }
    finally:
        await conn.close()


def main() -> int:
    args = arguments()
    report = asyncio.run(run())
    secure_write(Path(args.report_path), report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
