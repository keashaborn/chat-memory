from __future__ import annotations

"""Sanitized, owner-scoped operational health for governed Memory V1."""

import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

import asyncpg
from qdrant_client.http import models as qmodels

from rag_engine.memory_v1_shadow import governed_activation_allowlisted
from rag_engine.qdrant_compat import make_qdrant_client
from scripts.memory_v1_v5_local_inference_scheduler import plan_owner


SCHEMA = "admin_memory_health_v1"
PIPELINE_SCHEMA = "memory_pipeline_status_v1"
ANSWER_WINDOW_DAYS = 7
OPERATIONS_SCHEMA = "memory_operational_status_v1"
PIPELINE_UNITS = (
    (
        "intake_dispatcher",
        "memory-v1-evidence-intake-dispatcher.service",
        "memory-v1-evidence-intake-dispatcher.timer",
    ),
    (
        "extraction_scheduler",
        "memory-v1-v5-local-inference-scheduler.service",
        "memory-v1-v5-local-inference-scheduler.timer",
    ),
    (
        "packet_router",
        "memory-v1-v5-2-local-packet-router.service",
        "memory-v1-v5-2-local-packet-router.timer",
    ),
    (
        "entity_validation",
        "memory-v1-v5-local-entity-validation.service",
        "memory-v1-v5-local-entity-validation.timer",
    ),
    (
        "auto_resolution",
        "memory-v1-v5-local-auto-resolution.service",
        "memory-v1-v5-local-auto-resolution.timer",
    ),
    (
        "entailment",
        "memory-v1-v5-local-entailment.service",
        "memory-v1-v5-local-entailment.timer",
    ),
    (
        "auto_stage",
        "memory-v1-v5-local-auto-stage.service",
        "memory-v1-v5-local-auto-stage.timer",
    ),
    (
        "claim_projection",
        "memory-v1-v5-local-claim-projection.service",
        "memory-v1-v5-local-claim-projection.timer",
    ),
    (
        "qdrant_projection",
        "memory-v1-projection.service",
        "memory-v1-projection.timer",
    ),
)


