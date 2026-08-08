from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import importlib
import inspect
import json
import re
import time
from typing import Any, Generic, Iterable, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from rag_engine.memory_v1_personal_evidence_prefilter_v1 import (
    CONTRACT_VERSION as PREFILTER_CONTRACT_VERSION,
    POLICY_SHA256 as PREFILTER_POLICY_SHA256,
    POLICY_VERSION as PREFILTER_POLICY_VERSION,
    TRUSTED_SOURCE_ROLE,
    PersonalEvidencePrefilterResultV1,
    classify_personal_evidence_v1,
)


CONTRACT_VERSION = "memory_v1_openai_structured_transport_v1"
SELECTED_INPUT_CONTRACT_VERSION = "memory_v1_openai_selected_spans_v2"
EXTERNAL_CALL_ENABLE_TOKEN = "memory_v1_openai_structured_calls_v1"
PRIVACY_AUTHORIZATION_TOKEN = "memory_v1_openai_privacy_authorization_v1"
TASK_PROFILE_CONTRACT_VERSION = "memory_v1_openai_task_profile_v1"
MODEL_POLICY_CONTRACT_VERSION = "memory_v1_openai_model_policy_v1"
MAX_SOURCE_CHARS = 200_000
MAX_SELECTED_INPUT_BYTES = 65_536
MAX_INSTRUCTIONS_CHARS = 120_000
MAX_OUTPUT_TOKENS = 4_096
MAX_PARSED_OUTPUT_BYTES = 524_288
INPUT_TOKEN_CEILING_FIXED_OVERHEAD = 1_024
MAX_PRIVACY_GRANT_SECONDS = 2_678_400

Purpose = Literal[
    "memory_extraction",
    "entity_validation",
    "observation_entailment",
]
RetentionMode = Literal[
    "zero_data_retention_verified",
    "modified_abuse_monitoring_verified",
    "standard_retention_explicitly_accepted",
]
SettlementOutcome = Literal[
    "success",
    "provider_error",
    "output_error",
    "request_error",
]
CostBasis = Literal["none", "provider_usage", "reservation_ceiling"]

_PURPOSES = {
    "memory_extraction",
    "entity_validation",
    "observation_entailment",
}
_RETENTION_MODES = {
    "zero_data_retention_verified",
    "modified_abuse_monitoring_verified",
    "standard_retention_explicitly_accepted",
}
_NONRETRYABLE_429_CODES = {
    "billing_hard_limit_reached",
    "billing_not_active",
    "credit_balance_exhausted",
    "insufficient_quota",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
    "usage_limit_reached",
}
_TRANSIENT_429_CODES = {
    "rate_limit_exceeded",
    "requests_per_minute_exceeded",
    "server_overloaded",
    "tokens_per_minute_exceeded",
}
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9._-]{0,40})?$")
_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{7,99}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFETY_ID_RE = re.compile(r"^mem_[0-9a-f]{32}$")
_RESPONSE_ID_RE = re.compile(r"^resp_[A-Za-z0-9_-]{1,190}$")
_PROVIDER_REQUEST_ID_RE = re.compile(r"^req_[A-Za-z0-9_-]{1,190}$")
_REQUIRED_PARSE_PARAMETERS = {
    "background",
    "input",
    "instructions",
    "max_output_tokens",
    "model",
    "parallel_tool_calls",
    "reasoning",
    "safety_identifier",
    "service_tier",
    "store",
    "stream",
    "text_format",
    "timeout",
    "truncation",
}

OutputT = TypeVar("OutputT", bound=BaseModel)


class StructuredTransportError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        audit: StructuredResponsesAuditV1 | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status
        self.audit = audit


@dataclass(frozen=True)
class ExternalPrivacyAuthorizationV1:
    policy_version: str
    policy_sha256: str
    retention_mode: RetentionMode
    authorization_sha256: str
    standard_retention_risk_accepted: bool
    retention_attestation_sha256: str | None
    enable_token: str

    def validate(self) -> None:
        if not _SAFE_ID_RE.fullmatch(self.policy_version):
            raise StructuredTransportError("privacy_policy_version_invalid")
        if not _SHA256_RE.fullmatch(self.policy_sha256):
            raise StructuredTransportError("privacy_policy_identity_invalid")
        if self.retention_mode not in _RETENTION_MODES:
            raise StructuredTransportError("privacy_retention_mode_invalid")
        if not _SHA256_RE.fullmatch(self.authorization_sha256):
            raise StructuredTransportError("privacy_authorization_invalid")
        if self.enable_token != PRIVACY_AUTHORIZATION_TOKEN:
            raise StructuredTransportError("privacy_authorization_missing")
        if self.retention_mode == "standard_retention_explicitly_accepted":
            if self.standard_retention_risk_accepted is not True:
                raise StructuredTransportError("standard_retention_not_accepted")
            if self.retention_attestation_sha256 is not None:
                raise StructuredTransportError("standard_retention_false_attestation")
            return
        if self.standard_retention_risk_accepted is not False:
            raise StructuredTransportError("verified_retention_acceptance_conflict")
        if not isinstance(self.retention_attestation_sha256, str) or not (
            _SHA256_RE.fullmatch(self.retention_attestation_sha256)
        ):
            raise StructuredTransportError("retention_attestation_missing")


@dataclass(frozen=True)
class PrivacyAuthorizationGrantV1:
    grant_id: str
    authorized: bool
    durable: bool
    request_sha256: str
    owner_binding_sha256: str
    safety_identifier: str
    purpose: Purpose
    task_contract_sha256: str
    model_policy_sha256: str
    gate_policy_sha256: str
    privacy_policy_version: str
    privacy_policy_sha256: str
    privacy_authorization_sha256: str
    retention_mode: RetentionMode
    standard_retention_risk_accepted: bool
    retention_attestation_sha256: str | None
    issued_at_epoch_seconds: int
    expires_at_epoch_seconds: int

    def validate(
        self,
        *,
        request: StructuredResponsesRequestV1[Any],
        now_epoch_seconds: int,
    ) -> None:
        expected = {
            "request_sha256": request.request_sha256,
            "owner_binding_sha256": request.owner_binding_sha256,
            "safety_identifier": request.safety_identifier,
            "purpose": request.purpose,
            "task_contract_sha256": request.task_contract_sha256,
            "model_policy_sha256": request.model_policy_sha256,
            "gate_policy_sha256": request.gate_result.policy_sha256,
            "privacy_policy_version": request.privacy_authorization.policy_version,
            "privacy_policy_sha256": request.privacy_authorization.policy_sha256,
            "privacy_authorization_sha256": (
                request.privacy_authorization.authorization_sha256
            ),
            "retention_mode": request.privacy_authorization.retention_mode,
            "standard_retention_risk_accepted": (
                request.privacy_authorization.standard_retention_risk_accepted
            ),
            "retention_attestation_sha256": (
                request.privacy_authorization.retention_attestation_sha256
            ),
        }
        if not _SAFE_ID_RE.fullmatch(self.grant_id):
            raise StructuredTransportError("privacy_grant_id_invalid")
        if self.authorized is not True or self.durable is not True:
            raise StructuredTransportError("privacy_grant_not_authoritative")
        for field_name, expected_value in expected.items():
            if getattr(self, field_name) != expected_value:
                raise StructuredTransportError(
                    f"privacy_grant_{field_name}_mismatch"
                )
        for value in (self.issued_at_epoch_seconds, self.expires_at_epoch_seconds):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise StructuredTransportError("privacy_grant_time_invalid")
        if not (
            self.issued_at_epoch_seconds
            <= now_epoch_seconds
            < self.expires_at_epoch_seconds
        ):
            raise StructuredTransportError("privacy_grant_not_current")
        if self.expires_at_epoch_seconds - self.issued_at_epoch_seconds > (
            MAX_PRIVACY_GRANT_SECONDS
        ):
            raise StructuredTransportError("privacy_grant_window_too_long")


