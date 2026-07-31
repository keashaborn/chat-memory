#!/usr/bin/env python3
from __future__ import annotations

"""Run the privacy-safe trusted-web monitor and deliver bounded alerts."""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Sequence

import asyncpg
import httpx

from rag_engine.trusted_web_monitoring_v1 import (
    MAX_MONITOR_HOURS,
    MIN_MONITOR_HOURS,
    evaluate_trusted_web_monitoring_thresholds_v1,
    load_trusted_web_monitoring_summary_v1,
)


JOB_CONTRACT_VERSION = "trusted_web_monitor_job_v1"
AUTHORIZED_VALUE = "authorized"
DEFAULT_WINDOW_HOURS = 24
DEFAULT_MAX_RELEVANCE_FAIL_CLOSED = 0
DEFAULT_MAX_DEPENDENCY_FAILURES = 0
DEFAULT_MAX_FAIL_CLOSED_RATE = 0.25


class MonitorJobConfigurationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TrustedWebMonitorJobConfigV1:
    enabled: str
    postgres_dsn: str
    window_hours: int = DEFAULT_WINDOW_HOURS
    max_relevance_fail_closed: int = (
        DEFAULT_MAX_RELEVANCE_FAIL_CLOSED
    )
    max_dependency_failures: int = DEFAULT_MAX_DEPENDENCY_FAILURES
    max_fail_closed_rate: float = DEFAULT_MAX_FAIL_CLOSED_RATE
    alert_webhook_url: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise MonitorJobConfigurationError(
            f"invalid_{name.lower()}"
        ) from exc


def _parse_float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise MonitorJobConfigurationError(
            f"invalid_{name.lower()}"
        ) from exc


def _config_from_environment() -> TrustedWebMonitorJobConfigV1:
    return TrustedWebMonitorJobConfigV1(
        enabled=(os.getenv("TRUSTED_WEB_MONITOR_ENABLED") or "").strip(),
        postgres_dsn=(os.getenv("POSTGRES_DSN") or "").strip(),
        window_hours=_parse_int_env(
            "TRUSTED_WEB_MONITOR_WINDOW_HOURS",
            DEFAULT_WINDOW_HOURS,
        ),
        max_relevance_fail_closed=_parse_int_env(
            "TRUSTED_WEB_MONITOR_MAX_RELEVANCE_FAIL_CLOSED",
            DEFAULT_MAX_RELEVANCE_FAIL_CLOSED,
        ),
        max_dependency_failures=_parse_int_env(
            "TRUSTED_WEB_MONITOR_MAX_DEPENDENCY_FAILURES",
            DEFAULT_MAX_DEPENDENCY_FAILURES,
        ),
        max_fail_closed_rate=_parse_float_env(
            "TRUSTED_WEB_MONITOR_MAX_FAIL_CLOSED_RATE",
            DEFAULT_MAX_FAIL_CLOSED_RATE,
        ),
        alert_webhook_url=(
            os.getenv("TRUSTED_WEB_MONITOR_ALERT_WEBHOOK_URL") or ""
        ).strip(),
    )


def _validate_config(
    config: TrustedWebMonitorJobConfigV1,
) -> TrustedWebMonitorJobConfigV1:
    if config.enabled != AUTHORIZED_VALUE:
        raise MonitorJobConfigurationError("monitor_not_authorized")
    if not config.postgres_dsn.strip():
        raise MonitorJobConfigurationError("missing_postgres_dsn")
    if not MIN_MONITOR_HOURS <= config.window_hours <= MAX_MONITOR_HOURS:
        raise MonitorJobConfigurationError("invalid_window_hours")
    if config.max_relevance_fail_closed < 0:
        raise MonitorJobConfigurationError(
            "invalid_max_relevance_fail_closed"
        )
    if config.max_dependency_failures < 0:
        raise MonitorJobConfigurationError(
            "invalid_max_dependency_failures"
        )
    if not 0.0 <= config.max_fail_closed_rate <= 1.0:
        raise MonitorJobConfigurationError(
            "invalid_max_fail_closed_rate"
        )
    if (
        config.alert_webhook_url
        and not config.alert_webhook_url.startswith("https://")
    ):
        raise MonitorJobConfigurationError("invalid_alert_webhook_url")
    return TrustedWebMonitorJobConfigV1(
        enabled=config.enabled,
        postgres_dsn=config.postgres_dsn.strip(),
        window_hours=int(config.window_hours),
        max_relevance_fail_closed=int(
            config.max_relevance_fail_closed
        ),
        max_dependency_failures=int(config.max_dependency_failures),
        max_fail_closed_rate=float(config.max_fail_closed_rate),
        alert_webhook_url=config.alert_webhook_url.strip(),
    )


