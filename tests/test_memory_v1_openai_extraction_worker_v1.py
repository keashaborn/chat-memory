from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
from pathlib import Path
from unittest import mock
import unittest
import uuid

import scripts.memory_v1_openai_extraction_worker_v1 as worker_module

from rag_engine.memory_v1_openai_v5_2_semantic_tasks_v1 import (
    OpenAIExtractionResultV1,
)
from scripts.memory_v1_openai_extraction_worker_v1 import (
    APPLY_ENABLE_TOKEN,
    DEFAULT_REGISTRY,
    DEFAULT_SCHEMA,
    EXPECTED_REGISTRY_SHA256,
    EXPECTED_SCHEMA_SHA256,
    MAX_REJECTED_PACKET_EVIDENCE_BYTES,
    OPENAI_PROVIDER_ID,
    PROVIDER_VERSION,
    ProcessingRejected,
    REJECTED_PACKET_EVIDENCE_CONTRACT,
    _BoundPacketProvider,
    _completion_audit,
    _rejected_packet_evidence,
    configured_exact_target,
    configured_owners,
    process_job,
    stable_json,
    validate_arguments,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    CONTRACT_VERSION_V5_2,
    TrustedExtractionSource,
    load_registry,
    load_schema,
    validate_and_normalize,
)


SOURCE = "Who won the tournament in 1964?"
SOURCE_SHA = "8a1ee909df2aec5eb44bad420077ecf9685ed0cd9de23767ac7deef08ee4503d"
OWNER = uuid.UUID("00000000-0000-4000-8000-000000000001")


def args(*, apply: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        run_id="00000000-0000-4000-8000-000000000003",
        job_id=None,
        expected_content_sha256=None,
        max_jobs=1,
        max_attempts=1,
        lease_seconds=300,
        timeout_seconds=30.0,
        max_output_tokens=1024,
        rolling_window_seconds=86400,
        max_reserved_calls=4,
        failure_threshold=2,
        model="gpt-memory-test",
        sdk_version="test-sdk",
        apply=apply,
        owner_user_id=[],
        owner_allowlist_env="MEMORY_V1_OPENAI_OWNER_ALLOWLIST",
    )


def job() -> dict:
    return {
        "job_id": uuid.UUID("00000000-0000-4000-8000-000000000010"),
        "evidence_source_system": "public.chat_log",
        "evidence_external_id": (
            "chat_log:legacy-capture:"
            "00000000-0000-4000-8000-000000000011"
        ),
        "evidence_content_sha256": SOURCE_SHA,
        "evidence_recorded_at": "2026-08-06T12:00:00Z",
        "evidence_observed_at": "2026-08-06T12:00:00Z",
        "evidence_content": SOURCE,
    }


class NoCallAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def prepare(self, **_: object):
        self.calls += 1
        return None


class DumpPacket:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def model_dump(self, *, mode: str) -> dict[str, object]:
        if mode != "json":
            raise AssertionError("packet evidence must use JSON mode")
        return self.value


class TransportAudit:
    external_call_count = 1

    def public_dict(self) -> dict[str, object]:
        return {"external_call_count": self.external_call_count}


