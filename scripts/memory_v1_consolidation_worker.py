#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sys
import uuid
from datetime import datetime
from typing import Any, Optional

import asyncpg
from openai import OpenAI

from rag_engine.memory_v1_consolidation import (
    EXTRACTOR_VERSION,
    ExtractionResult,
    extract_with_openai,
    looks_like_artifact,
    persist_extraction,
)
from rag_engine.memory_v1_store import actor_uuid, record_evidence


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--project-key")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--allow-auto-apply", action="store_true")
    return parser.parse_args()


def _owner_ids(args: argparse.Namespace) -> list[uuid.UUID]:
    raw = list(args.owner_user_id)
    raw.extend(
        value.strip()
        for value in os.getenv("MEMORY_V1_CONSOLIDATION_OWNER_IDS", "").split(",")
        if value.strip()
    )
    owners = sorted({actor_uuid(value) for value in raw}, key=str)
    if not owners:
        raise RuntimeError("no consolidation owner allowlist is configured")
    return owners


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


async def _set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(owner))


async def claim_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    worker_id: str,
    lease_seconds: int,
) -> Optional[dict[str, Any]]:
    lease_token = uuid.uuid4()
    async with conn.transaction():
        await _set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            WITH selected AS (
              SELECT job_id, status
              FROM memory.consolidation_job
              WHERE owner_user_id=$1
                AND available_at <= clock_timestamp()
                AND (
                  status IN ('pending', 'error')
                  OR (status='processing' AND lease_expires_at < clock_timestamp())
                )
              ORDER BY priority, source_recorded_at, job_id
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE memory.consolidation_job AS job
            SET status='processing',
                attempts=job.attempts + 1,
                lease_token=$2,
                lease_expires_at=clock_timestamp() + make_interval(secs => $3),
                worker_id=$4,
                last_error=NULL
            FROM selected
            WHERE job.owner_user_id=$1 AND job.job_id=selected.job_id
            RETURNING job.job_id, job.source_system, job.source_external_id,
                      job.source_sha256, job.source_recorded_at, job.attempts,
                      job.lease_token, job.result,
                      selected.status::text AS prior_status
            """,
            owner,
            lease_token,
            lease_seconds,
            worker_id,
        )
        if row is None:
            return None
        await conn.execute(
            """
            INSERT INTO memory.consolidation_event(
              owner_user_id, job_id, event_type, from_status, to_status,
              actor_type, actor_ref, details
            ) VALUES($1,$2,'claimed',$3::memory.consolidation_job_status,
                     'processing','worker',$4,$5::jsonb)
            """,
            owner,
            row["job_id"],
            row["prior_status"],
            worker_id,
            _stable_json({"attempt": int(row["attempts"])}),
        )
        return dict(row)


async def checkpoint_extraction(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    extraction: ExtractionResult,
    response_id: str,
) -> bool:
    prior_result = _json_object(job.get("result"))
    checkpoint = {
        "pipeline": EXTRACTOR_VERSION,
        "source_sha256": job["source_sha256"],
        "model_response_id": response_id,
        "extraction": extraction.model_dump(mode="json"),
    }
    result = {**prior_result, "extraction_checkpoint": checkpoint}
    serialized = _stable_json(result)
    if len(serialized.encode("utf-8")) > 24000:
        return False
    async with conn.transaction():
        await _set_actor(conn, owner)
        updated = await conn.fetchval(
            """
            UPDATE memory.consolidation_job
            SET result=$4::jsonb, worker_id=$5
            WHERE owner_user_id=$1 AND job_id=$2 AND lease_token=$3
              AND status='processing'
            RETURNING job_id
            """,
            owner,
            job["job_id"],
            job["lease_token"],
            serialized,
            worker_id,
        )
        if updated is None:
            raise RuntimeError("consolidation lease was lost before checkpoint")
        await conn.execute(
            """
            INSERT INTO memory.consolidation_event(
              owner_user_id, job_id, event_type, from_status, to_status,
              actor_type, actor_ref, details
            ) VALUES($1,$2,'extraction_checkpointed','processing','processing',
                     'worker',$3,$4::jsonb)
            """,
            owner,
            job["job_id"],
            worker_id,
            _stable_json({
                "candidate_count": len(extraction.candidates),
                "extraction_sha256": _sha256(
                    _stable_json(checkpoint["extraction"])
                ),
            }),
        )
    job["result"] = result
    return True


def checkpointed_extraction(
    job: dict[str, Any],
) -> Optional[tuple[ExtractionResult, str]]:
    result = _json_object(job.get("result"))
    checkpoint = result.get("extraction_checkpoint")
    if not isinstance(checkpoint, dict):
        return None
    if checkpoint.get("pipeline") != EXTRACTOR_VERSION:
        raise RuntimeError("consolidation checkpoint pipeline mismatch")
    if checkpoint.get("source_sha256") != job["source_sha256"]:
        raise RuntimeError("consolidation checkpoint source hash mismatch")
    extraction = ExtractionResult.model_validate(checkpoint.get("extraction"))
    response_id = str(checkpoint.get("model_response_id") or "checkpoint_replay")
    return extraction, response_id


async def source_record(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    source_external_id: str,
) -> Optional[dict[str, Any]]:
    try:
        source_id = uuid.UUID(source_external_id)
    except ValueError as exc:
        raise RuntimeError("job source_external_id is not a UUID") from exc
    async with conn.transaction(readonly=True):
        await _set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT id, owner_user_id, source, text, tags, thread_id,
                   vantage_id, request_id, created_at
            FROM public.chat_log
            WHERE id=$1 AND owner_user_id=$2
            """,
            source_id,
            owner,
        )
    return dict(row) if row else None


