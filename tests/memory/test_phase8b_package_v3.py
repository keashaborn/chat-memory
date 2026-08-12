from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.governed_memory_install import controller_v2, package_v3
from tools.governed_memory_validation import (
    generate_phase8b_package_manifest,
    verify_store_migration_manifest,
)


ROOT = Path(__file__).resolve().parents[2]


class Phase8BPackageV3Tests(unittest.TestCase):
    def _verify_generated_manifest(self) -> dict[str, object]:
        manifest = generate_phase8b_package_manifest.generate()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "package_manifest.json"
            path.write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="ascii",
            )
            with patch.object(package_v3, "MANIFEST", path):
                return package_v3.verify()

    def test_current_members_verify_as_inactive_stores_only(self) -> None:
        receipt = self._verify_generated_manifest()
        self.assertEqual(receipt["schema_version"], (
            "governed-memory-phase8b-package-verification-v2"
        ))
        self.assertEqual(receipt["artifact_count"], 33)
        self.assertFalse(receipt["installation_executor_packaged"])
        self.assertFalse(receipt["rollback_executor_packaged"])
        self.assertFalse(receipt["activation_executor_packaged"])
        self.assertTrue(receipt["stores_supervisor_cli_packaged"])
        self.assertEqual(
            receipt["stores_supervisor_cli_docker_surface"],
            ["container_inspect", "container_start", "container_stop"],
        )
        self.assertFalse(receipt["installation_performed_by_verifier"])
        self.assertFalse(receipt["images_staged_by_verifier"])
        self.assertFalse(receipt["secrets_touched_by_verifier"])
        self.assertFalse(receipt["activation_performed_by_verifier"])
        self.assertEqual(
            receipt["contract_canonical_sha256"],
            package_v3.EXPECTED_CONTRACT_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["plan_canonical_sha256"],
            package_v3.EXPECTED_PLAN_CANONICAL_SHA256,
        )
        self.assertEqual(
            receipt["controller_model_sha256"],
            package_v3.EXPECTED_CONTROLLER_MODEL_SHA256,
        )
        self.assertEqual(
            receipt["controller_source_sha256"],
            package_v3.EXPECTED_CONTROLLER_SOURCE_SHA256,
        )
        self.assertEqual(
            receipt["migration_verifier_source_sha256"],
            package_v3.EXPECTED_MIGRATION_VERIFIER_SOURCE_SHA256,
        )
        artifacts = set(receipt["artifact_sha256"])
        self.assertEqual(artifacts, package_v3.EXPECTED_ARTIFACTS)
        for forbidden in package_v3.FORBIDDEN_ARTIFACT_MARKERS:
            self.assertFalse(any(forbidden in path for path in artifacts))

    def test_verifier_generator_and_local_migration_binding_are_hash_bound(
        self,
    ) -> None:
        generated = generate_phase8b_package_manifest.generate()
        artifacts = generated["artifacts"]
        required = {
            "tools/governed_memory_install/package_v3.py",
            "tools/governed_memory_install/secure_file.py",
            "tools/governed_memory_install/authority_v2.py",
            "tools/governed_memory_validation/generate_phase8b_package_manifest.py",
            "tools/governed_memory_validation/verify_store_migration_manifest.py",
            "ops/governed_memory/installation/phase8b/postgres/migration_bindings.json",
        }
        self.assertTrue(required.issubset(artifacts))
        for historical in (
            "governed-memory-migrations/0001_foundation/package.json",
            "governed-memory-migrations/0003_owner_claim_detail/package.json",
            "governed-memory-migrations/0004_pilot_marker/package.json",
        ):
            self.assertNotIn(historical, artifacts)

    def test_phase8a_historical_package_is_not_rewritten(self) -> None:
        old_manifest = ROOT / "ops/governed_memory/installation/package_manifest.json"
        old_verifier = ROOT / "tools/governed_memory_install/inactive_installation.py"
        self.assertTrue(old_manifest.is_file())
        self.assertTrue(old_verifier.is_file())
        self.assertNotIn(
            str(old_manifest.relative_to(ROOT)),
            package_v3.EXPECTED_ARTIFACTS,
        )
        self.assertNotIn(
            str(old_verifier.relative_to(ROOT)),
            package_v3.EXPECTED_ARTIFACTS,
        )
        self.assertNotIn(
            "tools/governed_memory_install/authority.py",
            package_v3.EXPECTED_ARTIFACTS,
        )
        self.assertIn(
            "tools/governed_memory_install/authority_v2.py",
            package_v3.EXPECTED_ARTIFACTS,
        )

    def test_manifest_rejects_extra_forbidden_or_hash_tampering(self) -> None:
        manifest = generate_phase8b_package_manifest.generate()
        cases = []
        extra = copy.deepcopy(manifest)
        extra["artifacts"][
            "governed-memory-migrations/0002_conversation_bridge/forward.pgsql"
        ] = "0" * 64
        cases.append(extra)
        historical = copy.deepcopy(manifest)
        historical["artifacts"][
            "governed-memory-migrations/0001_foundation/package.json"
        ] = "0" * 64
        cases.append(historical)
        bad_hash = copy.deepcopy(manifest)
        key = next(iter(bad_hash["artifacts"]))
        bad_hash["artifacts"][key] = "0" * 64
        cases.append(bad_hash)
        for candidate in cases:
            with self.subTest(candidate=len(candidate["artifacts"])):
                with self.assertRaises(package_v3.PackageV3Error):
                    package_v3._verify_manifest(candidate)

    def test_contract_and_plan_are_exact_closed_and_controller_bound(self) -> None:
        contract = json.loads(package_v3.CONTRACT.read_text(encoding="utf-8"))
        package_v3._verify_contract(contract)
        contract["scope"]["current_phase_installs_or_activates"] = True
        with self.assertRaisesRegex(
            package_v3.PackageV3Error,
            "contract_semantics_invalid",
        ):
            package_v3._verify_contract(contract)

        contract = json.loads(package_v3.CONTRACT.read_text(encoding="utf-8"))
        contract["scope"]["unexpected"] = False
        with self.assertRaisesRegex(
            package_v3.PackageV3Error,
            "contract_semantics_invalid",
        ):
            package_v3._verify_contract(contract)

        plan = json.loads(package_v3.PLAN.read_text(encoding="utf-8"))
        package_v3._verify_plan(plan)
        model_hash = package_v3._verify_controller_binding(plan, controller_v2)
        self.assertEqual(model_hash, package_v3.EXPECTED_CONTROLLER_MODEL_SHA256)
        plan["live_execution"]["install_command_exposed"] = True
        with self.assertRaisesRegex(
            package_v3.PackageV3Error,
            "plan_semantics_invalid",
        ):
            package_v3._verify_plan(plan)

        plan = json.loads(package_v3.PLAN.read_text(encoding="utf-8"))
        plan["install_steps"][0]["effect"] = "different_but_well_formed_effect"
        with self.assertRaisesRegex(
            package_v3.PackageV3Error,
            "controller_plan_binding_invalid",
        ):
            package_v3._verify_controller_binding(plan, controller_v2)

    def test_verified_controller_loader_is_dependency_closed(self) -> None:
        controller = package_v3._load_verified_controller_module(
            package_v3.CONTROLLER_MODEL_RELATIVE,
            expected_sha256=package_v3.artifact_sha256(
                package_v3.CONTROLLER_MODEL_RELATIVE
            ),
        )
        projection = controller.plan_install_steps_projection(
            controller.STORES_ONLY_PLAN
        )
        self.assertEqual(len(projection), 20)
        self.assertEqual(
            tuple(step["id"] for step in projection),
            package_v3.EXPECTED_CONTROLLER_STEP_IDS,
        )
        self.assertRegex(
            controller.validate_plan(controller.STORES_ONLY_PLAN),
            r"[0-9a-f]{64}\Z",
        )
        with self.assertRaisesRegex(
            controller.ExecutionLockError,
            "verifier_lock_surface_unavailable",
        ):
            controller.validate_held_execution_lock(object())

    def test_repository_reader_rejects_symlink_file_and_directory_components(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "safe"
            safe.mkdir()
            member = safe / "member"
            member.write_bytes(b"member")
            (root / "directory-link").symlink_to(safe, target_is_directory=True)
            (safe / "file-link").symlink_to(member)
            with patch.object(package_v3, "ROOT", root):
                self.assertEqual(
                    package_v3.artifact_sha256("safe/member"),
                    "e31ab643c44f7a0ec824b59d1194d60dac334200d845e61d2d289daa0f087ea4",
                )
                for relative in (
                    "directory-link/member",
                    "safe/file-link",
                ):
                    with self.subTest(relative=relative):
                        with self.assertRaisesRegex(
                            package_v3.PackageV3Error,
                            "artifact_path_invalid",
                        ):
                            package_v3.artifact_sha256(relative)

    def test_migration_verifier_binds_local_descriptor_and_excludes_history(
        self,
    ) -> None:
        receipt = verify_store_migration_manifest.verify()
        self.assertEqual(
            receipt["schema_version"],
            "governed-memory-phase8b-store-migration-verification-v2",
        )
        self.assertEqual(receipt["file_count"], 10)
        self.assertEqual(receipt["historical_package_descriptor_count"], 0)
        self.assertEqual(
            receipt["migration_bindings_canonical_sha256"],
            verify_store_migration_manifest.EXPECTED_BINDINGS_CANONICAL_SHA256,
        )
        bindings = verify_store_migration_manifest._load(
            verify_store_migration_manifest.BINDINGS_RELATIVE
        )
        tampered_observed = dict(receipt["artifact_sha256"])
        tampered_observed["0001_foundation/package.json"] = "0" * 64
        with self.assertRaisesRegex(
            verify_store_migration_manifest.StoreMigrationManifestError,
            "historical_package_descriptor",
        ):
            verify_store_migration_manifest._verify_bindings(
                bindings,
                tampered_observed,
            )

    def test_package_rejects_nested_migration_receipt_drift(self) -> None:
        observed = generate_phase8b_package_manifest.generate()["artifacts"]
        receipt = verify_store_migration_manifest.verify()
        accepted = package_v3._verify_migration_binding(
            observed,
            SimpleNamespace(verify=lambda: copy.deepcopy(receipt)),
        )
        self.assertEqual(
            accepted["manifest_sha256"],
            observed[package_v3.MIGRATION_MANIFEST_RELATIVE],
        )

        bad_manifest = copy.deepcopy(receipt)
        bad_manifest["manifest_sha256"] = "0" * 64
        bad_child = copy.deepcopy(receipt)
        child = next(iter(bad_child["artifact_sha256"]))
        bad_child["artifact_sha256"][child] = "0" * 64
        extra = copy.deepcopy(receipt)
        extra["unexpected"] = False
        for drifted in (bad_manifest, bad_child, extra):
            with self.subTest(drifted=set(drifted)):
                with self.assertRaises(package_v3.PackageV3Error):
                    package_v3._verify_migration_binding(
                        observed,
                        SimpleNamespace(verify=lambda value=drifted: value),
                    )

    def test_migration_reader_rejects_symlink_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "safe"
            safe.mkdir()
            (safe / "member").write_bytes(b"member")
            (root / "link").symlink_to(safe, target_is_directory=True)
            with (
                patch.object(verify_store_migration_manifest, "MIGRATIONS", root),
                self.assertRaisesRegex(
                    verify_store_migration_manifest.StoreMigrationManifestError,
                    "manifest_path_invalid",
                ),
            ):
                verify_store_migration_manifest._read_checked("link/member")

    def test_migration_rejects_unapproved_paths_before_reading_them(self) -> None:
        manifest_path = ROOT / verify_store_migration_manifest.MANIFEST_RELATIVE
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append(
            {"path": "unapproved/repository/member", "sha256": "0" * 64}
        )
        raw = json.dumps(manifest).encode("ascii")

        def controlled_read(relative: str) -> bytes:
            if relative == verify_store_migration_manifest.MANIFEST_RELATIVE:
                return raw
            self.fail("unapproved manifest was used as a repository read list")

        with (
            patch.object(
                verify_store_migration_manifest,
                "_read_checked",
                side_effect=controlled_read,
            ),
            self.assertRaisesRegex(
                verify_store_migration_manifest.StoreMigrationManifestError,
                "manifest_file_set_invalid",
            ),
        ):
            verify_store_migration_manifest.verify()

    def test_phase8b_preflight_is_fail_closed_without_numeric_quit(self) -> None:
        phase8b = (
            ROOT
            / "ops/governed_memory/installation/phase8b/postgres/roles_preflight.pgsql"
        ).read_text(encoding="utf-8")
        self.assertIn("\\set ON_ERROR_STOP on", phase8b)
        self.assertIn("ERRCODE = '22023'", phase8b)
        self.assertNotIn("\\quit", phase8b)


if __name__ == "__main__":
    unittest.main()
