from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from tools.governed_memory_validation import staged_prefix_disposition as subject


FAILED_PACKAGE = "1" * 64
FAILED_RUNTIME = "2" * 64
CORRECTED_PACKAGE = "3" * 64
CORRECTED_RUNTIME = "4" * 64
TAG_COMMIT = "a" * 40
TAG_TREE = "b" * 40
DISPOSITION_ID = "phase9-v6-staged-to-v7-000003"


class ClosedRunner:
    def __init__(
        self,
        paths: subject.StagedPrefixDispositionPaths,
        *,
        tag_commit: str = TAG_COMMIT,
        docker_container_output: bytes = b"",
        listener_output: bytes = b"",
        systemd_output: bytes = b"not-found\n",
    ) -> None:
        self.paths = paths
        self.tag_commit = tag_commit
        self.docker_container_output = docker_container_output
        self.listener_output = listener_output
        self.systemd_output = systemd_output
        self.commands: list[tuple[str, ...]] = []

    def run(self, argv: object) -> subprocess.CompletedProcess[bytes]:
        command = tuple(argv)
        self.commands.append(command)
        if command == subject._git_command(
            self.paths, "rev-parse", "--show-toplevel"
        ):
            output = (str(self.paths.repository_root) + "\n").encode()
        elif command == subject._git_command(
            self.paths, "cat-file", "-t", subject.PRODUCTION_OLD_TAG_REF
        ):
            output = b"commit\n"
        elif command in {
            subject._git_command(
                self.paths,
                "rev-parse",
                "--verify",
                subject.PRODUCTION_OLD_TAG_REF,
            ),
            subject._git_command(
                self.paths,
                "rev-parse",
                "--verify",
                subject.PRODUCTION_OLD_TAG_REF + "^{commit}",
            ),
        }:
            output = (self.tag_commit + "\n").encode()
        elif command == subject._git_command(
            self.paths,
            "rev-parse",
            "--verify",
            subject.PRODUCTION_OLD_TAG_REF + "^{tree}",
        ):
            output = (TAG_TREE + "\n").encode()
        elif command == subject._DOCKER_COMMANDS["docker_container"]:
            output = self.docker_container_output
        elif command in {
            subject._DOCKER_COMMANDS["docker_network"],
            subject._DOCKER_COMMANDS["docker_volume"],
        }:
            output = b""
        elif command in {
            subject._tcp_command(port)
            for port in subject._TCP_IDENTITIES.values()
        }:
            output = self.listener_output
        elif command in {
            subject._systemd_command(service)
            for service in subject._SYSTEMD_SERVICES
        }:
            output = self.systemd_output
        else:
            raise OSError("closed test runner")
        return subprocess.CompletedProcess(command, 0, output, b"")


class StagedPrefixDispositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.uid = os.getuid()
        self.gid = os.getgid()

        self.repository = self.directory("repository")
        self.state = self.directory("state")
        self.contract_root = self.directory("state/contracts")
        self.control = self.directory("control")
        self.locks = self.directory("locks")
        self.executions = self.directory("state/executions-v2")
        self.secret_root = self.directory("secrets/9a54cf123493-000002")
        self.systemd = self.directory("systemd")
        self.wants = self.directory("systemd/wants")

        self.paths = subject.StagedPrefixDispositionPaths(
            repository_root=self.repository,
            durable_contract_path=self.contract_root / "new-contract.json",
            tombstone_path=self.control / "store_spec-v2.json",
            old_permit_path=self.state / "old-permit.json",
            old_contract_path=self.contract_root / "old-contract.json",
            old_pre_effect_receipt_path=self.control / "store_spec.json",
            staged_capsule_path=self.state / "capsule-v3.json.publishing",
            final_capsule_path=self.state / "capsule-v3.json",
            authority_state_path=self.state / "authority-state-v2.sqlite3",
            promotable_receipt_path=self.state / "promotable.json",
            promotable_receipt_staging_path=self.state / "promotable.json.publishing",
            systemd_unit_path=self.systemd / "governed-memory-stores-v2.service",
            systemd_link_path=self.wants / "governed-memory-stores-v2.service",
            executions_root=self.executions,
            store_secret_root=self.secret_root,
            corrected_capsule_path=self.state / "capsule-v4.json",
            corrected_capsule_staging_path=(
                self.state / "capsule-v4.json.publishing"
            ),
            corrected_authority_state_path=(
                self.state / "authority-state-v3.sqlite3"
            ),
            corrected_promotable_receipt_path=(
                self.state / "promotable-000003.json"
            ),
            corrected_promotable_receipt_staging_path=(
                self.state / "promotable-000003.json.publishing"
            ),
            corrected_executions_root=self.state / "executions-v3",
            corrected_secret_root=self.root / "secrets/9a54cf123493-000003",
            corrected_store_spec_path=self.control / "store_spec-v3.json",
            corrected_systemd_unit_path=(
                self.systemd / "governed-memory-stores-v3.service"
            ),
            corrected_systemd_link_path=(
                self.wants / "governed-memory-stores-v3.service"
            ),
            global_lock_path=self.locks / "execution.lock",
            live_guard_path=self.locks / "live-proof.lock",
            expected_uid=self.uid,
            expected_gid=self.gid,
        )
        self.write_evidence(self.paths.old_permit_path, b'{"permit":"old"}')
        self.write_evidence(self.paths.old_contract_path, b'{"contract":"old"}')
        self.write_evidence(
            self.paths.old_pre_effect_receipt_path, b'{"receipt":"old"}'
        )
        self.write_evidence(
            self.paths.staged_capsule_path, b'{"capsule":"staged-only"}'
        )
        contract = self.contract_document()
        raw = subject.canonical_json_bytes(contract)
        self.write_evidence(self.paths.durable_contract_path, raw)
        self.expectation = subject.ReviewedStagedPrefixExpectation(
            contract_sha256=hashlib.sha256(raw).hexdigest(),
            disposition_id=DISPOSITION_ID,
            old_tag_ref=subject.PRODUCTION_OLD_TAG_REF,
            old_tag_commit=TAG_COMMIT,
            old_tag_tree=TAG_TREE,
            failed_package_manifest_sha256=FAILED_PACKAGE,
            failed_controller_runtime_receipt_sha256=FAILED_RUNTIME,
            corrected_generation="000003",
            corrected_package_manifest_sha256=CORRECTED_PACKAGE,
            corrected_controller_runtime_receipt_sha256=CORRECTED_RUNTIME,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def directory(self, relative: str) -> Path:
        path = self.root / relative
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    @staticmethod
    def write_evidence(path: Path, raw: bytes) -> None:
        path.write_bytes(raw)
        path.chmod(0o400)

    def file_spec(self, role: str, path: Path) -> dict[str, object]:
        value = path.stat(follow_symlinks=False)
        raw = path.read_bytes()
        return {
            "role": role,
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "mode": stat.S_IMODE(value.st_mode),
            "uid": value.st_uid,
            "gid": value.st_gid,
            "nlink": value.st_nlink,
            "device": value.st_dev,
            "inode": value.st_ino,
        }

    def directory_spec(self, role: str, path: Path) -> dict[str, object]:
        value = path.stat(follow_symlinks=False)
        return {
            "role": role,
            "path": str(path),
            "mode": stat.S_IMODE(value.st_mode),
            "uid": value.st_uid,
            "gid": value.st_gid,
            "nlink": value.st_nlink,
            "device": value.st_dev,
            "inode": value.st_ino,
        }

    def contract_document(self) -> dict[str, object]:
        return {
            "schema_version": subject.CONTRACT_SCHEMA,
            "disposition_id": DISPOSITION_ID,
            "repository_contract_source": (
                subject.PRODUCTION_REPOSITORY_CONTRACT_SOURCE
            ),
            "durable_contract_path": str(self.paths.durable_contract_path),
            "tombstone_path": str(self.paths.tombstone_path),
            "failed_attempt": {
                "generation": "000002",
                "tag": {
                    "ref": subject.PRODUCTION_OLD_TAG_REF,
                    "object_type": "commit",
                    "commit": TAG_COMMIT,
                    "tree": TAG_TREE,
                },
                "package_manifest_sha256": FAILED_PACKAGE,
                "controller_runtime_receipt_sha256": FAILED_RUNTIME,
                "evidence_files": [
                    self.file_spec("old_contract", self.paths.old_contract_path),
                    self.file_spec("old_permit", self.paths.old_permit_path),
                    self.file_spec(
                        "old_pre_effect_receipt",
                        self.paths.old_pre_effect_receipt_path,
                    ),
                    self.file_spec(
                        "staged_capsule", self.paths.staged_capsule_path
                    ),
                ],
            },
            "corrected_successor": subject._corrected_successor_document(
                subject.ReviewedStagedPrefixExpectation(
                    contract_sha256="0" * 64,
                    disposition_id=DISPOSITION_ID,
                    old_tag_ref=subject.PRODUCTION_OLD_TAG_REF,
                    old_tag_commit=TAG_COMMIT,
                    old_tag_tree=TAG_TREE,
                    failed_package_manifest_sha256=FAILED_PACKAGE,
                    failed_controller_runtime_receipt_sha256=FAILED_RUNTIME,
                    corrected_generation="000003",
                    corrected_package_manifest_sha256=CORRECTED_PACKAGE,
                    corrected_controller_runtime_receipt_sha256=(
                        CORRECTED_RUNTIME
                    ),
                )
            ),
            "empty_directories": [
                self.directory_spec("executions_v2", self.executions),
                self.directory_spec("store_secret", self.secret_root),
            ],
            "absent_resources": subject._expected_absent_resources(self.paths),
        }

    def execute(
        self, runner: ClosedRunner | None = None
    ) -> dict[str, object]:
        value = subject.execute_staged_prefix_disposition(
            self.paths,
            self.expectation,
            command_runner=runner or ClosedRunner(self.paths),
        )
        return dict(value)

    def test_exact_prefix_creates_one_0400_tombstone_and_replays(self) -> None:
        evidence = {
            path: (path.read_bytes(), path.stat().st_ino)
            for path in (
                self.paths.old_contract_path,
                self.paths.old_permit_path,
                self.paths.old_pre_effect_receipt_path,
                self.paths.staged_capsule_path,
            )
        }
        first = self.execute()
        tombstone_inode = self.paths.tombstone_path.stat().st_ino
        second = self.execute()

        self.assertEqual(first, second)
        self.assertEqual(first["result"], subject.RESULT)
        self.assertEqual(first["corrected_generation"], "000003")
        self.assertEqual(
            first["corrected_package_manifest_sha256"], CORRECTED_PACKAGE
        )
        self.assertEqual(
            first["corrected_controller_runtime_receipt_sha256"],
            CORRECTED_RUNTIME,
        )
        self.assertFalse(first["deletion_performed"])
        tombstone = self.paths.tombstone_path.stat(follow_symlinks=False)
        self.assertEqual(stat.S_IMODE(tombstone.st_mode), 0o400)
        self.assertEqual(tombstone.st_uid, self.uid)
        self.assertEqual(tombstone.st_gid, self.gid)
        self.assertEqual(tombstone.st_nlink, 1)
        self.assertEqual(tombstone.st_ino, tombstone_inode)
        self.assertFalse(
            (self.control / ".store_spec-v2.json.publishing").exists()
        )
        for path, expected in evidence.items():
            self.assertEqual((path.read_bytes(), path.stat().st_ino), expected)

    def test_old_evidence_hash_mismatch_refuses_without_tombstone(self) -> None:
        self.paths.old_permit_path.chmod(0o600)
        self.paths.old_permit_path.write_bytes(b'{"permit":"substituted"}')
        self.paths.old_permit_path.chmod(0o400)
        with self.assertRaises(subject.StagedPrefixDispositionIntegrityError):
            self.execute()
        self.assertFalse(self.paths.tombstone_path.exists())

    def test_reviewed_evidence_inode_mismatch_refuses(self) -> None:
        contract = json.loads(
            self.paths.durable_contract_path.read_text(encoding="ascii")
        )
        contract["failed_attempt"]["evidence_files"][1]["inode"] += 1
        raw = subject.canonical_json_bytes(contract)
        self.paths.durable_contract_path.chmod(0o600)
        self.paths.durable_contract_path.write_bytes(raw)
        self.paths.durable_contract_path.chmod(0o400)
        expectation = replace(
            self.expectation, contract_sha256=hashlib.sha256(raw).hexdigest()
        )
        with self.assertRaisesRegex(
            subject.StagedPrefixDispositionIntegrityError,
            "staged_prefix_disposition_evidence_mismatch",
        ):
            subject.execute_staged_prefix_disposition(
                self.paths,
                expectation,
                command_runner=ClosedRunner(self.paths),
            )
        self.assertFalse(self.paths.tombstone_path.exists())

    def test_nonempty_execution_or_secret_directory_refuses(self) -> None:
        for root, name in (
            (self.executions, "execution-artifact"),
            (self.secret_root, "postgres.env"),
        ):
            with self.subTest(root=root):
                member = root / name
                member.write_bytes(b"opaque")
                member.chmod(0o400)
                with self.assertRaisesRegex(
                    subject.StagedPrefixDispositionHostStateError,
                    "staged_prefix_disposition_directory_not_empty",
                ):
                    self.execute()
                self.assertEqual(member.read_bytes(), b"opaque")
                self.assertFalse(self.paths.tombstone_path.exists())
                member.unlink()

    def test_final_capsule_or_docker_resource_presence_refuses(self) -> None:
        self.write_evidence(self.paths.final_capsule_path, b"unexpected-final")
        with self.assertRaisesRegex(
            subject.StagedPrefixDispositionHostStateError,
            "staged_prefix_disposition_host_effect_present",
        ):
            self.execute()
        self.assertFalse(self.paths.tombstone_path.exists())
        self.paths.final_capsule_path.unlink()

        container = subject._DOCKER_IDENTITIES["docker_container"][0]
        runner = ClosedRunner(
            self.paths, docker_container_output=(container + "\n").encode()
        )
        with self.assertRaisesRegex(
            subject.StagedPrefixDispositionHostStateError,
            "staged_prefix_disposition_host_effect_present",
        ):
            self.execute(runner)
        self.assertFalse(self.paths.tombstone_path.exists())

    def test_historical_lightweight_tag_mismatch_refuses(self) -> None:
        with self.assertRaisesRegex(
            subject.StagedPrefixDispositionIntegrityError,
            "staged_prefix_disposition_git_tag_mismatch",
        ):
            self.execute(ClosedRunner(self.paths, tag_commit="c" * 40))
        self.assertFalse(self.paths.tombstone_path.exists())

    def test_foreign_tombstone_is_preserved_and_refused(self) -> None:
        self.write_evidence(self.paths.tombstone_path, b'{"foreign":true}')
        before = (
            self.paths.tombstone_path.read_bytes(),
            self.paths.tombstone_path.stat().st_ino,
        )
        with self.assertRaisesRegex(
            subject.StagedPrefixDispositionIntegrityError,
            "staged_prefix_disposition_tombstone_conflict",
        ):
            self.execute()
        self.assertEqual(
            (
                self.paths.tombstone_path.read_bytes(),
                self.paths.tombstone_path.stat().st_ino,
            ),
            before,
        )


if __name__ == "__main__":
    unittest.main()
