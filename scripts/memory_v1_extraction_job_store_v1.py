#!/usr/bin/env python3
"""Provider-neutral PostgreSQL authority for one governed extraction job."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from typing import Any
import uuid

import asyncpg


CONTRACT_VERSION = "memory_v1_extraction_job_store_v1"
PERSIST_NAMESPACE = uuid.UUID("3fc762b4-6e78-4cbd-b669-36427f647eb2")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EXACT_CLAIM_MODE = "memory_v1_openai_exact_job_claim_v2"
RESERVATION_MODE = "memory_v1_openai_request_reservation_v1"
RECEIPT_CONTRACT = "memory_v1_openai_provider_request_receipt_v1"
ELIGIBILITY_DISPOSITION_CONTRACT = (
    "memory_v1_personal_evidence_disposition_receipt_v2"
)
ELIGIBILITY_DISPOSITION_MODE = "memory_v1_openai_eligibility_disposition_v2"
_GATE_DISPOSITION_CONTRACT = (
    "memory_v1_personal_evidence_disposition_receipt_v1"
)
_ELIGIBILITY_DECISIONS = frozenset(
    {
        "send_external",
        "skip_zero_call",
        "review_context",
        "route_internal",
        "block_local",
    }
)
_NON_EXTERNAL_DECISIONS = _ELIGIBILITY_DECISIONS - {"send_external"}
_SAFE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")


class ProcessingRejected(RuntimeError):
    def __init__(self, code: str, external_model_calls: int) -> None:
        super().__init__(code)
        self.code = code
        self.external_model_calls = external_model_calls


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bind_eligibility_disposition_v2(
    *,
    owner: uuid.UUID,
    evidence_id: uuid.UUID,
    exact_job_id: uuid.UUID,
    expected_content_sha256: str,
    gate_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Bind a content-free gate result to one owner/evidence/job exchange."""
    expected_gate_keys = {
        "contract_version",
        "context_envelope_sha256",
        "context_turn_count",
        "decision",
        "gate_contract_version",
        "gate_result_sha256",
        "policy_sha256",
        "policy_version",
        "reason_codes",
        "selected_span_count",
        "selected_spans_sha256",
        "source_gate_sha256",
    }
    if not isinstance(gate_receipt, dict) or set(gate_receipt) != expected_gate_keys:
        raise RuntimeError("eligibility gate receipt contract is invalid")
    decision = gate_receipt.get("decision")
    reason_codes = gate_receipt.get("reason_codes")
    context_sha256 = gate_receipt.get("context_envelope_sha256")
    source_gate_sha256 = gate_receipt.get("source_gate_sha256")
    if (
        gate_receipt.get("contract_version") != _GATE_DISPOSITION_CONTRACT
        or decision not in _ELIGIBILITY_DECISIONS
        or not isinstance(reason_codes, list)
        or not (1 <= len(reason_codes) <= 16)
        or any(
            not isinstance(code, str) or _SAFE_TOKEN_RE.fullmatch(code) is None
            for code in reason_codes
        )
        or len(set(reason_codes)) != len(reason_codes)
        or _SAFE_TOKEN_RE.fullmatch(str(gate_receipt.get("gate_contract_version", "")))
        is None
        or _SAFE_TOKEN_RE.fullmatch(str(gate_receipt.get("policy_version", "")))
        is None
        or SHA256_RE.fullmatch(str(gate_receipt.get("gate_result_sha256", "")))
        is None
        or SHA256_RE.fullmatch(str(gate_receipt.get("policy_sha256", ""))) is None
        or SHA256_RE.fullmatch(str(gate_receipt.get("selected_spans_sha256", "")))
        is None
        or (
            context_sha256 is not None
            and SHA256_RE.fullmatch(str(context_sha256)) is None
        )
        or (
            source_gate_sha256 is not None
            and SHA256_RE.fullmatch(str(source_gate_sha256)) is None
        )
        or not isinstance(gate_receipt.get("context_turn_count"), int)
        or isinstance(gate_receipt.get("context_turn_count"), bool)
        or not (0 <= gate_receipt["context_turn_count"] <= 8)
        or not isinstance(gate_receipt.get("selected_span_count"), int)
        or isinstance(gate_receipt.get("selected_span_count"), bool)
        or not (0 <= gate_receipt["selected_span_count"] <= 64)
        or SHA256_RE.fullmatch(expected_content_sha256) is None
    ):
        raise RuntimeError("eligibility gate receipt contract is invalid")

    exchange_id = uuid.uuid5(
        PERSIST_NAMESPACE,
        "|".join(
            (
                "eligibility-exchange-v2",
                str(owner),
                str(evidence_id),
                str(exact_job_id),
                expected_content_sha256,
                gate_receipt["gate_result_sha256"],
                str(context_sha256 or ""),
            )
        ),
    )
    lineage_binding = "|".join(
        (
            ELIGIBILITY_DISPOSITION_CONTRACT,
            str(owner),
            str(evidence_id),
            str(exact_job_id),
            expected_content_sha256,
            str(exchange_id),
            str(decision),
            gate_receipt["gate_result_sha256"],
            gate_receipt["policy_sha256"],
            str(context_sha256 or ""),
            str(source_gate_sha256 or ""),
            ",".join(reason_codes),
        )
    )
    return {
        "contract_version": ELIGIBILITY_DISPOSITION_CONTRACT,
        "context_envelope_sha256": context_sha256,
        "context_turn_count": gate_receipt["context_turn_count"],
        "decision": decision,
        "evidence_content_sha256": expected_content_sha256,
        "evidence_id": str(evidence_id),
        "exchange_id": str(exchange_id),
        "external_model_calls": 0,
        "gate_contract_version": gate_receipt["gate_contract_version"],
        "gate_result_sha256": gate_receipt["gate_result_sha256"],
        "job_id": str(exact_job_id),
        "lineage_sha256": sha256_text(lineage_binding),
        "owner_user_id": str(owner),
        "policy_sha256": gate_receipt["policy_sha256"],
        "policy_version": gate_receipt["policy_version"],
        "reason_codes": reason_codes,
        "selected_span_count": gate_receipt["selected_span_count"],
        "selected_spans_sha256": gate_receipt["selected_spans_sha256"],
        "source_gate_sha256": source_gate_sha256,
    }


