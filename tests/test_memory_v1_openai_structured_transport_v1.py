from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import hashlib
import json
import unittest

from pydantic import BaseModel, ConfigDict, Field

from rag_engine.memory_v1_openai_structured_transport_v1 import (
    BudgetReservationGrantV1,
    EXTERNAL_CALL_ENABLE_TOKEN,
    ExternalPrivacyAuthorizationV1,
    OpenAIStructuredResponsesTransportV1,
    PRIVACY_AUTHORIZATION_TOKEN,
    PrivacyAuthorizationGrantV1,
    StructuredModelPolicyV1,
    StructuredResponsesRequestV1,
    StructuredTaskProfileV1,
    StructuredTransportError,
    owner_safety_identifier_v1,
)
from rag_engine.memory_v1_personal_evidence_prefilter_v1 import (
    TRUSTED_SOURCE_ROLE,
    classify_personal_evidence_v1,
)


MODEL = "gpt-test-1"
SDK_VERSION = "2.6.1"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
OWNER_BINDING = "e" * 64
SAFETY_IDENTIFIER = owner_safety_identifier_v1(OWNER_BINDING)


class StrictObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: str
    summary: str
    confidence_millionths: int = Field(ge=0, le=1_000_000)


class OpenObservation(BaseModel):
    kind: str


