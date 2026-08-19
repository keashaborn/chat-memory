from __future__ import annotations

import argparse
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from seebx.capabilities.forms.migration import (
    build_forms_migration_plan,
    canonical_json_bytes,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "migrate_lifeswitch_forms_v1.py"
SPEC = importlib.util.spec_from_file_location("migrate_lifeswitch_forms_v1", SCRIPT_PATH)
assert SPEC and SPEC.loader
SCRIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCRIPT)

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
TEMPLATE = "33333333-3333-4333-8333-333333333333"
VERSION = "44444444-4444-4444-8444-444444444444"
ENTRY = "55555555-5555-4555-8555-555555555555"
INVALID_ENTRY = "66666666-6666-4666-8666-666666666666"
NOW = datetime(2026, 8, 19, 6, 0, tzinfo=timezone.utc)


def template(*, owner: str = OWNER) -> dict[str, object]:
    return {
        "id": TEMPLATE,
        "owner_user_id": owner,
        "name": "Daily check-in",
        "status": "published",
        "created_at": NOW,
    }


def version(*, schema: object | None = None) -> dict[str, object]:
    return {
        "id": VERSION,
        "template_id": TEMPLATE,
        "version": 1,
        "json_schema": schema
        if schema is not None
        else {
            "type": "object",
            "properties": {"energy": {"type": "integer"}},
            "required": ["energy"],
        },
        "ui_schema": {},
        "metadata": {"source": "synthetic"},
        "created_at": NOW,
    }


def entry(
    *,
    entry_id: str = ENTRY,
    owner: str = OWNER,
    version_id: str = VERSION,
) -> dict[str, object]:
    return {
        "id": entry_id,
        "owner_user_id": owner,
        "subject_id": "self",
        "template_version_id": version_id,
        "occurred_at": NOW,
        "data": {"energy": 7},
        "created_at": NOW,
    }


class FormsMigrationPlanTests(unittest.TestCase):
    def test_valid_chain_and_invalid_owner_are_partitioned_exactly(self) -> None:
        plan = build_forms_migration_plan(
            [template()],
            [version()],
            [
                entry(),
                entry(entry_id=INVALID_ENTRY, owner="legacy-owner"),
            ],
        )
        summary = plan.summary()
        self.assertEqual(
            summary["source_counts"],
            {"templates": 1, "versions": 1, "entries": 2},
        )
        self.assertEqual(
            summary["eligible_counts"],
            {"templates": 1, "versions": 1, "entries": 1},
        )
        self.assertEqual(summary["quarantine_count"], 1)
        self.assertEqual(
            summary["quarantine_reason_counts"],
            {"entry_owner_invalid_uuid": 1},
        )
        self.assertEqual(plan.entries[0]["owner_user_id"], OWNER)
        self.assertNotIn("legacy-owner", canonical_json_bytes(plan.entries).decode())
        self.assertIn(
            "legacy-owner",
            canonical_json_bytes(plan.quarantine).decode(),
        )

    def test_plan_hashes_are_order_independent_and_content_bound(self) -> None:
        first = build_forms_migration_plan(
            [template()],
            [version()],
            [
                entry(),
                entry(entry_id=INVALID_ENTRY, owner="legacy-owner"),
            ],
        )
        reordered = build_forms_migration_plan(
            list(reversed([template()])),
            list(reversed([version()])),
            list(
                reversed(
                    [
                        entry(),
                        entry(entry_id=INVALID_ENTRY, owner="legacy-owner"),
                    ]
                )
            ),
        )
        self.assertEqual(first.source_bundle_sha256, reordered.source_bundle_sha256)
        self.assertEqual(first.valid_bundle_sha256, reordered.valid_bundle_sha256)
        self.assertEqual(first.quarantine_sha256, reordered.quarantine_sha256)
        changed_entry = entry()
        changed_entry["data"] = {"energy": 8}
        changed = build_forms_migration_plan(
            [template()],
            [version()],
            [changed_entry],
        )
        self.assertNotEqual(first.source_bundle_sha256, changed.source_bundle_sha256)
        self.assertNotEqual(first.valid_bundle_sha256, changed.valid_bundle_sha256)

    def test_cross_owner_entry_is_quarantined(self) -> None:
        plan = build_forms_migration_plan(
            [template()],
            [version()],
            [entry(owner=OTHER)],
        )
        self.assertEqual(len(plan.entries), 0)
        self.assertEqual(
            plan.summary()["quarantine_reason_counts"],
            {"entry_owner_mismatch": 1},
        )

    def test_invalid_template_cascades_to_dependent_rows(self) -> None:
        plan = build_forms_migration_plan(
            [template(owner="not-a-uuid")],
            [version()],
            [entry()],
        )
        self.assertEqual(
            plan.summary()["eligible_counts"],
            {"templates": 0, "versions": 0, "entries": 0},
        )
        self.assertEqual(plan.summary()["quarantine_count"], 3)
        self.assertEqual(
            plan.summary()["quarantine_reason_counts"],
            {
                "entry_parent_version_ineligible": 1,
                "template_owner_invalid_uuid": 1,
                "version_parent_template_ineligible": 1,
            },
        )

    def test_invalid_json_schema_is_quarantined(self) -> None:
        plan = build_forms_migration_plan(
            [template()],
            [version(schema={"type": "not-a-json-schema-type"})],
            [entry()],
        )
        self.assertEqual(len(plan.versions), 0)
        self.assertEqual(len(plan.entries), 0)
        self.assertEqual(
            plan.summary()["quarantine_reason_counts"],
            {
                "entry_parent_version_ineligible": 1,
                "version_json_schema_invalid": 1,
            },
        )


