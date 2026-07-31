from pathlib import Path
import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from starlette.requests import Request

from rag_engine.lifeswitch_account_timezone_router_v1 import (
    TimezoneUpdateV1,
    _actor,
    _request_hash,
    _timezone,
)


ROOT = Path(__file__).resolve().parents[1]


def request(*, actor: str = "", request_id: str = "request-1") -> Request:
    headers = []
    if actor:
        headers.append((b"x-vs-actor-user-id", actor.encode("ascii")))
    value = Request({"type": "http", "headers": headers})
    value.state.request_id = request_id
    return value


class LifeSwitchAccountTimezoneV1Tests(unittest.TestCase):
    def test_actor_is_derived_only_from_trusted_header(self) -> None:
        actor = "673d64a3-c4ba-4d1c-89e3-e0c579022fad"
        self.assertEqual(str(_actor(request(actor=actor))), actor)
        with self.assertRaises(HTTPException):
            _actor(request())

    def test_timezone_validation_accepts_iana_and_rejects_invalid(self) -> None:
        self.assertEqual(_timezone("America/Chicago"), "America/Chicago")
        with self.assertRaises(HTTPException):
            _timezone("not/a/timezone")
        with self.assertRaises(HTTPException):
            _timezone(" America/Chicago ")

    def test_request_binding_is_content_free_sha256(self) -> None:
        value = _request_hash(request(request_id="request-1"))
        self.assertRegex(value, r"^[0-9a-f]{64}$")

    def test_update_contract_forbids_owner_and_unknown_fields(self) -> None:
        valid = TimezoneUpdateV1(
            timezone_name="America/Chicago",
            expected_revision=1,
        )
        self.assertEqual(valid.expected_revision, 1)
        with self.assertRaises(Exception):
            TimezoneUpdateV1.model_validate(
                {
                    "timezone_name": "America/Chicago",
                    "expected_revision": 1,
                    "owner_user_id": "673d64a3-c4ba-4d1c-89e3-e0c579022fad",
                }
            )

    def test_sql_uses_restricted_owner_bound_gateway_and_history(self) -> None:
        sql = (
            ROOT
            / "ops/sql/20260731_lifeswitch_account_timezone_setting_v1.sql"
        ).read_text()
        lowered = sql.lower()
        self.assertRegex(
            lowered,
            r"lifeswitch_chat_account_writer_v1\s+nologin noinherit",
        )
        self.assertIn("session_user <> 'brains_app'", lowered)
        self.assertIn("current_setting('app.user_id',true)", lowered)
        self.assertIn("current_setting('app.lifeswitch_owner_id',true)", lowered)
        self.assertIn("pg_catalog.pg_timezone_names", lowered)
        self.assertIn("p_expected_revision", lowered)
        self.assertIn("timezone revision conflict", lowered)
        self.assertIn("account_timezone_history_v1", lowered)
        self.assertIn("append-only", lowered)
        self.assertIn("force row level security", lowered)
        self.assertNotIn(
            "grant select on lifeswitch_chat.account_timezone_v1",
            lowered,
        )

    def test_router_does_not_accept_owner_from_query_or_body(self) -> None:
        source = (
            ROOT / "rag_engine/lifeswitch_account_timezone_router_v1.py"
        ).read_text()
        self.assertNotIn("owner_user_id: str", source)
        self.assertIn('req.headers.get("x-vs-actor-user-id")', source)
        self.assertIn("set local role", source)


if __name__ == "__main__":
    unittest.main()
