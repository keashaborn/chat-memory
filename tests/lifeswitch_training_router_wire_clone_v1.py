from __future__ import annotations

import asyncio
import json
import os
from uuid import UUID

from starlette.requests import Request


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B_SESSION = UUID("bbbbbbbb-4000-4000-8000-000000000001")
OWNER_A_SESSION = UUID("aaaaaaaa-4000-4000-8000-000000000001")

LEGACY_FIELDS = {
    "training_set_log_id",
    "training_session_id",
    "owner_user_id",
    "workout_template_id",
    "exercise_id",
    "exercise_name",
    "set_type",
    "exercise_role_snapshot",
    "capture_role",
    "exercise_role",
    "exercise_sort_order",
    "set_index",
    "weight",
    "reps",
    "volume",
    "load_unit",
    "flags",
    "notes",
    "is_active",
    "created_at",
    "updated_at",
    "_target_user_id",
    "_delegated_view",
}


def request_for(owner: UUID) -> Request:
    header_value = str(owner).encode("ascii")
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-vs-actor-user-id", header_value)],
        }
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def main() -> None:
    os.environ["POSTGRES_DSN"] = os.environ["LIFESWITCH_GATEWAY_TEST_DSN"]
    from seebx.capabilities.training.routes import list_training_session_sets

    response = await list_training_session_sets(
        str(OWNER_A_SESSION), request_for(OWNER_A), str(OWNER_A), 0, ""
    )
    payload = json.loads(response.body)
    require(len(payload) == 1, "expected one Owner A set")
    row = payload[0]
    require(LEGACY_FIELDS <= set(row), "legacy set wire fields changed")
    require(
        {"effective_role", "resolution_source", "role_conflict"} <= set(row),
        "effective-role wire fields missing",
    )
    require(row["capture_role"] == "unknown", "raw capture role changed")
    require(row["exercise_role"] == "unknown", "legacy exercise role changed")
    require(row["effective_role"] == "strength", "reviewed role not resolved")
    require(
        row["resolution_source"] == "training_session_role_event",
        "wrong reviewed-role provenance",
    )
    require(row["role_conflict"] is False, "unexpected conflict")

    isolated = await list_training_session_sets(
        str(OWNER_B_SESSION), request_for(OWNER_A), str(OWNER_A), 0, ""
    )
    require(json.loads(isolated.body) == [], "cross-owner session leaked")
    print(
        "TRAINING_SET_WIRE_CLONE="
        "legacy_fields_preserved,effective_fields_additive,raw_role_immutable,"
        "owner_isolation"
    )


if __name__ == "__main__":
    asyncio.run(main())
