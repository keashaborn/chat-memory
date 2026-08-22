from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
import json
import os
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import HTTPException


os.environ.setdefault("POSTGRES_DSN", "postgresql://test-only")

from seebx.adapters.lifeswitch_catalog_postgres import (  # noqa: E402
    EXERCISE_BROWSE_SQL,
    EXERCISE_MUSCLES_SQL,
    MUSCLE_CATALOG_SQL,
    PostgresLifeSwitchCatalogReader,
)
from seebx.capabilities.catalog import routes as catalog_routes  # noqa: E402
from seebx.capabilities.catalog.exercise_muscles import (  # noqa: E402
    ExerciseMuscleContractError,
    build_exercise_muscle_profile,
    build_muscle_catalog_response,
)


EXERCISE_ID = UUID("51b3a3ec-613c-41b4-a4ab-1273b6e1dcf6")


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.transactions = []
        self.fetched = []

    def transaction(self, **options):
        self.transactions.append(options)
        return Transaction()

    async def fetch(self, sql, *arguments):
        self.fetched.append((sql, arguments))
        return self.rows


class FakeReader:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.list_arguments = None
        self.exercise_arguments = None

    async def list_muscles(self, *arguments):
        self.list_arguments = arguments
        return self.rows

    async def read_exercise_muscles(self, *arguments):
        self.exercise_arguments = arguments
        return self.rows


def mapping_row(**changes):
    row = {
        "exercise_id": EXERCISE_ID,
        "exercise_slug": "barbell_bench_press",
        "exercise_display_name": "Barbell Bench Press",
        "muscle_slug": "pectoralis_major",
        "muscle_display_name": "Pectoralis Major",
        "parent_slug": "chest",
        "region": "torso",
        "role": "primary",
        "weight": Decimal("1.000"),
        "aliases": ["Chest", "Pecs"],
    }
    row.update(changes)
    return row