async def finish_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    status: str,
    result: dict[str, Any],
) -> None:
    async with conn.transaction():
        await _set_actor(conn, owner)
        updated = await conn.fetchval(
            """
            UPDATE memory.consolidation_job
            SET status=$4::memory.consolidation_job_status,
                lease_token=NULL,
                lease_expires_at=NULL,
                worker_id=$5,
                result=$6::jsonb,
                last_error=NULL
            WHERE owner_user_id=$1 AND job_id=$2 AND lease_token=$3
            RETURNING job_id
            """,
            owner,
            job["job_id"],
            job["lease_token"],
            status,
            worker_id,
            _stable_json(result),
        )
        if updated is None:
            raise RuntimeError("consolidation lease was lost before completion")
        await conn.execute(
            """
            INSERT INTO memory.consolidation_event(
              owner_user_id, job_id, event_type, from_status, to_status,
              actor_type, actor_ref, details
            ) VALUES($1,$2,'finished','processing',$3::memory.consolidation_job_status,
                     'worker',$4,$5::jsonb)
            """,
            owner,
            job["job_id"],
            status,
            worker_id,
            _stable_json({
                "candidate_count": sum(
                    len(value) for key, value in result.items()
                    if key.endswith("_candidate_ids") and isinstance(value, list)
                ),
                "route": result.get("route"),
            }),
        )


async def fail_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    error: Exception,
    max_attempts: int,
) -> None:
    message = f"{type(error).__name__}: {error}"[:2000]
    target = "skipped" if int(job["attempts"]) >= max_attempts else "error"
    delay_seconds = min(3600, 30 * (2 ** max(0, int(job["attempts"]) - 1)))
    async with conn.transaction():
        await _set_actor(conn, owner)
        updated = await conn.fetchval(
            """
            UPDATE memory.consolidation_job
            SET status=$4::memory.consolidation_job_status,
                lease_token=NULL,
                lease_expires_at=NULL,
                worker_id=$5,
                last_error=$6,
                available_at=CASE WHEN $4='error'
                  THEN clock_timestamp() + make_interval(secs => $7)
                  ELSE available_at END
            WHERE owner_user_id=$1 AND job_id=$2 AND lease_token=$3
            RETURNING job_id
            """,
            owner,
            job["job_id"],
            job["lease_token"],
            target,
            worker_id,
            message,
            delay_seconds,
        )
        if updated is None:
            return
        await conn.execute(
            """
            INSERT INTO memory.consolidation_event(
              owner_user_id, job_id, event_type, from_status, to_status,
              actor_type, actor_ref, details
            ) VALUES($1,$2,'failed','processing',$3::memory.consolidation_job_status,
                     'worker',$4,$5::jsonb)
            """,
            owner,
            job["job_id"],
            target,
            worker_id,
            _stable_json({"error_class": type(error).__name__, "attempt": int(job["attempts"])}),
        )


