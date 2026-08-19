from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.verify_atomic_release_package import ReleasePackageError, main, verify_release_package


SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "ops/releases/seebx_atomic_release_package_v1.schema.json"
)
ARTIFACT_NAMES = (
    "backend_source_bundle",
    "frontend_source_bundle",
    "frontend_build_archive",
    "frontend_build_manifest",
    "runtime_receipt",
    "migration_package",
    "migration_receipt",
    "recovery_receipt",
    "database_backup",
    "backend_systemd_snapshot",
    "backend_environment_manifest",
    "frontend_systemd_snapshot",
    "frontend_environment_manifest",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AtomicReleasePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (
            self.backend_production,
            self.backend_candidate,
            self.backend_production_tree,
            self.backend_tree,
            self.backend_bundle,
        ) = self._git_bundle("backend")
        (
            self.frontend_production,
            self.frontend_candidate,
            self.frontend_production_tree,
            self.frontend_tree,
            self.frontend_bundle,
        ) = self._git_bundle("frontend")
        self.backup = self.root / "database.dump"
        self.backup.write_bytes(b"database-backup")
        backup_sha = digest(self.backup)

        self.recovery = self.root / "recovery.json"
        self.recovery.write_text(
            json.dumps(
                {
                    "schema_version": "seebx-legacy-memory-recovery-receipt-v1",
                    "status": "pass",
                    "backup": {"sha256": backup_sha},
                    "restore": {"verified": True, "disposable_database_dropped": True},
                }
            ),
            encoding="utf-8",
        )
        recovery_sha = digest(self.recovery)

        self.migration = self.root / "migration.json"
        self.migration_package = self.root / "migration-package.json"
        self.migration_package.write_text(
            json.dumps(
                {
                    "schema_version": "seebx-zep-chat-history-migration-package-v1",
                    "status": "candidate_not_applied",
                    "execution": {
                        "production_database_mutated": False,
                        "zep_called": False,
                        "temporary_database_required": True,
                        "forward_and_rollback_required": True,
                        "exact_baseline_restoration_required": True,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.migration.write_text(
            json.dumps(
                {
                    "schema_version": "seebx-zep-chat-history-migration-receipt-v1",
                    "status": "pass",
                    "temporary_database_dropped": True,
                    "bindings": {"package_sha256": digest(self.migration_package)},
                    "forward": {"outbox_rows": 0},
                    "rollback": {"exact_baseline_restored": True},
                    "source": {
                        "backup_sha256": backup_sha,
                        "recovery_receipt_sha256": recovery_sha,
                    },
                }
            ),
            encoding="utf-8",
        )

        self.runtime = self.root / "runtime.json"
        self.runtime.write_text(
            json.dumps(
                {
                    "schema_version": "seebx-runtime-environment-receipt-v1",
                    "status": "pass",
                    "activated": False,
                    "runtime_lock_sha256": "7" * 64,
                    "repository": {
                        "commit": self.backend_candidate,
                        "tree": self.backend_tree,
                    },
                    "runtime": {
                        "dependency_preflight": {
                            "dependencies": {"verified_count": 12, "expected_count": 12}
                        },
                        "import_count": 12,
                    },
                    "wheelhouse": {
                        "lock_sha256": "7" * 64,
                        "manifest_sha256": "8" * 64,
                    },
                }
            ),
            encoding="utf-8",
        )

        self.frontend_build_archive = self.root / "frontend-build.tar"
        self.frontend_build_archive.write_bytes(b"frontend-build")
        self.frontend_build_manifest = self.root / "frontend-build-manifest.json"
        self.frontend_build_manifest.write_text(
            json.dumps(
                {
                    "schema_version": "verbalsage-build-artifact-manifest-v1",
                    "status": "pass",
                    "source_commit": self.frontend_candidate,
                    "source_tree": self.frontend_tree,
                    "archive_sha256": digest(self.frontend_build_archive),
                    "production_environment_preflight": "pass",
                    "artifact_preflight": "pass",
                    "static_page_count": 70,
                }
            ),
            encoding="utf-8",
        )

        artifact_paths = {
            "backend_source_bundle": self.backend_bundle,
            "frontend_source_bundle": self.frontend_bundle,
            "frontend_build_archive": self.frontend_build_archive,
            "frontend_build_manifest": self.frontend_build_manifest,
            "runtime_receipt": self.runtime,
            "migration_package": self.migration_package,
            "migration_receipt": self.migration,
            "recovery_receipt": self.recovery,
            "database_backup": self.backup,
        }
        for name in ARTIFACT_NAMES:
            if name not in artifact_paths:
                path = self.root / (name + ".artifact")
                path.write_bytes((name + "\n").encode())
                artifact_paths[name] = path
        self.package = {
            "schema_version": "seebx-atomic-release-package-v1",
            "package_id": "release-test-1",
            "status": "candidate_not_activated",
            "authority": {
                "production_activation_authorized": False,
                "authorization_id": None,
            },
            "sources": {
                "backend": self._source(
                    "seebx",
                    self.backend_production,
                    self.backend_production_tree,
                    self.backend_candidate,
                    self.backend_tree,
                ),
                "frontend": self._source(
                    "verbalsage",
                    self.frontend_production,
                    self.frontend_production_tree,
                    self.frontend_candidate,
                    self.frontend_tree,
                ),
            },
            "artifacts": {
                name: {"path": path.name, "sha256": digest(path)}
                for name, path in artifact_paths.items()
            },
            "runtime_binding": {
                "status": "pass",
                "activated": False,
                "repository_commit": self.backend_candidate,
                "repository_tree": self.backend_tree,
                "runtime_lock_sha256": "7" * 64,
                "wheelhouse_manifest_sha256": "8" * 64,
                "dependency_count": 12,
                "import_count": 12,
            },
            "migration_binding": {
                "status": "pass",
                "production_database_mutated": False,
                "zep_called": False,
                "temporary_database_dropped": True,
                "exact_baseline_restored": True,
                "forward_outbox_rows": 0,
                "backup_sha256": backup_sha,
                "recovery_receipt_sha256": recovery_sha,
            },
            "recovery_binding": {
                "status": "pass",
                "restore_verified": True,
                "temporary_database_dropped": True,
                "backup_sha256": backup_sha,
            },
            "rollback": {
                "backend_source_bound": True,
                "frontend_source_bound": True,
                "frontend_build_bound": True,
                "systemd_bound": True,
                "environment_manifests_bound": True,
                "database_backup_bound": True,
                "service_health_baseline_bound": True,
                "timer_state_bound": True,
                "reverse_order_defined": True,
                "empty_outbox_before_database_rollback": True,
            },
            "quiescence": {
                "required": True,
                "backend_write_boundary": "stop_service",
                "frontend_write_boundary": "stop_service",
                "approved": False,
            },
        }
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def _git_bundle(self, name: str) -> tuple[str, str, str, str, Path]:
        repository = self.root / (name + "-repository")
        subprocess.run(["git", "init", "-q", repository], check=True)
        subprocess.run(
            ["git", "-C", repository, "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repository, "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repository, "checkout", "-qb", "codex/release-test"],
            check=True,
        )
        tracked = repository / "tracked.txt"
        tracked.write_text(name + "-production\n", encoding="utf-8")
        subprocess.run(["git", "-C", repository, "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", repository, "commit", "-qm", "production"],
            check=True,
        )
        production = subprocess.check_output(
            ["git", "-C", repository, "rev-parse", "HEAD"],
            text=True,
        ).strip()
        production_tree = subprocess.check_output(
            ["git", "-C", repository, "rev-parse", "HEAD^{tree}"],
            text=True,
        ).strip()
        tracked.write_text(name + "-candidate\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", repository, "commit", "-qam", "candidate"],
            check=True,
        )
        candidate = subprocess.check_output(
            ["git", "-C", repository, "rev-parse", "HEAD"],
            text=True,
        ).strip()
        candidate_tree = subprocess.check_output(
            ["git", "-C", repository, "rev-parse", "HEAD^{tree}"],
            text=True,
        ).strip()
        bundle = self.root / (name + ".bundle")
        subprocess.run(
            [
                "git",
                "-C",
                repository,
                "bundle",
                "create",
                bundle,
                "refs/heads/codex/release-test",
            ],
            check=True,
        )
        return production, candidate, production_tree, candidate_tree, bundle

    def _source(
        self,
        server: str,
        production: str,
        production_tree: str,
        candidate: str,
        tree: str,
    ) -> dict[str, object]:
        return {
            "server": server,
            "production_commit": production,
            "production_tree": production_tree,
            "candidate_commit": candidate,
            "candidate_tree": tree,
            "remote_commit": candidate,
            "branch_ref": "refs/heads/codex/release-test",
            "ahead_count": 1,
            "clean": True,
        }

    def verify(self) -> dict[str, object]:
        return verify_release_package(self.package, artifact_root=self.root, schema=self.schema)

    def test_exact_package_passes_without_granting_activation(self) -> None:
        result = self.verify()
        self.assertTrue(result["package_integrity_ready"])
        self.assertFalse(result["production_activation_authorized"])
        self.assertEqual(result["artifact_count"], 13)
        self.assertEqual(result["backend_commit"], self.backend_candidate)
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_artifact_hash_drift_fails_closed(self) -> None:
        self.backup.write_bytes(b"changed")
        with self.assertRaisesRegex(ReleasePackageError, "database_backup_sha256_mismatch"):
            self.verify()

    def test_git_bundle_must_contain_the_declared_history(self) -> None:
        self.backend_bundle.write_bytes(self.frontend_bundle.read_bytes())
        self.package["artifacts"]["backend_source_bundle"]["sha256"] = digest(
            self.backend_bundle
        )
        with self.assertRaisesRegex(ReleasePackageError, "seebx_bundle_candidate_mismatch"):
            self.verify()

    def test_frontend_build_manifest_must_bind_the_candidate_tree(self) -> None:
        document = json.loads(self.frontend_build_manifest.read_text(encoding="utf-8"))
        document["source_tree"] = "a" * 40
        self.frontend_build_manifest.write_text(json.dumps(document), encoding="utf-8")
        self.package["artifacts"]["frontend_build_manifest"]["sha256"] = digest(
            self.frontend_build_manifest
        )
        with self.assertRaisesRegex(
            ReleasePackageError,
            "frontend_build_source_binding_mismatch",
        ):
            self.verify()

    def test_runtime_cross_binding_mismatch_fails_closed(self) -> None:
        self.package["runtime_binding"]["repository_commit"] = "a" * 40
        with self.assertRaisesRegex(ReleasePackageError, "declared_runtime_commit_mismatch"):
            self.verify()

    def test_unsafe_or_duplicate_artifact_paths_fail_closed(self) -> None:
        self.package["artifacts"]["backend_source_bundle"]["path"] = "../escape"
        with self.assertRaisesRegex(ReleasePackageError, "package_schema_invalid"):
            self.verify()
        self.package["artifacts"]["backend_source_bundle"] = dict(
            self.package["artifacts"]["frontend_source_bundle"]
        )
        with self.assertRaisesRegex(ReleasePackageError, "artifact_paths_not_unique"):
            self.verify()

    def test_package_cannot_self_authorize_or_skip_rollback(self) -> None:
        self.package["authority"]["production_activation_authorized"] = True
        with self.assertRaisesRegex(ReleasePackageError, "package_schema_invalid"):
            self.verify()
        self.package["authority"]["production_activation_authorized"] = False
        self.package["rollback"]["reverse_order_defined"] = False
        with self.assertRaisesRegex(ReleasePackageError, "package_schema_invalid"):
            self.verify()

    def test_cli_redacts_unexpected_artifact_content(self) -> None:
        package_path = self.root / "package.json"
        package_path.write_text(json.dumps(self.package), encoding="utf-8")
        self.runtime.write_text("secret-runtime-content", encoding="utf-8")
        self.package["artifacts"]["runtime_receipt"]["sha256"] = digest(self.runtime)
        package_path.write_text(json.dumps(self.package), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "--package",
                    str(package_path),
                    "--artifact-root",
                    str(self.root),
                    "--schema",
                    str(SCHEMA_PATH),
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertNotIn("secret-runtime-content", output.getvalue())


if __name__ == "__main__":
    unittest.main()
