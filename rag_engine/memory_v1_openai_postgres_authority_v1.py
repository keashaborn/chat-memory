"""One-use transport grants derived from a durable PostgreSQL reservation."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from rag_engine.memory_v1_openai_structured_transport_v1 import (
    BudgetReservationGrantV1,
    BudgetSettlementV1,
    ExternalPrivacyAuthorizationV1,
    PrivacyAuthorizationGrantV1,
    StructuredResponsesRequestV1,
    StructuredTransportError,
)


CONTRACT_VERSION = "memory_v1_openai_postgres_authority_v1"


@dataclass(frozen=True)
class PostgresProviderReservationV1:
    reservation_id: str
    request_sha256: str
    model: str
    owner_binding_sha256: str
    safety_identifier: str
    purpose: str
    task_contract_sha256: str
    model_policy_sha256: str
    gate_policy_sha256: str
    privacy_policy_version: str
    privacy_policy_sha256: str
    privacy_authorization_sha256: str
    retention_mode: str
    standard_retention_risk_accepted: bool
    retention_attestation_sha256: str | None
    budget_policy_version: str
    budget_policy_sha256: str
    pricing_policy_version: str
    pricing_policy_sha256: str
    output_schema_sha256: str
    input_microusd_per_million_tokens: int
    cached_input_microusd_per_million_tokens: int
    cache_write_input_microusd_per_million_tokens: int
    output_microusd_per_million_tokens: int
    max_cost_microusd: int
    issued_at_epoch_seconds: int
    expires_at_epoch_seconds: int

    @classmethod
    def from_request(
        cls,
        *,
        reservation_id: str,
        request: StructuredResponsesRequestV1[Any],
        rates: dict[str, int],
        max_cost_microusd: int,
        issued_at_epoch_seconds: int | None = None,
        ttl_seconds: int = 300,
    ) -> "PostgresProviderReservationV1":
        issued = int(time.time()) if issued_at_epoch_seconds is None else issued_at_epoch_seconds
        privacy = request.privacy_authorization
        return cls(
            reservation_id=reservation_id,
            request_sha256=request.request_sha256,
            model=request.model,
            owner_binding_sha256=request.owner_binding_sha256,
            safety_identifier=request.safety_identifier,
            purpose=request.purpose,
            task_contract_sha256=request.task_contract_sha256,
            model_policy_sha256=request.model_policy_sha256,
            gate_policy_sha256=request.gate_result.policy_sha256,
            privacy_policy_version=privacy.policy_version,
            privacy_policy_sha256=privacy.policy_sha256,
            privacy_authorization_sha256=privacy.authorization_sha256,
            retention_mode=privacy.retention_mode,
            standard_retention_risk_accepted=privacy.standard_retention_risk_accepted,
            retention_attestation_sha256=privacy.retention_attestation_sha256,
            budget_policy_version=request.budget_policy_version,
            budget_policy_sha256=request.budget_policy_sha256,
            pricing_policy_version=request.pricing_policy_version,
            pricing_policy_sha256=request.pricing_policy_sha256,
            output_schema_sha256=request.output_schema_sha256,
            input_microusd_per_million_tokens=rates["input_microusd_per_million_tokens"],
            cached_input_microusd_per_million_tokens=rates["cached_input_microusd_per_million_tokens"],
            cache_write_input_microusd_per_million_tokens=rates["cache_write_input_microusd_per_million_tokens"],
            output_microusd_per_million_tokens=rates["output_microusd_per_million_tokens"],
            max_cost_microusd=max_cost_microusd,
            issued_at_epoch_seconds=issued,
            expires_at_epoch_seconds=issued + ttl_seconds,
        )


class PostgresPrivacyAuthorizerV1:
    def __init__(self, reservation: PostgresProviderReservationV1) -> None:
        self._reservation = reservation
        self._used = False

    def authorize(self, **kwargs: Any) -> PrivacyAuthorizationGrantV1:
        if self._used:
            raise StructuredTransportError("postgres_privacy_grant_reused")
        reservation = self._reservation
        privacy: ExternalPrivacyAuthorizationV1 = kwargs["privacy_authorization"]
        expected = {
            "request_sha256": reservation.request_sha256,
            "owner_binding_sha256": reservation.owner_binding_sha256,
            "safety_identifier": reservation.safety_identifier,
            "purpose": reservation.purpose,
            "task_contract_sha256": reservation.task_contract_sha256,
            "model_policy_sha256": reservation.model_policy_sha256,
            "gate_policy_sha256": reservation.gate_policy_sha256,
        }
        if any(kwargs.get(key) != value for key, value in expected.items()):
            raise StructuredTransportError("postgres_privacy_binding_mismatch")
        if (
            privacy.policy_version != reservation.privacy_policy_version
            or privacy.policy_sha256 != reservation.privacy_policy_sha256
            or privacy.authorization_sha256
            != reservation.privacy_authorization_sha256
            or privacy.retention_mode != reservation.retention_mode
            or privacy.standard_retention_risk_accepted
            != reservation.standard_retention_risk_accepted
            or privacy.retention_attestation_sha256
            != reservation.retention_attestation_sha256
        ):
            raise StructuredTransportError("postgres_privacy_policy_mismatch")
        self._used = True
        return PrivacyAuthorizationGrantV1(
            grant_id=f"pgprivacy_{reservation.reservation_id}",
            authorized=True,
            durable=True,
            request_sha256=reservation.request_sha256,
            owner_binding_sha256=reservation.owner_binding_sha256,
            safety_identifier=reservation.safety_identifier,
            purpose=reservation.purpose,  # type: ignore[arg-type]
            task_contract_sha256=reservation.task_contract_sha256,
            model_policy_sha256=reservation.model_policy_sha256,
            gate_policy_sha256=reservation.gate_policy_sha256,
            privacy_policy_version=reservation.privacy_policy_version,
            privacy_policy_sha256=reservation.privacy_policy_sha256,
            privacy_authorization_sha256=reservation.privacy_authorization_sha256,
            retention_mode=reservation.retention_mode,  # type: ignore[arg-type]
            standard_retention_risk_accepted=reservation.standard_retention_risk_accepted,
            retention_attestation_sha256=reservation.retention_attestation_sha256,
            issued_at_epoch_seconds=reservation.issued_at_epoch_seconds,
            expires_at_epoch_seconds=reservation.expires_at_epoch_seconds,
        )


class PostgresBudgetAuthorizerV1:
    def __init__(self, reservation: PostgresProviderReservationV1) -> None:
        self._reservation = reservation
        self._reserved = False
        self._settlement: BudgetSettlementV1 | None = None

    @property
    def settlement(self) -> BudgetSettlementV1 | None:
        return self._settlement

    def reserve(self, **kwargs: Any) -> BudgetReservationGrantV1:
        reservation = self._reservation
        if self._reserved or kwargs.get("attempt_ordinal") != 1:
            raise StructuredTransportError("postgres_budget_reservation_reused")
        expected = {
            "request_sha256": reservation.request_sha256,
            "model": reservation.model,
            "privacy_policy_sha256": reservation.privacy_policy_sha256,
            "output_schema_sha256": reservation.output_schema_sha256,
            "budget_policy_version": reservation.budget_policy_version,
            "budget_policy_sha256": reservation.budget_policy_sha256,
            "pricing_policy_version": reservation.pricing_policy_version,
            "pricing_policy_sha256": reservation.pricing_policy_sha256,
        }
        if any(kwargs.get(key) != value for key, value in expected.items()):
            raise StructuredTransportError("postgres_budget_binding_mismatch")
        self._reserved = True
        return BudgetReservationGrantV1(
            reservation_id=f"pgbudget_{reservation.reservation_id}",
            authorized=True,
            durable=True,
            request_sha256=reservation.request_sha256,
            attempt_ordinal=1,
            model=reservation.model,
            privacy_policy_sha256=reservation.privacy_policy_sha256,
            output_schema_sha256=reservation.output_schema_sha256,
            budget_policy_version=reservation.budget_policy_version,
            budget_policy_sha256=reservation.budget_policy_sha256,
            pricing_policy_version=reservation.pricing_policy_version,
            pricing_policy_sha256=reservation.pricing_policy_sha256,
            input_microusd_per_million_tokens=reservation.input_microusd_per_million_tokens,
            cached_input_microusd_per_million_tokens=reservation.cached_input_microusd_per_million_tokens,
            cache_write_input_microusd_per_million_tokens=reservation.cache_write_input_microusd_per_million_tokens,
            output_microusd_per_million_tokens=reservation.output_microusd_per_million_tokens,
            max_cost_microusd=reservation.max_cost_microusd,
        )

    def settle(self, settlement: BudgetSettlementV1) -> None:
        if self._settlement is not None:
            raise StructuredTransportError("postgres_budget_settlement_reused")
        if settlement.reservation_id != f"pgbudget_{self._reservation.reservation_id}":
            raise StructuredTransportError("postgres_budget_settlement_mismatch")
        self._settlement = settlement


__all__ = [
    "CONTRACT_VERSION",
    "PostgresBudgetAuthorizerV1",
    "PostgresPrivacyAuthorizerV1",
    "PostgresProviderReservationV1",
]
