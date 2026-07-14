#!/usr/bin/env python3
from __future__ import annotations

import asyncio

from fastapi import HTTPException
from starlette.requests import Request

from app import VantageSyncReq, vantages_list, vantages_sync


ACTOR_A = "11111111-1111-4111-8111-111111111111"
ACTOR_B = "22222222-2222-4222-8222-222222222222"


def request(actor: str | None, path: str) -> Request:
    headers = []
    if actor is not None:
        headers.append((b"x-vs-actor-user-id", actor.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers,
        }
    )


async def expect_http(status: int, detail: str, awaitable) -> None:
    try:
        await awaitable
    except HTTPException as exc:
        if exc.status_code != status or exc.detail != detail:
            raise AssertionError(
                f"expected {status}/{detail}, got {exc.status_code}/{exc.detail}"
            ) from exc
        return
    raise AssertionError(f"expected HTTP {status}/{detail}")


async def main() -> int:
    sync_body = VantageSyncReq(user_id=ACTOR_B, mode="active", active={})

    await expect_http(
        401,
        "missing_actor_user_id",
        vantages_sync(sync_body, request(None, "/vantages/sync")),
    )
    await expect_http(
        403,
        "actor_owner_mismatch",
        vantages_sync(sync_body, request(ACTOR_A, "/vantages/sync")),
    )
    await expect_http(
        401,
        "missing_actor_user_id",
        vantages_list(ACTOR_B, request(None, f"/vantages/{ACTOR_B}")),
    )
    await expect_http(
        403,
        "actor_owner_mismatch",
        vantages_list(ACTOR_B, request(ACTOR_A, f"/vantages/{ACTOR_B}")),
    )

    print("vantage_registry_actor_auth: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
