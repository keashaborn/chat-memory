#!/usr/bin/env python3
from __future__ import annotations

"""Deliver metadata-only AI Operations incident email alerts."""

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import asyncpg
import httpx


JOB_CONTRACT_VERSION = "ai_operations_alert_delivery_job_v1"
CLAIM_CONTRACT_VERSION = "ai_operations_alert_delivery_claim_v1"
COMPLETE_CONTRACT_VERSION = "ai_operations_alert_delivery_complete_v1"
AUTHORIZED_VALUE = "authorized"
RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM_EMAIL = "LifeSwitch <no-reply@mail.lifeswitch.com>"
ADMIN_URL = "https://lifeswitch.com/admin"
DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 25
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_PROVIDER_RESPONSE_BYTES = 16_384
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MONITOR_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
REASON_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class AlertDeliveryConfigurationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AlertDeliveryContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class AlertDeliveryConfigV1:
    enabled: str
    postgres_dsn: str
    resend_api_key: str
    to_email: str
    from_email: str = DEFAULT_FROM_EMAIL
    batch_size: int = DEFAULT_BATCH_SIZE
    drill_enabled: str = ""


@dataclass(frozen=True)
class ClaimedAlertV1:
    delivery_id: UUID
    incident_id: UUID
    monitor_name: str
    severity: str
    observation_status: str
    reason_codes: tuple[str, ...]
    first_seen_at: str
    last_seen_at: str
    observation_count: int
    window_hours: int
    request_count: int
    completed_count: int
    fail_closed_count: int
    dependency_failure_count: int
    fail_closed_rate: float
    attempt_count: int


@dataclass(frozen=True)
class DeliveryOutcomeV1:
    outcome: str
    provider_message_id: str | None = None
    error_code: str | None = None


