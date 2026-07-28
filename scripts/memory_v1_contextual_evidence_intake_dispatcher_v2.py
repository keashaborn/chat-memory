#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid

import asyncpg

from rag_engine.memory_v1_contextual_span_splitter_v2 import (
    SPLITTER_VERSION,
    contextual_span_plan_v2,
)
from scripts.memory_v1_authenticated_owners import resolve_authenticated_owners


CONTRACT_VERSION = "memory_v1_contextual_evidence_intake_dispatcher_v2"
SELECTOR_VERSION = "20260728_v3_contextual"
APPLY_CAPABILITY = "memory_v1_contextual_evidence_intake_apply_v2"
TERMINAL_OUTCOMES = {"empty", "skipped"}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Atomize authenticated chat evidence into exact source-bound spans "
            "before the private relational extractor can receive it."
        )
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--evidence-id", action="append", default=[])
    parser.add_argument("--selector-version", default=SELECTOR_VERSION)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report-path", required=True)
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return sha256_text(stable_json(value))


def secure_write(path: Path, value: dict[str, Any]) -> str:
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


def serialize_plan_row(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "evidence_id": str(row["evidence_id"]),
        "evidence_content_sha256": row["evidence_content_sha256"],
        "outcome": row["outcome"],
        "route": row["route"],
        "reason_code": row["reason_code"],
        "source_job_id": (
            str(row["source_job_id"]) if row["source_job_id"] else None
        ),
        "source_job_status": row["source_job_status"],
        "source_job_pipeline_version": row["source_job_pipeline_version"],
        "upstream_candidate_count": int(row["upstream_candidate_count"]),
        "source_bound": bool(row["source_bound"]),
        "requires_contextual_split": bool(row["requires_contextual_split"]),
    }


async def set_actor(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
) -> None:
    await conn.execute(
        "SELECT set_config('app.user_id',$1,true)",
        str(owner),
    )