def _validate_bound_eligibility_disposition_v2(
    receipt: dict[str, Any],
    *,
    owner: uuid.UUID,
    exact_job_id: uuid.UUID,
    expected_content_sha256: str,
    allow_external: bool,
) -> None:
    expected_keys = {
        "contract_version",
        "context_envelope_sha256",
        "context_turn_count",
        "decision",
        "evidence_content_sha256",
        "evidence_id",
        "exchange_id",
        "external_model_calls",
        "gate_contract_version",
        "gate_result_sha256",
        "job_id",
        "lineage_sha256",
        "owner_user_id",
        "policy_sha256",
        "policy_version",
        "reason_codes",
        "selected_span_count",
        "selected_spans_sha256",
        "source_gate_sha256",
    }
    allowed_decisions = (
        _ELIGIBILITY_DECISIONS if allow_external else _NON_EXTERNAL_DECISIONS
    )
    if (
        not isinstance(receipt, dict)
        or set(receipt) != expected_keys
        or receipt.get("contract_version") != ELIGIBILITY_DISPOSITION_CONTRACT
        or receipt.get("decision") not in allowed_decisions
        or receipt.get("owner_user_id") != str(owner)
        or receipt.get("job_id") != str(exact_job_id)
        or receipt.get("evidence_content_sha256") != expected_content_sha256
        or receipt.get("external_model_calls") != 0
        or SHA256_RE.fullmatch(str(receipt.get("gate_result_sha256", ""))) is None
        or SHA256_RE.fullmatch(str(receipt.get("lineage_sha256", ""))) is None
    ):
        raise RuntimeError("bound eligibility disposition contract is invalid")
    try:
        uuid.UUID(str(receipt["evidence_id"]))
        uuid.UUID(str(receipt["exchange_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("bound eligibility disposition contract is invalid") from exc


def canonical_owners(values: list[str]) -> list[uuid.UUID]:
    if not values:
        raise RuntimeError("at least one explicit owner UUID is required")
    try:
        owners = sorted({uuid.UUID(value) for value in values}, key=str)
    except ValueError as exc:
        raise RuntimeError("owner allowlist contains an invalid UUID") from exc
    if len(owners) > 100:
        raise RuntimeError("owner allowlist exceeds 100 entries")
    return owners


def worker_reference(value: str | None, *, version: str) -> str:
    result = value or (
        f"{version}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
    )
    if not result.strip() or len(result) > 500:
        raise RuntimeError("worker id must contain 1 to 500 characters")
    return result


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def plan_owner(
    conn: asyncpg.Connection,
    owner: uuid.UUID,
    *,
    exact_job_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    async with conn.transaction(readonly=True):
        await set_actor(conn, owner)
        rows = await conn.fetch(
            """
            SELECT status::text AS status,count(*)::integer AS count
            FROM memory.evidence_extraction_job
            WHERE owner_user_id=$1 AND route='relational_extraction'
            GROUP BY status ORDER BY status
            """,
            owner,
        )
        next_job = await conn.fetchval(
            """
            SELECT job_id::text
            FROM memory.evidence_extraction_job
            WHERE owner_user_id=$1 AND route='relational_extraction'
              AND available_at<=clock_timestamp()
              AND status IN ('pending','error')
              AND ($2::uuid IS NULL OR job_id=$2)
            ORDER BY priority,available_at,created_at,job_id LIMIT 1
            """,
            owner,
            exact_job_id,
        )
    return {
        "owner_sha256": sha256_text(str(owner)),
        "route": "relational_extraction",
        "status_counts": {str(row["status"]): row["count"] for row in rows},
        "next_job_sha256": sha256_text(next_job) if next_job else None,
        "exact_job": exact_job_id is not None,
    }


async def probe_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    run_id: uuid.UUID,
    worker_id: str,
    exact_job_id: uuid.UUID,
    expected_content_sha256: str,
    provider_id: str,
    provider_version: str,
    provider_model_sha256: str,
    rolling_window_seconds: int,
    max_reserved_calls: int,
    failure_threshold: int,
) -> dict[str, Any] | None:
    if SHA256_RE.fullmatch(expected_content_sha256) is None:
        raise RuntimeError("exact content hash must be lowercase SHA-256")
    async with conn.transaction():
        await set_actor(conn, owner)
        await conn.execute(
            "SELECT set_config('memory.openai_prefilter_mode',$1,true)",
            "memory_v1_openai_prefilter_probe_v1",
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_claim_mode',$1,true)",
            "memory_v1_openai_exact_job_claim_v1",
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_job_id',$1,true)",
            str(exact_job_id),
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_content_sha256',$1,true)",
            expected_content_sha256,
        )
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.claim_owner_v5_bounded_extraction_job_v1(
              $1,$2,'relational_extraction',$3,300,1,$4,$5,$6,$7,$8,$9
            )
            """,
            operation_id,
            run_id,
            worker_id,
            provider_id,
            provider_version,
            provider_model_sha256,
            rolling_window_seconds,
            max_reserved_calls,
            failure_threshold,
        )
    return dict(row) if row else None


async def reserve_call(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    run_id: uuid.UUID,
    exact_job_id: uuid.UUID,
    expected_content_sha256: str,
    worker_id: str,
    provider_id: str,
    provider_version: str,
    provider_model_sha256: str,
    rolling_window_seconds: int,
    max_reserved_calls: int,
    failure_threshold: int,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    if receipt.get("contract_version") != RECEIPT_CONTRACT:
        raise RuntimeError("provider request receipt contract is invalid")
    canonical = stable_json(receipt)
    if len(canonical.encode("utf-8")) > 16_384:
        raise RuntimeError("provider request receipt exceeds its bound")
    async with conn.transaction():
        await set_actor(conn, owner)
        await conn.execute(
            "SELECT set_config('memory.openai_reservation_mode',$1,true)",
            RESERVATION_MODE,
        )
        await conn.execute(
            "SELECT set_config('memory.openai_request_receipt_v1',$1,true)",
            canonical,
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_claim_mode',$1,true)",
            "memory_v1_openai_exact_job_claim_v1",
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_job_id',$1,true)",
            str(exact_job_id),
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_content_sha256',$1,true)",
            expected_content_sha256,
        )
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.claim_owner_v5_bounded_extraction_job_v1(
              $1,$2,'relational_extraction',$3,300,1,$4,$5,$6,$7,$8,$9
            )
            """,
            operation_id,
            run_id,
            worker_id,
            provider_id,
            provider_version,
            provider_model_sha256,
            rolling_window_seconds,
            max_reserved_calls,
            failure_threshold,
        )
    if row is None:
        raise ProcessingRejected("provider_reservation_unavailable", 0)
    result = dict(row)
    if result.get("control_outcome") != "reserved":
        raise ProcessingRejected(str(result.get("control_outcome")), 0)
    if (
        result.get("job_id") != exact_job_id
        or result.get("evidence_content_sha256") != expected_content_sha256
    ):
        raise RuntimeError("provider reservation changed the exact job binding")
    return result


async def complete_call(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    reservation_event_id: uuid.UUID,
    run_id: uuid.UUID,
    job_id: uuid.UUID,
    outcome: str,
    external_model_calls: int,
    rejection_code_value: str | None = None,
    provider_output_sha256: str | None = None,
    validator_packet_sha256: str | None = None,
    packet_storage_sha256: str | None = None,
) -> dict[str, Any]:
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.complete_owner_v5_extraction_call_v1(
              $1,$2,$3,$4,$5,$6,$7,$8,$9,$10
            )
            """,
            operation_id,
            reservation_event_id,
            run_id,
            job_id,
            outcome,
            external_model_calls,
            rejection_code_value,
            provider_output_sha256,
            validator_packet_sha256,
            packet_storage_sha256,
        )
    if row is None:
        raise RuntimeError("provider call completion returned no row")
    return dict(row)


async def skip_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    run_id: uuid.UUID,
    exact_job_id: uuid.UUID,
    expected_content_sha256: str,
    worker_id: str,
    provider_id: str,
    provider_version: str,
    provider_model_sha256: str,
    rolling_window_seconds: int,
    max_reserved_calls: int,
    failure_threshold: int,
    reason_code: str,
    gate_sha256: str,
    eligibility_disposition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "contract_version": (
            "memory_v1_prefilter_skip_receipt_v2"
            if eligibility_disposition is not None
            else "memory_v1_prefilter_skip_receipt_v1"
        ),
        "external_model_calls": 0,
        "gate_sha256": gate_sha256,
        "reason_code": reason_code,
    }
    if eligibility_disposition is not None:
        _validate_bound_eligibility_disposition_v2(
            eligibility_disposition,
            owner=owner,
            exact_job_id=exact_job_id,
            expected_content_sha256=expected_content_sha256,
            allow_external=True,
        )
        if eligibility_disposition["gate_result_sha256"] != gate_sha256:
            raise RuntimeError("budget skip eligibility gate binding is invalid")
        result["eligibility_disposition"] = eligibility_disposition
    canonical = stable_json(result)
    async with conn.transaction():
        await set_actor(conn, owner)
        await conn.execute(
            "SELECT set_config('memory.openai_prefilter_mode',$1,true)",
            "memory_v1_openai_prefilter_skip_v1",
        )
        await conn.execute(
            "SELECT set_config('memory.openai_prefilter_receipt_v1',$1,true)",
            canonical,
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_claim_mode',$1,true)",
            "memory_v1_openai_exact_job_claim_v1",
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_job_id',$1,true)",
            str(exact_job_id),
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_content_sha256',$1,true)",
            expected_content_sha256,
        )
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.claim_owner_v5_bounded_extraction_job_v1(
              $1,$2,'relational_extraction',$3,300,1,$4,$5,$6,$7,$8,$9
            )
            """,
            operation_id,
            run_id,
            worker_id,
            provider_id,
            provider_version,
            provider_model_sha256,
            rolling_window_seconds,
            max_reserved_calls,
            failure_threshold,
        )
    if row is None:
        raise RuntimeError("prefilter skip completion returned no row")
    return dict(row)


async def record_eligibility_disposition(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    run_id: uuid.UUID,
    exact_job_id: uuid.UUID,
    expected_content_sha256: str,
    worker_id: str,
    provider_id: str,
    provider_version: str,
    provider_model_sha256: str,
    rolling_window_seconds: int,
    max_reserved_calls: int,
    failure_threshold: int,
    eligibility_disposition: dict[str, Any],
) -> dict[str, Any]:
    _validate_bound_eligibility_disposition_v2(
        eligibility_disposition,
        owner=owner,
        exact_job_id=exact_job_id,
        expected_content_sha256=expected_content_sha256,
        allow_external=False,
    )
    canonical = stable_json(eligibility_disposition)
    async with conn.transaction():
        await set_actor(conn, owner)
        await conn.execute(
            "SELECT set_config('memory.openai_prefilter_mode',$1,true)",
            ELIGIBILITY_DISPOSITION_MODE,
        )
        await conn.execute(
            "SELECT set_config('memory.openai_prefilter_receipt_v1',$1,true)",
            canonical,
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_claim_mode',$1,true)",
            "memory_v1_openai_exact_job_claim_v1",
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_job_id',$1,true)",
            str(exact_job_id),
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_content_sha256',$1,true)",
            expected_content_sha256,
        )
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.claim_owner_v5_bounded_extraction_job_v1(
              $1,$2,'relational_extraction',$3,300,1,$4,$5,$6,$7,$8,$9
            )
            """,
            operation_id,
            run_id,
            worker_id,
            provider_id,
            provider_version,
            provider_model_sha256,
            rolling_window_seconds,
            max_reserved_calls,
            failure_threshold,
        )
    if row is None:
        raise RuntimeError("eligibility disposition completion returned no row")
    return dict(row)


async def finalize_receipt(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    run_id: uuid.UUID,
    exact_job_id: uuid.UUID,
    expected_content_sha256: str,
    worker_id: str,
    provider_id: str,
    provider_version: str,
    provider_model_sha256: str,
    rolling_window_seconds: int,
    max_reserved_calls: int,
    failure_threshold: int,
    audit_receipt: dict[str, Any],
) -> dict[str, Any]:
    canonical = stable_json(audit_receipt)
    if len(canonical.encode("utf-8")) > 24_576:
        raise RuntimeError("provider completion audit exceeds its bound")
    async with conn.transaction():
        await set_actor(conn, owner)
        await conn.execute(
            "SELECT set_config('memory.openai_finalize_mode',$1,true)",
            "memory_v1_openai_request_receipt_finalize_v1",
        )
        await conn.execute(
            "SELECT set_config('memory.openai_completion_audit_v1',$1,true)",
            canonical,
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_claim_mode',$1,true)",
            "memory_v1_openai_exact_job_claim_v1",
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_job_id',$1,true)",
            str(exact_job_id),
        )
        await conn.execute(
            "SELECT set_config('memory.v5_exact_content_sha256',$1,true)",
            expected_content_sha256,
        )
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.claim_owner_v5_bounded_extraction_job_v1(
              $1,$2,'relational_extraction',$3,300,1,$4,$5,$6,$7,$8,$9
            )
            """,
            operation_id,
            run_id,
            worker_id,
            provider_id,
            provider_version,
            provider_model_sha256,
            rolling_window_seconds,
            max_reserved_calls,
            failure_threshold,
        )
    if row is None:
        raise RuntimeError("provider request receipt finalizer returned no row")
    return dict(row)


