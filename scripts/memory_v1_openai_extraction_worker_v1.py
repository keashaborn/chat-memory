#!/usr/bin/env python3
"""Run one exact, owner-scoped governed OpenAI memory extraction."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import os
from pathlib import Path
from typing import Any
import uuid

import asyncpg

from rag_engine.memory_v1_evidence_context_loader_v2 import (
    load_memory_evidence_context_v2,
)
from rag_engine.memory_v1_evidence_context_v1 import (
    EvidenceContextContractError,
)
from rag_engine.memory_v1_openai_postgres_authority_v1 import (
    PostgresBudgetAuthorizerV1,
    PostgresPrivacyAuthorizerV1,
    PostgresProviderReservationV1,
)
from rag_engine.memory_v1_openai_provider_adapter_v1 import (
    EXPECTED_PREDICATE_REGISTRY_SHA256,
    OpenAIV52ProviderAdapterV1,
    extraction_model_policy_v1,
    extraction_task_profile_v1,
)
from rag_engine.memory_v1_openai_structured_transport_v1 import (
    EXTERNAL_CALL_ENABLE_TOKEN,
    PRIVACY_AUTHORIZATION_TOKEN,
    ExternalPrivacyAuthorizationV1,
    OpenAIStructuredResponsesTransportV1,
    StructuredTransportError,
)
from rag_engine.memory_v1_personal_evidence_prefilter_v1 import (
    TRUSTED_SOURCE_ROLE,
    classify_personal_evidence_v1,
)
from rag_engine.memory_v1_personal_evidence_exchange_v2 import (
    classify_personal_evidence_exchange_v2,
    content_free_disposition_receipt_v2,
)
from scripts.memory_v1_extraction_job_store_v1 import (
    PERSIST_NAMESPACE,
    ProcessingRejected,
    SHA256_RE,
    bind_eligibility_disposition_v2,
    canonical_owners,
    complete_call,
    fail_job,
    finalize_receipt,
    persist_packet,
    plan_owner,
    probe_job,
    record_eligibility_disposition,
    reserve_call,
    sha256_text,
    stable_json,
    skip_job,
    worker_reference,
)
from scripts.memory_v1_relational_extraction_v5_openai_provider import (
    OPENAI_PROVIDER_ID,
    OPENAI_PROVIDER_VERSION,
)
from scripts.memory_v1_relational_extraction_v5_observable_provider import (
    classify_validator_rejection,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    CONTRACT_VERSION_V5_2,
    ProviderPacket,
    TrustedExtractionSource,
    load_registry,
    load_schema,
    validate_and_normalize,
)


WORKER_VERSION = "memory_v1_openai_extraction_worker_v1"
APPLY_ENABLE_TOKEN = "memory_v1_openai_extraction_apply_v1"
PROVIDER_VERSION = OPENAI_PROVIDER_VERSION
MAX_JOBS = 1
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = REPOSITORY_ROOT / "specs/memory_v1_predicate_registry_v5_2.json"
DEFAULT_SCHEMA = REPOSITORY_ROOT / "specs/memory_v1_relational_extraction_v5_2.schema.json"
EXPECTED_REGISTRY_SHA256 = EXPECTED_PREDICATE_REGISTRY_SHA256
EXPECTED_SCHEMA_SHA256 = "21d0438d381715d7e2397564eb093364aee2149312b870caed6e0753a6cd31f5"
REJECTED_PACKET_EVIDENCE_CONTRACT = (
    "memory_v1_rejected_provider_packet_evidence_v1"
)
MAX_REJECTED_PACKET_EVIDENCE_BYTES = 16_384


class _BoundPacketProvider:
    provider_id = OPENAI_PROVIDER_ID
    provider_version = PROVIDER_VERSION
    external_call_capability = True

    def __init__(
        self,
        *,
        packet: ProviderPacket,
        source_sha256: str,
        external_model_calls: int,
    ) -> None:
        if external_model_calls != 1:
            raise RuntimeError("OpenAI packet must bind exactly one provider call")
        self._packet = packet.model_copy(deep=True)
        self._source_sha256 = source_sha256
        self._expected_external_model_calls = external_model_calls
        self.external_model_calls = 0

    def extract(self, source: TrustedExtractionSource) -> ProviderPacket:
        if source.source_sha256 != self._source_sha256:
            raise RuntimeError("OpenAI packet source binding changed")
        if self.external_model_calls != 0:
            raise RuntimeError("OpenAI packet cannot be validated more than once")
        self.external_model_calls = self._expected_external_model_calls
        return self._packet.model_copy(deep=True)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value.strip()


def _sha_env(name: str) -> str:
    value = _required_env(name)
    if SHA256_RE.fullmatch(value) is None:
        raise RuntimeError(f"{name} must be lowercase SHA-256")
    return value


def _positive_int_env(name: str) -> int:
    try:
        value = int(_required_env(name))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one fail-closed exact-job OpenAI memory extraction."
    )
    parser.add_argument("--owner-user-id", action="append", default=[])
    parser.add_argument("--owner-allowlist-env", default="MEMORY_V1_OPENAI_OWNER_ALLOWLIST")
    parser.add_argument("--run-id")
    parser.add_argument("--job-id")
    parser.add_argument("--expected-content-sha256")
    parser.add_argument("--model", required=True)
    parser.add_argument("--sdk-version", required=True)
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--worker-id")
    parser.add_argument("--max-jobs", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--rolling-window-seconds", type=int, default=86400)
    parser.add_argument("--max-reserved-calls", type=int, default=12)
    parser.add_argument("--failure-threshold", type=int, default=3)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def configured_owners(args: argparse.Namespace) -> list[uuid.UUID]:
    values = list(args.owner_user_id)
    values.extend(
        item.strip()
        for item in os.getenv(args.owner_allowlist_env, "").split(",")
        if item.strip()
    )
    return canonical_owners(values)


def configured_exact_target(
    args: argparse.Namespace, owners: list[uuid.UUID]
) -> tuple[uuid.UUID | None, str | None]:
    if (args.job_id is None) != (args.expected_content_sha256 is None):
        raise RuntimeError("--job-id and --expected-content-sha256 are required together")
    if args.job_id is None:
        return None, None
    try:
        job_id = uuid.UUID(str(args.job_id))
    except ValueError as exc:
        raise RuntimeError("job id must be a UUID") from exc
    expected = str(args.expected_content_sha256)
    if SHA256_RE.fullmatch(expected) is None:
        raise RuntimeError("expected content hash must be lowercase SHA-256")
    if len(owners) != 1 or args.max_jobs != 1:
        raise RuntimeError("exact-job mode requires one owner and --max-jobs 1")
    return job_id, expected


def validate_arguments(args: argparse.Namespace) -> uuid.UUID:
    try:
        run_id = uuid.UUID(args.run_id) if args.run_id else uuid.uuid4()
    except ValueError as exc:
        raise RuntimeError("run id must be a UUID") from exc
    if args.max_jobs != 1 or args.max_attempts != 1:
        raise RuntimeError("initial activation permits exactly one job and one attempt")
    if not 30 <= args.lease_seconds <= 3600:
        raise RuntimeError("lease-seconds must be between 30 and 3600")
    if not 1.0 <= args.timeout_seconds <= 180.0:
        raise RuntimeError("timeout-seconds must be between 1 and 180")
    if not 16 <= args.max_output_tokens <= 4096:
        raise RuntimeError("max-output-tokens must be between 16 and 4096")
    if not 3600 <= args.rolling_window_seconds <= 604800:
        raise RuntimeError("rolling window is invalid")
    if not 1 <= args.max_reserved_calls <= 100:
        raise RuntimeError("reserved-call ceiling is invalid")
    if not 1 <= args.failure_threshold <= 10:
        raise RuntimeError("failure threshold is invalid")
    if args.apply:
        if args.job_id is None:
            raise RuntimeError("initial activation requires one exact job target")
        if os.getenv("MEMORY_V1_OPENAI_EXTRACTION_APPLY") != APPLY_ENABLE_TOKEN:
            raise RuntimeError("OpenAI extraction apply capability is absent")
        if os.getenv("MEMORY_V1_OPENAI_STRUCTURED_CALLS") != EXTERNAL_CALL_ENABLE_TOKEN:
            raise RuntimeError("OpenAI structured-call capability is absent")
    return run_id


def build_adapter(args: argparse.Namespace) -> OpenAIV52ProviderAdapterV1:
    retention_mode = _required_env("MEMORY_V1_OPENAI_RETENTION_MODE")
    risk_accepted = os.getenv("MEMORY_V1_OPENAI_STANDARD_RETENTION_ACCEPTED") == "true"
    privacy = ExternalPrivacyAuthorizationV1(
        policy_version=_required_env("MEMORY_V1_OPENAI_PRIVACY_POLICY_VERSION"),
        policy_sha256=_sha_env("MEMORY_V1_OPENAI_PRIVACY_POLICY_SHA256"),
        retention_mode=retention_mode,
        authorization_sha256=_sha_env("MEMORY_V1_OPENAI_PRIVACY_AUTHORIZATION_SHA256"),
        standard_retention_risk_accepted=risk_accepted,
        retention_attestation_sha256=os.getenv(
            "MEMORY_V1_OPENAI_RETENTION_ATTESTATION_SHA256"
        ),
        enable_token=PRIVACY_AUTHORIZATION_TOKEN,
    )
    privacy.validate()
    task = extraction_task_profile_v1()
    model = extraction_model_policy_v1(
        model=args.model,
        sdk_package_version=args.sdk_version,
        max_output_tokens_ceiling=args.max_output_tokens,
        timeout_seconds_ceiling=int(args.timeout_seconds),
    )
    return OpenAIV52ProviderAdapterV1(
        transport=None,
        task_profile=task,
        model_policy=model,
        privacy_authorization=privacy,
        budget_policy_version=_required_env("MEMORY_V1_OPENAI_BUDGET_POLICY_VERSION"),
        budget_policy_sha256=_sha_env("MEMORY_V1_OPENAI_BUDGET_POLICY_SHA256"),
        pricing_policy_version=_required_env("MEMORY_V1_OPENAI_PRICING_POLICY_VERSION"),
        pricing_policy_sha256=_sha_env("MEMORY_V1_OPENAI_PRICING_POLICY_SHA256"),
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout_seconds,
        max_attempts=1,
    )


def pricing_rates() -> dict[str, int]:
    return {
        "input_microusd_per_million_tokens": _positive_int_env(
            "MEMORY_V1_OPENAI_INPUT_RATE_MICROUSD"
        ),
        "cached_input_microusd_per_million_tokens": _positive_int_env(
            "MEMORY_V1_OPENAI_CACHED_INPUT_RATE_MICROUSD"
        ),
        "cache_write_input_microusd_per_million_tokens": _positive_int_env(
            "MEMORY_V1_OPENAI_CACHE_WRITE_RATE_MICROUSD"
        ),
        "output_microusd_per_million_tokens": _positive_int_env(
            "MEMORY_V1_OPENAI_OUTPUT_RATE_MICROUSD"
        ),
    }


def maximum_request_cost(
    *, estimated_input_tokens: int, max_output_tokens: int, rates: dict[str, int]
) -> int:
    input_rate = max(
        rates["input_microusd_per_million_tokens"],
        rates["cached_input_microusd_per_million_tokens"],
        rates["cache_write_input_microusd_per_million_tokens"],
    )
    return (
        (estimated_input_tokens * input_rate + 999_999) // 1_000_000
        + (max_output_tokens * rates["output_microusd_per_million_tokens"] + 999_999)
        // 1_000_000
    )


def rejection_code(exc: BaseException) -> str:
    if isinstance(exc, StructuredTransportError):
        return exc.code
    if isinstance(exc, (ValueError, TypeError)):
        return "validator_rejected"
    if isinstance(exc, asyncpg.PostgresError):
        return "database_contract_rejected"
    return "worker_rejected"


def _rejected_packet_evidence(
    *,
    packet: ProviderPacket,
    exc: ValueError | TypeError,
    request_sha256: str,
    reservation_event_id: uuid.UUID,
) -> dict[str, Any]:
    if SHA256_RE.fullmatch(request_sha256) is None:
        raise RuntimeError("rejected packet request hash is invalid")
    packet_value = packet.model_dump(mode="json")
    canonical_packet = stable_json(packet_value)
    packet_bytes = len(canonical_packet.encode("utf-8"))
    capture_status = (
        "complete"
        if packet_bytes <= MAX_REJECTED_PACKET_EVIDENCE_BYTES
        else "oversize"
    )
    return {
        "capture_status": capture_status,
        "contract_version": REJECTED_PACKET_EVIDENCE_CONTRACT,
        "packet": packet_value if capture_status == "complete" else None,
        "packet_bytes": packet_bytes,
        "packet_canonicalization": "stable_json_utf8_v1",
        "packet_sha256": sha256_text(canonical_packet),
        "request_sha256": request_sha256,
        "reservation_event_id": str(reservation_event_id),
        "validator_error_class": (
            "validator_value_error"
            if isinstance(exc, ValueError)
            else "validator_type_error"
        ),
        "validator_error_sha256": sha256_text(str(exc)),
        "validator_rejection_code": classify_validator_rejection(str(exc)),
    }


def _validate_rejected_packet_evidence(
    evidence: dict[str, Any],
    *,
    request_sha256: str,
    reservation_event_id: uuid.UUID,
) -> None:
    if set(evidence) != {
        "capture_status",
        "contract_version",
        "packet",
        "packet_bytes",
        "packet_canonicalization",
        "packet_sha256",
        "request_sha256",
        "reservation_event_id",
        "validator_error_class",
        "validator_error_sha256",
        "validator_rejection_code",
    }:
        raise RuntimeError("rejected packet evidence shape is invalid")
    if (
        evidence["contract_version"] != REJECTED_PACKET_EVIDENCE_CONTRACT
        or evidence["request_sha256"] != request_sha256
        or evidence["reservation_event_id"] != str(reservation_event_id)
        or evidence["packet_canonicalization"] != "stable_json_utf8_v1"
        or evidence["validator_error_class"]
        not in {"validator_value_error", "validator_type_error"}
        or not isinstance(evidence["validator_rejection_code"], str)
        or not evidence["validator_rejection_code"]
        or not all(
            char in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for char in evidence["validator_rejection_code"]
        )
        or len(evidence["validator_rejection_code"]) > 100
        or SHA256_RE.fullmatch(str(evidence["packet_sha256"])) is None
        or SHA256_RE.fullmatch(str(evidence["validator_error_sha256"])) is None
        or type(evidence["packet_bytes"]) is not int
        or evidence["packet_bytes"] < 2
    ):
        raise RuntimeError("rejected packet evidence binding is invalid")
    if evidence["capture_status"] == "complete":
        if not isinstance(evidence["packet"], dict):
            raise RuntimeError("rejected packet evidence content is invalid")
        canonical_packet = stable_json(evidence["packet"])
        if (
            len(canonical_packet.encode("utf-8")) != evidence["packet_bytes"]
            or sha256_text(canonical_packet) != evidence["packet_sha256"]
            or evidence["packet_bytes"] > MAX_REJECTED_PACKET_EVIDENCE_BYTES
        ):
            raise RuntimeError("rejected packet evidence content is invalid")
    elif evidence["capture_status"] == "oversize":
        if (
            evidence["packet"] is not None
            or evidence["packet_bytes"] <= MAX_REJECTED_PACKET_EVIDENCE_BYTES
        ):
            raise RuntimeError("rejected packet oversize evidence is invalid")
    else:
        raise RuntimeError("rejected packet capture status is invalid")


def _completion_audit(
    *,
    outcome: str,
    request_sha256: str,
    reservation_event_id: uuid.UUID,
    budget: PostgresBudgetAuthorizerV1 | None,
    transport_audit: Any,
    error_code: str | None = None,
    rejected_packet_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    audit = {
        "contract_version": "memory_v1_openai_provider_completion_audit_v1",
        "error_code": error_code,
        "outcome": outcome,
        "request_sha256": request_sha256,
        "reservation_event_id": str(reservation_event_id),
        "settlement": (
            asdict(budget.settlement)
            if budget is not None and budget.settlement is not None
            else None
        ),
        "transport_audit": (
            transport_audit.public_dict() if transport_audit is not None else None
        ),
    }
    if rejected_packet_evidence is not None:
        if outcome != "rejected" or error_code != "validator_rejected":
            raise RuntimeError("rejected packet evidence outcome is invalid")
        _validate_rejected_packet_evidence(
            rejected_packet_evidence,
            request_sha256=request_sha256,
            reservation_event_id=reservation_event_id,
        )
        audit["rejected_packet_evidence"] = rejected_packet_evidence
    return audit


async def process_job(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    run_id: uuid.UUID,
    model: str,
    adapter: OpenAIV52ProviderAdapterV1,
    registry: dict[str, Any],
    schema: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[dict[str, Any], int]:
    reservation: dict[str, Any] | None = None
    budget: PostgresBudgetAuthorizerV1 | None = None
    prepared = None
    rejected_packet_evidence: dict[str, Any] | None = None
    provider_transport_audit = None
    external_calls = 0
    try:
        source = TrustedExtractionSource.create(
            job_id=job["job_id"],
            source_system=job["evidence_source_system"],
            source_external_id=job["evidence_external_id"],
            source_sha256=job["evidence_content_sha256"],
            source_recorded_at=job["evidence_recorded_at"],
            content=job["evidence_content"],
            source_observed_at=job["evidence_observed_at"],
        )
        prepared = adapter.prepare(
            owner_user_id=str(owner),
            source_text=source.content,
            operation_id=f"{run_id}:{job['job_id']}",
        )
        gate = classify_personal_evidence_v1(
            source.content,
            source_role=TRUSTED_SOURCE_ROLE,
        )
        if prepared is None and gate.decision == "skip_zero_call":
            exchange_gate = classify_personal_evidence_exchange_v2(
                source.content,
                source_role=TRUSTED_SOURCE_ROLE,
            )
            gate = exchange_gate
            if exchange_gate.decision == "send_external":
                prepared = adapter.prepare(
                    owner_user_id=str(owner),
                    source_text=source.content,
                    operation_id=f"{run_id}:{job['job_id']}",
                    exchange_eligibility=True,
                )
            elif exchange_gate.reason_codes in {
                ("context_binding_required",),
                ("personal_question_needs_context",),
            }:
                evidence_context = None
                try:
                    evidence_context = await load_memory_evidence_context_v2(
                        conn,
                        expected_owner_user_id=owner,
                        target_evidence_id=job["evidence_id"],
                        expected_target_content_sha256=(
                            job["evidence_content_sha256"]
                        ),
                    )
                except EvidenceContextContractError:
                    evidence_context = None
                if evidence_context is not None:
                    gate = classify_personal_evidence_exchange_v2(
                        source.content,
                        source_role=TRUSTED_SOURCE_ROLE,
                        evidence_context=evidence_context,
                    )
                    if gate.decision == "send_external":
                        prepared = adapter.prepare(
                            owner_user_id=str(owner),
                            source_text=source.content,
                            operation_id=f"{run_id}:{job['job_id']}",
                            exchange_eligibility=True,
                            evidence_context=evidence_context,
                        )
        eligibility_disposition = bind_eligibility_disposition_v2(
            owner=owner,
            evidence_id=job["evidence_id"],
            exact_job_id=job["job_id"],
            expected_content_sha256=job["evidence_content_sha256"],
            gate_receipt=content_free_disposition_receipt_v2(gate),
        )
        if prepared is None:
            disposition = await record_eligibility_disposition(
                conn,
                owner=owner,
                operation_id=uuid.uuid5(
                    PERSIST_NAMESPACE,
                    "openai-eligibility:"
                    f"{job['job_id']}:"
                    f"{eligibility_disposition['exchange_id']}:"
                    f"{eligibility_disposition['decision']}",
                ),
                run_id=run_id,
                exact_job_id=job["job_id"],
                expected_content_sha256=job["evidence_content_sha256"],
                worker_id=worker_id,
                provider_id=OPENAI_PROVIDER_ID,
                provider_version=PROVIDER_VERSION,
                provider_model_sha256=sha256_text(model),
                rolling_window_seconds=args.rolling_window_seconds,
                max_reserved_calls=args.max_reserved_calls,
                failure_threshold=args.failure_threshold,
                eligibility_disposition=eligibility_disposition,
            )
            return {
                "eligibility_disposition": content_free_disposition_receipt_v2(
                    gate
                ),
                "job_sha256": sha256_text(str(job["job_id"])),
                "status": str(disposition["status"]),
                "outcome": str(disposition["apply_outcome"]),
                "rejection_code": gate.reason_codes[0],
                "provider_reservation_created": False,
                "external_model_calls": 0,
            }, 0

        rates = pricing_rates()
        max_cost = maximum_request_cost(
            estimated_input_tokens=prepared.request.estimated_input_tokens,
            max_output_tokens=prepared.request.max_output_tokens,
            rates=rates,
        )
        max_request_cost = _positive_int_env("MEMORY_V1_OPENAI_MAX_REQUEST_MICROUSD")
        if max_cost > max_request_cost:
            skipped = await skip_job(
                conn,
                owner=owner,
                operation_id=uuid.uuid5(
                    PERSIST_NAMESPACE, f"openai-budget-skip:{job['job_id']}"
                ),
                run_id=run_id,
                exact_job_id=job["job_id"],
                expected_content_sha256=job["evidence_content_sha256"],
                worker_id=worker_id,
                provider_id=OPENAI_PROVIDER_ID,
                provider_version=PROVIDER_VERSION,
                provider_model_sha256=sha256_text(model),
                rolling_window_seconds=args.rolling_window_seconds,
                max_reserved_calls=args.max_reserved_calls,
                failure_threshold=args.failure_threshold,
                reason_code="request_budget_exceeded",
                gate_sha256=sha256_text(
                    stable_json(prepared.gate_result.public_dict())
                ),
                eligibility_disposition=eligibility_disposition,
            )
            return {
                "eligibility_disposition": content_free_disposition_receipt_v2(
                    prepared.gate_result
                ),
                "job_sha256": sha256_text(str(job["job_id"])),
                "status": str(skipped["status"]),
                "outcome": str(skipped["apply_outcome"]),
                "rejection_code": "request_budget_exceeded",
                "provider_reservation_created": False,
                "external_model_calls": 0,
            }, 0
        receipt = prepared.content_free_receipt()
        receipt.update(
            {
                "eligibility_disposition": eligibility_disposition,
                "evidence_content_sha256": job["evidence_content_sha256"],
                "job_id": str(job["job_id"]),
                "max_request_microusd": max_request_cost,
                "max_utc_day_microusd": _positive_int_env(
                    "MEMORY_V1_OPENAI_MAX_UTC_DAY_MICROUSD"
                ),
                "maximum_cost_microusd": max_cost,
                "pricing_rates": rates,
                "run_id": str(run_id),
                "worker_id_sha256": sha256_text(
                    "|".join(
                        (
                            worker_id,
                            "memory_v1_openai_exact_job_claim_v1",
                            str(job["job_id"]),
                            job["evidence_content_sha256"],
                        )
                    )
                ),
            }
        )
        reservation = await reserve_call(
            conn,
            owner=owner,
            operation_id=uuid.uuid5(
                PERSIST_NAMESPACE,
                f"openai-reserve:{run_id}:{job['job_id']}:{prepared.request.request_sha256}",
            ),
            run_id=run_id,
            exact_job_id=job["job_id"],
            expected_content_sha256=job["evidence_content_sha256"],
            worker_id=worker_id,
            provider_id=OPENAI_PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
            provider_model_sha256=sha256_text(model),
            rolling_window_seconds=args.rolling_window_seconds,
            max_reserved_calls=args.max_reserved_calls,
            failure_threshold=args.failure_threshold,
            receipt=receipt,
        )
        job = reservation
        durable = PostgresProviderReservationV1.from_request(
            reservation_id="r" + reservation["reservation_event_id"].hex,
            request=prepared.request,
            rates=rates,
            max_cost_microusd=max_cost,
            ttl_seconds=min(args.lease_seconds, 300),
        )
        budget = PostgresBudgetAuthorizerV1(durable)
        transport = OpenAIStructuredResponsesTransportV1(
            enable_token=os.getenv("MEMORY_V1_OPENAI_STRUCTURED_CALLS"),
            budget_authorizer=budget,
            privacy_authorizer=PostgresPrivacyAuthorizerV1(durable),
            task_profiles=(extraction_task_profile_v1(),),
            model_policies=(
                extraction_model_policy_v1(
                    model=args.model,
                    sdk_package_version=args.sdk_version,
                    max_output_tokens_ceiling=args.max_output_tokens,
                    timeout_seconds_ceiling=int(args.timeout_seconds),
                ),
            ),
        )
        result = adapter.execute_prepared(prepared, transport=transport)
        external_calls = result.external_model_calls
        provider_transport_audit = result.audit
        provider = _BoundPacketProvider(
            packet=result.packet,
            source_sha256=source.source_sha256,
            external_model_calls=external_calls,
        )
        try:
            validated = validate_and_normalize(
                provider,
                source=source,
                registry=registry,
                schema=schema,
                trusted_project_binding=None,
                allowed_provider_versions={OPENAI_PROVIDER_ID: PROVIDER_VERSION},
                max_external_model_calls=1,
            )
        except (ValueError, TypeError) as exc:
            rejected_packet_evidence = _rejected_packet_evidence(
                packet=result.packet,
                exc=exc,
                request_sha256=prepared.request.request_sha256,
                reservation_event_id=reservation["reservation_event_id"],
            )
            raise
        packet_id = uuid.uuid5(PERSIST_NAMESPACE, f"openai:{job['job_id']}")
        persisted = await persist_packet(
            conn,
            owner=owner,
            operation_id=uuid.uuid5(
                PERSIST_NAMESPACE, f"openai-persist:{job['job_id']}"
            ),
            packet_id=packet_id,
            job=job,
            worker_id=worker_id,
            model_sha256=sha256_text(model),
            validated=validated,
            binding_event_id=None,
        )
        report = {
            "eligibility_disposition": content_free_disposition_receipt_v2(
                prepared.gate_result
            ),
            "job_sha256": sha256_text(str(job["job_id"])),
            "status": str(persisted["status"]),
            "outcome": str(persisted["apply_outcome"]),
            "provider_output_sha256": validated.provider_output_sha256,
            "validator_packet_sha256": validated.normalized_packet_sha256,
            "packet_storage_sha256": str(persisted["packet_storage_sha256"]),
            "provider_reservation_created": True,
            "external_model_calls": external_calls,
            "counts": {
                "entity_mentions": len(validated.normalized_packet["entity_mentions"]),
                "observations": len(validated.normalized_packet["observations"]),
                "deferrals": len(validated.normalized_packet["deferrals"]),
            },
        }
        completion_audit = _completion_audit(
            outcome="accepted",
            request_sha256=prepared.request.request_sha256,
            reservation_event_id=reservation["reservation_event_id"],
            budget=budget,
            transport_audit=result.audit,
        )
        await complete_call(
            conn,
            owner=owner,
            operation_id=uuid.uuid5(
                PERSIST_NAMESPACE,
                f"openai-complete:{reservation['reservation_event_id']}",
            ),
            reservation_event_id=reservation["reservation_event_id"],
            run_id=run_id,
            job_id=job["job_id"],
            outcome="accepted",
            external_model_calls=external_calls,
            provider_output_sha256=report["provider_output_sha256"],
            validator_packet_sha256=report["validator_packet_sha256"],
            packet_storage_sha256=report["packet_storage_sha256"],
        )
        await finalize_receipt(
            conn,
            owner=owner,
            operation_id=uuid.uuid5(
                PERSIST_NAMESPACE,
                f"openai-finalize:{reservation['reservation_event_id']}",
            ),
            run_id=run_id,
            exact_job_id=job["job_id"],
            expected_content_sha256=job["evidence_content_sha256"],
            worker_id=worker_id,
            provider_id=OPENAI_PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
            provider_model_sha256=sha256_text(model),
            rolling_window_seconds=args.rolling_window_seconds,
            max_reserved_calls=args.max_reserved_calls,
            failure_threshold=args.failure_threshold,
            audit_receipt=completion_audit,
        )
        return report, external_calls
    except ProcessingRejected:
        raise
    except Exception as exc:
        transport_audit = getattr(exc, "audit", None) or provider_transport_audit
        if transport_audit is not None:
            external_calls = int(transport_audit.external_call_count)
        code = rejection_code(exc)
        if reservation is None:
            raise ProcessingRejected(code, external_calls) from exc
        if reservation is not None and prepared is not None:
            completion_audit = _completion_audit(
                outcome="rejected",
                request_sha256=prepared.request.request_sha256,
                reservation_event_id=reservation["reservation_event_id"],
                budget=budget,
                transport_audit=transport_audit,
                error_code=code,
                rejected_packet_evidence=rejected_packet_evidence,
            )
            await complete_call(
                conn,
                owner=owner,
                operation_id=uuid.uuid5(
                    PERSIST_NAMESPACE,
                    f"openai-complete:{reservation['reservation_event_id']}",
                ),
                reservation_event_id=reservation["reservation_event_id"],
                run_id=run_id,
                job_id=job["job_id"],
                outcome="rejected",
                external_model_calls=external_calls,
                rejection_code_value=code,
            )
            await finalize_receipt(
                conn,
                owner=owner,
                operation_id=uuid.uuid5(
                    PERSIST_NAMESPACE,
                    f"openai-finalize:{reservation['reservation_event_id']}",
                ),
                run_id=run_id,
                exact_job_id=job["job_id"],
                expected_content_sha256=job["evidence_content_sha256"],
                worker_id=worker_id,
                provider_id=OPENAI_PROVIDER_ID,
                provider_version=PROVIDER_VERSION,
                provider_model_sha256=sha256_text(model),
                rolling_window_seconds=args.rolling_window_seconds,
                max_reserved_calls=args.max_reserved_calls,
                failure_threshold=args.failure_threshold,
                audit_receipt=completion_audit,
            )
        failed = await fail_job(
            conn,
            owner=owner,
            operation_id=uuid.uuid5(
                PERSIST_NAMESPACE, f"openai-fail:{job['job_id']}:{job['attempts']}"
            ),
            job=job,
            worker_id=worker_id,
            code=code,
            max_attempts=args.max_attempts,
        )
        return {
            "eligibility_disposition": (
                content_free_disposition_receipt_v2(prepared.gate_result)
                if prepared is not None
                else None
            ),
            "job_sha256": sha256_text(str(job["job_id"])),
            "status": str(failed["status"]),
            "outcome": str(failed["apply_outcome"]),
            "rejection_code": code,
            "provider_reservation_created": reservation is not None,
            "external_model_calls": external_calls,
        }, external_calls


async def main() -> int:
    args = arguments()
    run_id = validate_arguments(args)
    owners = configured_owners(args)
    exact_job_id, expected_content_sha256 = configured_exact_target(args, owners)
    worker_id = worker_reference(args.worker_id, version=WORKER_VERSION)
    registry = load_registry(Path(args.registry), EXPECTED_REGISTRY_SHA256)
    schema = load_schema(
        Path(args.schema),
        EXPECTED_SCHEMA_SHA256,
        expected_contract_version=CONTRACT_VERSION_V5_2,
    )
    conn = await asyncpg.connect(
        _required_env("POSTGRES_DSN"), command_timeout=args.lease_seconds
    )
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("OpenAI extraction requires brains_app session")
        plans = [
            await plan_owner(conn, owner, exact_job_id=exact_job_id)
            for owner in owners
        ]
        if not args.apply:
            print(
                stable_json(
                    {
                        "worker_version": WORKER_VERSION,
                        "apply": False,
                        "owner_count": len(owners),
                        "plans": plans,
                        "external_model_calls": 0,
                    }
                )
            )
            return 0
        assert exact_job_id is not None and expected_content_sha256 is not None
        adapter = build_adapter(args)
        owner = owners[0]
        job = await probe_job(
            conn,
            owner=owner,
            operation_id=uuid.uuid5(
                run_id,
                f"probe-exact:{owner}:{exact_job_id}:{expected_content_sha256}",
            ),
            run_id=run_id,
            worker_id=worker_id,
            exact_job_id=exact_job_id,
            expected_content_sha256=expected_content_sha256,
            provider_id=OPENAI_PROVIDER_ID,
            provider_version=PROVIDER_VERSION,
            provider_model_sha256=sha256_text(args.model),
            rolling_window_seconds=args.rolling_window_seconds,
            max_reserved_calls=args.max_reserved_calls,
            failure_threshold=args.failure_threshold,
        )
        if job is None:
            results: list[dict[str, Any]] = []
            total_calls = 0
        else:
            if (
                job["job_id"] != exact_job_id
                or job["evidence_content_sha256"] != expected_content_sha256
            ):
                raise RuntimeError("database returned a job outside the exact target")
            result, total_calls = await process_job(
                conn,
                owner=owner,
                job=job,
                worker_id=worker_id,
                run_id=run_id,
                model=args.model,
                adapter=adapter,
                registry=registry,
                schema=schema,
                args=args,
            )
            results = [result]
        if total_calls > 1:
            raise RuntimeError("worker exceeded the one-call pilot ceiling")
        print(
            stable_json(
                {
                    "worker_version": WORKER_VERSION,
                    "apply": True,
                    "model_sha256": sha256_text(args.model),
                    "owner_count": len(owners),
                    "processed": len(results),
                    "external_model_calls": total_calls,
                    "results": results,
                }
            )
        )
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
