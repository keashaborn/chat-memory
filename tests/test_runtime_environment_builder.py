from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.build_runtime_environment import (
    RuntimeBuildContractError,
    RuntimeBuildExecutionError,
    build_runtime_archive,
    build_pip_install_command,
    validate_repository,
    validate_runtime_output_root,
    validate_wheelhouse,
)


class RuntimeEnvironmentBuilderTests(unittest.TestCase):
    def _wheelhouse_fixture(self) -> tuple[Path, Path, Path, Path, tempfile.TemporaryDirectory[str]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        wheelhouse = root / "wheelhouse"
        wheelhouse.mkdir(mode=0o700)
        wheel = wheelhouse / "example-1.0-py3-none-any.whl"
        wheel.write_bytes(b"wheel-bytes")
        os.chmod(wheel, 0o600)
        requirements = root / "requirements-ci.txt"
        requirements.write_text("example==1.0\n", encoding="utf-8")
        lock = root / "requirements-runtime.lock"
        lock.write_text(
            "example==1.0 \\\n"
            f"    --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n",
            encoding="utf-8",
        )
        manifest = root / "manifest.json"
        document = {
            "schema_version": "seebx-runtime-wheelhouse-manifest-v1",
            "python": {
                "implementation": "CPython",
                "major": 3,
                "minor": 12,
                "architecture": "x86_64",
                "platform": "linux",
            },
            "source_requirements_sha256": hashlib.sha256(
                requirements.read_bytes()
            ).hexdigest(),
            "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
            "wheel_count": 1,
            "wheels": [
                {
                    "name": "example",
                    "normalized_name": "example",
                    "version": "1.0",
                    "filename": wheel.name,
                    "bytes": wheel.stat().st_size,
                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                }
            ],
        }
        manifest.write_text(json.dumps(document), encoding="utf-8")
        return manifest, lock, requirements, wheelhouse, temporary

    def test_hash_bound_wheelhouse_accepts_exact_file_set(self) -> None:
        manifest, lock, requirements, wheelhouse, _ = self._wheelhouse_fixture()
        result = validate_wheelhouse(manifest, lock, requirements, wheelhouse)
        self.assertEqual(result["wheel_count"], 1)
        self.assertEqual(result["lock_sha256"], hashlib.sha256(lock.read_bytes()).hexdigest())

    def test_wheelhouse_rejects_hash_drift_and_extra_files(self) -> None:
        manifest, lock, requirements, wheelhouse, _ = self._wheelhouse_fixture()
        wheel = next(wheelhouse.glob("*.whl"))
        wheel.write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeBuildContractError, "wheel_artifact_mismatch"):
            validate_wheelhouse(manifest, lock, requirements, wheelhouse)
        wheel.write_bytes(b"wheel-bytes")
        extra = wheelhouse / "extra.whl"
        extra.write_bytes(b"extra")
        os.chmod(extra, 0o600)
        with self.assertRaisesRegex(RuntimeBuildContractError, "wheelhouse_file_set_mismatch"):
            validate_wheelhouse(manifest, lock, requirements, wheelhouse)
        extra.unlink()
        (wheelhouse / "unexpected-directory").mkdir()
        with self.assertRaisesRegex(RuntimeBuildContractError, "wheelhouse_file_set_mismatch"):
            validate_wheelhouse(manifest, lock, requirements, wheelhouse)

    def test_offline_install_command_is_hash_required(self) -> None:
        command = build_pip_install_command(
            Path("/runtime/venv"),
            Path("/wheelhouse"),
            Path("/repo/requirements-runtime.lock"),
        )
        self.assertIn("--isolated", command)
        self.assertIn("--no-index", command)
        self.assertIn("--no-cache-dir", command)
        self.assertIn("--only-binary=:all:", command)
        self.assertIn("--require-hashes", command)
        self.assertNotIn("https://pypi.org/simple", command)

    def test_runtime_archive_is_deterministic_and_binds_internal_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / "venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "lib").mkdir()
            for name in ("python", "uvicorn"):
                path = venv / "bin" / name
                path.write_bytes((name + "\n").encode())
                path.chmod(0o555)
            (venv / "lib" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            (venv / "lib" / "module.py").chmod(0o444)
            (venv / "lib64").symlink_to("lib")
            first = build_runtime_archive(venv, root / "runtime-one.tar")
            second = build_runtime_archive(venv, root / "runtime-two.tar")
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(first["tree_manifest_sha256"], second["tree_manifest_sha256"])
            self.assertEqual(first["file_count"], 3)
            self.assertEqual(first["directory_count"], 3)
            self.assertEqual(first["symlink_count"], 1)

    def test_runtime_archive_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venv = root / "venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").write_bytes(b"python")
            (venv / "escape").symlink_to("../../outside")
            with self.assertRaisesRegex(
                RuntimeBuildExecutionError,
                "runtime_symlink_outside_environment",
            ):
                build_runtime_archive(venv, root / "runtime.tar")

    def test_output_root_is_root_owned_non_writable_and_traversable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "runtimes"
            output_root.mkdir(mode=0o755)
            self.assertEqual(validate_runtime_output_root(output_root), output_root.resolve())
            output_root.chmod(0o700)
            with self.assertRaisesRegex(
                RuntimeBuildContractError,
                "runtime_output_root_permissions_invalid",
            ):
                validate_runtime_output_root(output_root)
            output_root.chmod(0o775)
            with self.assertRaisesRegex(
                RuntimeBuildContractError,
                "runtime_output_root_permissions_invalid",
            ):
                validate_runtime_output_root(output_root)

    def test_repository_must_be_clean_fast_forward(self) -> None:
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(["git", "-C", repository, "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", repository, "config", "user.name", "Test"], check=True)
            (repository / "file.txt").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "-C", repository, "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", repository, "commit", "-qm", "one"], check=True)
            production = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"], text=True
            ).strip()
            (repository / "file.txt").write_text("two\n", encoding="utf-8")
            subprocess.run(["git", "-C", repository, "commit", "-qam", "two"], check=True)
            result = validate_repository(repository, production)
            self.assertEqual(result["ahead_count"], 1)
            self.assertTrue(result["clean"])
            (repository / "file.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeBuildContractError, "repository_not_clean"):
                validate_repository(repository, production)


if __name__ == "__main__":
    unittest.main()
