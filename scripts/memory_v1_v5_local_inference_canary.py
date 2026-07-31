#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import asyncpg

from rag_engine.memory_v1_evidence_context_loader_v2 import (
    load_memory_evidence_context_v2,
)
from rag_engine.memory_v1_evidence_context_v1 import (
    EvidenceContextContractError,
)
from rag_engine.memory_v1_evidence_context_v2 import (
    MemoryEvidenceContextEnvelopeV2,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LOCAL_CALL_ENABLE_TOKEN,
    LOCAL_POLICY_COMPILER_VERSION,
    LOCAL_PROVIDER_ID,
    LOCAL_PROVIDER_VERSION,
    RELATIONSHIP_V5_1_POLICY_COMPILER_VERSION,
    SEMANTIC_V5_2_POLICY_COMPILER_VERSION,
    LlamaCppSecureTransport,
    LocalLlamaCppProvider,
    LocalProviderAdapterError,
)
from scripts.memory_v1_relational_extraction_v5_observable_provider import (
    CapturingProvider,
    classify_validator_rejection,
    sanitize_provider_packet,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
    canonical_sha256,
    load_registry,
    load_schema,
    validate_and_normalize,
)
from scripts.memory_v1_predicate_runtime_profile_v2 import (
    PROFILE_NAMES,
    load_runtime_profile_v2,
)
from scripts.memory_v1_v5_local_outcome_policy import classify_rejection_code


WORKER_VERSION = "memory_v1_v5_local_inference_canary_v1"
APPLY_ENABLE_TOKEN = "memory_v1_v5_local_inference_canary_apply_v1"
DIAGNOSTIC_ENABLE_TOKEN = (
    "memory_v1_v5_local_inference_diagnostic_replay_v1"
)
PERSIST_NAMESPACE = uuid.UUID("a1ab4c90-2b6a-4a4b-a8e9-746c03917721")
PINNED_MODEL_FILE_SHA256 = (
    "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0"
)
PINNED_RUNTIME_REVISION = "llama.cpp-b10066-86a9c79f8"
PINNED_MODEL_ALIAS = "qwen3-14b-local-extractor"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SELECTOR_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,63}$")
REJECTION_RE = re.compile(r"^[a-z][a-z0-9_]{1,99}$")


class EvidenceContextBoundLocalProvider:
    """Bind one owner-scoped context envelope to one provider invocation."""

    def __init__(
        self,
        delegate: LocalLlamaCppProvider,
        evidence_context: MemoryEvidenceContextEnvelopeV2,
    ) -> None:
        self._delegate = delegate
        self._evidence_context = evidence_context

    @property
    def provider_id(self) -> str:
        return self._delegate.provider_id

    @property
    def provider_version(self) -> str:
        return self._delegate.provider_version

    @property
    def external_model_calls(self) -> int:
        return int(self._delegate.external_model_calls)

    @property
    def external_call_capability(self) -> bool:
        return bool(self._delegate.external_call_capability)

    def extract(self, source: TrustedExtractionSource) -> ProviderPacket:
        return self._delegate.extract(
            source,
            evidence_context=self._evidence_context,
        )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_source_external_id(
    value: Any,
    *,
    evidence_id: uuid.UUID,
) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return str(evidence_id)


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def runtime_profile_audit(profile: Any) -> dict[str, str]:
    return {
        "predicate_contract_profile": profile.name,
        "extraction_contract_version": profile.contract_version,
        "predicate_registry_version": profile.registry_version,
    }


def effective_policy_compiler_version(profile: Any) -> str:
    if profile.name == "v5":
        return LOCAL_POLICY_COMPILER_VERSION
    if profile.name == "v5_1":
        return RELATIONSHIP_V5_1_POLICY_COMPILER_VERSION
    if profile.name == "v5_2":
        return SEMANTIC_V5_2_POLICY_COMPILER_VERSION
    raise RuntimeError("unsupported local inference contract profile")


def effective_policy_compiler_sha256(profile: Any) -> str:
    version = effective_policy_compiler_version(profile)
    if profile.name in {"v5", "v5_1"}:
        return canonical_sha256(version)
    return sha256_text(version)