class ExerciseMuscleCapabilityV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_queries_are_read_only_and_argument_bound(self):
        connection = FakeConnection([{"muscle_slug": "pectoralis_major"}])
        reader = PostgresLifeSwitchCatalogReader(connection)

        await reader.list_muscles("chest", "torso", "en", 50)
        await reader.read_exercise_muscles(EXERCISE_ID, "en")

        self.assertEqual(connection.transactions, [{"readonly": True}] * 2)
        self.assertEqual(
            connection.fetched,
            [
                (MUSCLE_CATALOG_SQL, ("chest", "torso", "en", 50)),
                (EXERCISE_MUSCLES_SQL, (EXERCISE_ID, "en")),
            ],
        )
        combined = f"{MUSCLE_CATALOG_SQL}\n{EXERCISE_MUSCLES_SQL}".lower()
        for forbidden in ("insert into", "update ", "delete from", "truncate "):
            self.assertNotIn(forbidden, combined)
        self.assertIn("e.is_active=true", combined)
        self.assertIn("e.is_public=true", combined)

    def test_existing_browse_compatibility_arrays_use_normalized_authority(self):
        lowered = EXERCISE_BROWSE_SQL.lower()
        self.assertNotIn("\n    f.primary_muscles,\n", lowered)
        self.assertNotIn("\n  e.primary_muscles,\n", lowered)
        self.assertIn("catalog_dev.exercise_muscle canonical_em", lowered)
        self.assertIn("catalog_dev.exercise_muscle variant_em", lowered)
        self.assertIn("canonical_em.role='primary'", lowered)
        self.assertIn("variant_em.role='primary'", lowered)

    def test_contract_is_canonical_deterministic_and_projects_legacy_arrays(self):
        profile = build_exercise_muscle_profile(
            [
                mapping_row(
                    muscle_slug="triceps_brachii",
                    muscle_display_name="Triceps Brachii",
                    parent_slug="upper_arm",
                    region="arm",
                    role="secondary",
                    weight=Decimal("0.600"),
                    aliases=["Triceps"],
                ),
                mapping_row(aliases=["Pecs", "Chest", "Chest"]),
                mapping_row(
                    muscle_slug="serratus_anterior",
                    muscle_display_name="Serratus Anterior",
                    parent_slug="chest",
                    role="stabilizer",
                    weight=Decimal("0.250"),
                    aliases=[],
                ),
            ],
            locale="en",
        )

        payload = profile.model_dump(mode="json")
        self.assertEqual(payload["contract_version"], "seebx_exercise_muscle_profile_v1")
        self.assertTrue(payload["normalized_relationships_authoritative"])
        self.assertFalse(payload["legacy_arrays_authoritative"])
        self.assertEqual(
            [item["role"] for item in payload["mappings"]],
            ["primary", "secondary", "stabilizer"],
        )
        self.assertEqual(payload["mappings"][0]["aliases"], ["Chest", "Pecs"])
        self.assertEqual(payload["compatibility_primary_muscles"], ["Pectoralis Major"])
        self.assertEqual(payload["compatibility_secondary_muscles"], ["Triceps Brachii"])

    def test_public_exercise_without_mapping_has_explicit_empty_profile(self):
        profile = build_exercise_muscle_profile(
            [
                mapping_row(
                    muscle_slug=None,
                    muscle_display_name=None,
                    parent_slug=None,
                    region=None,
                    role=None,
                    weight=None,
                    aliases=[],
                )
            ],
            locale="en",
        )
        self.assertFalse(profile.mapped)
        self.assertEqual(profile.mappings, ())
        self.assertEqual(profile.compatibility_primary_muscles, ())

    def test_duplicate_or_unknown_mapping_fails_closed(self):
        with self.assertRaisesRegex(
            ExerciseMuscleContractError, "exercise_muscle_identity_invalid"
        ):
            build_exercise_muscle_profile(
                [mapping_row(), mapping_row()], locale="en"
            )
        with self.assertRaisesRegex(
            ExerciseMuscleContractError, "exercise_muscle_identity_invalid"
        ):
            build_exercise_muscle_profile(
                [mapping_row(role="target")], locale="en"
            )

    def test_catalog_contract_rejects_duplicate_identity(self):
        row = {
            "muscle_slug": "pectoralis_major",
            "display_name": "Pectoralis Major",
            "parent_slug": "chest",
            "region": "torso",
            "aliases": ["Chest"],
        }
        with self.assertRaisesRegex(
            ExerciseMuscleContractError, "canonical_muscle_identity_invalid"
        ):
            build_muscle_catalog_response(
                [row, row], query="", region="", locale="en"
            )

    async def test_routes_normalize_inputs_and_emit_versioned_contracts(self):
        catalog_reader = FakeReader(
            [
                {
                    "muscle_slug": "pectoralis_major",
                    "display_name": "Pectoralis Major",
                    "parent_slug": "chest",
                    "region": "torso",
                    "aliases": ["Chest"],
                }
            ]
        )

        @asynccontextmanager
        async def catalog_context():
            yield catalog_reader

        with patch.object(catalog_routes, "lifeswitch_catalog_reader", catalog_context):
            response = await catalog_routes.list_muscles(
                q=" Chest ", region=" TORSO ", locale=" EN ", limit=25
            )
        payload = json.loads(response.body)
        self.assertEqual(catalog_reader.list_arguments, ("Chest", "torso", "en", 25))
        self.assertEqual(payload["contract_version"], "seebx_normalized_muscle_catalog_v1")
        self.assertEqual(payload["muscles"][0]["muscle_slug"], "pectoralis_major")

        exercise_reader = FakeReader([mapping_row()])

        @asynccontextmanager
        async def exercise_context():
            yield exercise_reader

        with patch.object(catalog_routes, "lifeswitch_catalog_reader", exercise_context):
            response = await catalog_routes.exercise_muscles(EXERCISE_ID, locale=" EN ")
        payload = json.loads(response.body)
        self.assertEqual(exercise_reader.exercise_arguments, (EXERCISE_ID, "en"))
        self.assertEqual(payload["exercise_id"], str(EXERCISE_ID))

    async def test_missing_exercise_is_sanitized_not_found(self):
        reader = FakeReader([])

        @asynccontextmanager
        async def context():
            yield reader

        with patch.object(catalog_routes, "lifeswitch_catalog_reader", context):
            with self.assertRaises(HTTPException) as caught:
                await catalog_routes.exercise_muscles(EXERCISE_ID, locale="en")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.detail, "exercise not found")


if __name__ == "__main__":
    unittest.main()
