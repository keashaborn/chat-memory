from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import asyncpg


CONTRACT_VERSION = "telemetry_retention_v1"


async def enforce_telemetry_retention_v1(
    conn: Any,
) -> dict[str, Any]:
    result = await conn.fetchval(
        "SELECT ai_operations.enforce_telemetry_retention_v1()"
    )
    if not isinstance(result, dict):
        result = json.loads(str(result))
    if result.get("contract_version") != CONTRACT_VERSION:
        raise RuntimeError("unexpected telemetry retention contract")
    return result


async def _main() -> int:
    dsn = (os.getenv("POSTGRES_DSN") or "").strip()
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")

    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        result = await enforce_telemetry_retention_v1(conn)
    finally:
        await conn.close()

    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