ConnectCallable = Callable[..., Awaitable[Any]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_batch_size(raw: str | None) -> int:
    value = (raw or str(DEFAULT_BATCH_SIZE)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise AlertDeliveryConfigurationError("invalid_batch_size") from exc
    if not 1 <= parsed <= MAX_BATCH_SIZE:
        raise AlertDeliveryConfigurationError("invalid_batch_size")
    return parsed


def _config_from_environment() -> AlertDeliveryConfigV1:
    return AlertDeliveryConfigV1(
        enabled=(os.getenv("AI_OPERATIONS_ALERT_DELIVERY_ENABLED") or "").strip(),
        postgres_dsn=(os.getenv("POSTGRES_DSN") or "").strip(),
        resend_api_key=(os.getenv("AI_OPERATIONS_RESEND_API_KEY") or "").strip(),
        to_email=(os.getenv("AI_OPERATIONS_ALERT_TO_EMAIL") or "").strip(),
        from_email=(
            os.getenv("AI_OPERATIONS_ALERT_FROM_EMAIL") or DEFAULT_FROM_EMAIL
        ).strip(),
        batch_size=_parse_batch_size(
            os.getenv("AI_OPERATIONS_ALERT_DELIVERY_BATCH_SIZE")
        ),
        drill_enabled=(
            os.getenv("AI_OPERATIONS_ALERT_DRILL_ENABLED") or ""
        ).strip(),
    )


def _validate_config(config: AlertDeliveryConfigV1) -> AlertDeliveryConfigV1:
    if config.enabled != AUTHORIZED_VALUE:
        raise AlertDeliveryConfigurationError("delivery_not_authorized")
    if not config.postgres_dsn.strip():
        raise AlertDeliveryConfigurationError("missing_postgres_dsn")
    api_key = config.resend_api_key.strip()
    if (
        not api_key.startswith("re_")
        or len(api_key) < 8
        or len(api_key) > 512
    ):
        raise AlertDeliveryConfigurationError("invalid_resend_api_key")
    to_email = config.to_email.strip().lower()
    if len(to_email) > 320 or not EMAIL_RE.fullmatch(to_email):
        raise AlertDeliveryConfigurationError("invalid_to_email")
    from_email = config.from_email.strip()
    if len(from_email) > 320 or "\r" in from_email or "\n" in from_email:
        raise AlertDeliveryConfigurationError("invalid_from_email")
    if "<" in from_email:
        address = from_email.rsplit("<", 1)[-1].rstrip(">").strip()
    else:
        address = from_email
    if not EMAIL_RE.fullmatch(address):
        raise AlertDeliveryConfigurationError("invalid_from_email")
    if not 1 <= int(config.batch_size) <= MAX_BATCH_SIZE:
        raise AlertDeliveryConfigurationError("invalid_batch_size")
    return AlertDeliveryConfigV1(
        enabled=AUTHORIZED_VALUE,
        postgres_dsn=config.postgres_dsn.strip(),
        resend_api_key=api_key,
        to_email=to_email,
        from_email=from_email,
        batch_size=int(config.batch_size),
        drill_enabled=config.drill_enabled.strip(),
    )


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise AlertDeliveryContractError(f"invalid_{field}") from exc


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise AlertDeliveryContractError(f"invalid_{field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AlertDeliveryContractError(f"invalid_{field}") from exc
    if parsed < minimum:
        raise AlertDeliveryContractError(f"invalid_{field}")
    return parsed


def _decode_claim(payload: object) -> ClaimedAlertV1 | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AlertDeliveryContractError("invalid_claim_json") from exc
    if not isinstance(payload, Mapping):
        raise AlertDeliveryContractError("invalid_claim_payload")
    if payload.get("contract_version") != CLAIM_CONTRACT_VERSION:
        raise AlertDeliveryContractError("invalid_claim_contract")
    if payload.get("status") == "empty":
        if set(payload) != {"contract_version", "status"}:
            raise AlertDeliveryContractError("invalid_empty_claim")
        return None
    expected = {
        "contract_version",
        "status",
        "delivery_id",
        "incident_id",
        "monitor_name",
        "severity",
        "observation_status",
        "reason_codes",
        "first_seen_at",
        "last_seen_at",
        "observation_count",
        "window_hours",
        "request_count",
        "completed_count",
        "fail_closed_count",
        "dependency_failure_count",
        "fail_closed_rate",
        "attempt_count",
    }
    if set(payload) != expected or payload.get("status") != "claimed":
        raise AlertDeliveryContractError("invalid_claim_shape")
    monitor_name = str(payload["monitor_name"])
    if not MONITOR_RE.fullmatch(monitor_name):
        raise AlertDeliveryContractError("invalid_monitor_name")
    severity = str(payload["severity"])
    if severity not in {"warning", "critical"}:
        raise AlertDeliveryContractError("invalid_severity")
    observation_status = str(payload["observation_status"])
    if observation_status not in {"violated", "unavailable"}:
        raise AlertDeliveryContractError("invalid_observation_status")
    reasons = payload["reason_codes"]
    if (
        not isinstance(reasons, list)
        or len(reasons) > 16
        or any(not isinstance(item, str) or not REASON_RE.fullmatch(item) for item in reasons)
    ):
        raise AlertDeliveryContractError("invalid_reason_codes")
    first_seen_at = str(payload["first_seen_at"])
    last_seen_at = str(payload["last_seen_at"])
    if len(first_seen_at) > 64 or len(last_seen_at) > 64:
        raise AlertDeliveryContractError("invalid_incident_times")
    try:
        fail_closed_rate = float(payload["fail_closed_rate"])
    except (TypeError, ValueError) as exc:
        raise AlertDeliveryContractError("invalid_fail_closed_rate") from exc
    if not 0.0 <= fail_closed_rate <= 1.0:
        raise AlertDeliveryContractError("invalid_fail_closed_rate")
    return ClaimedAlertV1(
        delivery_id=_uuid(payload["delivery_id"], "delivery_id"),
        incident_id=_uuid(payload["incident_id"], "incident_id"),
        monitor_name=monitor_name,
        severity=severity,
        observation_status=observation_status,
        reason_codes=tuple(reasons),
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        observation_count=_integer(payload["observation_count"], "observation_count", minimum=1),
        window_hours=_integer(payload["window_hours"], "window_hours", minimum=1),
        request_count=_integer(payload["request_count"], "request_count"),
        completed_count=_integer(payload["completed_count"], "completed_count"),
        fail_closed_count=_integer(payload["fail_closed_count"], "fail_closed_count"),
        dependency_failure_count=_integer(
            payload["dependency_failure_count"], "dependency_failure_count"
        ),
        fail_closed_rate=fail_closed_rate,
        attempt_count=_integer(payload["attempt_count"], "attempt_count", minimum=1),
    )


def _alert_subject(alert: ClaimedAlertV1, *, drill: bool = False) -> str:
    if drill:
        return "[TEST] LifeSwitch AI Operations alert delivery"
    severity = alert.severity.upper()
    monitor = alert.monitor_name.replace("_", " ").title()
    return f"[{severity}] LifeSwitch AI Operations: {monitor}"


def _alert_text(alert: ClaimedAlertV1, *, drill: bool = False) -> str:
    lines = [
        (
            "This is an authorized metadata-only delivery drill."
            if drill
            else "A private AI Operations monitor requires attention."
        ),
        "",
        f"Monitor: {alert.monitor_name}",
        f"Severity: {'test' if drill else alert.severity}",
        f"Status: {'drill' if drill else alert.observation_status}",
        f"Reason codes: {', '.join(alert.reason_codes) or 'none'}",
        f"First observed: {alert.first_seen_at}",
        f"Last observed: {alert.last_seen_at}",
        f"Observation count: {alert.observation_count}",
        f"Window hours: {alert.window_hours}",
        f"Requests: {alert.request_count}",
        f"Completed: {alert.completed_count}",
        f"Fail-closed: {alert.fail_closed_count}",
        f"Dependency failures: {alert.dependency_failure_count}",
        f"Fail-closed rate: {alert.fail_closed_rate:.4f}",
        "",
        f"Review: {ADMIN_URL}",
        "",
        "No prompts, queries, URLs, retrieved content, or answer text are included.",
    ]
    return "\n".join(lines)


async def _send_resend_email(
    config: AlertDeliveryConfigV1,
    alert: ClaimedAlertV1,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    drill: bool = False,
) -> DeliveryOutcomeV1:
    idempotency_suffix = (
        f"drill-{alert.delivery_id}" if drill else str(alert.delivery_id)
    )
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
        ) as client:
            response = await client.post(
                RESEND_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {config.resend_api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Idempotency-Key": f"ai-operations-alert-{idempotency_suffix}",
                },
                json={
                    "from": config.from_email,
                    "to": [config.to_email],
                    "subject": _alert_subject(alert, drill=drill),
                    "text": _alert_text(alert, drill=drill),
                    "tags": [
                        {"name": "category", "value": "ai_operations"},
                        {
                            "name": "severity",
                            "value": "test" if drill else alert.severity,
                        },
                    ],
                },
            )
    except (httpx.TimeoutException, httpx.TransportError):
        return DeliveryOutcomeV1(
            outcome="retryable_failed",
            error_code="provider_transport_failure",
        )
    if response.status_code == 429 or 500 <= response.status_code <= 599:
        return DeliveryOutcomeV1(
            outcome="retryable_failed",
            error_code=f"provider_http_{response.status_code}",
        )
    if not 200 <= response.status_code <= 299:
        return DeliveryOutcomeV1(
            outcome="permanent_failed",
            error_code=f"provider_http_{response.status_code}",
        )
    if len(response.content) > MAX_PROVIDER_RESPONSE_BYTES:
        return DeliveryOutcomeV1(
            outcome="retryable_failed",
            error_code="provider_response_oversized",
        )
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return DeliveryOutcomeV1(
            outcome="retryable_failed",
            error_code="provider_response_invalid",
        )
    provider_id = payload.get("id") if isinstance(payload, Mapping) else None
    if not isinstance(provider_id, str) or not PROVIDER_ID_RE.fullmatch(provider_id):
        return DeliveryOutcomeV1(
            outcome="retryable_failed",
            error_code="provider_id_invalid",
        )
    return DeliveryOutcomeV1(
        outcome="delivered",
        provider_message_id=provider_id,
    )


async def _claim_next(connection: Any, worker_id: UUID) -> ClaimedAlertV1 | None:
    async with connection.transaction():
        await connection.execute(
            "SELECT set_config('app.ai_operations_alert_delivery',$1,true)",
            AUTHORIZED_VALUE,
        )
        await connection.execute(
            "SELECT set_config('app.ai_operations_alert_worker_id',$1,true)",
            str(worker_id),
        )
        payload = await connection.fetchval(
            "SELECT ai_operations.claim_monitor_alert_delivery_v1($1)",
            worker_id,
        )
    return _decode_claim(payload)


async def _complete_delivery(
    connection: Any,
    worker_id: UUID,
    alert: ClaimedAlertV1,
    outcome: DeliveryOutcomeV1,
) -> None:
    async with connection.transaction():
        await connection.execute(
            "SELECT set_config('app.ai_operations_alert_delivery',$1,true)",
            AUTHORIZED_VALUE,
        )
        await connection.execute(
            "SELECT set_config('app.ai_operations_alert_worker_id',$1,true)",
            str(worker_id),
        )
        payload = await connection.fetchval(
            "SELECT ai_operations.complete_monitor_alert_delivery_v1($1,$2,$3,$4,$5)",
            alert.delivery_id,
            worker_id,
            outcome.outcome,
            outcome.provider_message_id,
            outcome.error_code,
        )
    if isinstance(payload, str):
        payload = json.loads(payload)
    if (
        not isinstance(payload, Mapping)
        or payload.get("contract_version") != COMPLETE_CONTRACT_VERSION
        or payload.get("delivery_id") != str(alert.delivery_id)
        or payload.get("outcome") not in {
            "delivered",
            "retry_scheduled",
            "permanent_failed",
        }
    ):
        raise AlertDeliveryContractError("invalid_complete_contract")


async def run_alert_delivery_job_v1(
    config: AlertDeliveryConfigV1,
    *,
    connect: ConnectCallable = asyncpg.connect,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, object], int]:
    config = _validate_config(config)
    worker_id = uuid4()
    report: dict[str, object] = {
        "contract_version": JOB_CONTRACT_VERSION,
        "status": "completed",
        "claimed_count": 0,
        "delivered_count": 0,
        "retry_scheduled_count": 0,
        "permanent_failed_count": 0,
        "occurred_at": _now_iso(),
    }
    connection = await connect(config.postgres_dsn, command_timeout=15)
    try:
        for _ in range(config.batch_size):
            alert = await _claim_next(connection, worker_id)
            if alert is None:
                break
            report["claimed_count"] = int(report["claimed_count"]) + 1
            outcome = await _send_resend_email(
                config,
                alert,
                transport=transport,
            )
            await _complete_delivery(connection, worker_id, alert, outcome)
            if outcome.outcome == "delivered":
                report["delivered_count"] = int(report["delivered_count"]) + 1
            elif outcome.outcome == "retryable_failed":
                report["retry_scheduled_count"] = (
                    int(report["retry_scheduled_count"]) + 1
                )
            else:
                report["permanent_failed_count"] = (
                    int(report["permanent_failed_count"]) + 1
                )
    finally:
        await connection.close()
    failures = int(report["retry_scheduled_count"]) + int(
        report["permanent_failed_count"]
    )
    if failures:
        report["status"] = "delivery_incomplete"
    return report, 3 if failures else 0


async def run_alert_delivery_drill_v1(
    config: AlertDeliveryConfigV1,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, object], int]:
    config = _validate_config(config)
    if config.drill_enabled != AUTHORIZED_VALUE:
        raise AlertDeliveryConfigurationError("drill_not_authorized")
    day = datetime.now(timezone.utc).date().isoformat()
    synthetic_id = uuid5(NAMESPACE_URL, f"lifeswitch-ai-operations-alert-drill:{day}")
    occurred_at = _now_iso()
    alert = ClaimedAlertV1(
        delivery_id=synthetic_id,
        incident_id=synthetic_id,
        monitor_name="alert_delivery_safe_drill",
        severity="warning",
        observation_status="unavailable",
        reason_codes=("synthetic_delivery_drill",),
        first_seen_at=occurred_at,
        last_seen_at=occurred_at,
        observation_count=1,
        window_hours=1,
        request_count=0,
        completed_count=0,
        fail_closed_count=0,
        dependency_failure_count=0,
        fail_closed_rate=0.0,
        attempt_count=1,
    )
    outcome = await _send_resend_email(
        config,
        alert,
        transport=transport,
        drill=True,
    )
    report = {
        "contract_version": JOB_CONTRACT_VERSION,
        "status": "drill_delivered" if outcome.outcome == "delivered" else "drill_failed",
        "drill": True,
        "delivery_outcome": outcome.outcome,
        "occurred_at": occurred_at,
        "metadata_only": True,
    }
    return report, 0 if outcome.outcome == "delivered" else 3