def _iso(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _integer(row: Mapping[str, Any], key: str) -> int:
    return int(row.get(key) or 0)


def _systemd_timestamp(value: str | None) -> str | None:
    if not value or value == "n/a":
        return None
    try:
        parsed = datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return None
    return _iso(parsed.replace(tzinfo=timezone.utc))


def _systemd_properties(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    completed = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            "--no-pager",
            f"--property={','.join(properties)}",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=2,
    )
    if completed.returncode != 0:
        return {}
    return {
        key: value
        for line in completed.stdout.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }


def _load_worker_operations_status() -> dict[str, Any]:
    workers: list[dict[str, Any]] = []
    for key, service_unit, timer_unit in PIPELINE_UNITS:
        service = _systemd_properties(
            service_unit,
            (
                "ActiveState",
                "SubState",
                "Result",
                "ExecMainStatus",
                "ExecMainStartTimestamp",
                "ExecMainExitTimestamp",
            ),
        )
        timer = _systemd_properties(
            timer_unit,
            (
                "ActiveState",
                "SubState",
                "Result",
                "LastTriggerUSec",
                "NextElapseUSecRealtime",
            ),
        )
        service_failed = (
            not service
            or service.get("ActiveState") == "failed"
            or service.get("Result") not in {"", "success"}
            or service.get("ExecMainStatus") not in {"", "0"}
        )
        timer_failed = (
            not timer
            or timer.get("ActiveState") != "active"
            or timer.get("SubState") != "waiting"
        )
        workers.append(
            {
                "key": key,
                "status": (
                    "unhealthy"
                    if service_failed or timer_failed
                    else "running"
                    if service.get("ActiveState") == "active"
                    else "idle"
                ),
                "service_result": service.get("Result") or "unavailable",
                "service_exit_status": (
                    int(service["ExecMainStatus"])
                    if service.get("ExecMainStatus", "").isdigit()
                    else None
                ),
                "last_run_started_at": _systemd_timestamp(
                    service.get("ExecMainStartTimestamp")
                ),
                "last_run_finished_at": _systemd_timestamp(
                    service.get("ExecMainExitTimestamp")
                ),
                "timer_status": (
                    "active" if not timer_failed else "unhealthy"
                ),
                "last_trigger_at": _systemd_timestamp(
                    timer.get("LastTriggerUSec")
                ),
                "next_trigger_at": _systemd_timestamp(
                    timer.get("NextElapseUSecRealtime")
                ),
            }
        )
    unhealthy = sum(worker["status"] == "unhealthy" for worker in workers)
    return {
        "schema": OPERATIONS_SCHEMA,
        "status": "unhealthy" if unhealthy else "healthy",
        "unhealthy_worker_count": unhealthy,
        "workers": workers,
    }


def _memory_bound_percent(attested: int, memory_bound: int) -> float:
    if attested <= 0:
        return 0.0
    return round((memory_bound / attested) * 100.0, 1)


def _summarize_memory_health_v1(
    *,
    claim: Mapping[str, Any],
    preference: Mapping[str, Any],
    project: Mapping[str, Any],
    processing: Mapping[str, Any],
    evidence: Mapping[str, Any],
    answers: Mapping[str, Any],
    pipeline: Mapping[str, Any],
    operations: Mapping[str, Any],
    vector_points: int | None,
    vector_error: bool,
    governed_active: bool,
    actor_active: bool,
    generated_at: datetime,
    collection_name: str,
) -> dict[str, Any]:
    supported = _integer(claim, "supported")
    retracted = _integer(claim, "retracted")
    vector_synchronized = vector_points is not None and vector_points == supported

    warnings: list[dict[str, str]] = []
    critical = False
    attention = False

    if not governed_active or not actor_active:
        attention = True
        warnings.append(
            {
                "code": "governed_memory_not_active",
                "severity": "attention",
                "message": "Governed memory is not active for this account.",
            }
        )

    if vector_error or vector_points is None:
        critical = True
        warnings.append(
            {
                "code": "claim_index_unavailable",
                "severity": "critical",
                "message": "The governed claim index could not be checked.",
            }
        )
    elif not vector_synchronized:
        critical = True
        warnings.append(
            {
                "code": "claim_index_mismatch",
                "severity": "critical",
                "message": "Supported claims and indexed claim vectors are out of sync.",
            }
        )

    if _integer(processing, "error") > 0:
        critical = True
        warnings.append(
            {
                "code": "extraction_errors",
                "severity": "critical",
                "message": "One or more memory extraction jobs are in an error state.",
            }
        )

    eligible = _integer(processing, "eligible")
    context_ready = _integer(processing, "context_ready")
    context_rebind_required = _integer(
        processing,
        "context_rebind_required",
    )
    retry_blocked = _integer(processing, "retry_blocked")
    head_of_line_blocked = eligible > 0 and context_ready == 0
    if head_of_line_blocked:
        critical = True
        warnings.append(
            {
                "code": "extraction_head_of_line_blocked",
                "severity": "critical",
                "message": "Pending extraction work exists, but no job is context-ready.",
            }
        )
    if context_rebind_required > 0:
        attention = True
        warnings.append(
            {
                "code": "context_rebind_required",
                "severity": "attention",
                "message": "Some extraction jobs require same-owner context rebinding.",
            }
        )
    if retry_blocked > 0:
        critical = True
        warnings.append(
            {
                "code": "retry_blocked_queue_items",
                "severity": "critical",
                "message": "Some extraction jobs are blocked by the retry ceiling.",
            }
        )

    unhealthy_workers = _integer(operations, "unhealthy_worker_count")
    if unhealthy_workers > 0:
        critical = True
        warnings.append(
            {
                "code": "memory_pipeline_workers_unhealthy",
                "severity": "critical",
                "message": "One or more governed-memory workers or timers are unhealthy.",
            }
        )

    if _integer(processing, "pending_older_7d") > 0:
        attention = True
        warnings.append(
            {
                "code": "extraction_backlog",
                "severity": "attention",
                "message": "Some memory extraction work has been pending for more than 7 days.",
            }
        )

    if _integer(processing, "review_required") > 0:
        attention = True
        warnings.append(
            {
                "code": "review_queue",
                "severity": "attention",
                "message": "Memory extraction items are waiting for review.",
            }
        )

    attested = _integer(answers, "attested_answers")
    memory_bound = _integer(answers, "memory_bound_answers")
    last_answer_at = answers.get("last_answer_at")
    last_memory_bound_at = answers.get("last_memory_bound_at")
    if attested > 0 and (
        last_memory_bound_at is None
        or (
            isinstance(last_answer_at, datetime)
            and isinstance(last_memory_bound_at, datetime)
            and (last_answer_at - last_memory_bound_at).total_seconds() > 172800
        )
    ):
        warnings.append(
            {
                "code": "memory_use_canary_recommended",
                "severity": "information",
                "message": "Recent answers have not used memory; run a specific-recall canary before treating this as a failure.",
            }
        )

    status = "critical" if critical else "attention" if attention else "healthy"
    pipeline_stages = dict(pipeline.get("stages") or {})
    pipeline_stages["indexed_vectors"] = vector_points
    pipeline_stages["memory_bound_answers_7d"] = memory_bound
    return {
        "ok": True,
        "schema": SCHEMA,
        "generated_at": _iso(generated_at),
        "scope": "current_actor",
        "status": status,
        "runtime": {
            "response_path": "resse_response_v0_2",
            "memory_mode": "governed_memory_v1",
            "governed_active": governed_active,
            "active_for_current_actor": actor_active,
            "lanes": {
                "claims": {
                    "status": "active" if actor_active else "inactive",
                    "supported": supported,
                    "retracted": retracted,
                },
                "preferences": {
                    "status": "active" if actor_active else "inactive",
                    "records": _integer(preference, "active"),
                },
                "projects": {
                    "status": "stored_not_response_active",
                    "records": _integer(project, "active"),
                },
            },
        },
        "index": {
            "collection": collection_name,
            "supported_claims": supported,
            "vector_points": vector_points,
            "synchronized": vector_synchronized,
        },
        "processing": {
            "pending": _integer(processing, "pending"),
            "review_required": _integer(processing, "review_required"),
            "skipped": _integer(processing, "skipped"),
            "processing": _integer(processing, "processing"),
            "error": _integer(processing, "error"),
            "pending_older_1d": _integer(processing, "pending_older_1d"),
            "pending_older_7d": _integer(processing, "pending_older_7d"),
            "oldest_pending_at": _iso(processing.get("oldest_pending_at")),
            "newest_job_at": _iso(processing.get("newest_job_at")),
            "completed_1d": _integer(processing, "completed_1d"),
            "completed_7d": _integer(processing, "completed_7d"),
            "eligible": eligible,
            "context_ready": context_ready,
            "context_rebind_required": context_rebind_required,
            "context_duplicate": _integer(processing, "context_duplicate"),
            "context_superseded": _integer(processing, "context_superseded"),
            "retry_blocked": retry_blocked,
            "head_of_line_blocked": head_of_line_blocked,
        },
        "operations_schema": OPERATIONS_SCHEMA,
        "operations": dict(operations),
        "freshness": {
            "last_evidence_at": _iso(evidence.get("last_evidence_at")),
            "last_claim_at": _iso(claim.get("last_claim_at")),
            "last_answer_at": _iso(last_answer_at),
            "last_memory_bound_at": _iso(last_memory_bound_at),
        },
        "answer_use": {
            "window_days": ANSWER_WINDOW_DAYS,
            "attested_answers": attested,
            "memory_bound_answers": memory_bound,
            "memory_bound_percent": _memory_bound_percent(attested, memory_bound),
        },
        "pipeline_schema": PIPELINE_SCHEMA,
        "pipeline": {
            "stages": {
                "evidence_rows": _integer(pipeline_stages, "evidence_rows"),
                "extracted_evidence_rows": _integer(
                    pipeline_stages,
                    "extracted_evidence_rows",
                ),
                "durable_observations": _integer(
                    pipeline_stages,
                    "durable_observations",
                ),
                "bound_observations": _integer(
                    pipeline_stages,
                    "bound_observations",
                ),
                "evaluated_observations": _integer(
                    pipeline_stages,
                    "evaluated_observations",
                ),
                "claim_plan_items": _integer(
                    pipeline_stages,
                    "claim_plan_items",
                ),
                "reviewed_claim_plan_items": _integer(
                    pipeline_stages,
                    "reviewed_claim_plan_items",
                ),
                "supported_claims": supported,
                "indexed_vectors": (
                    int(vector_points) if vector_points is not None else None
                ),
                "memory_bound_answers_7d": memory_bound,
            },
            "backlog": {
                "waiting_for_binding": _integer(
                    pipeline.get("backlog") or {},
                    "waiting_for_binding",
                ),
                "waiting_for_entailment": _integer(
                    pipeline.get("backlog") or {},
                    "waiting_for_entailment",
                ),
                "eligible_for_entailment": _integer(
                    pipeline.get("backlog") or {},
                    "eligible_for_entailment",
                ),
                "eligible_for_entailment_capped": bool(
                    (pipeline.get("backlog") or {}).get(
                        "eligible_for_entailment_capped",
                        False,
                    )
                ),
                "waiting_for_claim_review": _integer(
                    pipeline.get("backlog") or {},
                    "waiting_for_claim_review",
                ),
                "ready_for_materialization": _integer(
                    pipeline.get("backlog") or {},
                    "ready_for_materialization",
                ),
            },
        },
        "warnings": warnings,
    }


async def _load_actor_memory_health(
    dsn: str,
    actor: UUID,
) -> dict[str, Mapping[str, Any]]:
    conn = await asyncpg.connect(dsn, command_timeout=20)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SELECT set_config('app.user_id',$1,true)", str(actor))
            claim = await conn.fetchrow(
                """
                SELECT
                  count(*) FILTER (WHERE status='supported') AS supported,
                  count(*) FILTER (WHERE status='retracted') AS retracted,
                  max(updated_at) AS last_claim_at
                FROM memory.claim
                WHERE owner_user_id=$1
                """,
                actor,
            )
            preference = await conn.fetchrow(
                """
                SELECT count(*) FILTER (WHERE status='active') AS active
                FROM memory.user_preference
                WHERE owner_user_id=$1
                """,
                actor,
            )
            project = await conn.fetchrow(
                """
                SELECT count(*) AS active
                FROM memory.project_space
                WHERE owner_user_id=$1
                """,
                actor,
            )
            processing = await conn.fetchrow(
                """
                SELECT
                  count(*) FILTER (WHERE status='pending') AS pending,
                  count(*) FILTER (WHERE status='review_required') AS review_required,
                  count(*) FILTER (WHERE status='skipped') AS skipped,
                  count(*) FILTER (WHERE status='processing') AS processing,
                  count(*) FILTER (WHERE status='error') AS error,
                  count(*) FILTER (
                    WHERE status IN ('pending','error') AND attempts >= 2
                  ) AS retry_blocked,
                  count(*) FILTER (
                    WHERE status='pending' AND created_at < now()-interval '1 day'
                  ) AS pending_older_1d,
                  count(*) FILTER (
                    WHERE status='pending' AND created_at < now()-interval '7 days'
                  ) AS pending_older_7d,
                  min(created_at) FILTER (WHERE status='pending') AS oldest_pending_at,
                  max(created_at) AS newest_job_at,
                  count(*) FILTER (
                    WHERE status='completed' AND updated_at >= now()-interval '1 day'
                  ) AS completed_1d,
                  count(*) FILTER (
                    WHERE status='completed' AND updated_at >= now()-interval '7 days'
                  ) AS completed_7d
                FROM memory.evidence_extraction_job
                WHERE owner_user_id=$1
                """,
                actor,
            )
            evidence = await conn.fetchrow(
                """
                SELECT max(recorded_at) AS last_evidence_at
                FROM memory.evidence
                WHERE owner_user_id=$1
                """,
                actor,
            )
            answers = await conn.fetchrow(
                """
                SELECT
                  (
                    SELECT count(*)
                    FROM memory.assistant_transcript_attestation_v1
                    WHERE owner_user_id=$1
                      AND created_at >= now()-interval '7 days'
                  ) AS attested_answers,
                  (
                    SELECT count(*)
                    FROM memory.final_answer_memory_binding_v1
                    WHERE owner_user_id=$1
                      AND created_at >= now()-interval '7 days'
                  ) AS memory_bound_answers,
                  (
                    SELECT max(created_at)
                    FROM memory.assistant_transcript_attestation_v1
                    WHERE owner_user_id=$1
                  ) AS last_answer_at,
                  (
                    SELECT max(created_at)
                    FROM memory.final_answer_memory_binding_v1
                    WHERE owner_user_id=$1
                  ) AS last_memory_bound_at
                """,
                actor,
            )
            pipeline_value = await conn.fetchval(
                "SELECT memory.read_owner_pipeline_status_v1()"
            )
            eligible_for_entailment = await conn.fetchval(
                """
                SELECT count(*)
                FROM memory.plan_owner_v5_local_entailment_v1(20)
                """
            )
        queue_plan, _target, _last_service_at = await plan_owner(
            conn,
            actor,
            max_attempts=2,
            selector_version=None,
        )
    finally:
        await conn.close()
    if isinstance(pipeline_value, str):
        pipeline_value = json.loads(pipeline_value)
    if not isinstance(pipeline_value, Mapping):
        raise RuntimeError("memory pipeline status returned an invalid payload")
    if pipeline_value.get("schema") != PIPELINE_SCHEMA:
        raise RuntimeError("memory pipeline status schema is incompatible")
    pipeline_value = dict(pipeline_value)
    pipeline_backlog = dict(pipeline_value.get("backlog") or {})
    eligible_count = int(eligible_for_entailment or 0)
    pipeline_backlog["eligible_for_entailment"] = eligible_count
    pipeline_backlog["eligible_for_entailment_capped"] = eligible_count >= 20
    pipeline_value["backlog"] = pipeline_backlog
    processing_value = dict(processing or {})
    processing_value.update(
        {
            "eligible": int(queue_plan.get("eligible_count", 0)),
            "context_ready": int(queue_plan.get("context_ready_count", 0)),
            "context_rebind_required": int(
                queue_plan.get("context_rebind_required_count", 0)
            ),
            "context_duplicate": int(
                queue_plan.get("context_duplicate_count", 0)
            ),
            "context_superseded": int(
                queue_plan.get("context_superseded_count", 0)
            ),
        }
    )
    return {
        "claim": dict(claim or {}),
        "preference": dict(preference or {}),
        "project": dict(project or {}),
        "processing": processing_value,
        "evidence": dict(evidence or {}),
        "answers": dict(answers or {}),
        "pipeline": dict(pipeline_value),
    }


def _count_actor_vectors(
    qdrant_url: str,
    collection_name: str,
    actor: UUID,
) -> int:
    client = make_qdrant_client(url=qdrant_url, timeout=15.0)
    try:
        result = client.count(
            collection_name=collection_name,
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="owner_user_id",
                        match=qmodels.MatchValue(value=str(actor)),
                    )
                ]
            ),
            exact=True,
        )
        return int(result.count)
    finally:
        client.close()


