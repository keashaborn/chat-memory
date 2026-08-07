from __future__ import annotations

import unittest

from rag_engine.memory_v1_openai_postgres_authority_v1 import (
    PostgresBudgetAuthorizerV1,
    PostgresPrivacyAuthorizerV1,
    PostgresProviderReservationV1,
)
from rag_engine.memory_v1_openai_provider_adapter_v1 import (
    OpenAIV52ProviderAdapterV1,
    extraction_model_policy_v1,
    extraction_task_profile_v1,
)
from rag_engine.memory_v1_openai_structured_transport_v1 import (
    ExternalPrivacyAuthorizationV1,
    PRIVACY_AUTHORIZATION_TOKEN,
    StructuredTransportError,
)


def prepared():
    privacy = ExternalPrivacyAuthorizationV1(
        policy_version="privacy_v1",
        policy_sha256="a" * 64,
        retention_mode="standard_retention_explicitly_accepted",
        authorization_sha256="b" * 64,
        standard_retention_risk_accepted=True,
        retention_attestation_sha256=None,
        enable_token=PRIVACY_AUTHORIZATION_TOKEN,
    )
    adapter = OpenAIV52ProviderAdapterV1(
        transport=None,
        task_profile=extraction_task_profile_v1(),
        model_policy=extraction_model_policy_v1(
            model="gpt-memory-test",
            sdk_package_version="2.6.1",
        ),
        privacy_authorization=privacy,
        budget_policy_version="budget_v1",
        budget_policy_sha256="c" * 64,
        pricing_policy_version="pricing_v1",
        pricing_policy_sha256="d" * 64,
        max_output_tokens=128,
        timeout_seconds=20,
        max_attempts=1,
    ).prepare(
        owner_user_id="00000000-0000-4000-8000-000000000001",
        source_text="I have three sisters: Cindy, Lori, and Heidi.",
        operation_id="op-1",
    )
    assert adapter is not None
    return adapter


def reservation():
    request = prepared().request
    rates = {
        "input_microusd_per_million_tokens": 1_000_000,
        "cached_input_microusd_per_million_tokens": 1_000_000,
        "cache_write_input_microusd_per_million_tokens": 1_000_000,
        "output_microusd_per_million_tokens": 1_000_000,
    }
    return request, PostgresProviderReservationV1.from_request(
        reservation_id="r0001",
        request=request,
        rates=rates,
        max_cost_microusd=1000,
        issued_at_epoch_seconds=100,
        ttl_seconds=300,
    )


class PostgresAuthorityTests(unittest.TestCase):
    def test_privacy_grant_is_exact_bound_and_one_use(self) -> None:
        request, durable = reservation()
        authority = PostgresPrivacyAuthorizerV1(durable)
        grant = authority.authorize(
            request_sha256=request.request_sha256,
            owner_binding_sha256=request.owner_binding_sha256,
            safety_identifier=request.safety_identifier,
            purpose=request.purpose,
            task_contract_sha256=request.task_contract_sha256,
            model_policy_sha256=request.model_policy_sha256,
            gate_policy_sha256=request.gate_result.policy_sha256,
            privacy_authorization=request.privacy_authorization,
        )
        self.assertTrue(grant.authorized)
        self.assertTrue(grant.durable)
        with self.assertRaisesRegex(StructuredTransportError, "reused"):
            authority.authorize(
                request_sha256=request.request_sha256,
                owner_binding_sha256=request.owner_binding_sha256,
                safety_identifier=request.safety_identifier,
                purpose=request.purpose,
                task_contract_sha256=request.task_contract_sha256,
                model_policy_sha256=request.model_policy_sha256,
                gate_policy_sha256=request.gate_result.policy_sha256,
                privacy_authorization=request.privacy_authorization,
            )

    def test_budget_grant_is_exact_bound_and_one_attempt(self) -> None:
        request, durable = reservation()
        authority = PostgresBudgetAuthorizerV1(durable)
        kwargs = {
            "request_sha256": request.request_sha256,
            "attempt_ordinal": 1,
            "model": request.model,
            "privacy_policy_sha256": request.privacy_authorization.policy_sha256,
            "output_schema_sha256": request.output_schema_sha256,
            "budget_policy_version": request.budget_policy_version,
            "budget_policy_sha256": request.budget_policy_sha256,
            "pricing_policy_version": request.pricing_policy_version,
            "pricing_policy_sha256": request.pricing_policy_sha256,
        }
        grant = authority.reserve(**kwargs)
        self.assertTrue(grant.authorized)
        self.assertEqual(grant.attempt_ordinal, 1)
        with self.assertRaisesRegex(StructuredTransportError, "reused"):
            authority.reserve(**kwargs)

    def test_binding_mismatch_fails_closed(self) -> None:
        request, durable = reservation()
        authority = PostgresBudgetAuthorizerV1(durable)
        with self.assertRaisesRegex(StructuredTransportError, "binding_mismatch"):
            authority.reserve(
                request_sha256="f" * 64,
                attempt_ordinal=1,
                model=request.model,
                privacy_policy_sha256=request.privacy_authorization.policy_sha256,
                output_schema_sha256=request.output_schema_sha256,
                budget_policy_version=request.budget_policy_version,
                budget_policy_sha256=request.budget_policy_sha256,
                pricing_policy_version=request.pricing_policy_version,
                pricing_policy_sha256=request.pricing_policy_sha256,
            )


if __name__ == "__main__":
    unittest.main()