async def plan(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    selector_version: str,
    limit: int,
    evidence_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    await set_actor(conn, owner)
    rows = await conn.fetch(
        """
        SELECT *
          FROM memory.plan_owner_evidence_intake_v2($1,$2,$3)
        """,
        selector_version,
        limit,
        evidence_id,
    )
    return [serialize_plan_row(row) for row in rows]


async def parent_content(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    evidence_id: uuid.UUID,
    expected_sha256: str,
) -> str:
    await set_actor(conn, owner)
    row = await conn.fetchrow(
        """
        SELECT content,content_sha256
          FROM memory.evidence
         WHERE owner_user_id=$1
           AND evidence_id=$2
           AND status='active'
        """,
        owner,
        evidence_id,
    )
    if row is None or row["content_sha256"] != expected_sha256:
        raise RuntimeError("contextual split parent changed or is unavailable")
    content = row["content"]
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("contextual split parent content is invalid")
    if sha256_text(content) != expected_sha256:
        raise RuntimeError("contextual split parent content hash is invalid")
    return content


async def preflight_split(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    row: dict[str, Any],
) -> tuple[list[dict[str, Any]], str, int]:
    evidence_id = uuid.UUID(row["evidence_id"])
    content = await parent_content(
        conn,
        owner,
        evidence_id,
        row["evidence_content_sha256"],
    )
    spans = contextual_span_plan_v2(content)
    planned = await conn.fetchrow(
        """
        SELECT *
          FROM memory.preflight_owner_contextual_split_v2($1,$2,$3,$4::jsonb)
        """,
        evidence_id,
        row["evidence_content_sha256"],
        SPLITTER_VERSION,
        stable_json(spans),
    )
    if planned is None:
        raise RuntimeError("contextual split preflight returned no plan")
    plan_sha256 = str(planned["plan_sha256"])
    span_count = int(planned["span_count"])
    if span_count != len(spans) or len(plan_sha256) != 64:
        raise RuntimeError("contextual split preflight plan is inconsistent")
    return spans, plan_sha256, span_count


async def assert_target_removed_from_intake(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    selector_version: str,
    evidence_id: uuid.UUID,
) -> None:
    remaining = await plan(
        conn,
        owner,
        selector_version,
        1,
        evidence_id,
    )
    if remaining:
        raise RuntimeError("applied intake target remains selectable")


async def apply_contextual_split(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    selector_version: str,
    row: dict[str, Any],
    spans: list[dict[str, Any]],
    plan_sha256: str,
    span_count: int,
) -> dict[str, Any]:
    parent_id = uuid.UUID(row["evidence_id"])
    values = (
        parent_id,
        row["evidence_content_sha256"],
        SPLITTER_VERSION,
        stable_json(spans),
        plan_sha256,
    )
    first = await conn.fetch(
        """
        SELECT *
          FROM memory.apply_owner_contextual_split_v2(
            $1,$2,$3,$4::jsonb,$5
          )
        """,
        *values,
    )
    second = await conn.fetch(
        """
        SELECT *
          FROM memory.apply_owner_contextual_split_v2(
            $1,$2,$3,$4::jsonb,$5
          )
        """,
        *values,
    )
    if len(first) != span_count or len(second) != span_count:
        raise RuntimeError("contextual split apply row count changed")
    if any(item["apply_outcome"] not in {"applied", "replayed"} for item in first):
        raise RuntimeError("contextual split returned an invalid apply outcome")
    if any(item["apply_outcome"] != "replayed" for item in second):
        raise RuntimeError("contextual split replay was not zero-write")

    terminal_values = (
        parent_id,
        selector_version,
        row["evidence_content_sha256"],
        plan_sha256,
        span_count,
    )
    first_terminal = await conn.fetchrow(
        """
        SELECT *
          FROM memory.finalize_owner_contextual_split_v2($1,$2,$3,$4,$5)
        """,
        *terminal_values,
    )
    second_terminal = await conn.fetchrow(
        """
        SELECT *
          FROM memory.finalize_owner_contextual_split_v2($1,$2,$3,$4,$5)
        """,
        *terminal_values,
    )
    if first_terminal is None or second_terminal is None:
        raise RuntimeError("contextual split finalizer returned no result")
    if first_terminal["apply_outcome"] not in {"applied", "replayed"}:
        raise RuntimeError("contextual split parent finalizer failed")
    if second_terminal["apply_outcome"] != "replayed":
        raise RuntimeError("contextual split parent replay was not zero-write")

    queue_applied = 0
    queue_replayed = 0
    context_needed = 0
    for applied, span in zip(first, spans, strict=True):
        child_id = applied["child_evidence_id"]
        child_sha256 = applied["child_content_sha256"]
        exact = await plan(
            conn,
            owner,
            selector_version,
            1,
            child_id,
        )
        if len(exact) != 1:
            raise RuntimeError("contextual child is absent from exact intake")
        child = exact[0]
        if (
            child["outcome"] != "eligible"
            or child["route"] != "relational_extraction"
            or child["reason_code"] != "eligible_unprocessed"
            or child["evidence_content_sha256"] != child_sha256
            or not child["source_bound"]
            or child["requires_contextual_split"]
        ):
            raise RuntimeError("contextual child intake contract changed")
        queue_values = (
            child_id,
            selector_version,
            child_sha256,
            child["route"],
            child["reason_code"],
        )
        first_queue = await conn.fetchrow(
            """
            SELECT *
              FROM memory.enqueue_owner_evidence_extraction_v1(
                $1,$2,$3,$4,$5
              )
            """,
            *queue_values,
        )
        second_queue = await conn.fetchrow(
            """
            SELECT *
              FROM memory.enqueue_owner_evidence_extraction_v1(
                $1,$2,$3,$4,$5
              )
            """,
            *queue_values,
        )
        if first_queue is None or second_queue is None:
            raise RuntimeError("contextual child queue returned no result")
        if first_queue["apply_outcome"] not in {"applied", "replayed"}:
            raise RuntimeError("contextual child queue apply failed")
        if second_queue["apply_outcome"] != "replayed":
            raise RuntimeError("contextual child queue replay was not zero-write")
        queue_applied += int(first_queue["apply_outcome"] == "applied")
        queue_replayed += int(second_queue["apply_outcome"] == "replayed")
        context_needed += int(bool(span["context_needed"]))
        await assert_target_removed_from_intake(
            conn,
            owner,
            selector_version,
            child_id,
        )

    await assert_target_removed_from_intake(
        conn,
        owner,
        selector_version,
        parent_id,
    )
    return {
        "parent_terminal_applied": int(
            first_terminal["apply_outcome"] == "applied"
        ),
        "parent_terminal_replayed": int(
            second_terminal["apply_outcome"] == "replayed"
        ),
        "children_applied": sum(
            item["apply_outcome"] == "applied" for item in first
        ),
        "children_replayed": sum(
            item["apply_outcome"] == "replayed" for item in second
        ),
        "children_queued": queue_applied,
        "queue_replayed": queue_replayed,
        "context_needed": context_needed,
    }


async def apply_existing_route(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    selector_version: str,
    row: dict[str, Any],
) -> tuple[str, str]:
    evidence_id = uuid.UUID(row["evidence_id"])
    if row["outcome"] in TERMINAL_OUTCOMES:
        result = await conn.fetchrow(
            """
            SELECT *
              FROM memory.record_owner_evidence_intake_terminal_v1(
                $1,$2,$3,$4,$5
              )
            """,
            evidence_id,
            selector_version,
            row["evidence_content_sha256"],
            row["outcome"],
            row["reason_code"],
        )
        operation = "terminal"
    elif row["outcome"] == "eligible":
        if not row["source_bound"] or row["requires_contextual_split"]:
            raise RuntimeError("unbound evidence cannot enter extraction queue")
        result = await conn.fetchrow(
            """
            SELECT *
              FROM memory.enqueue_owner_evidence_extraction_v1(
                $1,$2,$3,$4,$5
              )
            """,
            evidence_id,
            selector_version,
            row["evidence_content_sha256"],
            row["route"],
            row["reason_code"],
        )
        operation = "queue"
    else:
        raise RuntimeError("existing route is neither terminal nor eligible")
    if result is None or result["apply_outcome"] not in {"applied", "replayed"}:
        raise RuntimeError("existing intake route apply failed")
    await assert_target_removed_from_intake(
        conn,
        owner,
        selector_version,
        evidence_id,
    )
    return operation, str(result["apply_outcome"])


async def dispatch_owner(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    selector_version: str,
    limit: int,
    apply: bool,
    target_evidence_ids: list[uuid.UUID],
) -> dict[str, Any]:
    if target_evidence_ids:
        before = []
        for evidence_id in target_evidence_ids:
            before.extend(
                await plan(
                    conn,
                    owner,
                    selector_version,
                    1,
                    evidence_id,
                )
            )
        if len(before) != len(target_evidence_ids):
            raise RuntimeError(
                "one or more exact contextual intake targets are unavailable"
            )
    else:
        before = await plan(conn, owner, selector_version, limit)
    actions: Counter[str] = Counter()
    split_reports: list[dict[str, Any]] = []

    for row in before:
        if row["requires_contextual_split"]:
            spans, plan_sha256, span_count = await preflight_split(
                conn,
                owner,
                row,
            )
            split_report = {
                "parent_evidence_id_sha256": sha256_text(row["evidence_id"]),
                "parent_content_sha256": row["evidence_content_sha256"],
                "plan_sha256": plan_sha256,
                "span_count": span_count,
                "context_needed": sum(
                    bool(span["context_needed"]) for span in spans
                ),
                "max_span_chars": max(
                    len(str(span["content"])) for span in spans
                ),
            }
            if apply:
                split_report["apply"] = await apply_contextual_split(
                    conn,
                    owner,
                    selector_version,
                    row,
                    spans,
                    plan_sha256,
                    span_count,
                )
                actions["contextual_split_applied"] += 1
            else:
                actions["contextual_split_planned"] += 1
            split_reports.append(split_report)
        elif apply and row["outcome"] in TERMINAL_OUTCOMES | {"eligible"}:
            operation, outcome = await apply_existing_route(
                conn,
                owner,
                selector_version,
                row,
            )
            actions[f"{operation}_{outcome}"] += 1
        elif row["outcome"] == "deferred":
            actions["deferred_unchanged"] += 1
        elif not apply and row["outcome"] in TERMINAL_OUTCOMES | {"eligible"}:
            actions[f"{row['outcome']}_planned"] += 1
        else:
            raise RuntimeError(f"unexpected intake outcome: {row['outcome']}")

    return {
        "owner_user_id_sha256": sha256_text(str(owner)),
        "plan_sha256": digest(before),
        "rows": len(before),
        "outcomes": dict(
            sorted(Counter(row["outcome"] for row in before).items())
        ),
        "reasons": dict(
            sorted(Counter(row["reason_code"] for row in before).items())
        ),
        "actions": dict(sorted(actions.items())),
        "contextual_splits": split_reports,
    }


async def main() -> int:
    args = arguments()
    if args.selector_version != SELECTOR_VERSION:
        raise RuntimeError(
            f"--selector-version must be exactly {SELECTOR_VERSION}"
        )
    if not 1 <= args.limit <= 500:
        raise RuntimeError("--limit must be between 1 and 500")
    if (
        args.apply
        and os.getenv("MEMORY_V1_CONTEXTUAL_INTAKE_APPLY")
        != APPLY_CAPABILITY
    ):
        raise RuntimeError("contextual intake apply capability is absent")
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    owners = await resolve_authenticated_owners(dsn, args.owner_user_id)
    target_evidence_ids = [uuid.UUID(value) for value in args.evidence_id]
    if len(set(target_evidence_ids)) != len(target_evidence_ids):
        raise RuntimeError("--evidence-id targets must be unique")
    if target_evidence_ids and len(owners) != 1:
        raise RuntimeError(
            "exact evidence targets require exactly one authenticated owner"
        )

    conn = await asyncpg.connect(dsn, command_timeout=120)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("POSTGRES_DSN must authenticate as brains_app")
        async with conn.transaction():
            owner_reports = [
                await dispatch_owner(
                    conn,
                    owner,
                    args.selector_version,
                    args.limit,
                    args.apply,
                    target_evidence_ids,
                )
                for owner in owners
            ]
    finally:
        await conn.close()

    report = {
        "contract_version": CONTRACT_VERSION,
        "completed_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        ),
        "selector_version": args.selector_version,
        "splitter_version": SPLITTER_VERSION,
        "apply": args.apply,
        "owners": owner_reports,
        "model_calls": 0,
        "packet_writes": 0,
        "claim_writes": 0,
        "qdrant_writes": 0,
        "prompt_influence": 0,
    }
    report["report_sha256"] = digest(report)
    report_path = Path(args.report_path).expanduser().resolve()
    report_file_sha256 = secure_write(report_path, report)
    print(
        stable_json(
            {
                "apply": args.apply,
                "owners": len(owner_reports),
                "rows": sum(item["rows"] for item in owner_reports),
                "splits": sum(
                    len(item["contextual_splits"])
                    for item in owner_reports
                ),
                "report": str(report_path),
                "report_file_sha256": report_file_sha256,
                "model_calls": 0,
                "claim_writes": 0,
                "qdrant_writes": 0,
                "prompt_influence": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