async def build_admin_memory_health_v1(
    *,
    dsn: str,
    actor_user_id: str,
    qdrant_url: str,
    collection_name: str = "memory_claim_v1",
) -> dict[str, Any]:
    actor = UUID(actor_user_id)
    database = await _load_actor_memory_health(dsn, actor)
    operations = await asyncio.to_thread(_load_worker_operations_status)

    vector_points: int | None = None
    vector_error = False
    try:
        vector_points = await asyncio.to_thread(
            _count_actor_vectors,
            qdrant_url,
            collection_name,
            actor,
        )
    except Exception:
        vector_error = True

    governed_active = os.getenv("MEMORY_V1_GOVERNED_ACTIVE", "0").strip() == "1"
    return _summarize_memory_health_v1(
        claim=database["claim"],
        preference=database["preference"],
        project=database["project"],
        processing=database["processing"],
        evidence=database["evidence"],
        answers=database["answers"],
        pipeline=database["pipeline"],
        operations=operations,
        vector_points=vector_points,
        vector_error=vector_error,
        governed_active=governed_active,
        actor_active=governed_activation_allowlisted(actor),
        generated_at=datetime.now(timezone.utc),
        collection_name=collection_name,
    )


__all__ = ["PIPELINE_SCHEMA", "SCHEMA", "build_admin_memory_health_v1"]