async def persist_packet(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    packet_id: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    model_sha256: str,
    validated: Any,
    binding_event_id: uuid.UUID | None,
) -> dict[str, Any]:
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.persist_owner_evidence_extraction_packet_v5(
              $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14,$15
            )
            """,
            operation_id,
            packet_id,
            job["job_id"],
            job["lease_token"],
            worker_id,
            job["evidence_content_sha256"],
            validated.provider_id,
            validated.provider_version,
            model_sha256,
            validated.provider_output_sha256,
            validated.normalized_packet_sha256,
            stable_json(validated.normalized_packet),
            validated.manual_review_required,
            validated.external_model_calls,
            binding_event_id,
        )
    if row is None:
        raise RuntimeError("V5 persistence function returned no row")
    return dict(row)


async def fail_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    code: str,
    max_attempts: int,
) -> dict[str, Any]:
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.fail_owner_evidence_extraction_job_v1(
              $1,$2,$3,$4,$5,$6,$7,$8
            )
            """,
            operation_id,
            job["job_id"],
            job["lease_token"],
            worker_id,
            job["evidence_content_sha256"],
            code,
            "sanitized governed extraction rejection",
            max_attempts,
        )
    if row is None:
        raise RuntimeError("extraction failure function returned no row")
    return dict(row)


__all__ = [
    "CONTRACT_VERSION",
    "ELIGIBILITY_DISPOSITION_CONTRACT",
    "ELIGIBILITY_DISPOSITION_MODE",
    "EXACT_CLAIM_MODE",
    "PERSIST_NAMESPACE",
    "ProcessingRejected",
    "RECEIPT_CONTRACT",
    "RESERVATION_MODE",
    "SHA256_RE",
    "canonical_owners",
    "bind_eligibility_disposition_v2",
    "complete_call",
    "fail_job",
    "finalize_receipt",
    "persist_packet",
    "plan_owner",
    "probe_job",
    "record_eligibility_disposition",
    "reserve_call",
    "set_actor",
    "sha256_text",
    "stable_json",
    "skip_job",
    "worker_reference",
]