class AuthoritativePrivacyAuthorizerV1(Protocol):
    def authorize(
        self,
        *,
        request_sha256: str,
        owner_binding_sha256: str,
        safety_identifier: str,
        purpose: Purpose,
        task_contract_sha256: str,
        model_policy_sha256: str,
        gate_policy_sha256: str,
        privacy_authorization: ExternalPrivacyAuthorizationV1,
    ) -> PrivacyAuthorizationGrantV1:
        ...


@dataclass(frozen=True)
class StructuredTaskProfileV1:
    profile_version: str
    purpose: Purpose
    output_schema_sha256: str
    instructions: str = field(repr=False)
    contract_sha256: str = ""

    @classmethod
    def create(
        cls,
        *,
        profile_version: str,
        purpose: Purpose,
        instructions: str,
        output_model: type[BaseModel],
    ) -> StructuredTaskProfileV1:
        output_schema_sha256 = _canonical_sha256(output_model.model_json_schema())
        provisional = cls(
            profile_version=profile_version,
            purpose=purpose,
            output_schema_sha256=output_schema_sha256,
            instructions=instructions,
        )
        return cls(
            profile_version=profile_version,
            purpose=purpose,
            output_schema_sha256=output_schema_sha256,
            instructions=instructions,
            contract_sha256=provisional.expected_contract_sha256,
        )

    @property
    def expected_contract_sha256(self) -> str:
        return _canonical_sha256(
            {
                "contract_version": TASK_PROFILE_CONTRACT_VERSION,
                "profile_version": self.profile_version,
                "purpose": self.purpose,
                "output_schema_sha256": self.output_schema_sha256,
                "instructions_sha256": hashlib.sha256(
                    self.instructions.encode("utf-8")
                ).hexdigest(),
            }
        )

    def validate(self) -> None:
        if not _SAFE_ID_RE.fullmatch(self.profile_version):
            raise StructuredTransportError("task_profile_version_invalid")
        if self.purpose not in _PURPOSES:
            raise StructuredTransportError("task_profile_purpose_invalid")
        if not _SHA256_RE.fullmatch(self.output_schema_sha256):
            raise StructuredTransportError("task_profile_schema_identity_invalid")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise StructuredTransportError("task_profile_instructions_empty")
        if len(self.instructions) > MAX_INSTRUCTIONS_CHARS:
            raise StructuredTransportError("task_profile_instructions_oversize")
        if self.contract_sha256 != self.expected_contract_sha256:
            raise StructuredTransportError("task_profile_identity_invalid")


@dataclass(frozen=True)
class StructuredModelPolicyV1:
    policy_version: str
    model: str
    sdk_package_version: str
    reasoning_effort: Literal["low"]
    service_tier: Literal["default"]
    max_output_tokens_ceiling: int
    timeout_seconds_ceiling: int
    policy_sha256: str = ""

    @classmethod
    def create(
        cls,
        *,
        policy_version: str,
        model: str,
        sdk_package_version: str,
        max_output_tokens_ceiling: int,
        timeout_seconds_ceiling: int,
    ) -> StructuredModelPolicyV1:
        provisional = cls(
            policy_version=policy_version,
            model=model,
            sdk_package_version=sdk_package_version,
            reasoning_effort="low",
            service_tier="default",
            max_output_tokens_ceiling=max_output_tokens_ceiling,
            timeout_seconds_ceiling=timeout_seconds_ceiling,
        )
        return cls(
            policy_version=policy_version,
            model=model,
            sdk_package_version=sdk_package_version,
            reasoning_effort="low",
            service_tier="default",
            max_output_tokens_ceiling=max_output_tokens_ceiling,
            timeout_seconds_ceiling=timeout_seconds_ceiling,
            policy_sha256=provisional.expected_policy_sha256,
        )

    @property
    def expected_policy_sha256(self) -> str:
        return _canonical_sha256(
            {
                "contract_version": MODEL_POLICY_CONTRACT_VERSION,
                "policy_version": self.policy_version,
                "model": self.model,
                "sdk_package_version": self.sdk_package_version,
                "reasoning_effort": self.reasoning_effort,
                "service_tier": self.service_tier,
                "max_output_tokens_ceiling": self.max_output_tokens_ceiling,
                "timeout_seconds_ceiling": self.timeout_seconds_ceiling,
            }
        )

    def validate(self) -> None:
        if not _SAFE_ID_RE.fullmatch(self.policy_version):
            raise StructuredTransportError("model_policy_version_invalid")
        if not _MODEL_RE.fullmatch(self.model):
            raise StructuredTransportError("model_policy_model_invalid")
        if not _VERSION_RE.fullmatch(self.sdk_package_version):
            raise StructuredTransportError("model_policy_sdk_version_invalid")
        if self.reasoning_effort != "low" or self.service_tier != "default":
            raise StructuredTransportError("model_policy_runtime_invalid")
        if not isinstance(self.max_output_tokens_ceiling, int) or isinstance(
            self.max_output_tokens_ceiling, bool
        ) or not (16 <= self.max_output_tokens_ceiling <= MAX_OUTPUT_TOKENS):
            raise StructuredTransportError("model_policy_output_ceiling_invalid")
        if not isinstance(self.timeout_seconds_ceiling, int) or isinstance(
            self.timeout_seconds_ceiling, bool
        ) or not (1 <= self.timeout_seconds_ceiling <= 180):
            raise StructuredTransportError("model_policy_timeout_ceiling_invalid")
        if self.policy_sha256 != self.expected_policy_sha256:
            raise StructuredTransportError("model_policy_identity_invalid")


