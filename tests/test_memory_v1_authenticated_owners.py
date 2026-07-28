from __future__ import annotations

import unittest
import uuid

from scripts.memory_v1_authenticated_owners import explicit_owners


class AuthenticatedOwnerResolverTest(unittest.TestCase):
    def test_explicit_owner_scope_is_sorted_and_deduplicated(self) -> None:
        first = "22222222-2222-4222-8222-222222222222"
        second = "11111111-1111-4111-8111-111111111111"
        self.assertEqual(
            explicit_owners([first, second, first]),
            [uuid.UUID(second), uuid.UUID(first)],
        )

    def test_invalid_uuid_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid UUID"):
            explicit_owners(["not-a-uuid"])


if __name__ == "__main__":
    unittest.main()
