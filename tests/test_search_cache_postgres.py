from __future__ import annotations

import unittest
from typing import Any

from seebx.adapters.search_cache import load_cached_ods_creatine_guidance
from seebx.capabilities.search.ods import ODSClientError


SOURCE_ID = "ExerciseAndAthleticPerformance:Creatine:Consumer"


class FakeConnection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((sql, args))
        return self.row


def valid_row() -> dict[str, Any]:
    return {
        "source_id": SOURCE_ID,
        "url": "https://ods.od.nih.gov/factsheets/ExerciseAndAthleticPerformance-Consumer/",
        "title": "Exercise and Athletic Performance",
        "section_title": "Creatine",
        "evidence_type": "official_public_guidance",
        "guidance_text": "Controlled ODS guidance.",
    }


class SearchCachePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_unexpired_cache_record_is_admitted(self) -> None:
        connection = FakeConnection(valid_row())

        record = await load_cached_ods_creatine_guidance(connection)

        assert record is not None
        self.assertEqual(record.source_id, SOURCE_ID)
        self.assertEqual(record.guidance_text, "Controlled ODS guidance.")
        self.assertEqual(len(connection.calls), 1)
        sql, args = connection.calls[0]
        self.assertIn("status = 'active'", sql)
        self.assertIn("expires_at > now()", sql)
        self.assertEqual(args, (SOURCE_ID,))

    async def test_cache_miss_returns_none(self) -> None:
        self.assertIsNone(
            await load_cached_ods_creatine_guidance(FakeConnection(None))
        )

    async def test_invalid_cache_record_fails_closed(self) -> None:
        row = valid_row()
        row["guidance_text"] = ""

        with self.assertRaisesRegex(
            ODSClientError,
            "ods_cache_record_invalid",
        ):
            await load_cached_ods_creatine_guidance(FakeConnection(row))


if __name__ == "__main__":
    unittest.main()
