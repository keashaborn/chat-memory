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

from seebx.capabilities.search.monitoring import (
    MAX_MONITOR_HOURS,
    MIN_MONITOR_HOURS,
    evaluate_trusted_web_monitoring_thresholds_v1,
    load_trusted_web_monitoring_summary_v1,
)


JOB_CONTRACT_VERSION = "trusted_web_monitor_job_v1"
ALERT_CONTRACT_VERSION = "trusted_web_monitor_alert_v1"
INBOX_CONTRACT_VERSION = "ai_operations_monitor_record_v1"
AUTHORIZED_VALUE = "authorized"
DRILL_ERROR_CODE = "synthetic_failure_drill"
MONITOR_NAME = "trusted_web_retrieval"
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
    drill_enabled: str = ""


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
        drill_enabled=(
            os.getenv("TRUSTED_WEB_MONITOR_DRILL_ENABLED") or ""
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
        drill_enabled=config.drill_enabled.strip(),
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


def _alert_payload(
    report: dict[str, object],
    *,
    drill: bool = False,
) -> dict[str, object]:
    return {
        "text": (
            "Verbal Sage trusted-web monitor delivery drill"
            if drill
            else "Verbal Sage trusted-web monitor requires attention"
        ),
        "contract_version": JOB_CONTRACT_VERSION,
        "alert_contract_version": ALERT_CONTRACT_VERSION,
        "event_type": (
            "trusted_web_monitor_delivery_drill"
            if drill
            else "trusted_web_monitor_failure"
        ),
        "severity": "test" if drill else "critical",
        "drill": drill,
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
    drill: bool = False,
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
                json=_alert_payload(report, drill=drill),
            )
            if 200 <= response.status_code < 300:
                return "delivered"
            return f"http_{response.status_code}"
    except Exception:
        return "delivery_failed"


ConnectCallable = Callable[..., Awaitable[Any]]


async def _record_alert_inbox(
    connection: Any,
    report: dict[str, object],
    *,
    drill: bool = False,
) -> str:
    status = "drill" if drill else str(report["status"])
    if drill:
        reason_codes = [DRILL_ERROR_CODE]
        severity = "test"
    elif status == "pass":
        reason_codes = []
        severity = "info"
    else:
        reason_codes = list(
            report.get("threshold_violations") or []
        )
        if not reason_codes and report.get("error_code"):
            reason_codes = [str(report["error_code"])]
        severity = "critical"
    try:
        result = await connection.fetchval(
            """
            SELECT ai_operations.record_monitor_observation_v1(
                $1,$2,$3,$4,$5::text[],$6,$7,$8,$9,$10,$11,$12,$13
            )
            """,
            MONITOR_NAME,
            status,
            severity,
            drill,
            reason_codes,
            int(report.get("window_hours") or DEFAULT_WINDOW_HOURS),
            int(report.get("request_count") or 0),
            int(report.get("completed_count") or 0),
            int(report.get("fail_closed_count") or 0),
            int(report.get("relevance_fail_closed_count") or 0),
            int(report.get("dependency_failure_count") or 0),
            float(report.get("fail_closed_rate") or 0.0),
            datetime.fromisoformat(
                str(report["occurred_at"]).replace("Z", "+00:00")
            ),
        )
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            return "failed"
        if result.get("contract_version") != INBOX_CONTRACT_VERSION:
            return "failed"
        return "recorded"
    except Exception:
        return "failed"


async def run_trusted_web_monitor_job_v1(
    config: TrustedWebMonitorJobConfigV1,
    *,
    connect: ConnectCallable = asyncpg.connect,
    alert_transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, object], int]:
    config = _validate_config(config)
    occurred_at = _now_iso()
    conn: Any | None = None
    try:
        conn = await connect(config.postgres_dsn, command_timeout=15)
        summary = await load_trusted_web_monitoring_summary_v1(
            conn,
            hours=config.window_hours,
        )
    except Exception:
        report = _base_report(
            status="unavailable",
            occurred_at=occurred_at,
        )
        report["error_code"] = "monitor_query_failed"
        report["window_hours"] = config.window_hours
        report["alert_store"] = "failed"
        if conn is not None:
            report["alert_store"] = await _record_alert_inbox(
                conn,
                report,
            )
            await conn.close()
        report["alert_delivery"] = await _send_alert(
            config,
            report,
            transport=alert_transport,
        )
        return report, 3

    try:
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
        report["alert_store"] = await _record_alert_inbox(
            conn,
            report,
        )
    finally:
        await conn.close()
    report["alert_delivery"] = "not_needed"
    if violations:
        report["alert_delivery"] = await _send_alert(
            config,
            report,
            transport=alert_transport,
        )
    if report["alert_store"] != "recorded":
        return report, 3
    return report, 2 if violations else 0


async def run_trusted_web_alert_drill_v1(
    config: TrustedWebMonitorJobConfigV1,
    *,
    connect: ConnectCallable = asyncpg.connect,
    alert_transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, object], int]:
    """Record one synthetic alert without querying or mutating search data."""

    config = _validate_config(config)
    if config.drill_enabled != AUTHORIZED_VALUE:
        raise MonitorJobConfigurationError("drill_not_authorized")

    report = _base_report(status="drill", occurred_at=_now_iso())
    report.update(
        {
            "drill": True,
            "error_code": DRILL_ERROR_CODE,
            "threshold_violations": [DRILL_ERROR_CODE],
            "window_hours": config.window_hours,
            "request_count": 0,
            "completed_count": 0,
            "fail_closed_count": 0,
            "relevance_fail_closed_count": 0,
            "dependency_failure_count": 0,
            "fail_closed_rate": 0.0,
        }
    )
    try:
        conn = await connect(config.postgres_dsn, command_timeout=15)
        try:
            report["alert_store"] = await _record_alert_inbox(
                conn,
                report,
                drill=True,
            )
        finally:
            await conn.close()
    except Exception:
        report["alert_store"] = "failed"
    report["alert_delivery"] = await _send_alert(
        config,
        report,
        transport=alert_transport,
        drill=True,
    )
    delivery_ok = report["alert_delivery"] in {
        "unconfigured",
        "delivered",
    }
    return (
        report,
        0
        if report["alert_store"] == "recorded" and delivery_ok
        else 3,
    )


def _configuration_failure_report(
    exc: MonitorJobConfigurationError,
) -> dict[str, object]:
    return {
        "contract_version": JOB_CONTRACT_VERSION,
        "status": "unavailable",
        "error_code": exc.code,
        "alert_delivery": "not_attempted",
        "alert_store": "not_attempted",
        "occurred_at": _now_iso(),
    }


async def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--print-contract",
        action="store_true",
        help="Print the metadata-only monitor contract without querying.",
    )
    parser.add_argument(
        "--drill",
        action="store_true",
        help="Send one authorized synthetic alert without querying data.",
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
                    "alert_contract_version": ALERT_CONTRACT_VERSION,
                    "inbox_contract_version": INBOX_CONTRACT_VERSION,
                    "internal_inbox_required": True,
                    "drill_requires_secondary_authorization": True,
                    "metadata_only": True,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    try:
        config = _config_from_environment()
        if args.drill:
            report, exit_code = await run_trusted_web_alert_drill_v1(
                config
            )
        else:
            report, exit_code = await run_trusted_web_monitor_job_v1(
                config
            )
    except MonitorJobConfigurationError as exc:
        report = _configuration_failure_report(exc)
        exit_code = 3
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