def loopback_dsn(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("local canary database DSN scheme is invalid")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("local canary database DSN must be loopback-only")
    return value


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute one exact, owner-scoped Memory V1 local "
            "inference canary. Reports never include source or packet prose."
        )
    )
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--evidence-id", required=True)
    parser.add_argument("--expected-job-id")
    parser.add_argument("--expected-content-sha256", required=True)
    parser.add_argument("--selector-version", default="20260718_local_v1")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--endpoint", default="http://127.0.0.1:18080/v1/chat/completions"
    )
    parser.add_argument("--model", default=PINNED_MODEL_ALIAS)
    parser.add_argument(
        "--model-file-sha256", default=PINNED_MODEL_FILE_SHA256
    )
    parser.add_argument("--runtime-revision", default=PINNED_RUNTIME_REVISION)
    parser.add_argument(
        "--contract-profile",
        choices=sorted(PROFILE_NAMES),
        default="v5",
    )
    parser.add_argument("--worker-id")
    parser.add_argument("--lease-seconds", type=int, default=900)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--rolling-window-seconds", type=int, default=86400)
    parser.add_argument("--max-reserved-jobs", type=int, default=1)
    parser.add_argument("--failure-threshold", type=int, default=1)
    parser.add_argument("--diagnostic-replay", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def validated_arguments(args: argparse.Namespace) -> dict[str, Any]:
    try:
        owner = uuid.UUID(str(args.owner_user_id))
        evidence_id = uuid.UUID(str(args.evidence_id))
        expected_job_id = (
            uuid.UUID(str(args.expected_job_id))
            if args.expected_job_id
            else None
        )
        run_id = uuid.UUID(str(args.run_id)) if args.run_id else None
    except ValueError as exc:
        raise RuntimeError("owner, evidence, and run ids must be UUIDs") from exc
    if not SHA256_RE.fullmatch(str(args.expected_content_sha256)):
        raise RuntimeError("expected content hash must be lowercase SHA-256")
    if not SELECTOR_RE.fullmatch(str(args.selector_version)):
        raise RuntimeError("selector version is invalid")
    if not SHA256_RE.fullmatch(str(args.model_file_sha256)):
        raise RuntimeError("model file hash must be lowercase SHA-256")
    if not 30 <= args.lease_seconds <= 3600:
        raise RuntimeError("lease seconds must be between 30 and 3600")
    if not 1 <= args.max_attempts <= 20:
        raise RuntimeError("max attempts must be between 1 and 20")
    if not 1.0 <= args.timeout_seconds <= 600.0:
        raise RuntimeError("timeout seconds must be between 1 and 600")
    if not 1000 <= args.max_output_tokens <= 20000:
        raise RuntimeError("max output tokens must be between 1000 and 20000")
    if not 3600 <= args.rolling_window_seconds <= 604800:
        raise RuntimeError("rolling window must be between 3600 and 604800")
    if not 1 <= args.max_reserved_jobs <= 100:
        raise RuntimeError("max reserved jobs must be between 1 and 100")
    if not 1 <= args.failure_threshold <= 10:
        raise RuntimeError("failure threshold must be between 1 and 10")
    if args.apply and run_id is None:
        raise RuntimeError("--apply requires --run-id")
    if args.apply and os.getenv("MEMORY_V1_V5_LOCAL_INFERENCE_APPLY") != (
        APPLY_ENABLE_TOKEN
    ):
        raise RuntimeError("local inference apply capability is absent")
    if args.diagnostic_replay and not args.apply:
        raise RuntimeError("--diagnostic-replay requires --apply")
    if args.diagnostic_replay and os.getenv(
        "MEMORY_V1_V5_LOCAL_INFERENCE_DIAGNOSTIC"
    ) != DIAGNOSTIC_ENABLE_TOKEN:
        raise RuntimeError(
            "local inference diagnostic replay capability is absent"
        )
    return {
        "owner": owner,
        "evidence_id": evidence_id,
        "expected_job_id": expected_job_id,
        "run_id": run_id,
    }


def worker_reference(value: str | None) -> str:
    candidate = value or f"{socket.gethostname()}:{WORKER_VERSION}"
    if not candidate.strip() or len(candidate) > 500:
        raise RuntimeError("worker id is invalid")
    return candidate


async def set_actor(conn: asyncpg.Connection, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def plan_exact(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    evidence_id: uuid.UUID,
    selector_version: str,
    expected_content_sha256: str,
) -> dict[str, Any]:
    async with conn.transaction(readonly=True):
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT *
            FROM memory.plan_owner_evidence_intake_v1($1,1,$2)
            """,
            selector_version,
            evidence_id,
        )
    if row is None:
        raise RuntimeError("exact evidence has no intake plan")
    value = dict(row)
    if (
        value.get("evidence_id") != evidence_id
        or value.get("evidence_content_sha256") != expected_content_sha256
        or value.get("outcome") != "eligible"
        or value.get("route") != "relational_extraction"
        or value.get("reason_code") != "eligible_unprocessed"
    ):
        raise RuntimeError("exact evidence is not eligible for local extraction")
    return {
        "evidence_id_sha256": sha256_text(str(evidence_id)),
        "evidence_content_sha256": expected_content_sha256,
        "route": "relational_extraction",
        "outcome": "eligible",
    }


async def enqueue_exact(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    evidence_id: uuid.UUID,
    selector_version: str,
    expected_content_sha256: str,
) -> dict[str, Any]:
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.enqueue_owner_evidence_extraction_v1(
              $1,$2,$3,'relational_extraction','eligible_unprocessed'
            )
            """,
            evidence_id,
            selector_version,
            expected_content_sha256,
        )
    if row is None:
        raise RuntimeError("exact evidence enqueue returned no row")
    return dict(row)


async def claim_exact(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    run_id: uuid.UUID,
    job_id: uuid.UUID,
    expected_content_sha256: str,
    worker_id: str,
    args: argparse.Namespace,
    profile: Any,
) -> dict[str, Any]:
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.claim_owner_v5_local_inference_job_v1(
              $1,$2,$3,$4,'relational_extraction',$5,$6,$7,
              $8,$9,$10,$11,$12,$13,$14,$15,$16
            )
            """,
            operation_id,
            run_id,
            job_id,
            expected_content_sha256,
            worker_id,
            args.lease_seconds,
            args.max_attempts,
            LOCAL_PROVIDER_ID,
            LOCAL_PROVIDER_VERSION,
            canonical_sha256(args.model),
            args.model_file_sha256,
            canonical_sha256(args.runtime_revision),
            effective_policy_compiler_sha256(profile),
            args.rolling_window_seconds,
            args.max_reserved_jobs,
            args.failure_threshold,
        )
    if row is None:
        raise RuntimeError("exact local inference claim returned no row")
    return dict(row)


async def persist_packet(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    packet_id: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    args: argparse.Namespace,
    profile: Any,
    validated: Any,
    local_model_calls: int,
) -> dict[str, Any]:
    if profile.name == "v5":
        persist_sql = """
            SELECT * FROM memory.persist_owner_v5_local_packet_v1(
              $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,
              $15,$16
            )
            """
    elif profile.name == "v5_1":
        persist_sql = """
            SELECT * FROM memory.persist_owner_v5_1_local_packet_v1(
              $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,
              $15,$16
            )
            """
    elif profile.name == "v5_2":
        persist_sql = """
            SELECT * FROM memory.persist_owner_v5_2_local_packet_v1(
              $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,
              $15,$16
            )
            """
    else:
        raise RuntimeError("unsupported local inference contract profile")
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            persist_sql,
            operation_id,
            packet_id,
            job["job_id"],
            job["lease_token"],
            worker_id,
            job["evidence_content_sha256"],
            LOCAL_PROVIDER_VERSION,
            canonical_sha256(args.model),
            args.model_file_sha256,
            canonical_sha256(args.runtime_revision),
            effective_policy_compiler_sha256(profile),
            validated.provider_output_sha256,
            validated.normalized_packet_sha256,
            stable_json(validated.normalized_packet),
            validated.manual_review_required,
            local_model_calls,
        )
    if row is None:
        raise RuntimeError("local V5 packet persistence returned no row")
    return dict(row)


async def complete(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    reservation_event_id: uuid.UUID,
    run_id: uuid.UUID,
    job_id: uuid.UUID,
    outcome: str,
    local_model_calls: int,
    rejection_code: str | None,
    provider_output_sha256: str | None,
    validator_packet_sha256: str | None,
    packet_storage_sha256: str | None,
) -> dict[str, Any]:
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.complete_owner_v5_local_inference_v1(
              $1,$2,$3,$4,$5,$6,$7,$8,$9,$10
            )
            """,
            operation_id,
            reservation_event_id,
            run_id,
            job_id,
            outcome,
            local_model_calls,
            rejection_code,
            provider_output_sha256,
            validator_packet_sha256,
            packet_storage_sha256,
        )
    if row is None:
        raise RuntimeError("local inference completion returned no row")
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
            "local_inference_rejected",
            code,
            max_attempts,
        )
    if row is None:
        raise RuntimeError("local inference job failure returned no row")
    return dict(row)


