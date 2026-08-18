from __future__ import annotations

import unittest

from fastapi import HTTPException
from starlette.requests import Request

from seebx.core.ownership import (
    require_actor_matches_owner,
    require_authenticated_actor,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "9062eaa7-1105-49af-9308-44d06b378c4d"


def request(actor: str | None) -> Request:
    headers = [] if actor is None else [(b"x-vs-actor-user-id", actor.encode())]
    return Request({"type": "http", "headers": headers})


class CoreOwnershipTests(unittest.TestCase):
    def test_matching_actor_and_owner_returns_canonical_uuid(self) -> None:
        self.assertEqual(require_actor_matches_owner(request(OWNER), OWNER), OWNER)

    def test_mismatched_actor_is_forbidden(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            require_actor_matches_owner(request(OTHER), OWNER)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.detail, "actor_owner_mismatch")

    def test_missing_actor_is_unauthorized(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            require_authenticated_actor(request(None))
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(caught.exception.detail, "missing_actor_user_id")

    def test_invalid_owner_is_rejected_before_comparison(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            require_actor_matches_owner(request(OWNER), "not-a-uuid")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "invalid owner_user_id")


if __name__ == "__main__":
    unittest.main()