class StrictObservationWithDefault(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: str = "fact"


class FakeResponses:
    def __init__(self, actions: list[object]) -> None:
        self.actions = list(actions)
        self.calls: list[dict[str, object]] = []

    def parse(
        self,
        *,
        model,
        instructions,
        input,
        text_format,
        store,
        background,
        stream,
        parallel_tool_calls,
        reasoning,
        service_tier,
        truncation,
        safety_identifier,
        max_output_tokens,
        timeout,
    ):
        call = {
            "model": model,
            "instructions": instructions,
            "input": input,
            "text_format": text_format,
            "store": store,
            "background": background,
            "stream": stream,
            "parallel_tool_calls": parallel_tool_calls,
            "reasoning": reasoning,
            "service_tier": service_tier,
            "truncation": truncation,
            "safety_identifier": safety_identifier,
            "max_output_tokens": max_output_tokens,
            "timeout": timeout,
        }
        self.calls.append(call)
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class FakeClient:
    def __init__(self, actions: list[object]) -> None:
        self.max_retries = 0
        self.responses = FakeResponses(actions)


class IncompatibleResponses:
    def parse(self, *, model):
        raise AssertionError("must not be called")


class RecordingSDK:
    __version__ = SDK_VERSION
    APITimeoutError = TimeoutError
    APIConnectionError = ConnectionError
    RateLimitError = ()

    def __init__(self, client) -> None:
        self.client = client
        self.max_retries: list[int] = []

    def OpenAI(self, *, max_retries: int):
        self.max_retries.append(max_retries)
        return self.client


class FakeRateLimitError(Exception):
    def __init__(self, code: str | None) -> None:
        super().__init__("rate limited")
        self.status_code = 429
        self.code = code
        self.response = SimpleNamespace(headers={})


class FakeServerError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("server error")
        self.status_code = status_code
        self.response = SimpleNamespace(headers={})


class SelectedInputContractTests(unittest.TestCase):
    def test_selected_span_contract_carries_original_offsets_and_hash(self) -> None:
        source = "Before. I have three sisters: Cindy, Lori, and Heidi. After."
        request = _request(source)
        payload = json.loads(request.selected_input_text)
        self.assertEqual(
            payload["contract_version"],
            "memory_v1_openai_selected_spans_v2",
        )
        self.assertGreaterEqual(len(payload["spans"]), 1)
        for span in payload["spans"]:
            selected = source[span["char_start"] : span["char_end"]]
            self.assertEqual(span["text"], selected)
            self.assertEqual(
                span["content_sha256"],
                hashlib.sha256(selected.encode("utf-8")).hexdigest(),
            )


FAKE_SDK = SimpleNamespace(
    __version__=SDK_VERSION,
    APITimeoutError=TimeoutError,
    APIConnectionError=ConnectionError,
    RateLimitError=FakeRateLimitError,
)


class FakeBudget:
    def __init__(self) -> None:
        self.reserves: list[dict[str, object]] = []
        self.settlements: list[object] = []
        self.reserve_errors: dict[int, Exception] = {}
        self.grant_changes: dict[int, dict[str, object]] = {}
        self.fail_settlement = False
        self.after_reserve = None

    def reserve(self, **kwargs) -> BudgetReservationGrantV1:
        self.reserves.append(dict(kwargs))
        ordinal = int(kwargs["attempt_ordinal"])
        if ordinal in self.reserve_errors:
            raise self.reserve_errors[ordinal]
        rates = {
            "input_microusd_per_million_tokens": 2_000_000,
            "cached_input_microusd_per_million_tokens": 500_000,
            "cache_write_input_microusd_per_million_tokens": 2_500_000,
            "output_microusd_per_million_tokens": 8_000_000,
        }
        max_input_rate = max(rates[key] for key in rates if key != "output_microusd_per_million_tokens")
        max_cost = _cost(
            int(kwargs["estimated_input_tokens"]),
            max_input_rate,
        ) + _cost(
            int(kwargs["max_output_tokens"]),
            rates["output_microusd_per_million_tokens"],
        )
        values = {
            "reservation_id": f"reservation_{ordinal:04d}",
            "authorized": True,
            "durable": True,
            "request_sha256": kwargs["request_sha256"],
            "attempt_ordinal": ordinal,
            "model": kwargs["model"],
            "privacy_policy_sha256": kwargs["privacy_policy_sha256"],
            "output_schema_sha256": kwargs["output_schema_sha256"],
            "budget_policy_version": kwargs["budget_policy_version"],
            "budget_policy_sha256": kwargs["budget_policy_sha256"],
            "pricing_policy_version": kwargs["pricing_policy_version"],
            "pricing_policy_sha256": kwargs["pricing_policy_sha256"],
            **rates,
            "max_cost_microusd": max_cost,
        }
        values.update(self.grant_changes.get(ordinal, {}))
        grant = BudgetReservationGrantV1(**values)
        if self.after_reserve is not None:
            self.after_reserve()
        return grant

    def settle(self, settlement) -> None:
        if self.fail_settlement:
            raise RuntimeError("settlement unavailable")
        self.settlements.append(settlement)


class FakePrivacyAuthorizer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.grant_changes: dict[str, object] = {}
        self.error: Exception | None = None

    def authorize(self, **kwargs) -> PrivacyAuthorizationGrantV1:
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        privacy = kwargs["privacy_authorization"]
        values = {
            "grant_id": "privacy_grant_0001",
            "authorized": True,
            "durable": True,
            "request_sha256": kwargs["request_sha256"],
            "owner_binding_sha256": kwargs["owner_binding_sha256"],
            "safety_identifier": kwargs["safety_identifier"],
            "purpose": kwargs["purpose"],
            "task_contract_sha256": kwargs["task_contract_sha256"],
            "model_policy_sha256": kwargs["model_policy_sha256"],
            "gate_policy_sha256": kwargs["gate_policy_sha256"],
            "privacy_policy_version": privacy.policy_version,
            "privacy_policy_sha256": privacy.policy_sha256,
            "privacy_authorization_sha256": privacy.authorization_sha256,
            "retention_mode": privacy.retention_mode,
            "standard_retention_risk_accepted": (
                privacy.standard_retention_risk_accepted
            ),
            "retention_attestation_sha256": privacy.retention_attestation_sha256,
            "issued_at_epoch_seconds": 100,
            "expires_at_epoch_seconds": 200,
        }
        values.update(self.grant_changes)
        return PrivacyAuthorizationGrantV1(**values)


def _cost(tokens: int, rate: int) -> int:
    return (tokens * rate + 999_999) // 1_000_000


def _profiles():
    task = StructuredTaskProfileV1.create(
        profile_version="memory_extract_v1",
        purpose="memory_extraction",
        instructions="Extract only the selected owner evidence into the strict schema.",
        output_model=StrictObservation,
    )
    model = StructuredModelPolicyV1.create(
        policy_version="memory_model_v1",
        model=MODEL,
        sdk_package_version=SDK_VERSION,
        max_output_tokens_ceiling=128,
        timeout_seconds_ceiling=30,
    )
    return task, model


def _privacy(
    *,
    enable_token: str = PRIVACY_AUTHORIZATION_TOKEN,
    retention_mode: str = "standard_retention_explicitly_accepted",
):
    return ExternalPrivacyAuthorizationV1(
        policy_version="privacy_v1",
        policy_sha256=SHA_A,
        retention_mode=retention_mode,
        authorization_sha256=SHA_B,
        standard_retention_risk_accepted=(
            retention_mode == "standard_retention_explicitly_accepted"
        ),
        retention_attestation_sha256=(
            None
            if retention_mode == "standard_retention_explicitly_accepted"
            else SHA_C
        ),
        enable_token=enable_token,
    )


def _request(
    source: str = "I have three sisters: Cindy, Lori, and Heidi.",
    **changes,
):
    task, model_policy = _profiles()
    values = {
        "request_id": "request_0001",
        "pipeline_version": "pipeline_v1",
        "purpose": "memory_extraction",
        "model": MODEL,
        "task_contract_sha256": task.contract_sha256,
        "model_policy_sha256": model_policy.policy_sha256,
        "budget_policy_version": "budget_v1",
        "budget_policy_sha256": SHA_C,
        "pricing_policy_version": "pricing_v1",
        "pricing_policy_sha256": SHA_D,
        "owner_binding_sha256": OWNER_BINDING,
        "instructions": task.instructions,
        "source_text": source,
        "gate_result": classify_personal_evidence_v1(
            source,
            source_role=TRUSTED_SOURCE_ROLE,
        ),
        "privacy_authorization": _privacy(),
        "output_model": StrictObservation,
        "safety_identifier": SAFETY_IDENTIFIER,
        "max_output_tokens": 64,
        "timeout_seconds": 20.0,
        "max_attempts": 2,
    }
    values.update(changes)
    return StructuredResponsesRequestV1(**values)


def _response(**changes):
    values = {
        "id": "resp_test_001",
        "_request_id": "req_test_001",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": MODEL,
        "service_tier": "default",
        "background": False,
        "conversation": None,
        "previous_response_id": None,
        "tools": [],
        "parallel_tool_calls": False,
        "truncation": "disabled",
        "safety_identifier": SAFETY_IDENTIFIER,
        "max_output_tokens": 64,
        "output": [
            SimpleNamespace(
                type="message",
                role="assistant",
                status="completed",
                content=[
                    SimpleNamespace(
                        type="output_text",
                        text=(
                            '{"kind":"fact","summary":"The owner has three '
                            'sisters.","confidence_millionths":900000}'
                        ),
                        annotations=[],
                    )
                ],
            )
        ],
        "output_parsed": StrictObservation(
            kind="fact",
            summary="The owner has three sisters.",
            confidence_millionths=900_000,
        ),
        "usage": SimpleNamespace(
            input_tokens=100,
            input_tokens_details=SimpleNamespace(
                cached_tokens=20,
                cache_write_tokens=10,
            ),
            output_tokens=7,
            output_tokens_details=SimpleNamespace(reasoning_tokens=2),
            total_tokens=107,
        ),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _transport(actions, budget=None, **changes):
    task, model = _profiles()
    budget = budget or FakeBudget()
    privacy_authorizer = changes.pop(
        "privacy_authorizer",
        FakePrivacyAuthorizer(),
    )
    client = FakeClient(actions)
    values = {
        "enable_token": EXTERNAL_CALL_ENABLE_TOKEN,
        "budget_authorizer": budget,
        "privacy_authorizer": privacy_authorizer,
        "task_profiles": (task,),
        "model_policies": (model,),
        "client": client,
        "sdk_module": FAKE_SDK,
        "retry_delay_seconds": 0,
        "clock": lambda: 150,
    }
    values.update(changes)
    return OpenAIStructuredResponsesTransportV1(**values), client, budget


class OpenAIStructuredTransportTests(unittest.TestCase):
    def test_success_selected_span_only_exact_envelope_and_content_free_audit(self):
        discarded = "Who won America's Next Top Model in 1964?"
        source = f"I have three sisters: Cindy, Lori, and Heidi. {discarded}"
        transport, client, budget = _transport([_response()])
        request = _request(source)

        result = transport.execute(request)

        self.assertEqual(result.parsed.summary, "The owner has three sisters.")
        self.assertEqual(len(client.responses.calls), 1)
        call = client.responses.calls[0]
        self.assertEqual(
            set(call),
            {
                "model", "instructions", "input", "text_format", "store",
                "background", "stream", "parallel_tool_calls", "reasoning",
                "service_tier", "truncation", "safety_identifier",
                "max_output_tokens", "timeout",
            },
        )
        self.assertIn("three sisters", str(call["input"]))
        self.assertNotIn(discarded, str(call["input"]))
        self.assertFalse(call["store"])
        self.assertFalse(call["background"])
        self.assertFalse(call["stream"])
        self.assertFalse(call["parallel_tool_calls"])
        self.assertEqual(call["reasoning"], {"effort": "low"})
        self.assertEqual(call["service_tier"], "default")
        self.assertEqual(call["truncation"], "disabled")
        self.assertEqual(len(budget.reserves), 1)
        self.assertEqual(len(budget.settlements), 1)
        settlement = budget.settlements[0]
        self.assertEqual(settlement.outcome, "success")
        self.assertEqual(settlement.cost_microusd, 231)
        self.assertEqual(settlement.cost_basis, "provider_usage")
        self.assertTrue(settlement.usage_observed)
        public = result.audit.public_dict()
        serialized = json.dumps(public, sort_keys=True)
        for secret in (
            source,
            discarded,
            request.instructions,
            "reservation_0001",
            "privacy_grant_0001",
            "req_test_001",
            "resp_test_001",
        ):
            self.assertNotIn(secret, serialized)
        self.assertEqual(len(public["audit_sha256"]), 64)
        audit_sha256 = public.pop("audit_sha256")
        expected_audit_sha256 = hashlib.sha256(
            json.dumps(
                public,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(audit_sha256, expected_audit_sha256)
        self.assertEqual(public["service_tier"], "default")
        self.assertTrue(public["privacy_authority_verified"])
        self.assertEqual(public["cache_write_input_tokens"], 10)
        self.assertEqual(public["cost_basis"], "provider_usage")
        self.assertTrue(public["usage_observed"])

    def test_disabled_and_non_external_inputs_make_zero_calls(self):
        cases = [
            _request(),
            _request("What is the capital of France?"),
            _request("My father has dementia."),
        ]
        for request in cases:
            with self.subTest(decision=request.gate_result.decision):
                transport, client, budget = _transport(
                    [_response()],
                    enable_token=None,
                )
                with self.assertRaises(StructuredTransportError):
                    transport.execute(request)
                self.assertEqual(client.responses.calls, [])
                self.assertEqual(budget.reserves, [])
                self.assertEqual(budget.settlements, [])

    def test_forged_gate_and_privacy_fail_before_reservation(self):
        request = _request()
        span = request.gate_result.selected_spans[0]
        forged_span = replace(span, content_sha256="0" * 64)
        cases = [
            replace(
                request,
                gate_result=replace(
                    request.gate_result,
                    selected_spans=(forged_span,),
                ),
            ),
            replace(
                request,
                privacy_authorization=_privacy(enable_token="wrong_token"),
            ),
            replace(
                request,
                safety_identifier="mem_" + "0" * 32,
            ),
        ]
        for candidate in cases:
            transport, client, budget = _transport([_response()])
            with self.assertRaises(StructuredTransportError):
                transport.execute(candidate)
            self.assertEqual(client.responses.calls, [])
            self.assertEqual(budget.reserves, [])

    def test_privacy_authority_binds_owner_request_and_expiry(self):
        cases = []
        wrong_owner = FakePrivacyAuthorizer()
        wrong_owner.grant_changes["owner_binding_sha256"] = "f" * 64
        cases.append((wrong_owner, "privacy_grant_owner_binding_sha256_mismatch"))
        expired = FakePrivacyAuthorizer()
        expired.grant_changes["expires_at_epoch_seconds"] = 150
        cases.append((expired, "privacy_grant_not_current"))
        denied = FakePrivacyAuthorizer()
        denied.error = RuntimeError("raw private registry value")
        cases.append((denied, "privacy_authorization_denied"))
        for authority, code in cases:
            transport, client, budget = _transport(
                [_response()],
                privacy_authorizer=authority,
            )
            with self.assertRaises(StructuredTransportError) as caught:
                transport.execute(_request())
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(client.responses.calls, [])
            self.assertEqual(budget.reserves, [])
            self.assertNotIn("raw private", str(caught.exception))
            self.assertIsNone(caught.exception.__context__)

    def test_privacy_expiry_after_budget_reservation_settles_without_call(self):
        now = {"value": 199}
        budget = FakeBudget()
        budget.after_reserve = lambda: now.__setitem__("value", 201)
        transport, client, _ = _transport(
            [_response()],
            budget,
            clock=lambda: now["value"],
        )

        with self.assertRaises(StructuredTransportError) as caught:
            transport.execute(_request())

        self.assertEqual(caught.exception.code, "privacy_grant_not_current")
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(len(budget.reserves), 1)
        self.assertEqual(len(budget.settlements), 1)
        settlement = budget.settlements[0]
        self.assertEqual(settlement.outcome, "request_error")
        self.assertFalse(settlement.external_call_attempted)
        self.assertEqual(settlement.cost_microusd, 0)
        self.assertEqual(settlement.cost_basis, "none")
        self.assertFalse(settlement.usage_observed)
        self.assertEqual(settlement.error_code, "privacy_grant_not_current")
        self.assertEqual(caught.exception.audit.attempt_count, 0)
        self.assertEqual(caught.exception.audit.external_call_count, 0)

    def test_nonfinite_privacy_clock_fails_closed_before_reservation(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                transport, client, budget = _transport(
                    [_response()],
                    clock=lambda value=value: value,
                )
                with self.assertRaises(StructuredTransportError) as caught:
                    transport.execute(_request())
                self.assertEqual(caught.exception.code, "privacy_clock_invalid")
                self.assertEqual(client.responses.calls, [])
                self.assertEqual(budget.reserves, [])
                self.assertEqual(budget.settlements, [])

    def test_enabled_transport_rejects_non_external_gate_decisions(self):
        for source in (
            "What is the capital of France?",
            "My father has dementia.",
            "Hello!",
        ):
            with self.subTest(source=source):
                transport, client, budget = _transport([_response()])
                with self.assertRaises(StructuredTransportError):
                    transport.execute(_request(source))
                self.assertEqual(client.responses.calls, [])
                self.assertEqual(budget.reserves, [])

    def test_unknown_task_or_model_profile_fails_before_reservation(self):
        request = _request()
        for candidate in (
            replace(request, task_contract_sha256="f" * 64),
            replace(request, model_policy_sha256="f" * 64),
        ):
            transport, client, budget = _transport([_response()])
            with self.assertRaises(StructuredTransportError):
                transport.execute(candidate)
            self.assertEqual(client.responses.calls, [])
            self.assertEqual(budget.reserves, [])

    def test_response_topology_and_identity_are_fail_closed(self):
        two_messages = deepcopy(_response().output)
        two_messages.append(deepcopy(two_messages[0]))
        cases = {
            "response_provider_error": {"error": {"code": "bad"}},
            "response_model_mismatch": {"model": "wrong-model"},
            "response_service_tier_mismatch": {"service_tier": "priority"},
            "response_background_mismatch": {"background": True},
            "response_stateful_echo": {"previous_response_id": "resp_prior"},
            "response_tool_configuration_forbidden": {"tools": ["tool"]},
            "response_parallel_tools_mismatch": {"parallel_tool_calls": True},
            "response_truncation_mismatch": {"truncation": "auto"},
            "response_safety_identifier_mismatch": {
                "safety_identifier": "mem_" + "2" * 32
            },
            "response_output_ceiling_mismatch": {"max_output_tokens": 63},
            "response_message_count_invalid": {"output": two_messages},
            "response_message_role_invalid": {
                "output": [replace_namespace(_response().output[0], role="user")]
            },
            "response_content_count_invalid": {
                "output": [replace_namespace(_response().output[0], content=[])]
            },
            "response_reasoning_count_invalid": {
                "output": [
                    SimpleNamespace(type="reasoning"),
                    SimpleNamespace(type="reasoning"),
                    deepcopy(_response().output[0]),
                ]
            },
            "response_output_type_forbidden": {
                "output": [SimpleNamespace(type="function_call")]
            },
            "model_refusal": {
                "output": [
                    replace_namespace(
                        _response().output[0],
                        content=[SimpleNamespace(type="refusal", refusal="no")],
                    )
                ]
            },
            "parsed_output_missing": {"output_parsed": None},
            "response_completed_with_incomplete_details": {
                "incomplete_details": SimpleNamespace(reason="max_output_tokens")
            },
            "response_id_invalid": {"id": "bad"},
            "provider_request_id_invalid": {"_request_id": "bad"},
        }
        for code, changes in cases.items():
            with self.subTest(code=code):
                transport, client, budget = _transport([_response(**changes)])
                with self.assertRaises(StructuredTransportError) as caught:
                    transport.execute(_request())
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(client.responses.calls), 1)
                self.assertEqual(len(budget.settlements), 1)
                self.assertEqual(budget.settlements[0].outcome, "output_error")
                self.assertIsNotNone(budget.settlements[0].cost_microusd)

    def test_output_text_is_strictly_bound_to_sdk_parsed_output(self):
        mismatch = _response()
        mismatch.output_parsed = StrictObservation(
            kind="fact",
            summary="Different provider-side object.",
            confidence_millionths=900_000,
        )
        duplicate = _response()
        duplicate.output[0].content[0].text = (
            '{"kind":"fact","kind":"stance","summary":"The owner has '
            'three sisters.","confidence_millionths":900000}'
        )
        for response in (mismatch, duplicate):
            transport, _, budget = _transport([response])
            with self.assertRaises(StructuredTransportError) as caught:
                transport.execute(_request())
            self.assertEqual(caught.exception.code, "parsed_output_invalid")
            self.assertEqual(budget.settlements[0].outcome, "output_error")

    def test_usage_and_cost_validation(self):
        usage_cases = {
            "response_usage_details_inconsistent": SimpleNamespace(
                input_tokens=10,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=8,
                    cache_write_tokens=3,
                ),
                output_tokens=2,
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                total_tokens=12,
            ),
            "response_usage_inconsistent": SimpleNamespace(
                input_tokens=10,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=0,
                    cache_write_tokens=0,
                ),
                output_tokens=2,
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                total_tokens=13,
            ),
        }
        for code, usage in usage_cases.items():
            transport, _, budget = _transport([_response(usage=usage)])
            with self.assertRaises(StructuredTransportError) as caught:
                transport.execute(_request())
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(budget.settlements[0].outcome, "output_error")

        for code, response in (
            (
                "response_input_exceeds_reservation",
                _response(
                    usage=SimpleNamespace(
                        input_tokens=10_000_000,
                        input_tokens_details=SimpleNamespace(
                            cached_tokens=0,
                            cache_write_tokens=0,
                        ),
                        output_tokens=1,
                        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                        total_tokens=10_000_001,
                    )
                ),
            ),
            (
                "response_output_exceeds_reservation",
                _response(
                    usage=SimpleNamespace(
                        input_tokens=10,
                        input_tokens_details=SimpleNamespace(
                            cached_tokens=0,
                            cache_write_tokens=0,
                        ),
                        output_tokens=65,
                        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
                        total_tokens=75,
                    )
                ),
            ),
        ):
            transport, _, budget = _transport([response])
            with self.assertRaises(StructuredTransportError) as caught:
                transport.execute(_request())
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(budget.settlements[0].outcome, "output_error")

    def test_transient_retry_uses_two_reservations_and_settles_each(self):
        transport, client, budget = _transport([TimeoutError(), _response()])
        result = transport.execute(_request())
        self.assertEqual(result.audit.attempt_count, 2)
        self.assertEqual(len(client.responses.calls), 2)
        self.assertEqual(len(budget.reserves), 2)
        self.assertEqual(
            [item.outcome for item in budget.settlements],
            ["provider_error", "success"],
        )
        self.assertEqual(len(result.audit.reservation_id_sha256s), 2)

    def test_only_explicit_transient_429_and_server_errors_retry(self):
        for error in (
            FakeRateLimitError("rate_limit_exceeded"),
            FakeServerError(503),
        ):
            transport, client, budget = _transport([error, _response()])
            result = transport.execute(_request())
            self.assertEqual(result.audit.attempt_count, 2)
            self.assertEqual(len(client.responses.calls), 2)
            self.assertEqual(len(budget.settlements), 2)

        error = FakeServerError(503)
        error.response.headers["Retry-After"] = "6"
        transport, client, budget = _transport([error, _response()])
        with self.assertRaises(StructuredTransportError):
            transport.execute(_request())
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(len(budget.settlements), 1)

    def test_quota_and_unknown_429_do_not_retry(self):
        for provider_code, expected in (
            ("insufficient_quota", "transport_quota_exhausted"),
            (None, "transport_rate_limit_unclassified"),
        ):
            transport, client, budget = _transport(
                [FakeRateLimitError(provider_code), _response()]
            )
            with self.assertRaises(StructuredTransportError) as caught:
                transport.execute(_request())
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(len(client.responses.calls), 1)
            self.assertEqual(len(budget.reserves), 1)
            self.assertEqual(len(budget.settlements), 1)

    def test_second_reservation_denial_does_not_make_second_provider_call(self):
        budget = FakeBudget()
        budget.reserve_errors[2] = RuntimeError("denied")
        transport, client, _ = _transport([TimeoutError(), _response()], budget)
        with self.assertRaises(StructuredTransportError) as caught:
            transport.execute(_request())
        self.assertEqual(caught.exception.code, "budget_reservation_denied")
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(len(budget.settlements), 1)
        self.assertEqual(len(caught.exception.audit.reservation_id_sha256s), 1)

    def test_duplicate_reservation_id_is_rejected_before_second_call(self):
        budget = FakeBudget()
        budget.grant_changes[2] = {"reservation_id": "reservation_0001"}
        transport, client, _ = _transport([TimeoutError(), _response()], budget)
        with self.assertRaises(StructuredTransportError) as caught:
            transport.execute(_request())
        self.assertEqual(caught.exception.code, "budget_reservation_reused")
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(len(budget.settlements), 1)

    def test_settlement_failure_suppresses_valid_output(self):
        budget = FakeBudget()
        budget.fail_settlement = True
        transport, _, _ = _transport([_response()], budget)
        with self.assertRaises(StructuredTransportError) as caught:
            transport.execute(_request())
        self.assertEqual(caught.exception.code, "budget_settlement_failed")

    def test_provider_exception_is_sanitized_and_charged_fail_closed(self):
        raw = "private owner statement must never enter a traceback"
        error = FakeServerError(400)
        error.args = (raw,)
        transport, _, budget = _transport([error])
        with self.assertRaises(StructuredTransportError) as caught:
            transport.execute(_request(max_attempts=1))
        self.assertEqual(caught.exception.code, "transport_client_error")
        self.assertNotIn(raw, str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertGreater(budget.settlements[0].cost_microusd, 0)
        self.assertEqual(
            budget.settlements[0].cost_basis,
            "reservation_ceiling",
        )
        self.assertFalse(budget.settlements[0].usage_observed)

    def test_budget_grant_mismatch_blocks_provider_call(self):
        mismatches = {
            "budget_model_binding_mismatch": {"model": "wrong-model"},
            "budget_policy_identity_mismatch": {
                "budget_policy_sha256": "f" * 64
            },
            "budget_pricing_identity_mismatch": {
                "pricing_policy_sha256": "f" * 64
            },
            "budget_cost_ceiling_mismatch": {"max_cost_microusd": 1},
        }
        for code, change in mismatches.items():
            budget = FakeBudget()
            budget.grant_changes[1] = change
            transport, client, _ = _transport([_response()], budget)
            with self.assertRaises(StructuredTransportError) as caught:
                transport.execute(_request())
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(client.responses.calls, [])

    def test_strict_schema_and_sdk_version_are_required(self):
        request = _request(output_model=OpenObservation)
        transport, client, budget = _transport([_response()])
        with self.assertRaises(StructuredTransportError) as caught:
            transport.execute(request)
        self.assertEqual(caught.exception.code, "output_model_not_strict")
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(budget.reserves, [])

    def test_preflight_signature_and_sdk_retry_policy_are_exact(self):
        task, model = _profiles()
        budget = FakeBudget()
        transport = OpenAIStructuredResponsesTransportV1(
            enable_token=EXTERNAL_CALL_ENABLE_TOKEN,
            budget_authorizer=budget,
            privacy_authorizer=FakePrivacyAuthorizer(),
            task_profiles=(task,),
            model_policies=(model,),
            client=SimpleNamespace(
                responses=IncompatibleResponses(),
                max_retries=0,
            ),
            sdk_module=FAKE_SDK,
            clock=lambda: 150,
        )
        with self.assertRaises(StructuredTransportError) as caught:
            transport.execute(_request())
        self.assertEqual(caught.exception.code, "openai_parse_signature_incompatible")
        self.assertEqual(budget.reserves, [])

        unsafe_client = FakeClient([_response()])
        unsafe_client.max_retries = 2
        transport, _, budget = _transport(
            [_response()],
            client=unsafe_client,
        )
        with self.assertRaises(StructuredTransportError) as caught:
            transport.execute(_request())
        self.assertEqual(caught.exception.code, "openai_client_retries_not_disabled")
        self.assertEqual(unsafe_client.responses.calls, [])
        self.assertEqual(budget.reserves, [])

        client = FakeClient([_response()])
        sdk = RecordingSDK(client)
        transport = OpenAIStructuredResponsesTransportV1(
            enable_token=EXTERNAL_CALL_ENABLE_TOKEN,
            budget_authorizer=budget,
            privacy_authorizer=FakePrivacyAuthorizer(),
            task_profiles=(task,),
            model_policies=(model,),
            client=None,
            sdk_module=sdk,
            retry_delay_seconds=0,
            clock=lambda: 150,
        )
        transport.execute(_request())
        self.assertEqual(sdk.max_retries, [0])

        request = _request(output_model=StrictObservationWithDefault)
        transport, client, budget = _transport([_response()])
        with self.assertRaises(StructuredTransportError) as caught:
            transport.execute(request)
        self.assertEqual(caught.exception.code, "output_schema_not_strict")
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(budget.reserves, [])

        bad_sdk = SimpleNamespace(**vars(FAKE_SDK))
        bad_sdk.__version__ = "2.6.0"
        transport, client, budget = _transport([_response()], sdk_module=bad_sdk)
        with self.assertRaises(StructuredTransportError) as caught:
            transport.execute(_request())
        self.assertEqual(caught.exception.code, "openai_sdk_version_mismatch")
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(budget.reserves, [])

    def test_privacy_modes_are_explicit(self):
        for mode in (
            "zero_data_retention_verified",
            "modified_abuse_monitoring_verified",
            "standard_retention_explicitly_accepted",
        ):
            transport, _, _ = _transport([_response()])
            result = transport.execute(
                _request(privacy_authorization=_privacy(retention_mode=mode))
            )
            self.assertEqual(result.audit.retention_mode, mode)

        invalid = replace(
            _privacy(),
            standard_retention_risk_accepted=False,
        )
        transport, client, budget = _transport([_response()])
        with self.assertRaises(StructuredTransportError) as caught:
            transport.execute(_request(privacy_authorization=invalid))
        self.assertEqual(caught.exception.code, "standard_retention_not_accepted")
        self.assertEqual(client.responses.calls, [])
        self.assertEqual(budget.reserves, [])

    def test_request_repr_hides_raw_text(self):
        request = _request()
        shown = repr(request)
        self.assertNotIn(request.source_text, shown)
        self.assertNotIn(request.instructions, shown)


def replace_namespace(value: SimpleNamespace, **changes) -> SimpleNamespace:
    result = vars(deepcopy(value))
    result.update(changes)
    return SimpleNamespace(**result)


if __name__ == "__main__":
    unittest.main()