async def finalize_record_outcome(
    conn: asyncpg.Connection,
    *,
    owner: uuid.UUID,
    operation_id: uuid.UUID,
    job: dict[str, Any],
    worker_id: str,
    completion_event_id: uuid.UUID,
    code: str,
) -> dict[str, Any]:
    async with conn.transaction():
        await set_actor(conn, owner)
        row = await conn.fetchrow(
            """
            SELECT * FROM memory.finalize_owner_v5_local_record_outcome_v2(
              $1,$2,$3,$4,$5,$6,$7
            )
            """,
            operation_id,
            job["job_id"],
            job["lease_token"],
            worker_id,
            job["evidence_content_sha256"],
            completion_event_id,
            code,
        )
    if row is None:
        raise RuntimeError("local inference record outcome returned no row")
    return dict(row)


def rejection_code(
    exc: BaseException,
    *,
    phase: str = "validation",
    audit: Mapping[str, Any] | None = None,
) -> str:
    if isinstance(exc, LocalProviderAdapterError) and REJECTION_RE.fullmatch(
        exc.code
    ):
        if exc.code == "invalid_structured_output" and audit is not None:
            error_types = {
                str(value)
                for value in audit.get("validation_error_types", ())
                if isinstance(value, str)
            }
            if "missing" in error_types:
                return "local_structured_required_field_missing"
            if "extra_forbidden" in error_types:
                return "local_structured_extra_field"
            if error_types & {
                "enum",
                "literal_error",
                "literal_mismatch",
            }:
                return "local_structured_enum_invalid"
            if error_types:
                return "local_structured_type_invalid"
        return exc.code
    if phase == "persistence":
        return "local_persistence_rejected"
    if phase == "completion":
        return "local_completion_rejected"
    if isinstance(exc, EvidenceContextContractError):
        if str(exc) == "database actor differs from authenticated owner":
            return "local_owner_scope_violation"
        if str(exc) in {
            "loader owner and target must be UUIDs",
            "max_spans must be between 1 and 32",
        }:
            return "local_validation_internal_error"
        return "context_missing"
    if isinstance(exc, ValueError):
        return classify_validator_rejection(str(exc))
    return "local_validation_internal_error"