@dataclass(frozen=True)
class BudgetReservationGrantV1:
    reservation_id: str
    authorized: bool
    durable: bool
    request_sha256: str
    attempt_ordinal: int
    model: str
    privacy_policy_sha256: str
    output_schema_sha256: str
    budget_policy_version: str
    budget_policy_sha256: str
    pricing_policy_version: str
    pricing_policy_sha256: str
    input_microusd_per_million_tokens: int
    cached_input_microusd_per_million_tokens: int
    cache_write_input_microusd_per_million_tokens: int
    output_microusd_per_million_tokens: int
    max_cost_microusd: int

    def validate(
        self,
        *,
        request_sha256: str,
        attempt_ordinal: int,
        model: str,
        privacy_policy_sha256: str,
        output_schema_sha256: str,
        budget_policy_version: str,
        budget_policy_sha256: str,
        pricing_policy_version: str,
        pricing_policy_sha256: str,
        estimated_input_tokens: int,
        max_output_tokens: int,
    ) -> None:
        if not _SAFE_ID_RE.fullmatch(self.reservation_id):
            raise StructuredTransportError("budget_reservation_id_invalid")
        if self.authorized is not True or self.durable is not True:
            raise StructuredTransportError("budget_reservation_not_authoritative")
        if self.request_sha256 != request_sha256:
            raise StructuredTransportError("budget_request_binding_mismatch")
        if self.attempt_ordinal != attempt_ordinal:
            raise StructuredTransportError("budget_attempt_binding_mismatch")
        if self.model != model:
            raise StructuredTransportError("budget_model_binding_mismatch")
        if self.privacy_policy_sha256 != privacy_policy_sha256:
            raise StructuredTransportError("budget_privacy_binding_mismatch")
        if self.output_schema_sha256 != output_schema_sha256:
            raise StructuredTransportError("budget_schema_binding_mismatch")
        if self.budget_policy_version != budget_policy_version:
            raise StructuredTransportError("budget_policy_version_mismatch")
        if self.budget_policy_sha256 != budget_policy_sha256:
            raise StructuredTransportError("budget_policy_identity_mismatch")
        if self.pricing_policy_version != pricing_policy_version:
            raise StructuredTransportError("budget_pricing_version_mismatch")
        if self.pricing_policy_sha256 != pricing_policy_sha256:
            raise StructuredTransportError("budget_pricing_identity_mismatch")
        if not _SAFE_ID_RE.fullmatch(self.budget_policy_version):
            raise StructuredTransportError("budget_policy_version_invalid")
        if not _SHA256_RE.fullmatch(self.budget_policy_sha256):
            raise StructuredTransportError("budget_policy_identity_invalid")
        if not _SAFE_ID_RE.fullmatch(self.pricing_policy_version):
            raise StructuredTransportError("budget_pricing_version_invalid")
        if not _SHA256_RE.fullmatch(self.pricing_policy_sha256):
            raise StructuredTransportError("budget_pricing_identity_invalid")
        for value in (
            self.input_microusd_per_million_tokens,
            self.cached_input_microusd_per_million_tokens,
            self.cache_write_input_microusd_per_million_tokens,
            self.output_microusd_per_million_tokens,
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not (
                0 < value <= 100_000_000
            ):
                raise StructuredTransportError("budget_pricing_rate_invalid")
        if not isinstance(self.max_cost_microusd, int) or isinstance(
            self.max_cost_microusd, bool
        ):
            raise StructuredTransportError("budget_cost_ceiling_invalid")
        expected_max = _token_cost_microusd(
            estimated_input_tokens,
            max(
                self.input_microusd_per_million_tokens,
                self.cached_input_microusd_per_million_tokens,
                self.cache_write_input_microusd_per_million_tokens,
            ),
        ) + _token_cost_microusd(
            max_output_tokens,
            self.output_microusd_per_million_tokens,
        )
        if self.max_cost_microusd != expected_max:
            raise StructuredTransportError("budget_cost_ceiling_mismatch")


@dataclass(frozen=True)
class BudgetSettlementV1:
    reservation_id: str
    request_sha256: str
    attempt_ordinal: int
    external_call_attempted: bool
    outcome: SettlementOutcome
    error_code: str | None
    provider_request_id: str | None
    response_id: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    cost_microusd: int | None
    cost_basis: CostBasis
    usage_observed: bool


class DurableCallBudgetAuthorizerV1(Protocol):
    def reserve(
        self,
        *,
        request_sha256: str,
        attempt_ordinal: int,
        model: str,
        estimated_input_tokens: int,
        max_output_tokens: int,
        timeout_seconds: float,
        privacy_policy_sha256: str,
        output_schema_sha256: str,
        budget_policy_version: str,
        budget_policy_sha256: str,
        pricing_policy_version: str,
        pricing_policy_sha256: str,
    ) -> BudgetReservationGrantV1:
        ...

    def settle(self, settlement: BudgetSettlementV1) -> None:
        ...


@dataclass(frozen=True)
class StructuredResponsesRequestV1(Generic[OutputT]):
    request_id: str
    pipeline_version: str
    purpose: Purpose
    model: str
    task_contract_sha256: str
    model_policy_sha256: str
    budget_policy_version: str
    budget_policy_sha256: str
    pricing_policy_version: str
    pricing_policy_sha256: str
    owner_binding_sha256: str
    instructions: str = field(repr=False)
    source_text: str = field(repr=False)
    gate_result: PersonalEvidencePrefilterResultV1
    privacy_authorization: ExternalPrivacyAuthorizationV1
    output_model: type[OutputT]
    safety_identifier: str
    max_output_tokens: int
    timeout_seconds: float
    max_attempts: int = 2

    def validate(self) -> None:
        if not _SAFE_ID_RE.fullmatch(self.request_id):
            raise StructuredTransportError("request_id_invalid")
        if not _SAFE_ID_RE.fullmatch(self.pipeline_version):
            raise StructuredTransportError("pipeline_version_invalid")
        if self.purpose not in _PURPOSES:
            raise StructuredTransportError("request_purpose_invalid")
        if not _MODEL_RE.fullmatch(self.model):
            raise StructuredTransportError("request_model_invalid")
        if not _SHA256_RE.fullmatch(self.task_contract_sha256):
            raise StructuredTransportError("task_contract_identity_invalid")
        if not _SHA256_RE.fullmatch(self.model_policy_sha256):
            raise StructuredTransportError("model_policy_identity_invalid")
        if not _SAFE_ID_RE.fullmatch(self.budget_policy_version):
            raise StructuredTransportError("budget_policy_version_invalid")
        if not _SHA256_RE.fullmatch(self.budget_policy_sha256):
            raise StructuredTransportError("budget_policy_identity_invalid")
        if not _SAFE_ID_RE.fullmatch(self.pricing_policy_version):
            raise StructuredTransportError("pricing_policy_version_invalid")
        if not _SHA256_RE.fullmatch(self.pricing_policy_sha256):
            raise StructuredTransportError("pricing_policy_identity_invalid")
        if not _SHA256_RE.fullmatch(self.owner_binding_sha256):
            raise StructuredTransportError("owner_binding_identity_invalid")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise StructuredTransportError("request_instructions_empty")
        if len(self.instructions) > MAX_INSTRUCTIONS_CHARS:
            raise StructuredTransportError("request_instructions_oversize")
        if not isinstance(self.source_text, str) or not self.source_text.strip():
            raise StructuredTransportError("request_source_empty")
        if len(self.source_text) > MAX_SOURCE_CHARS:
            raise StructuredTransportError("request_source_oversize")
        if not isinstance(self.output_model, type) or not issubclass(
            self.output_model, BaseModel
        ):
            raise StructuredTransportError("output_model_invalid")
        config = self.output_model.model_config
        if (
            config.get("extra") != "forbid"
            or config.get("strict") is not True
            or config.get("frozen") is not True
        ):
            raise StructuredTransportError("output_model_not_strict")
        schema = self.output_model.model_json_schema()
        schema_bytes = _canonical_json(schema).encode("utf-8")
        if len(schema_bytes) > 65_536 or not _structured_schema_is_strict(schema):
            raise StructuredTransportError("output_schema_not_strict")
        if not _SAFETY_ID_RE.fullmatch(self.safety_identifier):
            raise StructuredTransportError("safety_identifier_invalid")
        if self.safety_identifier != owner_safety_identifier_v1(
            self.owner_binding_sha256
        ):
            raise StructuredTransportError("safety_identifier_owner_mismatch")
        if not isinstance(self.max_output_tokens, int) or isinstance(
            self.max_output_tokens, bool
        ) or not (16 <= self.max_output_tokens <= MAX_OUTPUT_TOKENS):
            raise StructuredTransportError("max_output_tokens_invalid")
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(
            self.timeout_seconds, bool
        ) or not (1.0 <= float(self.timeout_seconds) <= 180.0):
            raise StructuredTransportError("timeout_invalid")
        if not isinstance(self.max_attempts, int) or isinstance(
            self.max_attempts, bool
        ) or self.max_attempts not in {1, 2}:
            raise StructuredTransportError("max_attempts_invalid")
        self._validate_gate()
        self.privacy_authorization.validate()
        if len(self.selected_input_text.encode("utf-8")) > MAX_SELECTED_INPUT_BYTES:
            raise StructuredTransportError("selected_input_oversize")

    def _validate_gate(self) -> None:
        gate = self.gate_result
        if not isinstance(gate, PersonalEvidencePrefilterResultV1):
            raise StructuredTransportError("gate_result_type_invalid")
        if gate.contract_version != PREFILTER_CONTRACT_VERSION:
            raise StructuredTransportError("gate_contract_mismatch")
        if gate.policy_version != PREFILTER_POLICY_VERSION:
            raise StructuredTransportError("gate_policy_version_mismatch")
        if gate.policy_sha256 != PREFILTER_POLICY_SHA256:
            raise StructuredTransportError("gate_policy_identity_mismatch")
        authoritative_gate = classify_personal_evidence_v1(
            self.source_text,
            source_role=TRUSTED_SOURCE_ROLE,
        )
        if gate != authoritative_gate:
            raise StructuredTransportError("gate_result_not_authoritative")
        if gate.decision != "send_external":
            raise StructuredTransportError("gate_decision_not_external")
        if gate.reason_codes != ("personal_evidence_selected",):
            raise StructuredTransportError("gate_reason_invalid")
        if not 1 <= len(gate.selected_spans) <= 64:
            raise StructuredTransportError("gate_span_count_invalid")
        cursor = 0
        for span in gate.selected_spans:
            if not (0 <= span.char_start < span.char_end <= len(self.source_text)):
                raise StructuredTransportError("gate_span_bounds_invalid")
            if span.char_start < cursor:
                raise StructuredTransportError("gate_span_order_invalid")
            cursor = span.char_end
            selected = self.source_text[span.char_start:span.char_end]
            if hashlib.sha256(selected.encode("utf-8")).hexdigest() != (
                span.content_sha256
            ):
                raise StructuredTransportError("gate_span_hash_mismatch")
            if span.sensitivity != "ordinary":
                raise StructuredTransportError("gate_sensitive_payload_forbidden")

    @property
    def selected_input_text(self) -> str:
        spans = []
        for ordinal, span in enumerate(self.gate_result.selected_spans):
            spans.append(
                {
                    "assertion_mode": span.assertion_mode,
                    "category": span.category,
                    "char_end": span.char_end,
                    "char_start": span.char_start,
                    "content_sha256": span.content_sha256,
                    "ordinal": ordinal,
                    "subject_hint": span.subject_hint,
                    "text": self.source_text[span.char_start:span.char_end],
                }
            )
        return _canonical_json(
            {
                "contract_version": SELECTED_INPUT_CONTRACT_VERSION,
                "spans": spans,
            }
        )

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()

    @property
    def selected_input_sha256(self) -> str:
        return hashlib.sha256(self.selected_input_text.encode("utf-8")).hexdigest()

    @property
    def instructions_sha256(self) -> str:
        return hashlib.sha256(self.instructions.encode("utf-8")).hexdigest()

    @property
    def output_schema_sha256(self) -> str:
        return _canonical_sha256(self.output_model.model_json_schema())

    @property
    def request_sha256(self) -> str:
        return _canonical_sha256(
            {
                "contract_version": CONTRACT_VERSION,
                "request_id": self.request_id,
                "pipeline_version": self.pipeline_version,
                "purpose": self.purpose,
                "model": self.model,
                "task_contract_sha256": self.task_contract_sha256,
                "model_policy_sha256": self.model_policy_sha256,
                "budget_policy_version": self.budget_policy_version,
                "budget_policy_sha256": self.budget_policy_sha256,
                "pricing_policy_version": self.pricing_policy_version,
                "pricing_policy_sha256": self.pricing_policy_sha256,
                "owner_binding_sha256": self.owner_binding_sha256,
                "instructions_sha256": self.instructions_sha256,
                "source_sha256": self.source_sha256,
                "selected_input_sha256": self.selected_input_sha256,
                "output_schema_sha256": self.output_schema_sha256,
                "gate_policy_sha256": self.gate_result.policy_sha256,
                "privacy_policy_sha256": self.privacy_authorization.policy_sha256,
                "privacy_authorization_sha256": (
                    self.privacy_authorization.authorization_sha256
                ),
                "retention_mode": self.privacy_authorization.retention_mode,
                "safety_identifier": self.safety_identifier,
                "max_output_tokens": self.max_output_tokens,
                "timeout_seconds": float(self.timeout_seconds),
                "max_attempts": self.max_attempts,
            }
        )

    @property
    def estimated_input_tokens(self) -> int:
        schema = _canonical_json(self.output_model.model_json_schema())
        return max(
            1,
            INPUT_TOKEN_CEILING_FIXED_OVERHEAD
            + len(self.instructions.encode("utf-8"))
            + len(self.selected_input_text.encode("utf-8"))
            + len(schema.encode("utf-8")),
        )

    def sdk_kwargs(self) -> dict[str, Any]:
        self.validate()
        return {
            "model": self.model,
            "instructions": self.instructions,
            "input": self.selected_input_text,
            "text_format": self.output_model,
            "store": False,
            "background": False,
            "stream": False,
            "parallel_tool_calls": False,
            "reasoning": {"effort": "low"},
            "service_tier": "default",
            "truncation": "disabled",
            "safety_identifier": self.safety_identifier,
            "max_output_tokens": self.max_output_tokens,
            "timeout": float(self.timeout_seconds),
        }


@dataclass(frozen=True)
class ResponsesUsageV1:
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class StructuredResponsesAuditV1:
    contract_version: str
    request_id_sha256: str
    request_sha256: str
    pipeline_version: str
    purpose: Purpose
    model_requested: str
    model_returned: str | None
    output_schema_sha256: str
    task_contract_sha256: str
    model_policy_sha256: str
    owner_binding_sha256: str
    gate_policy_sha256: str
    privacy_policy_sha256: str
    privacy_authorization_sha256: str
    privacy_grant_id_sha256: str | None
    privacy_authority_verified: bool
    retention_mode: RetentionMode
    attempt_count: int
    external_call_count: int
    reservation_id_sha256s: tuple[str, ...]
    budget_policy_version: str
    budget_policy_sha256: str
    pricing_policy_version: str
    pricing_policy_sha256: str
    provider_request_id_sha256: str | None
    response_id_sha256: str | None
    service_tier: str | None
    response_status: str
    store_requested: bool
    stateless_requested: bool
    no_tools_requested: bool
    background_requested: bool
    reasoning_effort_requested: str
    refusal: bool
    incomplete_reason: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    cost_microusd: int | None
    cost_basis: CostBasis
    usage_observed: bool
    error_code: str | None
    http_status: int | None

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["audit_sha256"] = _canonical_sha256(value)
        return value


@dataclass(frozen=True)
class StructuredResponsesResultV1(Generic[OutputT]):
    parsed: OutputT
    audit: StructuredResponsesAuditV1


class OpenAIStructuredResponsesTransportV1:
    def __init__(
        self,
        *,
        enable_token: str | None,
        budget_authorizer: DurableCallBudgetAuthorizerV1,
        privacy_authorizer: AuthoritativePrivacyAuthorizerV1,
        task_profiles: Iterable[StructuredTaskProfileV1],
        model_policies: Iterable[StructuredModelPolicyV1],
        client: Any | None = None,
        sdk_module: Any | None = None,
        retry_delay_seconds: float = 0.25,
        sleep: Any = time.sleep,
        clock: Any = time.time,
    ) -> None:
        if not 0.0 <= float(retry_delay_seconds) <= 5.0:
            raise ValueError("retry_delay_seconds must be between 0 and 5")
        self._enabled = enable_token == EXTERNAL_CALL_ENABLE_TOKEN
        self._budget = budget_authorizer
        self._privacy_authorizer = privacy_authorizer
        self._task_profiles = _task_profile_map(task_profiles)
        self._model_policies = _model_policy_map(model_policies)
        self._client = client
        self._sdk_module = sdk_module
        self._retry_delay_seconds = float(retry_delay_seconds)
        self._sleep = sleep
        self._clock = clock

    def preflight(self, *, expected_sdk_version: str) -> None:
        if not self._enabled:
            raise StructuredTransportError("external_provider_disabled")
        if self._sdk_module is None:
            sdk_import_failed = False
            try:
                self._sdk_module = importlib.import_module("openai")
            except Exception:
                sdk_import_failed = True
            if sdk_import_failed:
                raise StructuredTransportError(
                    "openai_sdk_unavailable"
                )
        if getattr(self._sdk_module, "__version__", None) != expected_sdk_version:
            raise StructuredTransportError("openai_sdk_version_mismatch")
        client = self._client
        if client is None:
            client_creation_failed = False
            try:
                client = self._sdk_module.OpenAI(max_retries=0)
            except Exception:
                client_creation_failed = True
            if client_creation_failed:
                raise StructuredTransportError(
                    "openai_sdk_or_credentials_unavailable"
                )
            self._client = client
        client_max_retries = getattr(client, "max_retries", None)
        if (
            not isinstance(client_max_retries, int)
            or isinstance(client_max_retries, bool)
            or client_max_retries != 0
        ):
            raise StructuredTransportError("openai_client_retries_not_disabled")
        parse = getattr(getattr(client, "responses", None), "parse", None)
        if not callable(parse):
            raise StructuredTransportError("openai_responses_parse_unavailable")
        signature_failed = False
        try:
            parameters = set(inspect.signature(parse).parameters)
        except (TypeError, ValueError):
            signature_failed = True
            parameters = set()
        if signature_failed:
            raise StructuredTransportError(
                "openai_parse_signature_unavailable"
            )
        if _REQUIRED_PARSE_PARAMETERS - parameters:
            raise StructuredTransportError("openai_parse_signature_incompatible")

    def execute(
        self,
        request: StructuredResponsesRequestV1[OutputT],
    ) -> StructuredResponsesResultV1[OutputT]:
        request.validate()
        try:
            _, model_policy = self._authorize_request(request)
        except StructuredTransportError as error:
            error.audit = _audit(request, error=error)
            raise
        try:
            privacy_grant = self._authorize_privacy(request)
        except StructuredTransportError as error:
            error.audit = _audit(request, error=error)
            raise
        try:
            sdk_kwargs = request.sdk_kwargs()
        except StructuredTransportError as error:
            error.audit = _audit(
                request,
                privacy_grant=privacy_grant,
                error=error,
            )
            raise
        reservations: list[str] = []
        try:
            self.preflight(expected_sdk_version=model_policy.sdk_package_version)
        except StructuredTransportError as error:
            error.audit = _audit(
                request,
                privacy_grant=privacy_grant,
                error=error,
            )
            raise

        for attempt in range(1, request.max_attempts + 1):
            try:
                privacy_grant.validate(
                    request=request,
                    now_epoch_seconds=self._current_epoch_seconds(),
                )
            except StructuredTransportError as error:
                error.audit = _audit(
                    request,
                    attempt_count=attempt - 1,
                    reservations=reservations,
                    privacy_grant=privacy_grant,
                    error=error,
                )
                raise
            grant = self._reserve(
                request,
                attempt,
                reservations,
                privacy_grant,
            )
            if grant.reservation_id in reservations:
                error = StructuredTransportError("budget_reservation_reused")
                error.audit = _audit(
                    request,
                    attempt_count=attempt - 1,
                    reservations=reservations,
                    privacy_grant=privacy_grant,
                    error=error,
                )
                raise error
            reservations.append(grant.reservation_id)
            try:
                privacy_grant.validate(
                    request=request,
                    now_epoch_seconds=self._current_epoch_seconds(),
                )
            except StructuredTransportError as error:
                settlement = _settlement(
                    request,
                    grant,
                    outcome="request_error",
                    error=error,
                    external_call_attempted=False,
                )
                self._settle_or_raise(
                    settlement,
                    request,
                    reservations,
                    grant,
                    privacy_grant,
                )
                error.audit = _audit(
                    request,
                    attempt_count=attempt - 1,
                    reservations=reservations,
                    privacy_grant=privacy_grant,
                    error=error,
                    cost_microusd=settlement.cost_microusd,
                    cost_basis=settlement.cost_basis,
                    usage_observed=settlement.usage_observed,
                )
                raise
            provider_error: StructuredTransportError | None = None
            retry_delay: float | None = None
            try:
                if self._client is None:
                    raise AssertionError("OpenAI client disappeared after preflight")
                response = self._client.responses.parse(
                    **sdk_kwargs,
                )
            except Exception as exc:
                provider_error = _classify_transport_exception(
                    exc,
                    self._sdk_module,
                )
                retry_delay = _bounded_retry_delay(
                    exc,
                    self._retry_delay_seconds,
                )
            if provider_error is not None:
                settlement = _settlement(
                    request,
                    grant,
                    outcome="provider_error",
                    error=provider_error,
                )
                self._settle_or_raise(
                    settlement,
                    request,
                    reservations,
                    grant,
                    privacy_grant,
                )
                provider_error.audit = _audit(
                    request,
                    attempt_count=attempt,
                    reservations=reservations,
                    privacy_grant=privacy_grant,
                    error=provider_error,
                    cost_microusd=settlement.cost_microusd,
                    cost_basis=settlement.cost_basis,
                    usage_observed=settlement.usage_observed,
                )
                if (
                    provider_error.retryable
                    and attempt < request.max_attempts
                    and retry_delay is not None
                ):
                    if retry_delay:
                        self._sleep(retry_delay)
                    continue
                raise provider_error

            response_error, usage, cost = _validate_response(
                response,
                request,
                grant,
            )
            parsed: OutputT | None = None
            if response_error is None:
                try:
                    output_text = _response_output_text(response)
                    if len(output_text.encode("utf-8")) > MAX_PARSED_OUTPUT_BYTES:
                        raise ValueError("output text exceeds byte limit")
                    parsed = request.output_model.model_validate(
                        _strict_json_loads(output_text),
                        strict=True,
                    )
                    sdk_parsed_value = _value(response, "output_parsed")
                    if isinstance(sdk_parsed_value, request.output_model):
                        sdk_parsed = sdk_parsed_value
                    else:
                        sdk_parsed = request.output_model.model_validate(
                            sdk_parsed_value,
                            strict=True,
                        )
                    if parsed.model_dump(mode="json") != sdk_parsed.model_dump(
                        mode="json"
                    ):
                        raise ValueError("SDK parsed output mismatch")
                    if len(parsed.model_dump_json().encode("utf-8")) > (
                        MAX_PARSED_OUTPUT_BYTES
                    ):
                        raise ValueError("parsed output exceeds byte limit")
                except (ValidationError, TypeError, ValueError):
                    response_error = StructuredTransportError(
                        "parsed_output_invalid"
                    )
                    parsed = None

            settlement = _settlement(
                request,
                grant,
                outcome="success" if response_error is None else "output_error",
                error=response_error,
                response=response,
                usage=usage,
                cost_microusd=cost,
            )
            self._settle_or_raise(
                settlement,
                request,
                reservations,
                grant,
                privacy_grant,
                response=response,
                usage=usage,
                cost_microusd=settlement.cost_microusd,
            )
            audit = _audit(
                request,
                attempt_count=attempt,
                reservations=reservations,
                privacy_grant=privacy_grant,
                error=response_error,
                response=response,
                usage=usage,
                cost_microusd=settlement.cost_microusd,
                cost_basis=settlement.cost_basis,
                usage_observed=settlement.usage_observed,
            )
            if response_error is not None:
                response_error.audit = audit
                raise response_error
            if parsed is None:
                raise AssertionError("validated parsed output disappeared")
            return StructuredResponsesResultV1(parsed=parsed, audit=audit)

        raise StructuredTransportError("transport_attempts_exhausted")

    def _authorize_request(
        self,
        request: StructuredResponsesRequestV1[Any],
    ) -> tuple[StructuredTaskProfileV1, StructuredModelPolicyV1]:
        task_profile = self._task_profiles.get(request.task_contract_sha256)
        if task_profile is None:
            raise StructuredTransportError("task_profile_not_authorized")
        if task_profile.purpose != request.purpose:
            raise StructuredTransportError("task_profile_purpose_mismatch")
        if task_profile.instructions != request.instructions:
            raise StructuredTransportError("task_profile_instructions_mismatch")
        if task_profile.output_schema_sha256 != request.output_schema_sha256:
            raise StructuredTransportError("task_profile_schema_mismatch")
        model_policy = self._model_policies.get(request.model_policy_sha256)
        if model_policy is None:
            raise StructuredTransportError("model_policy_not_authorized")
        if model_policy.model != request.model:
            raise StructuredTransportError("model_policy_model_mismatch")
        if request.max_output_tokens > model_policy.max_output_tokens_ceiling:
            raise StructuredTransportError("model_policy_output_ceiling_exceeded")
        if float(request.timeout_seconds) > model_policy.timeout_seconds_ceiling:
            raise StructuredTransportError("model_policy_timeout_ceiling_exceeded")
        return task_profile, model_policy

    def _authorize_privacy(
        self,
        request: StructuredResponsesRequestV1[Any],
    ) -> PrivacyAuthorizationGrantV1:
        authorization_failure = False
        try:
            grant = self._privacy_authorizer.authorize(
                request_sha256=request.request_sha256,
                owner_binding_sha256=request.owner_binding_sha256,
                safety_identifier=request.safety_identifier,
                purpose=request.purpose,
                task_contract_sha256=request.task_contract_sha256,
                model_policy_sha256=request.model_policy_sha256,
                gate_policy_sha256=request.gate_result.policy_sha256,
                privacy_authorization=request.privacy_authorization,
            )
        except Exception:
            authorization_failure = True
            grant = None
        if authorization_failure:
            raise StructuredTransportError("privacy_authorization_denied")
        if not isinstance(grant, PrivacyAuthorizationGrantV1):
            raise StructuredTransportError("privacy_grant_type_invalid")
        grant.validate(
            request=request,
            now_epoch_seconds=self._current_epoch_seconds(),
        )
        return grant

    def _current_epoch_seconds(self) -> int:
        clock_failed = False
        try:
            now = self._clock()
        except Exception:
            clock_failed = True
            now = None
        if clock_failed:
            raise StructuredTransportError("privacy_clock_unavailable")
        if (
            not isinstance(now, (int, float))
            or isinstance(now, bool)
            or not (0 <= float(now) <= 9_223_372_036_854_775_807)
        ):
            raise StructuredTransportError("privacy_clock_invalid")
        return int(now)

    def _reserve(
        self,
        request: StructuredResponsesRequestV1[Any],
        attempt: int,
        reservations: list[str],
        privacy_grant: PrivacyAuthorizationGrantV1,
    ) -> BudgetReservationGrantV1:
        reservation_failed = False
        try:
            grant = self._budget.reserve(
                request_sha256=request.request_sha256,
                attempt_ordinal=attempt,
                model=request.model,
                estimated_input_tokens=request.estimated_input_tokens,
                max_output_tokens=request.max_output_tokens,
                timeout_seconds=request.timeout_seconds,
                privacy_policy_sha256=request.privacy_authorization.policy_sha256,
                output_schema_sha256=request.output_schema_sha256,
                budget_policy_version=request.budget_policy_version,
                budget_policy_sha256=request.budget_policy_sha256,
                pricing_policy_version=request.pricing_policy_version,
                pricing_policy_sha256=request.pricing_policy_sha256,
            )
        except Exception:
            reservation_failed = True
            grant = None
        if reservation_failed:
            error = StructuredTransportError("budget_reservation_denied")
            error.audit = _audit(
                request,
                attempt_count=attempt - 1,
                reservations=reservations,
                privacy_grant=privacy_grant,
                error=error,
            )
            raise error
        try:
            if not isinstance(grant, BudgetReservationGrantV1):
                raise StructuredTransportError("budget_grant_type_invalid")
            grant.validate(
                request_sha256=request.request_sha256,
                attempt_ordinal=attempt,
                model=request.model,
                privacy_policy_sha256=request.privacy_authorization.policy_sha256,
                output_schema_sha256=request.output_schema_sha256,
                budget_policy_version=request.budget_policy_version,
                budget_policy_sha256=request.budget_policy_sha256,
                pricing_policy_version=request.pricing_policy_version,
                pricing_policy_sha256=request.pricing_policy_sha256,
                estimated_input_tokens=request.estimated_input_tokens,
                max_output_tokens=request.max_output_tokens,
            )
            return grant
        except StructuredTransportError as error:
            error.audit = _audit(
                request,
                attempt_count=attempt - 1,
                reservations=reservations,
                privacy_grant=privacy_grant,
                error=error,
            )
            raise

    def _settle_or_raise(
        self,
        settlement: BudgetSettlementV1,
        request: StructuredResponsesRequestV1[Any],
        reservations: list[str],
        grant: BudgetReservationGrantV1,
        privacy_grant: PrivacyAuthorizationGrantV1,
        *,
        response: Any | None = None,
        usage: ResponsesUsageV1 | None = None,
        cost_microusd: int | None = None,
    ) -> None:
        settlement_failed = False
        try:
            self._budget.settle(settlement)
        except Exception:
            settlement_failed = True
        if settlement_failed:
            error = StructuredTransportError("budget_settlement_failed")
            error.audit = _audit(
                request,
                attempt_count=(
                    settlement.attempt_ordinal
                    if settlement.external_call_attempted
                    else settlement.attempt_ordinal - 1
                ),
                reservations=reservations,
                privacy_grant=privacy_grant,
                error=error,
                response=response,
                usage=usage,
                cost_microusd=settlement.cost_microusd,
                cost_basis=settlement.cost_basis,
                usage_observed=settlement.usage_observed,
            )
            raise error


def _validate_response(
    response: Any,
    request: StructuredResponsesRequestV1[Any],
    grant: BudgetReservationGrantV1,
) -> tuple[StructuredTransportError | None, ResponsesUsageV1 | None, int | None]:
    response_id = _string_value(response, "id")
    provider_request_id = _string_value(response, "_request_id")
    if response_id is None or not _RESPONSE_ID_RE.fullmatch(response_id):
        return StructuredTransportError("response_id_invalid"), None, None
    if provider_request_id is None or not _PROVIDER_REQUEST_ID_RE.fullmatch(
        provider_request_id
    ):
        return StructuredTransportError("provider_request_id_invalid"), None, None
    if _value(response, "error") is not None:
        return StructuredTransportError("response_provider_error"), None, None
    if _string_value(response, "model") != request.model:
        return StructuredTransportError("response_model_mismatch"), None, None
    if _string_value(response, "service_tier") != "default":
        return StructuredTransportError("response_service_tier_mismatch"), None, None
    if _value(response, "background") is not False:
        return StructuredTransportError("response_background_mismatch"), None, None
    if _value(response, "conversation") is not None or _value(
        response, "previous_response_id"
    ) is not None:
        return StructuredTransportError("response_stateful_echo"), None, None
    if _value(response, "tools", []) not in (None, [], ()):
        return StructuredTransportError("response_tool_configuration_forbidden"), None, None
    if _value(response, "parallel_tool_calls") is not False:
        return StructuredTransportError("response_parallel_tools_mismatch"), None, None
    if _string_value(response, "truncation") != "disabled":
        return StructuredTransportError("response_truncation_mismatch"), None, None
    if _string_value(response, "safety_identifier") != request.safety_identifier:
        return StructuredTransportError("response_safety_identifier_mismatch"), None, None
    if _value(response, "max_output_tokens") != request.max_output_tokens:
        return StructuredTransportError("response_output_ceiling_mismatch"), None, None
    try:
        usage = _response_usage(response)
    except StructuredTransportError as error:
        return error, None, None
    if usage.input_tokens > request.estimated_input_tokens:
        return StructuredTransportError("response_input_exceeds_reservation"), usage, None
    if usage.output_tokens > request.max_output_tokens:
        return StructuredTransportError("response_output_exceeds_reservation"), usage, None
    uncached_input_tokens = (
        usage.input_tokens
        - usage.cached_input_tokens
        - usage.cache_write_input_tokens
    )
    cost = _token_cost_microusd(
        uncached_input_tokens,
        grant.input_microusd_per_million_tokens,
    ) + _token_cost_microusd(
        usage.cached_input_tokens,
        grant.cached_input_microusd_per_million_tokens,
    ) + _token_cost_microusd(
        usage.cache_write_input_tokens,
        grant.cache_write_input_microusd_per_million_tokens,
    ) + _token_cost_microusd(
        usage.output_tokens,
        grant.output_microusd_per_million_tokens,
    )
    if cost > grant.max_cost_microusd:
        return StructuredTransportError("response_cost_exceeds_reservation"), usage, cost
    status = _string_value(response, "status") or "unknown"
    if status != "completed":
        details = _value(response, "incomplete_details")
        reason = _string_value(details, "reason") if details is not None else None
        code = (
            "response_incomplete_max_output"
            if reason == "max_output_tokens"
            else "response_incomplete"
        )
        return StructuredTransportError(code), usage, cost
    if _value(response, "incomplete_details") is not None:
        return StructuredTransportError("response_completed_with_incomplete_details"), usage, cost
    topology_error = _validate_response_topology(response)
    if topology_error is not None:
        return topology_error, usage, cost
    if _value(response, "output_parsed") is None:
        return StructuredTransportError("parsed_output_missing"), usage, cost
    return None, usage, cost


def _settlement(
    request: StructuredResponsesRequestV1[Any],
    grant: BudgetReservationGrantV1,
    *,
    outcome: SettlementOutcome,
    error: StructuredTransportError | None,
    response: Any | None = None,
    usage: ResponsesUsageV1 | None = None,
    cost_microusd: int | None = None,
    external_call_attempted: bool = True,
) -> BudgetSettlementV1:
    if external_call_attempted is False:
        settled_cost = 0
        cost_basis: CostBasis = "none"
    elif usage is not None and cost_microusd is not None:
        settled_cost = cost_microusd
        cost_basis = "provider_usage"
    else:
        settled_cost = grant.max_cost_microusd
        cost_basis = "reservation_ceiling"
    return BudgetSettlementV1(
        reservation_id=grant.reservation_id,
        request_sha256=request.request_sha256,
        attempt_ordinal=grant.attempt_ordinal,
        external_call_attempted=external_call_attempted,
        outcome=outcome,
        error_code=error.code if error else None,
        provider_request_id=(
            _validated_optional_identifier(
                _string_value(response, "_request_id"),
                _PROVIDER_REQUEST_ID_RE,
            )
            if response is not None
            else None
        ),
        response_id=(
            _validated_optional_identifier(
                _string_value(response, "id"),
                _RESPONSE_ID_RE,
            )
            if response is not None
            else None
        ),
        input_tokens=usage.input_tokens if usage else None,
        cached_input_tokens=usage.cached_input_tokens if usage else None,
        cache_write_input_tokens=usage.cache_write_input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        reasoning_tokens=usage.reasoning_tokens if usage else None,
        total_tokens=usage.total_tokens if usage else None,
        cost_microusd=settled_cost,
        cost_basis=cost_basis,
        usage_observed=usage is not None,
    )


def _audit(
    request: StructuredResponsesRequestV1[Any],
    *,
    attempt_count: int = 0,
    reservations: list[str] | tuple[str, ...] = (),
    privacy_grant: PrivacyAuthorizationGrantV1 | None = None,
    error: StructuredTransportError | None = None,
    response: Any | None = None,
    usage: ResponsesUsageV1 | None = None,
    cost_microusd: int | None = None,
    cost_basis: CostBasis = "none",
    usage_observed: bool = False,
) -> StructuredResponsesAuditV1:
    details = _value(response, "incomplete_details") if response is not None else None
    return StructuredResponsesAuditV1(
        contract_version=CONTRACT_VERSION,
        request_id_sha256=_identifier_sha256("request", request.request_id),
        request_sha256=request.request_sha256,
        pipeline_version=request.pipeline_version,
        purpose=request.purpose,
        model_requested=request.model,
        model_returned=_string_value(response, "model") if response is not None else None,
        output_schema_sha256=request.output_schema_sha256,
        task_contract_sha256=request.task_contract_sha256,
        model_policy_sha256=request.model_policy_sha256,
        owner_binding_sha256=request.owner_binding_sha256,
        gate_policy_sha256=request.gate_result.policy_sha256,
        privacy_policy_sha256=request.privacy_authorization.policy_sha256,
        privacy_authorization_sha256=(
            request.privacy_authorization.authorization_sha256
        ),
        privacy_grant_id_sha256=(
            _identifier_sha256("privacy_grant", privacy_grant.grant_id)
            if privacy_grant is not None
            else None
        ),
        privacy_authority_verified=privacy_grant is not None,
        retention_mode=request.privacy_authorization.retention_mode,
        attempt_count=attempt_count,
        external_call_count=attempt_count,
        reservation_id_sha256s=tuple(
            _identifier_sha256("reservation", value) for value in reservations
        ),
        budget_policy_version=request.budget_policy_version,
        budget_policy_sha256=request.budget_policy_sha256,
        pricing_policy_version=request.pricing_policy_version,
        pricing_policy_sha256=request.pricing_policy_sha256,
        provider_request_id_sha256=_optional_identifier_sha256(
            "provider_request",
            _string_value(response, "_request_id") if response is not None else None,
        ),
        response_id_sha256=_optional_identifier_sha256(
            "response",
            _string_value(response, "id") if response is not None else None,
        ),
        service_tier=(
            _string_value(response, "service_tier") if response is not None else None
        ),
        response_status=(
            _string_value(response, "status") or "response_invalid"
            if response is not None
            else "request_blocked" if attempt_count == 0 else "request_error"
        ),
        store_requested=False,
        stateless_requested=True,
        no_tools_requested=True,
        background_requested=False,
        reasoning_effort_requested="low",
        refusal=_response_refusal(response) if response is not None else False,
        incomplete_reason=(
            _string_value(details, "reason") if details is not None else None
        ),
        input_tokens=usage.input_tokens if usage else None,
        cached_input_tokens=usage.cached_input_tokens if usage else None,
        cache_write_input_tokens=usage.cache_write_input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        reasoning_tokens=usage.reasoning_tokens if usage else None,
        total_tokens=usage.total_tokens if usage else None,
        cost_microusd=cost_microusd,
        cost_basis=cost_basis,
        usage_observed=usage_observed,
        error_code=error.code if error else None,
        http_status=error.http_status if error else None,
    )


def _response_usage(response: Any) -> ResponsesUsageV1:
    usage = _value(response, "usage")
    if usage is None:
        raise StructuredTransportError("response_usage_missing")
    input_tokens = _nonnegative_int(usage, "input_tokens")
    output_tokens = _nonnegative_int(usage, "output_tokens")
    total_tokens = _nonnegative_int(usage, "total_tokens")
    input_details = _value(usage, "input_tokens_details") or {}
    output_details = _value(usage, "output_tokens_details") or {}
    cached_input_tokens = _nonnegative_int(
        input_details,
        "cached_tokens",
        default=0,
    )
    cache_write_input_tokens = _nonnegative_int(
        input_details,
        "cache_write_tokens",
        default=0,
    )
    reasoning_tokens = _nonnegative_int(
        output_details,
        "reasoning_tokens",
        default=0,
    )
    if (
        cached_input_tokens + cache_write_input_tokens > input_tokens
        or reasoning_tokens > output_tokens
    ):
        raise StructuredTransportError("response_usage_details_inconsistent")
    if total_tokens != input_tokens + output_tokens:
        raise StructuredTransportError("response_usage_inconsistent")
    return ResponsesUsageV1(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
    )


def _response_refusal(response: Any) -> bool:
    for output in _value(response, "output", []) or []:
        if _value(output, "type") != "message":
            continue
        for item in _value(output, "content", []) or []:
            if _value(item, "type") == "refusal":
                return True
    return False


def _validate_response_topology(response: Any) -> StructuredTransportError | None:
    output = _value(response, "output")
    if not isinstance(output, (list, tuple)):
        return StructuredTransportError("response_output_invalid")
    messages: list[Any] = []
    reasoning_count = 0
    for item in output:
        item_type = _string_value(item, "type")
        if item_type == "reasoning":
            reasoning_count += 1
        elif item_type == "message":
            messages.append(item)
        else:
            return StructuredTransportError("response_output_type_forbidden")
    if reasoning_count > 1:
        return StructuredTransportError("response_reasoning_count_invalid")
    if len(messages) != 1:
        return StructuredTransportError("response_message_count_invalid")
    message = messages[0]
    if _string_value(message, "role") != "assistant":
        return StructuredTransportError("response_message_role_invalid")
    if _string_value(message, "status") != "completed":
        return StructuredTransportError("response_message_status_invalid")
    content = _value(message, "content")
    if not isinstance(content, (list, tuple)) or len(content) != 1:
        return StructuredTransportError("response_content_count_invalid")
    content_item = content[0]
    if _string_value(content_item, "type") == "refusal":
        return StructuredTransportError("model_refusal")
    if _string_value(content_item, "type") != "output_text":
        return StructuredTransportError("response_content_type_forbidden")
    text = _value(content_item, "text")
    if not isinstance(text, str) or not text:
        return StructuredTransportError("response_output_text_invalid")
    return None


def _response_output_text(response: Any) -> str:
    for item in _value(response, "output", []) or []:
        if _string_value(item, "type") != "message":
            continue
        content = _value(item, "content", []) or []
        if len(content) == 1 and _string_value(content[0], "type") == "output_text":
            text = _value(content[0], "text")
            if isinstance(text, str):
                return text
    raise ValueError("validated output text missing")


def _strict_json_loads(value: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def reject_constant(_: str) -> Any:
        raise ValueError("non-finite JSON number")

    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def _classify_transport_exception(
    exc: Exception,
    sdk_module: Any | None,
) -> StructuredTransportError:
    if isinstance(exc, TimeoutError):
        return StructuredTransportError("transport_timeout", retryable=True)
    if sdk_module is not None:
        for name, code in (
            ("APITimeoutError", "transport_timeout"),
            ("APIConnectionError", "transport_connection"),
        ):
            error_type = getattr(sdk_module, name, ())
            if error_type and isinstance(exc, error_type):
                return StructuredTransportError(code, retryable=True)
        rate_type = getattr(sdk_module, "RateLimitError", ())
        if rate_type and isinstance(exc, rate_type):
            return _classify_429(_provider_error_code(exc))
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status == 429:
            return _classify_429(_provider_error_code(exc))
        if status >= 500 or status in {408, 409}:
            return StructuredTransportError(
                "transport_server_error",
                retryable=True,
                http_status=status,
            )
        return StructuredTransportError(
            "transport_client_error",
            http_status=status,
        )
    return StructuredTransportError("transport_unknown_error")


def _classify_429(provider_code: str | None) -> StructuredTransportError:
    if provider_code in _TRANSIENT_429_CODES:
        return StructuredTransportError(
            "transport_rate_limited",
            retryable=True,
            http_status=429,
        )
    if provider_code in _NONRETRYABLE_429_CODES:
        code = "transport_quota_exhausted"
    else:
        code = "transport_rate_limit_unclassified"
    return StructuredTransportError(code, http_status=429)


def _provider_error_code(exc: Exception) -> str | None:
    direct = getattr(exc, "code", None)
    if isinstance(direct, str):
        return direct
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        value = body.get("error", body)
        if isinstance(value, dict) and isinstance(value.get("code"), str):
            return value["code"]
    return None


def _bounded_retry_delay(exc: Exception, default: float) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return default
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return default
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= delay <= 5.0:
        return None
    return delay


def _structured_schema_is_strict(value: Any) -> bool:
    if isinstance(value, dict):
        if "default" in value:
            return False
        if value.get("type") == "object" or "properties" in value:
            properties = value.get("properties")
            required = value.get("required")
            if not isinstance(properties, dict) or value.get(
                "additionalProperties"
            ) is not False:
                return False
            if not isinstance(required, list) or len(required) != len(set(required)):
                return False
            if set(required) != set(properties):
                return False
        return all(_structured_schema_is_strict(item) for item in value.values())
    if isinstance(value, list):
        return all(_structured_schema_is_strict(item) for item in value)
    return True


def _nonnegative_int(value: Any, key: str, *, default: int | None = None) -> int:
    item = _value(value, key, default)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise StructuredTransportError("response_usage_invalid")
    return item


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _string_value(value: Any, key: str) -> str | None:
    item = _value(value, key)
    if item in (None, ""):
        return None
    return item if isinstance(item, str) else None


def _identifier_sha256(kind: str, value: str) -> str:
    return hashlib.sha256(
        f"{CONTRACT_VERSION}\0{kind}\0{value}".encode("utf-8")
    ).hexdigest()


def owner_safety_identifier_v1(owner_binding_sha256: str) -> str:
    if not _SHA256_RE.fullmatch(owner_binding_sha256):
        raise StructuredTransportError("owner_binding_identity_invalid")
    digest = hashlib.sha256(
        f"{CONTRACT_VERSION}\0owner_safety\0{owner_binding_sha256}".encode("utf-8")
    ).hexdigest()
    return f"mem_{digest[:32]}"


def _optional_identifier_sha256(kind: str, value: str | None) -> str | None:
    return _identifier_sha256(kind, value) if value is not None else None


def _validated_optional_identifier(
    value: str | None,
    pattern: re.Pattern[str],
) -> str | None:
    return value if value is not None and pattern.fullmatch(value) else None


def _task_profile_map(
    profiles: Iterable[StructuredTaskProfileV1],
) -> dict[str, StructuredTaskProfileV1]:
    result: dict[str, StructuredTaskProfileV1] = {}
    for profile in profiles:
        if not isinstance(profile, StructuredTaskProfileV1):
            raise StructuredTransportError("task_profile_type_invalid")
        profile.validate()
        if profile.contract_sha256 in result:
            raise StructuredTransportError("task_profile_duplicate")
        result[profile.contract_sha256] = profile
    if not result:
        raise StructuredTransportError("task_profiles_empty")
    return result


def _model_policy_map(
    policies: Iterable[StructuredModelPolicyV1],
) -> dict[str, StructuredModelPolicyV1]:
    result: dict[str, StructuredModelPolicyV1] = {}
    for policy in policies:
        if not isinstance(policy, StructuredModelPolicyV1):
            raise StructuredTransportError("model_policy_type_invalid")
        policy.validate()
        if policy.policy_sha256 in result:
            raise StructuredTransportError("model_policy_duplicate")
        result[policy.policy_sha256] = policy
    if not result:
        raise StructuredTransportError("model_policies_empty")
    return result


def _token_cost_microusd(tokens: int, rate: int) -> int:
    if tokens < 0 or rate < 0:
        raise ValueError("token cost inputs must be nonnegative")
    return (tokens * rate + 999_999) // 1_000_000


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "AuthoritativePrivacyAuthorizerV1",
    "BudgetReservationGrantV1",
    "BudgetSettlementV1",
    "CONTRACT_VERSION",
    "CostBasis",
    "DurableCallBudgetAuthorizerV1",
    "EXTERNAL_CALL_ENABLE_TOKEN",
    "ExternalPrivacyAuthorizationV1",
    "INPUT_TOKEN_CEILING_FIXED_OVERHEAD",
    "MAX_INSTRUCTIONS_CHARS",
    "MAX_OUTPUT_TOKENS",
    "MAX_PARSED_OUTPUT_BYTES",
    "MAX_SELECTED_INPUT_BYTES",
    "MAX_SOURCE_CHARS",
    "OpenAIStructuredResponsesTransportV1",
    "PRIVACY_AUTHORIZATION_TOKEN",
    "PrivacyAuthorizationGrantV1",
    "ResponsesUsageV1",
    "SELECTED_INPUT_CONTRACT_VERSION",
    "StructuredModelPolicyV1",
    "StructuredResponsesAuditV1",
    "StructuredResponsesRequestV1",
    "StructuredResponsesResultV1",
    "StructuredTaskProfileV1",
    "StructuredTransportError",
    "owner_safety_identifier_v1",
]
