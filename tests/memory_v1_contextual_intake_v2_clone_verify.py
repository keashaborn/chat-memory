#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

import asyncpg

from rag_engine.memory_v1_contextual_span_splitter_v2 import (
    contextual_span_plan_v2,
)


LONG_TEXT = (
    "I spent many years working with German Shepherds and traveling for their "
    "training. Keasha was a German showline dog, and I trained her in "
    "Schutzhund. I also imported dogs from Germany and traveled between Green "
    "Bay and Chicago while clinics were being built. The dogs rode in a secure "
    "vehicle kennel, and I stopped regularly so they could exercise. My cat "
    "Neko later slept on my chest most nights. Losing several of these animals "
    "was difficult, but the experiences remain important parts of my life."
)
FRAGMENT_TEXT = "That was when I was in my early 20s."
OWNER_A = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OWNER_B = uuid.UUID("673d64a3-c4ba-4d1c-89e3-e0c579022fad")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("seed", "verify"), required=True)
    parser.add_argument("--report-path")
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def secure_write(path: Path, value: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
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


async def set_actor(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
) -> None:
    await conn.execute(
        "SELECT set_config('app.user_id',$1,true)",
        str(owner),
    )


async def assert_clone(conn: asyncpg.Connection) -> str:
    database = str(await conn.fetchval("SELECT current_database()"))
    if not database.startswith("memory_contextual_intake_v2_"):
        raise RuntimeError("verification requires the disposable clone")
    if await conn.fetchval("SELECT session_user") != "brains_app":
        raise RuntimeError("verification requires brains_app")
    return database


def fixture_ids(database: str) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, database)
    return [
        (
            uuid.uuid5(namespace, "long-source"),
            uuid.uuid5(namespace, "long-request"),
            LONG_TEXT,
        ),
        (
            uuid.uuid5(namespace, "fragment-source"),
            uuid.uuid5(namespace, "fragment-request"),
            FRAGMENT_TEXT,
        ),
    ]


