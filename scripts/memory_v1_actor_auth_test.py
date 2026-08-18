#!/usr/bin/env python3
from __future__ import annotations

from fastapi import HTTPException
from starlette.requests import Request

from seebx.core.ownership import require_actor_matches_owner


ACTOR_A = "11111111-1111-4111-8111-111111111111"
ACTOR_B = "22222222-2222-4222-8222-222222222222"


def request(actor: str | None) -> Request:
    headers = []
    if actor is not None:
        headers.append((b"x-vs-actor-user-id", actor.encode("ascii")))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def expect_http(status: int, detail: str, fn) -> None:
    try:
        fn()
    except HTTPException as exc:
        if exc.status_code != status or exc.detail != detail:
            raise AssertionError(
                f"expected {status}/{detail}, got {exc.status_code}/{exc.detail}"
            ) from exc
        return
    raise AssertionError(f"expected HTTP {status}/{detail}")


def main() -> int:
    if require_actor_matches_owner(request(ACTOR_A.upper()), ACTOR_A) != ACTOR_A:
        raise AssertionError("matching actor was not canonicalized")

    expect_http(
        401,
        "missing_actor_user_id",
        lambda: require_actor_matches_owner(request(None), ACTOR_A),
    )
    expect_http(
        400,
        "invalid actor_user_id",
        lambda: require_actor_matches_owner(request("not-a-uuid"), ACTOR_A),
    )
    expect_http(
        400,
        "invalid owner_user_id",
        lambda: require_actor_matches_owner(request(ACTOR_A), "legacy-alias"),
    )
    expect_http(
        403,
        "actor_owner_mismatch",
        lambda: require_actor_matches_owner(request(ACTOR_A), ACTOR_B),
    )

    print("memory_v1_actor_auth: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
