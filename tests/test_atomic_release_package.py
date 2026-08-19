from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.build_runtime_environment import build_runtime_archive
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
    "runtime_archive",
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

        self.runtime_archive = self.root / "runtime-environment.tar"
        runtime_archive = self._runtime_archive()
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
                    "runtime_archive": runtime_archive,
                    "wheelhouse": {
                        "lock_sha256": "7" * 64,
                        "manifest_sha256": "8" * 64,
                    },
                }
            ),
            encoding="utf-8",
        )

        self.frontend_build_archive = self.root / "frontend-build.tar"
        frontend_archive = self._frontend_archive()
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
                    "archive_format": "tar",
                    "entrypoint": "server.js",
                    "contains_environment_files": False,
                    "runtime_environment_required": True,
                    "file_count": frontend_archive["file_count"],
                    "archive_unpacked_bytes": frontend_archive["unpacked_bytes"],
                    "static_page_count": frontend_archive["route_artifact_count"],
                    "static_asset_count": frontend_archive["static_asset_count"],
                }
            ),
            encoding="utf-8",
        )

        artifact_paths = {
            "backend_source_bundle": self.backend_bundle,
            "frontend_source_bundle": self.frontend_bundle,
            "frontend_build_archive": self.frontend_build_archive,
            "frontend_build_manifest": self.frontend_build_manifest,
            "runtime_archive": self.runtime_archive,
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

    def _frontend_archive(self) -> dict[str, int]:
        release = self.root / "frontend-release"
        files = {
            "server.js": b"server\n",
            ".next/server/app/page.html": b"<html></html>\n",
            ".next/static/chunks/app.js": b"static\n",
            "public/health.txt": b"ok\n",
        }
        for relative, content in files.items():
            path = release / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        with tarfile.open(self.frontend_build_archive, mode="w") as archive:
            for path in sorted(release.rglob("*")):
                archive.add(path, arcname=path.relative_to(release), recursive=False)
        return {
            "file_count": len(files),
            "unpacked_bytes": sum(len(content) for content in files.values()),
            "route_artifact_count": 1,
            "static_asset_count": 1,
        }

    def _runtime_archive(self) -> dict[str, object]:
        runtime = self.root / "runtime-tree"
        install_path = Path(
            "/opt/lifeswitch/runtimes"
        ) / self.backend_candidate / "venv"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "lib").mkdir()
        python = runtime / "bin" / "python"
        python.write_bytes(b"python\n")
        python.chmod(0o555)
        uvicorn = runtime / "bin" / "uvicorn"
        uvicorn.write_bytes(
            ("#!" + install_path.as_posix() + "/bin/python\nuvicorn\n").encode()
        )
        uvicorn.chmod(0o555)
        module = runtime / "lib" / "module.py"
        module.write_text("VALUE = 1\n", encoding="utf-8")
        module.chmod(0o444)
        (runtime / "lib64").symlink_to("lib")
        return build_runtime_archive(runtime, self.runtime_archive, install_path)

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

    def rebind_frontend_archive(self) -> None:
        archive_sha = digest(self.frontend_build_archive)
        manifest = json.loads(self.frontend_build_manifest.read_text(encoding="utf-8"))
        manifest["archive_sha256"] = archive_sha
        self.frontend_build_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.package["artifacts"]["frontend_build_archive"]["sha256"] = archive_sha
        self.package["artifacts"]["frontend_build_manifest"]["sha256"] = digest(
            self.frontend_build_manifest
        )

    def test_exact_package_passes_without_granting_activation(self) -> None:
        result = self.verify()
        self.assertTrue(result["package_integrity_ready"])
        self.assertFalse(result["production_activation_authorized"])
        self.assertEqual(result["artifact_count"], 14)
        self.assertEqual(result["backend_commit"], self.backend_candidate)
        self.assertEqual(result["runtime_archive_sha256"], digest(self.runtime_archive))
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

    def test_frontend_archive_cannot_contain_environment_files(self) -> None:
        forbidden = self.root / ".env.production"
        forbidden.write_text("SECRET=value\n", encoding="utf-8")
        with tarfile.open(self.frontend_build_archive, mode="a") as archive:
            archive.add(forbidden, arcname=".env.production")
        self.rebind_frontend_archive()
        with self.assertRaisesRegex(
            ReleasePackageError,
            "frontend_archive_environment_file_forbidden",
        ):
            self.verify()

    def test_frontend_archive_rejects_path_traversal(self) -> None:
        payload = b"escape"
        with tarfile.open(self.frontend_build_archive, mode="w") as archive:
            member = tarfile.TarInfo("../escape")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        self.rebind_frontend_archive()
        with self.assertRaisesRegex(
            ReleasePackageError,
            "frontend_archive_path_invalid",
        ):
            self.verify()

    def test_runtime_cross_binding_mismatch_fails_closed(self) -> None:
        self.package["runtime_binding"]["repository_commit"] = "a" * 40
        with self.assertRaisesRegex(ReleasePackageError, "declared_runtime_commit_mismatch"):
            self.verify()

    def test_runtime_archive_receipt_inventory_mismatch_fails_closed(self) -> None:
        document = json.loads(self.runtime.read_text(encoding="utf-8"))
        document["runtime_archive"]["file_count"] += 1
        self.runtime.write_text(json.dumps(document), encoding="utf-8")
        self.package["artifacts"]["runtime_receipt"]["sha256"] = digest(self.runtime)
        with self.assertRaisesRegex(
            ReleasePackageError,
            "runtime_archive_receipt_binding_mismatch",
        ):
            self.verify()

    def test_runtime_archive_install_path_is_bound_to_backend_commit(self) -> None:
        document = json.loads(self.runtime.read_text(encoding="utf-8"))
        document["runtime_archive"]["install_path"] = (
            "/opt/lifeswitch/runtimes/" + "a" * 40 + "/venv"
        )
        self.runtime.write_text(json.dumps(document), encoding="utf-8")
        self.package["artifacts"]["runtime_receipt"]["sha256"] = digest(self.runtime)
        with self.assertRaisesRegex(
            ReleasePackageError,
            "runtime_archive_install_path_mismatch",
        ):
            self.verify()

    def test_runtime_archive_rejects_environment_files(self) -> None:
        payload = b"SECRET=value\n"
        self.runtime_archive.chmod(0o600)
        with tarfile.open(self.runtime_archive, mode="a") as archive:
            member = tarfile.TarInfo("venv/.env.production")
            member.mode = 0o444
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            member.mtime = 0
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        self.package["artifacts"]["runtime_archive"]["sha256"] = digest(
            self.runtime_archive
        )
        with self.assertRaisesRegex(
            ReleasePackageError,
            "runtime_archive_sensitive_path_forbidden",
        ):
            self.verify()

    def test_runtime_archive_rejects_symlink_escape(self) -> None:
        self.runtime_archive.chmod(0o600)
        with tarfile.open(self.runtime_archive, mode="a") as archive:
            member = tarfile.TarInfo("venv/lib/escape")
            member.type = tarfile.SYMTYPE
            member.mode = 0o777
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            member.mtime = 0
            member.linkname = "../../../outside"
            archive.addfile(member)
        self.package["artifacts"]["runtime_archive"]["sha256"] = digest(
            self.runtime_archive
        )
        with self.assertRaisesRegex(
            ReleasePackageError,
            "runtime_archive_symlink_escape",
        ):
            self.verify()

    def test_runtime_archive_rejects_bytecode(self) -> None:
        payload = b"compiled-bytecode"
        self.runtime_archive.chmod(0o600)
        with tarfile.open(self.runtime_archive, mode="a") as archive:
            member = tarfile.TarInfo("venv/lib/__pycache__/module.cpython-312.pyc")
            member.mode = 0o444
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            member.mtime = 0
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        self.package["artifacts"]["runtime_archive"]["sha256"] = digest(
            self.runtime_archive
        )
        with self.assertRaisesRegex(
            ReleasePackageError,
            "runtime_archive_bytecode_forbidden",
        ):
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