async def seed(conn: asyncpg.Connection) -> dict[str, object]:
    database = await assert_clone(conn)
    async with conn.transaction():
        await set_actor(conn, OWNER_A)
        thread_id = await conn.fetchval(
            """
            SELECT id
              FROM public.threads
             WHERE owner_user_id=$1
             ORDER BY created_at,id
             LIMIT 1
            """,
            OWNER_A,
        )
        if thread_id is None:
            raise RuntimeError("fixture owner has no thread in clone")
        evidence: list[dict[str, str]] = []
        for source_id, request_id, content in fixture_ids(database):
            await conn.execute(
                """
                INSERT INTO public.chat_log(
                  id,user_id,source,text,tags,created_at,thread_id,
                  request_id,owner_user_id
                ) VALUES (
                  $1,$2,'frontend/chat:user',$3,ARRAY['clone-fixture'],
                  now(),$4,$5,$6
                )
                """,
                source_id,
                str(OWNER_A),
                content,
                thread_id,
                str(request_id),
                OWNER_A,
            )
            recorded = await conn.fetchrow(
                """
                SELECT *
                  FROM memory.record_owner_evidence_v1(
                    'user_statement'::memory.evidence_kind,
                    'public.chat_log',$1,$2,now(),1,1,$3,
                    'high'::memory.sensitivity_level,$4::jsonb
                  )
                """,
                str(source_id),
                content,
                f"clone-fixture:{thread_id}",
                json.dumps(
                    {
                        "capture_version": "clone_contextual_intake_v2",
                        "source_type": "frontend/chat:user",
                        "source_external_id": str(source_id),
                        "thread_id": str(thread_id),
                        "request_id": str(request_id),
                        "semantic_processing": "pending",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            if recorded is None or recorded["outcome"] != "applied":
                raise RuntimeError("fixture evidence was not created")
            if recorded["content_sha256"] != sha256_text(content):
                raise RuntimeError("fixture evidence hash changed")
            evidence_id = recorded["evidence_id"]
            plan = await conn.fetchrow(
                """
                SELECT *
                  FROM memory.plan_owner_evidence_intake_v2(
                    '20260728_v3_contextual',1,$1
                  )
                """,
                evidence_id,
            )
            if (
                plan is None
                or plan["outcome"] != "deferred"
                or plan["route"] != "contextual_split"
                or plan["reason_code"] != "contextual_split_required"
                or not plan["requires_contextual_split"]
                or plan["source_bound"]
            ):
                raise RuntimeError("fixture parent did not enter split route")
            evidence.append(
                {
                    "evidence_id": str(evidence_id),
                    "content_sha256": sha256_text(content),
                }
            )
    return {
        "database": database,
        "owner_user_id": str(OWNER_A),
        "fixtures": evidence,
    }


async def verify(conn: asyncpg.Connection) -> dict[str, object]:
    database = await assert_clone(conn)
    source_ids = [item[0] for item in fixture_ids(database)]
    async with conn.transaction():
        await set_actor(conn, OWNER_A)
        parents = await conn.fetch(
            """
            SELECT evidence_id,content_sha256
              FROM memory.evidence
             WHERE owner_user_id=$1
               AND source_system='public.chat_log'
               AND external_id=ANY($2::text[])
             ORDER BY external_id
            """,
            OWNER_A,
            [str(item) for item in source_ids],
        )
        if len(parents) != 2:
            raise RuntimeError("fixture parent count changed")
        parent_ids = [row["evidence_id"] for row in parents]
        span_count = 0
        context_needed_count = 0
        queued = 0
        for parent in parents:
            content = (
                LONG_TEXT
                if parent["content_sha256"] == sha256_text(LONG_TEXT)
                else FRAGMENT_TEXT
            )
            span_plan = contextual_span_plan_v2(content)
            planned = await conn.fetchrow(
                """
                SELECT *
                  FROM memory.preflight_owner_contextual_split_v2(
                    $1,$2,
                    'memory_v1_contextual_span_splitter_20260728_v2',
                    $3::jsonb
                  )
                """,
                parent["evidence_id"],
                parent["content_sha256"],
                json.dumps(
                    span_plan,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            if planned is None or int(planned["span_count"]) != len(span_plan):
                raise RuntimeError("replay preflight changed")
            replayed = await conn.fetch(
                """
                SELECT *
                  FROM memory.apply_owner_contextual_split_v2(
                    $1,$2,
                    'memory_v1_contextual_span_splitter_20260728_v2',
                    $3::jsonb,$4
                  )
                """,
                parent["evidence_id"],
                parent["content_sha256"],
                json.dumps(
                    span_plan,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                planned["plan_sha256"],
            )
            if (
                len(replayed) != len(span_plan)
                or any(row["apply_outcome"] != "replayed" for row in replayed)
            ):
                raise RuntimeError("contextual span replay was not zero-write")
            terminal = await conn.fetchrow(
                """
                SELECT *
                  FROM memory.finalize_owner_contextual_split_v2(
                    $1,'20260728_v3_contextual',$2,$3,$4
                  )
                """,
                parent["evidence_id"],
                parent["content_sha256"],
                planned["plan_sha256"],
                len(span_plan),
            )
            if terminal is None or terminal["apply_outcome"] != "replayed":
                raise RuntimeError("parent terminal replay was not zero-write")
            source_text = await conn.fetchval(
                """
                SELECT text
                  FROM public.chat_log
                 WHERE owner_user_id=$1 AND id=$2
                """,
                OWNER_A,
                planned["source_id"],
            )
            if source_text != content:
                raise RuntimeError("preflight source binding changed")
            for result, item in zip(replayed, span_plan, strict=True):
                child = await conn.fetchrow(
                    """
                    SELECT content,content_sha256,metadata
                      FROM memory.evidence
                     WHERE owner_user_id=$1 AND evidence_id=$2
                    """,
                    OWNER_A,
                    result["child_evidence_id"],
                )
                if (
                    child is None
                    or child["content"] != item["content"]
                    or child["content_sha256"] != item["content_sha256"]
                    or source_text[
                        item["char_start"] : item["char_end"]
                    ]
                    != item["content"]
                    or len(item["content"]) > 520
                ):
                    raise RuntimeError("source-bound child verification failed")
                queue = await conn.fetchrow(
                    """
                    SELECT *
                      FROM memory.enqueue_owner_evidence_extraction_v1(
                        $1,'20260728_v3_contextual',$2,
                        'relational_extraction','eligible_unprocessed'
                      )
                    """,
                    result["child_evidence_id"],
                    result["child_content_sha256"],
                )
                if queue is None or queue["apply_outcome"] != "replayed":
                    raise RuntimeError("child queue replay was not zero-write")
                queued += 1
            span_count += len(span_plan)
            context_needed_count += sum(
                bool(item["context_needed"]) for item in span_plan
            )
        if span_count < 3:
            raise RuntimeError("fixture did not produce atomic spans")
        if context_needed_count < 1:
            raise RuntimeError("context-needed fragment was not classified")

        await set_actor(conn, OWNER_B)
        if int(
            await conn.fetchval(
                """
                SELECT count(*)
                  FROM memory.evidence
                 WHERE evidence_id=ANY($1::uuid[])
                """,
                parent_ids,
            )
        ):
            raise RuntimeError("cross-owner evidence visibility is possible")
        try:
            parent_content = (
                LONG_TEXT
                if parents[0]["content_sha256"] == sha256_text(LONG_TEXT)
                else FRAGMENT_TEXT
            )
            await conn.fetchrow(
                """
                SELECT *
                  FROM memory.preflight_owner_contextual_split_v2(
                    $1,$2,
                    'memory_v1_contextual_span_splitter_20260728_v2',
                    $3::jsonb
                  )
                """,
                parent_ids[0],
                parents[0]["content_sha256"],
                json.dumps(
                    contextual_span_plan_v2(parent_content),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        except asyncpg.PostgresError as exc:
            if exc.sqlstate != "P0002":
                raise
        else:
            raise RuntimeError("cross-owner preflight was not rejected")

    return {
        "database": database,
        "parent_count": 2,
        "span_count": span_count,
        "context_needed_count": context_needed_count,
        "terminal_count": 2,
        "queued_child_count": queued,
        "cross_owner_visible": 0,
        "model_calls": 0,
        "claim_writes": 0,
        "qdrant_writes": 0,
        "prompt_influence": 0,
    }


async def main() -> int:
    args = arguments()
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=120)
    try:
        result = (
            await seed(conn)
            if args.phase == "seed"
            else await verify(conn)
        )
    finally:
        await conn.close()
    result["phase"] = args.phase
    result["completed_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )
    if args.report_path:
        path = Path(args.report_path).resolve()
        result["report_file_sha256"] = secure_write(path, result)
    print(
        json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