def sanitized_audit(provider: LocalLlamaCppProvider) -> dict[str, Any] | None:
    if provider.last_audit is None:
        return None
    allowed = {
        "provider_id",
        "provider_version",
        "model_sha256",
        "model_file_sha256",
        "runtime_revision_sha256",
        "request_sha256",
        "output_schema_sha256",
        "response_status",
        "response_sha256",
        "prompt_tokens",
        "completion_tokens",
        "finish_reason",
        "error_code",
        "prompt_profile",
        "policy_compiler_version",
        "policy_guard_code",
        "compiler_repairs",
        "compiled_packet_sha256",
        "validation_exception_class",
        "validation_error_count",
        "validation_error_types",
        "validation_error_locations",
        "evidence_context_contract_version",
        "evidence_context_envelope_sha256",
        "evidence_context_span_count",
        "evidence_context_assertion_origin_count",
        "evidence_context_coreference_version",
    }
    return {key: value for key, value in provider.last_audit.items() if key in allowed}


async def run() -> int:
    args = arguments()
    ids = validated_arguments(args)
    worker_id = worker_reference(args.worker_id)
    root = Path(__file__).resolve().parents[1]
    profile = load_runtime_profile_v2(root, args.contract_profile)
    if args.apply and profile.lifecycle == "offline_review_only":
        raise RuntimeError("review-only predicate profile cannot persist packets")
    registry = load_registry(
        profile.registry_path,
        profile.registry_artifact_sha256,
    )
    schema = load_schema(
        profile.schema_path,
        profile.schema_artifact_sha256,
        expected_contract_version=profile.contract_version,
    )
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(
        loopback_dsn(dsn), command_timeout=args.lease_seconds, ssl=False
    )
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("local inference canary requires brains_app session")
        if ids["expected_job_id"] is None:
            plan = await plan_exact(
                conn,
                owner=ids["owner"],
                evidence_id=ids["evidence_id"],
                selector_version=args.selector_version,
                expected_content_sha256=args.expected_content_sha256,
            )
        else:
            plan = {
                "evidence_id_sha256": sha256_text(str(ids["evidence_id"])),
                "job_id_sha256": sha256_text(str(ids["expected_job_id"])),
                "evidence_content_sha256": args.expected_content_sha256,
                "route": "relational_extraction",
                "outcome": "exact_pending_job_supplied",
            }
        if not args.apply:
            print(
                stable_json(
                    {
                        "contract_version": WORKER_VERSION,
                        **runtime_profile_audit(profile),
                        "apply": False,
                        "plan": plan,
                        "external_model_calls": 0,
                        "local_model_calls": 0,
                        "write_counts": {
                            "queue": 0,
                            "ledger": 0,
                            "packets": 0,
                            "claims": 0,
                            "qdrant": 0,
                            "prompt_influence": 0,
                        },
                    }
                )
            )
            return 0

        run_id = ids["run_id"]
        if run_id is None:
            raise AssertionError("validated apply run lost its run id")
        if ids["expected_job_id"] is None:
            enqueued = await enqueue_exact(
                conn,
                owner=ids["owner"],
                evidence_id=ids["evidence_id"],
                selector_version=args.selector_version,
                expected_content_sha256=args.expected_content_sha256,
            )
            job_id = enqueued["job_id"]
        else:
            job_id = ids["expected_job_id"]
            enqueued = {"job_id": job_id, "apply_outcome": "replayed"}
        claim_operation_id = uuid.uuid5(
            PERSIST_NAMESPACE, f"claim:{run_id}:{job_id}"
        )
        claim = await claim_exact(
            conn,
            owner=ids["owner"],
            operation_id=claim_operation_id,
            run_id=run_id,
            job_id=job_id,
            expected_content_sha256=args.expected_content_sha256,
            worker_id=worker_id,
            args=args,
            profile=profile,
        )
        if (
            claim["control_outcome"] == "reserved"
            and claim.get("evidence_id") != ids["evidence_id"]
        ):
            raise RuntimeError("exact local claim evidence id changed")
        if args.diagnostic_replay and (
            enqueued["apply_outcome"] != "replayed"
            or claim["apply_outcome"] != "replayed"
        ):
            raise RuntimeError(
                "diagnostic mode requires a zero-write claim replay"
            )
        if claim["control_outcome"] != "reserved":
            print(
                stable_json(
                    {
                        "contract_version": WORKER_VERSION,
                        **runtime_profile_audit(profile),
                        "apply": True,
                        "outcome": claim["control_outcome"],
                        "external_model_calls": 0,
                        "local_model_calls": 0,
                        "write_counts": {
                            "queue": int(enqueued["apply_outcome"] == "applied"),
                            "ledger": int(claim["apply_outcome"] == "applied"),
                            "packets": 0,
                            "claims": 0,
                            "qdrant": 0,
                            "prompt_influence": 0,
                        },
                    }
                )
            )
            return 0

        source = TrustedExtractionSource.create(
            job_id=claim["job_id"],
            source_system=claim["evidence_source_system"],
            source_external_id=canonical_source_external_id(
                claim["evidence_external_id"],
                evidence_id=claim["evidence_id"],
            ),
            source_sha256=claim["evidence_content_sha256"],
            source_recorded_at=claim["evidence_recorded_at"],
            content=claim["evidence_content"],
            source_observed_at=claim["evidence_observed_at"],
        )
        api_key = os.getenv("MEMORY_V1_LOCAL_INFERENCE_API_KEY")
        transport = LlamaCppSecureTransport(
            endpoint=args.endpoint,
            enable_token=LOCAL_CALL_ENABLE_TOKEN,
            api_key=api_key,
            allow_loopback_http=True,
        )
        provider = LocalLlamaCppProvider(
            model=args.model,
            model_file_sha256=args.model_file_sha256,
            runtime_revision=args.runtime_revision,
            registry=registry,
            transport=transport,
            max_output_tokens=args.max_output_tokens,
            timeout_seconds=args.timeout_seconds,
        )
        phase = "validation"
        capturing = None
        try:
            evidence_context = (
                await load_memory_evidence_context_v2(
                    conn,
                    expected_owner_user_id=ids["owner"],
                    target_evidence_id=ids["evidence_id"],
                    expected_target_content_sha256=(
                        args.expected_content_sha256
                    ),
                )
                if profile.name == "v5_2"
                else None
            )
            validation_provider = (
                EvidenceContextBoundLocalProvider(
                    provider,
                    evidence_context,
                )
                if evidence_context is not None
                else provider
            )
            capturing = (
                CapturingProvider(validation_provider)
                if args.diagnostic_replay
                else None
            )
            validated = validate_and_normalize(
                capturing if capturing is not None else validation_provider,
                source=source,
                registry=registry,
                schema=schema,
                allowed_provider_versions={
                    LOCAL_PROVIDER_ID: LOCAL_PROVIDER_VERSION
                },
                max_external_model_calls=0,
            )
            local_calls = provider.local_model_calls
            if provider.external_model_calls != 0 or local_calls not in {0, 1}:
                raise RuntimeError("local/external call accounting changed")

            if args.diagnostic_replay:
                packet = (
                    capturing.last_packet
                    if capturing is not None
                    else None
                )
                if packet is None:
                    raise RuntimeError(
                        "diagnostic replay lost the provider packet"
                    )
                print(
                    stable_json(
                        {
                            "contract_version": (
                                "memory_v1_v5_local_diagnostic_replay_v1"
                            ),
                            **runtime_profile_audit(profile),
                            "apply": False,
                            "outcome": "accepted",
                            "external_model_calls": (
                                provider.external_model_calls
                            ),
                            "local_model_calls": provider.local_model_calls,
                            "provider_output_sha256": (
                                validated.provider_output_sha256
                            ),
                            "validator_packet_sha256": (
                                validated.normalized_packet_sha256
                            ),
                            "manual_review_required": (
                                validated.manual_review_required
                            ),
                            "sanitized_provider_packet": (
                                sanitize_provider_packet(packet)
                            ),
                            "audit": sanitized_audit(provider),
                            "claim_replay_proved": True,
                            "write_counts": {
                                "queue": 0,
                                "ledger": 0,
                                "packets": 0,
                                "claims": 0,
                                "qdrant": 0,
                                "prompt_influence": 0,
                            },
                        }
                    )
                )
                return 0

            packet_id = uuid.uuid5(PERSIST_NAMESPACE, f"packet:{job_id}")
            persist_operation_id = uuid.uuid5(
                PERSIST_NAMESPACE, f"persist:{run_id}:{job_id}"
            )
            completion_operation_id = uuid.uuid5(
                PERSIST_NAMESPACE, f"complete:{run_id}:{job_id}"
            )
            async with conn.transaction():
                phase = "persistence"
                persisted = await persist_packet(
                    conn,
                    owner=ids["owner"],
                    operation_id=persist_operation_id,
                    packet_id=packet_id,
                    job=claim,
                    worker_id=worker_id,
                    args=args,
                    profile=profile,
                    validated=validated,
                    local_model_calls=local_calls,
                )
                phase = "completion"
                completed = await complete(
                    conn,
                    owner=ids["owner"],
                    operation_id=completion_operation_id,
                    reservation_event_id=claim["reservation_event_id"],
                    run_id=run_id,
                    job_id=job_id,
                    outcome="accepted",
                    local_model_calls=local_calls,
                    rejection_code=None,
                    provider_output_sha256=validated.provider_output_sha256,
                    validator_packet_sha256=validated.normalized_packet_sha256,
                    packet_storage_sha256=persisted["packet_storage_sha256"],
                )
        except Exception as exc:
            if args.diagnostic_replay:
                if isinstance(exc, LocalProviderAdapterError):
                    diagnostic_code = exc.code
                elif isinstance(exc, ValueError):
                    diagnostic_code = classify_validator_rejection(str(exc))
                else:
                    diagnostic_code = "uncatalogued_diagnostic_error"
                packet = (
                    capturing.last_packet
                    if capturing is not None
                    else None
                )
                print(
                    stable_json(
                        {
                            "contract_version": (
                                "memory_v1_v5_local_diagnostic_replay_v1"
                            ),
                            **runtime_profile_audit(profile),
                            "apply": False,
                            "outcome": "rejected",
                            "rejection_code": diagnostic_code,
                            "rejection_class": type(exc).__name__,
                            "rejection_message_sha256": sha256_text(str(exc)),
                            "external_model_calls": (
                                provider.external_model_calls
                            ),
                            "local_model_calls": provider.local_model_calls,
                            "sanitized_provider_packet": (
                                sanitize_provider_packet(packet)
                                if packet is not None
                                else None
                            ),
                            "audit": sanitized_audit(provider),
                            "claim_replay_proved": True,
                            "write_counts": {
                                "queue": 0,
                                "ledger": 0,
                                "packets": 0,
                                "claims": 0,
                                "qdrant": 0,
                                "prompt_influence": 0,
                            },
                        }
                    )
                )
                return 1
            local_calls = provider.local_model_calls
            code = rejection_code(
                exc,
                phase=phase,
                audit=provider.last_audit,
            )
            policy = classify_rejection_code(code)
            terminal_operation_id = uuid.uuid5(
                PERSIST_NAMESPACE, f"terminal:{run_id}:{job_id}"
            )
            completion_operation_id = uuid.uuid5(
                PERSIST_NAMESPACE, f"complete:{run_id}:{job_id}"
            )
            async with conn.transaction():
                if policy.outcome_class == "record_terminal":
                    completed = await complete(
                        conn,
                        owner=ids["owner"],
                        operation_id=completion_operation_id,
                        reservation_event_id=claim["reservation_event_id"],
                        run_id=run_id,
                        job_id=job_id,
                        outcome="rejected",
                        local_model_calls=local_calls,
                        rejection_code=code,
                        provider_output_sha256=None,
                        validator_packet_sha256=None,
                        packet_storage_sha256=None,
                    )
                    terminal = await finalize_record_outcome(
                        conn,
                        owner=ids["owner"],
                        operation_id=terminal_operation_id,
                        job=claim,
                        worker_id=worker_id,
                        completion_event_id=completed["event_id"],
                        code=code,
                    )
                else:
                    terminal = await fail_job(
                        conn,
                        owner=ids["owner"],
                        operation_id=terminal_operation_id,
                        job=claim,
                        worker_id=worker_id,
                        code=code,
                        max_attempts=args.max_attempts,
                    )
                    completed = await complete(
                        conn,
                        owner=ids["owner"],
                        operation_id=completion_operation_id,
                        reservation_event_id=claim["reservation_event_id"],
                        run_id=run_id,
                        job_id=job_id,
                        outcome="rejected",
                        local_model_calls=local_calls,
                        rejection_code=code,
                        provider_output_sha256=None,
                        validator_packet_sha256=None,
                        packet_storage_sha256=None,
                    )
            completed_replay = await complete(
                conn,
                owner=ids["owner"],
                operation_id=completion_operation_id,
                reservation_event_id=claim["reservation_event_id"],
                run_id=run_id,
                job_id=job_id,
                outcome="rejected",
                local_model_calls=local_calls,
                rejection_code=code,
                provider_output_sha256=None,
                validator_packet_sha256=None,
                packet_storage_sha256=None,
            )
            if policy.outcome_class == "record_terminal":
                terminal_replay = await finalize_record_outcome(
                    conn,
                    owner=ids["owner"],
                    operation_id=terminal_operation_id,
                    job=claim,
                    worker_id=worker_id,
                    completion_event_id=completed["event_id"],
                    code=code,
                )
            else:
                terminal_replay = await fail_job(
                    conn,
                    owner=ids["owner"],
                    operation_id=terminal_operation_id,
                    job=claim,
                    worker_id=worker_id,
                    code=code,
                    max_attempts=args.max_attempts,
                )
            if (
                terminal_replay["apply_outcome"] != "replayed"
                or completed_replay["apply_outcome"] != "replayed"
            ):
                raise RuntimeError("rejected canary replay was not zero-write")
            print(
                stable_json(
                    {
                        "contract_version": WORKER_VERSION,
                        **runtime_profile_audit(profile),
                        "apply": True,
                        "outcome": "rejected",
                        "rejection_code": code,
                        "outcome_class": policy.outcome_class,
                        "terminal_disposition": policy.disposition,
                        "job_status": terminal["status"],
                        "external_model_calls": 0,
                        "local_model_calls": local_calls,
                        "audit": sanitized_audit(provider),
                        "zero_write_replay_proved": True,
                        "completion_apply_outcome": completed["apply_outcome"],
                        "write_counts": {
                            "queue": 3,
                            "ledger": 2,
                            "packets": 0,
                            "claims": 0,
                            "qdrant": 0,
                            "prompt_influence": 0,
                        },
                    }
                )
            )
            return 1

        if ids["expected_job_id"] is None:
            enqueued_replay = await enqueue_exact(
                conn,
                owner=ids["owner"],
                evidence_id=ids["evidence_id"],
                selector_version=args.selector_version,
                expected_content_sha256=args.expected_content_sha256,
            )
        else:
            enqueued_replay = {
                "job_id": job_id,
                "apply_outcome": "replayed",
            }
        claim_replay = await claim_exact(
            conn,
            owner=ids["owner"],
            operation_id=claim_operation_id,
            run_id=run_id,
            job_id=job_id,
            expected_content_sha256=args.expected_content_sha256,
            worker_id=worker_id,
            args=args,
            profile=profile,
        )
        persisted_replay = await persist_packet(
            conn,
            owner=ids["owner"],
            operation_id=persist_operation_id,
            packet_id=packet_id,
            job=claim,
            worker_id=worker_id,
            args=args,
            profile=profile,
            validated=validated,
            local_model_calls=local_calls,
        )
        completed_replay = await complete(
            conn,
            owner=ids["owner"],
            operation_id=completion_operation_id,
            reservation_event_id=claim["reservation_event_id"],
            run_id=run_id,
            job_id=job_id,
            outcome="accepted",
            local_model_calls=local_calls,
            rejection_code=None,
            provider_output_sha256=validated.provider_output_sha256,
            validator_packet_sha256=validated.normalized_packet_sha256,
            packet_storage_sha256=persisted["packet_storage_sha256"],
        )
        if any(
            item["apply_outcome"] != "replayed"
            for item in (
                enqueued_replay,
                claim_replay,
                persisted_replay,
                completed_replay,
            )
        ):
            raise RuntimeError("accepted canary replay was not zero-write")
        invocation_zero_write = all(
            item["apply_outcome"] == "replayed"
            for item in (enqueued, claim, persisted, completed)
        )

        print(
            stable_json(
                {
                    "contract_version": WORKER_VERSION,
                    **runtime_profile_audit(profile),
                    "apply": True,
                    "outcome": "accepted",
                    "owner_user_id_sha256": sha256_text(str(ids["owner"])),
                    "job_id_sha256": sha256_text(str(job_id)),
                    "provider_id": validated.provider_id,
                    "provider_version": validated.provider_version,
                    "provider_output_sha256": validated.provider_output_sha256,
                    "validator_packet_sha256": validated.normalized_packet_sha256,
                    "packet_storage_sha256": persisted["packet_storage_sha256"],
                    "manual_review_required": validated.manual_review_required,
                    "counts": {
                        "entity_mentions": len(
                            validated.normalized_packet["entity_mentions"]
                        ),
                        "observations": len(
                            validated.normalized_packet["observations"]
                        ),
                        "comparison_hints": len(
                            validated.normalized_packet["comparison_hints"]
                        ),
                        "deferrals": len(
                            validated.normalized_packet["deferrals"]
                        ),
                    },
                    "external_model_calls": 0,
                    "local_model_calls": local_calls,
                    "audit": sanitized_audit(provider),
                    "zero_write_replay_proved": True,
                    "invocation_zero_write": invocation_zero_write,
                    "completion_apply_outcome": completed["apply_outcome"],
                    "write_counts": {
                        "queue": 0 if invocation_zero_write else 3,
                        "ledger": 0 if invocation_zero_write else 2,
                        "packets": 0 if invocation_zero_write else 1,
                        "claims": 0,
                        "qdrant": 0,
                        "prompt_influence": 0,
                    },
                }
            )
        )
        return 0
    finally:
        await conn.close()


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