def _base_report(
    *,
    status: str,
    occurred_at: str,
) -> dict[str, object]:
    return {
        "contract_version": JOB_CONTRACT_VERSION,
        "status": status,
        "occurred_at": occurred_at,
    }


def _alert_payload(report: dict[str, object]) -> dict[str, object]:
    return {
        "text": "Verbal Sage trusted-web monitor requires attention",
        "contract_version": JOB_CONTRACT_VERSION,
        "status": report["status"],
        "error_code": report.get("error_code"),
        "threshold_violations": report.get(
            "threshold_violations",
            [],
        ),
        "window_hours": report.get("window_hours"),
        "request_count": report.get("request_count"),
        "completed_count": report.get("completed_count"),
        "fail_closed_count": report.get("fail_closed_count"),
        "relevance_fail_closed_count": report.get(
            "relevance_fail_closed_count"
        ),
        "dependency_failure_count": report.get(
            "dependency_failure_count"
        ),
        "fail_closed_rate": report.get("fail_closed_rate"),
        "occurred_at": report["occurred_at"],
    }


async def _send_alert(
    config: TrustedWebMonitorJobConfigV1,
    report: dict[str, object],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    if not config.alert_webhook_url:
        return "unconfigured"
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            transport=transport,
        ) as client:
            response = await client.post(
                config.alert_webhook_url,
                json=_alert_payload(report),
            )
            if 200 <= response.status_code < 300:
                return "delivered"
            return f"http_{response.status_code}"
    except Exception:
        return "delivery_failed"


ConnectCallable = Callable[..., Awaitable[Any]]


async def run_trusted_web_monitor_job_v1(
    config: TrustedWebMonitorJobConfigV1,
    *,
    connect: ConnectCallable = asyncpg.connect,
    alert_transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, object], int]:
    config = _validate_config(config)
    occurred_at = _now_iso()
    try:
        conn = await connect(config.postgres_dsn, command_timeout=15)
        try:
            summary = await load_trusted_web_monitoring_summary_v1(
                conn,
                hours=config.window_hours,
            )
        finally:
            await conn.close()
    except Exception:
        report = _base_report(
            status="unavailable",
            occurred_at=occurred_at,
        )
        report["error_code"] = "monitor_query_failed"
        report["alert_delivery"] = await _send_alert(
            config,
            report,
            transport=alert_transport,
        )
        return report, 3

    violations = evaluate_trusted_web_monitoring_thresholds_v1(
        summary,
        max_relevance_fail_closed=(
            config.max_relevance_fail_closed
        ),
        max_dependency_failures=config.max_dependency_failures,
        max_fail_closed_rate=config.max_fail_closed_rate,
    )
    report = _base_report(
        status="violated" if violations else "pass",
        occurred_at=occurred_at,
    )
    report["monitor_contract_version"] = summary.contract_version
    for key, value in summary.as_dict().items():
        if key not in {"buckets", "contract_version"}:
            report[key] = value
    report["threshold_violations"] = list(violations)
    report["alert_delivery"] = "not_needed"
    if violations:
        report["alert_delivery"] = await _send_alert(
            config,
            report,
            transport=alert_transport,
        )
    return report, 2 if violations else 0


def _configuration_failure_report(
    exc: MonitorJobConfigurationError,
) -> dict[str, object]:
    return {
        "contract_version": JOB_CONTRACT_VERSION,
        "status": "unavailable",
        "error_code": exc.code,
        "alert_delivery": "not_attempted",
        "occurred_at": _now_iso(),
    }


async def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--print-contract",
        action="store_true",
        help="Print the metadata-only monitor contract without querying.",
    )
    args = parser.parse_args(argv)
    if args.print_contract:
        print(
            json.dumps(
                {
                    "contract_version": JOB_CONTRACT_VERSION,
                    "authorized_value": AUTHORIZED_VALUE,
                    "default_window_hours": DEFAULT_WINDOW_HOURS,
                    "default_max_relevance_fail_closed": (
                        DEFAULT_MAX_RELEVANCE_FAIL_CLOSED
                    ),
                    "default_max_dependency_failures": (
                        DEFAULT_MAX_DEPENDENCY_FAILURES
                    ),
                    "default_max_fail_closed_rate": (
                        DEFAULT_MAX_FAIL_CLOSED_RATE
                    ),
                    "metadata_only": True,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    try:
        config = _config_from_environment()
        report, exit_code = await run_trusted_web_monitor_job_v1(config)
    except MonitorJobConfigurationError as exc:
        report = _configuration_failure_report(exc)
        exit_code = 3
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