class FakePreflightConnection:
    def __init__(self, **overrides: object):
        self.role = {
            "role_name": "lifeswitch_app_login",
            "rolsuper": False,
            "rolbypassrls": False,
            "rolcreaterole": False,
            "rolcreatedb": False,
            "rolreplication": False,
            "rolinherit": True,
            "app_member": True,
            "owner_member": False,
        }
        self.role.update(overrides)
        self.fetchrow_count = 0

    async def fetchrow(self, _query: str):
        self.fetchrow_count += 1
        if self.fetchrow_count == 1:
            return self.role
        return {
            "table_count": 3,
            "rls_enabled": True,
            "rls_forced": True,
        }

    async def fetchval(self, _query: str):
        return 3


class FormsMigrationDestinationPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_restricted_application_login_is_accepted(self) -> None:
        await SCRIPT._destination_preflight(FakePreflightConnection())

    async def test_elevated_or_unrelated_roles_are_rejected(self) -> None:
        cases = (
            {"app_member": False},
            {"owner_member": True},
            {"rolsuper": True},
            {"rolbypassrls": True},
            {"rolcreaterole": True},
            {"rolcreatedb": True},
            {"rolreplication": True},
            {"rolinherit": False},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(RuntimeError):
                    await SCRIPT._destination_preflight(
                        FakePreflightConnection(**overrides)
                    )


class FormsMigrationApplyGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = build_forms_migration_plan(
            [template()],
            [version()],
            [entry(), entry(entry_id=INVALID_ENTRY, owner="legacy-owner")],
        )

    def args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "expected_source_sha256": self.plan.source_bundle_sha256,
            "expected_valid_sha256": self.plan.valid_bundle_sha256,
            "expected_quarantine_sha256": self.plan.quarantine_sha256,
            "approval_id": "forms-migration-approval-1",
            "encryption_evidence_sha256": "a" * 64,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_apply_gate_requires_exact_hashes_approval_and_encryption(self) -> None:
        SCRIPT._assert_expected(self.args(), self.plan)
        for field, value in (
            ("expected_source_sha256", "0" * 64),
            ("expected_valid_sha256", "0" * 64),
            ("expected_quarantine_sha256", "0" * 64),
            ("approval_id", ""),
            ("encryption_evidence_sha256", "not-a-hash"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(RuntimeError):
                    SCRIPT._assert_expected(self.args(**{field: value}), self.plan)

    def test_quarantine_artifact_is_exclusive_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as parent_text:
            parent = Path(parent_text)
            output = parent / "receipt"
            partial = SCRIPT._prepare_artifact(
                output,
                self.plan,
                "b" * 64,
                "forms-migration-approval-1",
                "a" * 64,
            )
            self.assertEqual(os.stat(partial).st_mode & 0o777, 0o700)
            self.assertEqual(
                os.stat(partial / "quarantine.jsonl").st_mode & 0o777,
                0o600,
            )
            self.assertEqual(
                os.stat(partial / "prepared-receipt.json").st_mode & 0o777,
                0o600,
            )
            quarantine = (partial / "quarantine.jsonl").read_text()
            self.assertIn("entry_owner_invalid_uuid", quarantine)
            self.assertIn("legacy-owner", quarantine)
            with self.assertRaises(RuntimeError):
                SCRIPT._prepare_artifact(
                    partial,
                    self.plan,
                    "b" * 64,
                    "forms-migration-approval-1",
                    "a" * 64,
                )


if __name__ == "__main__":
    unittest.main()