def _configuration_failure_report(
    exc: AlertDeliveryConfigurationError,
) -> dict[str, object]:
    return {
        "contract_version": JOB_CONTRACT_VERSION,
        "status": "unavailable",
        "error_code": exc.code,
        "occurred_at": _now_iso(),
    }


async def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-contract", action="store_true")
    parser.add_argument("--drill", action="store_true")
    args = parser.parse_args(argv)
    if args.print_contract:
        print(json.dumps({
            "contract_version": JOB_CONTRACT_VERSION,
            "provider": "resend",
            "metadata_only": True,
            "max_batch_size": MAX_BATCH_SIZE,
            "drill_requires_secondary_authorization": True,
        }, sort_keys=True, separators=(",", ":")))
        return 0
    try:
        config = _config_from_environment()
        if args.drill:
            report, exit_code = await run_alert_delivery_drill_v1(config)
        else:
            report, exit_code = await run_alert_delivery_job_v1(config)
    except AlertDeliveryConfigurationError as exc:
        report = _configuration_failure_report(exc)
        exit_code = 3
    except Exception:
        report = {
            "contract_version": JOB_CONTRACT_VERSION,
            "status": "failed",
            "error_code": "delivery_job_failed",
            "occurred_at": _now_iso(),
        }
        exit_code = 3
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
