#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile

import asyncpg

from rag_engine.memory_v1_evidence_context_loader_v2 import (
    load_memory_evidence_context_v2,
)
from rag_engine.memory_v1_evidence_context_v1 import (
    EvidenceContextContractError,
)
from rag_engine.memory_v1_evidence_context_v2 import (
    sanitized_evidence_context_report_v2,
)
from tests.memory_v1_contextual_intake_v2_clone_verify import (
    FRAGMENT_TEXT,
    OWNER_A,
    OWNER_B,
    fixture_ids,
    set_actor,
)


def secure_write(path: Path, value: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True) + "\n").encode()
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
    try:
        database = str(await conn.fetchval("SELECT current_database()"))
        if not database.startswith("memory_contextual_intake_v2_"):
            raise RuntimeError("verification requires disposable clone")
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("verification requires brains_app")

        async with conn.transaction():
            await set_actor(conn, OWNER_A)
            target = await conn.fetchrow(
                """
                SELECT evidence_id,content_sha256
                  FROM memory.evidence
                 WHERE owner_user_id=$1
                   AND metadata->>'contextual_parent_content_sha256'=$2
                   AND content=$3
                 ORDER BY evidence_id
                 LIMIT 1
                """,
                OWNER_A,
                hashlib.sha256(FRAGMENT_TEXT.encode()).hexdigest(),
                FRAGMENT_TEXT,
            )
        if target is None:
            raise RuntimeError("fragment child evidence is absent")

        envelope = await load_memory_evidence_context_v2(
            conn,
            expected_owner_user_id=OWNER_A,
            target_evidence_id=target["evidence_id"],
            expected_target_content_sha256=target["content_sha256"],
        )
        envelope.validate_hash()
        if len(envelope.prior_turns) < 2:
            raise RuntimeError("bounded prior-turn context was not loaded")
        if envelope.prior_turns[-1].speaker_role != "assistant":
            raise RuntimeError("nearest assistant context ordering changed")
        if envelope.allowed_assertion_evidence_ids != (
            str(target["evidence_id"]),
        ):
            raise RuntimeError("target-only assertion authority changed")
        if any(
            turn.assertion_origin_allowed or turn.instruction_capability
            for turn in envelope.prior_turns
        ):
            raise RuntimeError("prior turn received authority")
        other_thread_source_id = str(fixture_ids(database)[0][0])
        if any(
            turn.source_id == other_thread_source_id
            for turn in envelope.prior_turns
        ):
            raise RuntimeError("cross-thread context became visible")

        cross_owner_rejected = False
        try:
            await load_memory_evidence_context_v2(
                conn,
                expected_owner_user_id=OWNER_B,
                target_evidence_id=target["evidence_id"],
                expected_target_content_sha256=target["content_sha256"],
            )
        except EvidenceContextContractError:
            cross_owner_rejected = True
        if not cross_owner_rejected:
            raise RuntimeError("cross-owner context load was not rejected")

        report = sanitized_evidence_context_report_v2(envelope)
        rendered = json.dumps(report, sort_keys=True)
        if "martial arts" in rendered or FRAGMENT_TEXT in rendered:
            raise RuntimeError("sanitized report leaked source content")
        return {
            **report,
            "database_sha256": hashlib.sha256(database.encode()).hexdigest(),
            "cross_owner_rejected": True,
            "model_calls": 0,
            "claim_writes": 0,
            "qdrant_writes": 0,
            "prompt_influence": False,
        }
    finally:
        await conn.close()


def main() -> int:
    report = asyncio.run(run())
    report_path = os.environ.get("REPORT_PATH")
    if report_path:
        secure_write(Path(report_path), report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