class WorkerTests(unittest.TestCase):
    def test_rejected_packet_evidence_is_canonical_replayable_and_bound(self) -> None:
        reservation_event_id = uuid.UUID(
            "00000000-0000-4000-8000-000000000020"
        )
        evidence = _rejected_packet_evidence(
            packet=DumpPacket({"z": "Eric", "a": [2, 1]}),  # type: ignore[arg-type]
            exc=ValueError("unregistered predicate: private-value"),
            request_sha256=SOURCE_SHA,
            reservation_event_id=reservation_event_id,
        )
        canonical_packet = '{"a":[2,1],"z":"Eric"}'
        self.assertEqual(
            evidence,
            {
                "capture_status": "complete",
                "contract_version": REJECTED_PACKET_EVIDENCE_CONTRACT,
                "packet": {"z": "Eric", "a": [2, 1]},
                "packet_bytes": len(canonical_packet.encode("utf-8")),
                "packet_canonicalization": "stable_json_utf8_v1",
                "packet_sha256": hashlib.sha256(
                    canonical_packet.encode("utf-8")
                ).hexdigest(),
                "request_sha256": SOURCE_SHA,
                "reservation_event_id": str(reservation_event_id),
                "validator_error_class": "validator_value_error",
                "validator_error_sha256": hashlib.sha256(
                    b"unregistered predicate: private-value"
                ).hexdigest(),
                "validator_rejection_code": "unregistered_predicate",
            },
        )
        self.assertNotIn("private-value", str(evidence))

    def test_rejected_packet_evidence_classifies_type_error_without_raw_error(self) -> None:
        evidence = _rejected_packet_evidence(
            packet=DumpPacket({"observations": []}),  # type: ignore[arg-type]
            exc=TypeError("identifier must not be disclosed"),
            request_sha256=SOURCE_SHA,
            reservation_event_id=uuid.UUID(
                "00000000-0000-4000-8000-000000000020"
            ),
        )
        self.assertEqual(evidence["validator_error_class"], "validator_type_error")
        self.assertEqual(
            evidence["validator_rejection_code"],
            "uncatalogued_validator_rejection",
        )
        self.assertNotIn("identifier must not be disclosed", str(evidence))

    def test_rejected_packet_evidence_never_truncates_oversize_packet(self) -> None:
        evidence = _rejected_packet_evidence(
            packet=DumpPacket(  # type: ignore[arg-type]
                {"value": "x" * MAX_REJECTED_PACKET_EVIDENCE_BYTES}
            ),
            exc=ValueError("rejected"),
            request_sha256=SOURCE_SHA,
            reservation_event_id=uuid.UUID(
                "00000000-0000-4000-8000-000000000020"
            ),
        )
        self.assertEqual(evidence["capture_status"], "oversize")
        self.assertIsNone(evidence["packet"])
        self.assertGreater(
            evidence["packet_bytes"], MAX_REJECTED_PACKET_EVIDENCE_BYTES
        )

    def test_completion_audit_binds_rejected_packet_evidence_only_when_present(
        self,
    ) -> None:
        reservation_event_id = uuid.UUID(
            "00000000-0000-4000-8000-000000000020"
        )
        evidence = _rejected_packet_evidence(
            packet=DumpPacket({"observations": []}),  # type: ignore[arg-type]
            exc=ValueError("rejected"),
            request_sha256=SOURCE_SHA,
            reservation_event_id=reservation_event_id,
        )
        rejected = _completion_audit(
            outcome="rejected",
            request_sha256=SOURCE_SHA,
            reservation_event_id=reservation_event_id,
            budget=None,
            transport_audit=None,
            error_code="validator_rejected",
            rejected_packet_evidence=evidence,
        )
        self.assertEqual(rejected["rejected_packet_evidence"], evidence)
        self.assertEqual(
            rejected["request_sha256"], evidence["request_sha256"]
        )
        self.assertEqual(
            rejected["reservation_event_id"], evidence["reservation_event_id"]
        )
        accepted = _completion_audit(
            outcome="accepted",
            request_sha256=SOURCE_SHA,
            reservation_event_id=reservation_event_id,
            budget=None,
            transport_audit=None,
        )
        self.assertNotIn("rejected_packet_evidence", accepted)

    def test_completion_audit_rejects_nested_evidence_binding_mismatches(
        self,
    ) -> None:
        reservation_event_id = uuid.UUID(
            "00000000-0000-4000-8000-000000000020"
        )
        evidence = _rejected_packet_evidence(
            packet=DumpPacket({"observations": []}),  # type: ignore[arg-type]
            exc=ValueError("unregistered predicate: private-value"),
            request_sha256=SOURCE_SHA,
            reservation_event_id=reservation_event_id,
        )
        for field, value in (
            ("request_sha256", "0" * 64),
            (
                "reservation_event_id",
                "00000000-0000-4000-8000-000000000021",
            ),
            ("packet_sha256", "0" * 64),
            ("packet_bytes", evidence["packet_bytes"] + 1),
        ):
            tampered = dict(evidence)
            tampered[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                RuntimeError, "rejected packet evidence"
            ):
                _completion_audit(
                    outcome="rejected",
                    request_sha256=SOURCE_SHA,
                    reservation_event_id=reservation_event_id,
                    budget=None,
                    transport_audit=None,
                    error_code="validator_rejected",
                    rejected_packet_evidence=tampered,
                )

    def test_completion_audit_rejects_unknown_nested_evidence_fields(self) -> None:
        reservation_event_id = uuid.UUID(
            "00000000-0000-4000-8000-000000000020"
        )
        evidence = _rejected_packet_evidence(
            packet=DumpPacket({"observations": []}),  # type: ignore[arg-type]
            exc=ValueError("unregistered predicate: private-value"),
            request_sha256=SOURCE_SHA,
            reservation_event_id=reservation_event_id,
        )
        evidence["raw_validator_message"] = "must never persist"
        with self.assertRaisesRegex(RuntimeError, "evidence shape is invalid"):
            _completion_audit(
                outcome="rejected",
                request_sha256=SOURCE_SHA,
                reservation_event_id=reservation_event_id,
                budget=None,
                transport_audit=None,
                error_code="validator_rejected",
                rejected_packet_evidence=evidence,
            )

    def test_completion_audit_rejects_packet_evidence_on_wrong_outcome(self) -> None:
        reservation_event_id = uuid.UUID(
            "00000000-0000-4000-8000-000000000020"
        )
        evidence = _rejected_packet_evidence(
            packet=DumpPacket({"observations": []}),  # type: ignore[arg-type]
            exc=ValueError("unregistered predicate: private-value"),
            request_sha256=SOURCE_SHA,
            reservation_event_id=reservation_event_id,
        )
        for outcome, error_code in (
            ("accepted", None),
            ("rejected", "worker_rejected"),
        ):
            with self.subTest(
                outcome=outcome, error_code=error_code
            ), self.assertRaisesRegex(RuntimeError, "evidence outcome is invalid"):
                _completion_audit(
                    outcome=outcome,
                    request_sha256=SOURCE_SHA,
                    reservation_event_id=reservation_event_id,
                    budget=None,
                    transport_audit=None,
                    error_code=error_code,
                    rejected_packet_evidence=evidence,
                )

    def test_maximum_complete_packet_fits_completion_audit_text_bound(self) -> None:
        packet_overhead = len('{"value":""}'.encode("utf-8"))
        evidence = _rejected_packet_evidence(
            packet=DumpPacket(  # type: ignore[arg-type]
                {
                    "value": "x"
                    * (MAX_REJECTED_PACKET_EVIDENCE_BYTES - packet_overhead)
                }
            ),
            exc=ValueError("rejected"),
            request_sha256=SOURCE_SHA,
            reservation_event_id=uuid.UUID(
                "00000000-0000-4000-8000-000000000020"
            ),
        )
        self.assertEqual(evidence["capture_status"], "complete")
        audit = _completion_audit(
            outcome="rejected",
            request_sha256=SOURCE_SHA,
            reservation_event_id=uuid.UUID(
                "00000000-0000-4000-8000-000000000020"
            ),
            budget=None,
            transport_audit=None,
            error_code="validator_rejected",
            rejected_packet_evidence=evidence,
        )
        self.assertLessEqual(len(stable_json(audit).encode("utf-8")), 24_576)

    def test_rejected_packet_evidence_rejects_invalid_request_binding(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "request hash is invalid"):
            _rejected_packet_evidence(
                packet=DumpPacket({}),  # type: ignore[arg-type]
                exc=ValueError("rejected"),
                request_sha256="not-a-sha",
                reservation_event_id=uuid.UUID(
                    "00000000-0000-4000-8000-000000000020"
                ),
            )

    def test_validator_rejection_finalizes_exact_packet_evidence_provider_free(
        self,
    ) -> None:
        packet = OpenAIExtractionResultV1.model_validate(
            {
                "entity_mentions": [],
                "observations": [],
                "comparison_hints": [],
                "deferrals": [],
                "packet_findings": [],
            },
            strict=True,
        )
        prepared = mock.Mock()
        prepared.request.estimated_input_tokens = 100
        prepared.request.max_output_tokens = 100
        prepared.request.request_sha256 = SOURCE_SHA
        prepared.content_free_receipt.return_value = {}
        result = mock.Mock(
            packet=packet,
            external_model_calls=1,
            audit=TransportAudit(),
        )
        adapter = mock.Mock()
        adapter.prepare.return_value = prepared
        adapter.execute_prepared.return_value = result
        reservation = {
            **job(),
            "reservation_event_id": uuid.UUID(
                "00000000-0000-4000-8000-000000000020"
            ),
            "attempts": 1,
        }
        budget = mock.Mock(settlement=None)

        async def run() -> None:
            with mock.patch.object(
                worker_module,
                "pricing_rates",
                return_value={
                    "input_microusd_per_million_tokens": 1,
                    "cached_input_microusd_per_million_tokens": 1,
                    "cache_write_input_microusd_per_million_tokens": 1,
                    "output_microusd_per_million_tokens": 1,
                },
            ), mock.patch.object(
                worker_module,
                "_positive_int_env",
                return_value=100_000,
            ), mock.patch.object(
                worker_module,
                "reserve_call",
                new=mock.AsyncMock(return_value=reservation),
            ), mock.patch.object(
                worker_module.PostgresProviderReservationV1,
                "from_request",
                return_value=object(),
            ), mock.patch.object(
                worker_module,
                "PostgresBudgetAuthorizerV1",
                return_value=budget,
            ), mock.patch.object(
                worker_module,
                "OpenAIStructuredResponsesTransportV1",
                return_value=object(),
            ), mock.patch.object(
                worker_module,
                "validate_and_normalize",
                side_effect=ValueError("unregistered predicate: private-value"),
            ), mock.patch.object(
                worker_module,
                "complete_call",
                new=mock.AsyncMock(),
            ) as complete, mock.patch.object(
                worker_module,
                "finalize_receipt",
                new=mock.AsyncMock(),
            ) as finalize, mock.patch.object(
                worker_module,
                "persist_packet",
                new=mock.AsyncMock(),
            ) as persist, mock.patch.object(
                worker_module,
                "fail_job",
                new=mock.AsyncMock(
                    return_value={"status": "skipped", "apply_outcome": "applied"}
                ),
            ):
                report, calls = await process_job(
                    object(),
                    owner=OWNER,
                    job=job(),
                    worker_id="test-worker",
                    run_id=uuid.UUID(
                        "00000000-0000-4000-8000-000000000003"
                    ),
                    model="gpt-memory-test",
                    adapter=adapter,
                    registry={},
                    schema={},
                    args=args(),
                )
            self.assertEqual(report["rejection_code"], "validator_rejected")
            self.assertEqual(calls, 1)
            persist.assert_not_awaited()
            complete.assert_awaited_once()
            finalize.assert_awaited_once()
            audit = finalize.await_args.kwargs["audit_receipt"]
            evidence = audit["rejected_packet_evidence"]
            self.assertEqual(evidence["packet"], packet.model_dump(mode="json"))
            self.assertEqual(evidence["request_sha256"], SOURCE_SHA)
            self.assertEqual(
                evidence["reservation_event_id"],
                str(reservation["reservation_event_id"]),
            )
            self.assertEqual(
                audit["transport_audit"], {"external_call_count": 1}
            )
            self.assertNotIn("private-value", stable_json(audit))

        asyncio.run(run())

    def test_owner_allowlist_is_explicit_deduplicated_and_sorted(self) -> None:
        value = (
            "00000000-0000-4000-8000-000000000002,"
            "00000000-0000-4000-8000-000000000001"
        )
        with mock.patch.dict(
            os.environ,
            {"MEMORY_V1_OPENAI_OWNER_ALLOWLIST": value},
            clear=True,
        ):
            self.assertEqual(
                [str(item) for item in configured_owners(args())],
                [
                    "00000000-0000-4000-8000-000000000001",
                    "00000000-0000-4000-8000-000000000002",
                ],
            )

    def test_apply_requires_both_capabilities(self) -> None:
        value = args(apply=True)
        value.job_id = "00000000-0000-4000-8000-000000000010"
        value.expected_content_sha256 = SOURCE_SHA
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "apply capability"):
                validate_arguments(value)

    def test_exact_target_requires_job_and_hash_together(self) -> None:
        value = args()
        value.job_id = "00000000-0000-4000-8000-000000000010"
        with self.assertRaisesRegex(RuntimeError, "required together"):
            configured_exact_target(value, [OWNER])

    def test_exact_target_requires_one_owner_and_one_job(self) -> None:
        value = args()
        value.job_id = "00000000-0000-4000-8000-000000000010"
        value.expected_content_sha256 = SOURCE_SHA
        with self.assertRaisesRegex(RuntimeError, "requires one owner"):
            configured_exact_target(value, [OWNER, uuid.uuid4()])
        value.max_jobs = 2
        with self.assertRaisesRegex(RuntimeError, "max-jobs 1"):
            configured_exact_target(value, [OWNER])
        value.max_jobs = 1
        self.assertEqual(
            configured_exact_target(value, [OWNER]),
            (
                uuid.UUID("00000000-0000-4000-8000-000000000010"),
                SOURCE_SHA,
            ),
        )
        value.apply = True
        with mock.patch.dict(
            os.environ,
            {"MEMORY_V1_OPENAI_EXTRACTION_APPLY": APPLY_ENABLE_TOKEN},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "structured-call"):
                validate_arguments(value)

    def test_irrelevant_question_is_rejected_with_zero_provider_calls(self) -> None:
        adapter = NoCallAdapter()

        async def run() -> None:
            with mock.patch.object(
                worker_module,
                "skip_job",
                new=mock.AsyncMock(
                    return_value={
                        "status": "skipped",
                        "apply_outcome": "applied",
                    }
                ),
            ) as skip, mock.patch.object(
                worker_module,
                "reserve_call",
                new=mock.AsyncMock(),
            ) as reserve:
                result, calls = await process_job(
                    object(),
                    owner=OWNER,
                    job=job(),
                    worker_id="test-worker",
                    run_id=uuid.UUID(
                        "00000000-0000-4000-8000-000000000003"
                    ),
                    model="gpt-memory-test",
                    adapter=adapter,
                    registry={},
                    schema={},
                    args=args(),
                )
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["rejection_code"], "pure_general_question")
            self.assertFalse(result["provider_reservation_created"])
            self.assertEqual(calls, 0)
            skip.assert_awaited_once()
            reserve.assert_not_awaited()

        asyncio.run(run())
        self.assertEqual(adapter.calls, 1)

    def test_bound_packet_provider_rejects_cross_source_use(self) -> None:
        packet = OpenAIExtractionResultV1.model_validate(
            {
                "entity_mentions": [],
                "observations": [],
                "comparison_hints": [],
                "deferrals": [],
                "packet_findings": [],
            },
            strict=True,
        )
        provider = _BoundPacketProvider(
            packet=packet,
            source_sha256=SOURCE_SHA,
            external_model_calls=1,
        )
        source = TrustedExtractionSource.create(
            job_id="00000000-0000-4000-8000-000000000010",
            source_system="public.chat_log",
            source_external_id="00000000-0000-4000-8000-000000000011",
            source_sha256=hashlib.sha256(
                b"Different source"
            ).hexdigest(),
            source_recorded_at="2026-08-06T12:00:00Z",
            content="Different source",
            source_observed_at="2026-08-06T12:00:00Z",
        )
        with self.assertRaisesRegex(RuntimeError, "source binding changed"):
            provider.extract(source)
        self.assertEqual(provider.external_model_calls, 0)

    def test_bound_packet_provider_records_exactly_one_validation_call(self) -> None:
        packet = OpenAIExtractionResultV1.model_validate(
            {
                "entity_mentions": [],
                "observations": [],
                "comparison_hints": [],
                "deferrals": [],
                "packet_findings": [],
            },
            strict=True,
        )
        provider = _BoundPacketProvider(
            packet=packet,
            source_sha256=SOURCE_SHA,
            external_model_calls=1,
        )
        source = TrustedExtractionSource.create(
            job_id="00000000-0000-4000-8000-000000000010",
            source_system="public.chat_log",
            source_external_id="00000000-0000-4000-8000-000000000011",
            source_sha256=SOURCE_SHA,
            source_recorded_at="2026-08-06T12:00:00Z",
            content=SOURCE,
            source_observed_at="2026-08-06T12:00:00Z",
        )
        self.assertEqual(provider.external_model_calls, 0)
        self.assertEqual(provider.extract(source), packet)
        self.assertEqual(provider.external_model_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "more than once"):
            provider.extract(source)
        self.assertEqual(provider.external_model_calls, 1)

    def test_v5_normalizer_records_the_bound_openai_call(self) -> None:
        packet = OpenAIExtractionResultV1.model_validate(
            {
                "entity_mentions": [],
                "observations": [],
                "comparison_hints": [],
                "deferrals": [],
                "packet_findings": [],
            },
            strict=True,
        )
        provider = _BoundPacketProvider(
            packet=packet,
            source_sha256=SOURCE_SHA,
            external_model_calls=1,
        )
        source = TrustedExtractionSource.create(
            job_id="00000000-0000-4000-8000-000000000010",
            source_system="public.chat_log",
            source_external_id=job()["evidence_external_id"],
            source_sha256=SOURCE_SHA,
            source_recorded_at="2026-08-06T12:00:00Z",
            content=SOURCE,
            source_observed_at="2026-08-06T12:00:00Z",
        )
        result = validate_and_normalize(
            provider,
            source=source,
            registry=load_registry(
                DEFAULT_REGISTRY,
                EXPECTED_REGISTRY_SHA256,
            ),
            schema=load_schema(
                DEFAULT_SCHEMA,
                EXPECTED_SCHEMA_SHA256,
                expected_contract_version=CONTRACT_VERSION_V5_2,
            ),
            trusted_project_binding=None,
            allowed_provider_versions={OPENAI_PROVIDER_ID: PROVIDER_VERSION},
            max_external_model_calls=1,
        )
        self.assertEqual(result.external_model_calls, 1)
        self.assertEqual(
            result.normalized_packet["contract_version"],
            "memory_v1_relational_extraction_v5_2",
        )
        self.assertEqual(
            result.normalized_packet["predicate_registry_version"],
            "memory_predicate_registry_v5_2",
        )
        self.assertEqual(
            result.normalized_packet["source_envelope"]["source_external_id"],
            job()["evidence_external_id"],
        )

    def test_v5_schema_accepts_exact_maximum_governed_external_id(self) -> None:
        packet = OpenAIExtractionResultV1.model_validate(
            {
                "entity_mentions": [],
                "observations": [],
                "comparison_hints": [],
                "deferrals": [],
                "packet_findings": [],
            },
            strict=True,
        )
        provider = _BoundPacketProvider(
            packet=packet,
            source_sha256=SOURCE_SHA,
            external_model_calls=1,
        )
        source = TrustedExtractionSource.create(
            job_id="00000000-0000-4000-8000-000000000010",
            source_system="public.chat_log",
            source_external_id="x" * 500,
            source_sha256=SOURCE_SHA,
            source_recorded_at="2026-08-06T12:00:00Z",
            content=SOURCE,
            source_observed_at="2026-08-06T12:00:00Z",
        )
        result = validate_and_normalize(
            provider,
            source=source,
            registry=load_registry(
                DEFAULT_REGISTRY,
                EXPECTED_REGISTRY_SHA256,
            ),
            schema=load_schema(
                DEFAULT_SCHEMA,
                EXPECTED_SCHEMA_SHA256,
                expected_contract_version=CONTRACT_VERSION_V5_2,
            ),
            trusted_project_binding=None,
            allowed_provider_versions={OPENAI_PROVIDER_ID: PROVIDER_VERSION},
            max_external_model_calls=1,
        )
        self.assertEqual(
            result.normalized_packet["source_envelope"]["source_external_id"],
            "x" * 500,
        )

    def test_v5_schema_external_id_definition_is_fail_closed(self) -> None:
        schema = load_schema(
            DEFAULT_SCHEMA,
            EXPECTED_SCHEMA_SHA256,
            expected_contract_version=CONTRACT_VERSION_V5_2,
        )
        contract = schema["$defs"]["governed_external_id"]
        self.assertEqual(contract["type"], "string")
        self.assertEqual(contract["minLength"], 1)
        self.assertEqual(contract["maxLength"], 500)
        pattern = re.compile(contract["pattern"])
        self.assertIsNotNone(pattern.fullmatch("legacy:record:01"))
        for invalid in ("", " \t\n", "unsafe\x00id"):
            with self.subTest(invalid=repr(invalid)):
                self.assertIsNone(pattern.fullmatch(invalid))
        self.assertEqual(
            schema["$defs"]["source_envelope"]["properties"][
                "source_external_id"
            ]["$ref"],
            "#/$defs/governed_external_id",
        )

    def test_worker_defaults_are_exact_v5_2_contract_inputs(self) -> None:
        self.assertEqual(
            DEFAULT_REGISTRY.name,
            "memory_v1_predicate_registry_v5_2.json",
        )
        self.assertEqual(
            DEFAULT_SCHEMA.name,
            "memory_v1_relational_extraction_v5_2.schema.json",
        )
        self.assertEqual(
            hashlib.sha256(DEFAULT_REGISTRY.read_bytes()).hexdigest(),
            EXPECTED_REGISTRY_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(DEFAULT_SCHEMA.read_bytes()).hexdigest(),
            EXPECTED_SCHEMA_SHA256,
        )

    def test_service_has_no_gpu_or_local_inference_dependency(self) -> None:
        root = Path(__file__).resolve().parents[1]
        service = (
            root / "ops/systemd/memory-v1-openai-extraction.service"
        ).read_text(encoding="utf-8")
        self.assertNotIn("resse", service.lower())
        self.assertNotIn("gpu", service.lower())
        self.assertNotIn("local-inference", service.lower())
        self.assertIn("NoNewPrivileges=true", service)
        self.assertNotIn("StateDirectory=", service)
        self.assertNotIn("--state-root", service)
        self.assertIn("--job-id ${MEMORY_V1_OPENAI_JOB_ID}", service)
        self.assertIn("--expected-content-sha256", service)
        self.assertIn("--max-jobs 1", service)
        self.assertIn("--max-attempts 1", service)


if __name__ == "__main__":
    unittest.main()
