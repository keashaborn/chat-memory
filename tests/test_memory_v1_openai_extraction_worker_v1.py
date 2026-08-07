from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
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
    OPENAI_PROVIDER_ID,
    PROVIDER_VERSION,
    ProcessingRejected,
    _BoundPacketProvider,
    configured_exact_target,
    configured_owners,
    process_job,
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
        apply=apply,
        owner_user_id=[],
        owner_allowlist_env="MEMORY_V1_OPENAI_OWNER_ALLOWLIST",
    )


def job() -> dict:
    return {
        "job_id": uuid.UUID("00000000-0000-4000-8000-000000000010"),
        "evidence_source_system": "public.chat_log",
        "evidence_external_id": "00000000-0000-4000-8000-000000000011",
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


class WorkerTests(unittest.TestCase):
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
            source_external_id="00000000-0000-4000-8000-000000000011",
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
