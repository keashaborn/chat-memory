#!/usr/bin/env python3
from __future__ import annotations

"""Emit a metadata-only trusted-web monitoring report."""

import argparse
import asyncio
import json
import os
import sys
from typing import Sequence

import asyncpg

from rag_engine.trusted_web_monitoring_v1 import (
    evaluate_trusted_web_monitoring_thresholds_v1,
    load_trusted_web_monitoring_summary_v1,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report trusted-web relevance and fail-closed counts without "
            "queries, user identifiers, or source URLs."
        )
    )
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument(
        "--max-relevance-fail-closed",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--max-dependency-failures",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--max-fail-closed-rate",
        type=float,
        default=None,
    )
    return parser


async def _report(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    dsn = (os.getenv("POSTGRES_DSN") or "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=15)
    try:
        summary = await load_trusted_web_monitoring_summary_v1(
            conn,
            hours=args.hours,
        )
    finally:
        await conn.close()

    payload = summary.as_dict()
    violations = evaluate_trusted_web_monitoring_thresholds_v1(
        summary,
        max_relevance_fail_closed=args.max_relevance_fail_closed,
        max_dependency_failures=args.max_dependency_failures,
        max_fail_closed_rate=args.max_fail_closed_rate,
    )
    payload["threshold_status"] = (
        "violated" if violations else "pass"
    )
    payload["threshold_violations"] = list(violations)
    return payload, 2 if violations else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        args.max_relevance_fail_closed is not None
        and args.max_relevance_fail_closed < 0
    ):
        raise SystemExit("--max-relevance-fail-closed must be >= 0")
    if (
        args.max_dependency_failures is not None
        and args.max_dependency_failures < 0
    ):
        raise SystemExit("--max-dependency-failures must be >= 0")
    if (
        args.max_fail_closed_rate is not None
        and not 0.0 <= args.max_fail_closed_rate <= 1.0
    ):
        raise SystemExit("--max-fail-closed-rate must be between 0 and 1")
    try:
        payload, status = asyncio.run(_report(args))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "contract_version": "trusted_web_monitoring_v1",
                    "status": "unavailable",
                    "error_code": type(exc).__name__,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 3
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