async def process_job(
    conn: asyncpg.Connection,
    client: OpenAI,
    *,
    owner: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    model: str,
    project_key: Optional[str],
    allow_auto_apply: bool,
) -> None:
    source = await source_record(
        conn,
        owner=owner,
        source_external_id=job["source_external_id"],
    )
    if source is None:
        await finish_job(
            conn,
            owner=owner,
            job=job,
            worker_id=worker_id,
            status="skipped",
            result={"route": "source_deleted", "pipeline": EXTRACTOR_VERSION},
        )
        return
    text = str(source.get("text") or "")
    if _sha256(text) != job["source_sha256"]:
        raise RuntimeError("source content hash changed after queueing")

    is_document = looks_like_artifact(text)
    evidence_id = await record_evidence(
        conn,
        owner,
        kind="document" if is_document else "user_statement",
        source_system="public.chat_log",
        external_id=f"chat_log:{source['id']}",
        content=text,
        observed_at=source.get("created_at"),
        directness=1.0,
        source_reliability=0.8,
        independence_key=f"chat_thread:{source.get('thread_id') or source['id']}",
        sensitivity="high",
        metadata={
            "chat_log_id": str(source["id"]),
            "thread_id": str(source["thread_id"]) if source.get("thread_id") else None,
            "source": source.get("source"),
            "vantage_id": source.get("vantage_id"),
            "request_id": source.get("request_id"),
            "capture_sha256": job["source_sha256"],
        },
    )

    if is_document:
        await finish_job(
            conn,
            owner=owner,
            job=job,
            worker_id=worker_id,
            status="review_required",
            result={
                "route": "artifact_assessment",
                "pipeline": EXTRACTOR_VERSION,
                "evidence_id": str(evidence_id),
            },
        )
        return

    checkpointed = checkpointed_extraction(job)
    if checkpointed is None:
        extraction, response_id = await asyncio.to_thread(
            extract_with_openai,
            client,
            model=model,
            owner_user_id=owner,
            source_external_id=str(source["id"]),
            text=text,
        )
    else:
        extraction, response_id = checkpointed
    if extraction.contains_quoted_or_pasted_content and (
        len(text) >= 500 or text.count("\n") >= 3
    ):
        await finish_job(
            conn,
            owner=owner,
            job=job,
            worker_id=worker_id,
            status="review_required",
            result={
                "route": "artifact_assessment",
                "pipeline": EXTRACTOR_VERSION,
                "evidence_id": str(evidence_id),
                "model_response_id": response_id,
            },
        )
        return

    if checkpointed is None and not await checkpoint_extraction(
        conn,
        owner=owner,
        job=job,
        worker_id=worker_id,
        extraction=extraction,
        response_id=response_id,
    ):
        await finish_job(
            conn,
            owner=owner,
            job=job,
            worker_id=worker_id,
            status="review_required",
            result={
                "route": "extraction_too_large",
                "pipeline": EXTRACTOR_VERSION,
                "evidence_id": str(evidence_id),
                "model_response_id": response_id,
            },
        )
        return

    staged = await persist_extraction(
        conn,
        actor_user_id=owner,
        evidence_id=evidence_id,
        extraction=extraction,
        source_text=text,
        observed_at=source.get("created_at") or datetime.utcnow(),
        configured_project_key=project_key,
        allow_auto_apply=allow_auto_apply,
    )
    candidate_count = sum(
        len(value)
        for key, value in staged.items()
        if key.endswith("_candidate_ids") and isinstance(value, list)
    )
    status = "review_required" if (
        candidate_count
        or staged["project_deferred"]
        or staged["validation_rejections"]
    ) else "completed"
    await finish_job(
        conn,
        owner=owner,
        job=job,
        worker_id=worker_id,
        status=status,
        result={
            "route": "candidate_review" if status == "review_required" else "evidence_only",
            "pipeline": EXTRACTOR_VERSION,
            "evidence_id": str(evidence_id),
            "model_response_id": response_id,
            **staged,
        },
    )


async def main() -> int:
    args = arguments()
    owners = _owner_ids(args)
    if args.batch_size < 1 or args.batch_size > 100:
        raise RuntimeError("batch-size must be between 1 and 100")
    if args.lease_seconds < 30 or args.lease_seconds > 3600:
        raise RuntimeError("lease-seconds must be between 30 and 3600")
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is not configured")
    model = (
        os.getenv("MEMORY_V1_CONSOLIDATION_MODEL")
        or os.getenv("VANTAGE_MODEL")
        or "gpt-5.2"
    ).strip()
    auto_env = os.getenv("MEMORY_V1_CONSOLIDATION_AUTO_APPLY", "0").lower() in {
        "1", "true", "yes", "on"
    }
    allow_auto_apply = bool(args.allow_auto_apply and auto_env)
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{EXTRACTOR_VERSION}"
    conn = await asyncpg.connect(dsn)
    try:
        role = await conn.fetchval("SELECT current_user")
        if role != "brains_app":
            raise RuntimeError(f"worker requires brains_app, current_user={role}")
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        summary = {"processed": 0, "owners": len(owners), "auto_apply": allow_auto_apply}
        for owner in owners:
            for _ in range(args.batch_size):
                job = await claim_job(
                    conn,
                    owner=owner,
                    worker_id=worker_id,
                    lease_seconds=args.lease_seconds,
                )
                if job is None:
                    break
                try:
                    await process_job(
                        conn,
                        client,
                        owner=owner,
                        job=job,
                        worker_id=worker_id,
                        model=model,
                        project_key=args.project_key,
                        allow_auto_apply=allow_auto_apply,
                    )
                except Exception as exc:
                    await fail_job(
                        conn,
                        owner=owner,
                        job=job,
                        worker_id=worker_id,
                        error=exc,
                        max_attempts=args.max_attempts,
                    )
                summary["processed"] += 1
        print(json.dumps(summary, sort_keys=True))
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
