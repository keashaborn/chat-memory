from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from tools.governed_memory_validation import (
    generate_staged_prefix_disposition_contract as generator,
)
from tools.governed_memory_validation import staged_prefix_disposition as subject


FAILED_PACKAGE = "1" * 64
FAILED_RUNTIME = "2" * 64
CORRECTED_PACKAGE = "3" * 64
CORRECTED_RUNTIME = "4" * 64
TAG_COMMIT = "a" * 40
TAG_TREE = "b" * 40
DISPOSITION_ID = "phase9-v8-staged-to-v9-000005"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECKED_IN_CONTRACT = (
    REPOSITORY_ROOT
    / "ops/governed_memory/staged_prefix_disposition_contract.json"
)


class ClosedRunner:
    def __init__(
        self,
        paths: subject.StagedPrefixDispositionPaths,
        *,
        failed_tag_commit: str = TAG_COMMIT,
        docker_output: bytes = b"",
    ) -> None:
        self.paths = paths
        self.failed_tag_commit = failed_tag_commit
        self.docker_output = docker_output

    def run(self, argv: object) -> subprocess.CompletedProcess[bytes]:
        command = tuple(argv)
        if command == subject._git_command(
            self.paths, "rev-parse", "--show-toplevel"
        ):
            output = (str(self.paths.repository_root) + "\n").encode()
        elif command in {
            subject._git_command(
                self.paths,
                "cat-file",
                "-t",
                subject.PRODUCTION_PRESERVED_TAG_REF,
            ),
            subject._git_command(
                self.paths,
                "cat-file",
                "-t",
                subject.PRODUCTION_OLD_TAG_REF,
            ),
        }:
            output = b"commit\n"
        elif command in {
            subject._git_command(
                self.paths,
                "rev-parse",
                "--verify",
                subject.PRODUCTION_PRESERVED_TAG_REF,
            ),
            subject._git_command(
                self.paths,
                "rev-parse",
                "--verify",
                subject.PRODUCTION_PRESERVED_TAG_REF + "^{commit}",
            ),
        }:
            output = (subject.PRODUCTION_PRESERVED_TAG_COMMIT + "\n").encode()
        elif command == subject._git_command(
            self.paths,
            "rev-parse",
            "--verify",
            subject.PRODUCTION_PRESERVED_TAG_REF + "^{tree}",
        ):
            output = (subject.PRODUCTION_PRESERVED_TAG_TREE + "\n").encode()
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
            output = (self.failed_tag_commit + "\n").encode()
        elif command == subject._git_command(
            self.paths,
            "rev-parse",
            "--verify",
            subject.PRODUCTION_OLD_TAG_REF + "^{tree}",
        ):
            output = (TAG_TREE + "\n").encode()
        elif command in subject._DOCKER_COMMANDS.values():
            output = self.docker_output
        elif command in {
            subject._tcp_command(port)
            for port in subject._TCP_IDENTITIES.values()
        }:
            output = b""
        elif command in {
            subject._systemd_command(service)
            for service in subject._SYSTEMD_SERVICES
        }:
            output = b"not-found\n"
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
        self.systemd = self.directory("systemd")
        self.wants = self.directory("systemd/wants")
        preserved_executions = self.directory("state/executions-v2")
        preserved_secret = self.directory("secrets/000002")
        failed_executions = self.directory("state/executions-v4")
        failed_execution = self.directory(
            "state/executions-v4/" + subject.PRODUCTION_EXECUTION_ID
        )
        failed_secret = self.directory("secrets/000004")
        self.paths = subject.StagedPrefixDispositionPaths(
            repository_root=self.repository,
            durable_contract_path=self.contract_root / "000005.contract.json",
            tombstone_path=self.control / "store_spec-v4.json",
            preserved_permit_path=self.state / "permit-000002.json",
            preserved_contract_path=self.contract_root / "contract-000002.json",
            preserved_pre_effect_receipt_path=self.control / "store_spec.json",
            preserved_staged_capsule_path=self.state / "capsule-v3.publishing",
            preserved_executions_root=preserved_executions,
            preserved_secret_root=preserved_secret,
            old_permit_path=self.state / "permit-000004.json",
            old_contract_path=self.contract_root / "contract-000004.json",
            old_pre_effect_receipt_path=self.control / "store_spec-v3.json",
            staged_capsule_path=self.state / "capsule-v5.json",
            failed_authority_state_path=self.state / "authority-v4.sqlite3",
            failed_execution_root=failed_execution,
            failed_execution_journal_path=failed_execution / "journal.jsonl",
            failed_execution_resources_path=failed_execution / "resources.jsonl",
            failed_capsule_staging_path=self.state / "capsule-v5.publishing",
            failed_promotable_receipt_path=self.state / "promotable-000004.json",
            failed_promotable_receipt_staging_path=(
                self.state / "promotable-000004.json.publishing"
            ),
            failed_systemd_unit_path=self.systemd / "stores-v4.service",
            failed_systemd_link_path=self.wants / "stores-v4.service",
            final_capsule_path=self.state / "capsule-v3.json",
            authority_state_path=self.state / "authority-v2.sqlite3",
            promotable_receipt_path=self.state / "promotable-000002.json",
            promotable_receipt_staging_path=(
                self.state / "promotable-000002.json.publishing"
            ),
            systemd_unit_path=self.systemd / "stores-v2.service",
            systemd_link_path=self.wants / "stores-v2.service",
            executions_root=failed_executions,
            store_secret_root=failed_secret,
            corrected_capsule_path=self.state / "capsule-v6.json",
            corrected_capsule_staging_path=self.state / "capsule-v6.publishing",
            corrected_authority_state_path=self.state / "authority-v5.sqlite3",
            corrected_promotable_receipt_path=self.state / "promotable-000005.json",
            corrected_promotable_receipt_staging_path=(
                self.state / "promotable-000005.json.publishing"
            ),
            corrected_executions_root=self.state / "executions-v5",
            corrected_secret_root=self.root / "secrets/000005",
            corrected_store_spec_path=self.control / "store_spec-v5.json",
            corrected_systemd_unit_path=self.systemd / "stores-v5.service",
            corrected_systemd_link_path=self.wants / "stores-v5.service",
            global_lock_path=self.locks / "execution.lock",
            live_guard_path=self.locks / "live-proof.lock",
            expected_uid=self.uid,
            expected_gid=self.gid,
        )
        for path, raw, mode in (
            (self.paths.preserved_contract_path, b"preserved-contract", 0o400),
            (self.paths.preserved_permit_path, b"preserved-permit", 0o400),
            (
                self.paths.preserved_pre_effect_receipt_path,
                b"preserved-receipt",
                0o400,
            ),
            (
                self.paths.preserved_staged_capsule_path,
                b"preserved-capsule",
                0o400,
            ),
            (self.paths.old_contract_path, b"failed-contract", 0o400),
            (self.paths.old_permit_path, b"failed-permit", 0o400),
            (
                self.paths.old_pre_effect_receipt_path,
                b"failed-tombstone",
                0o400,
            ),
            (self.paths.staged_capsule_path, b"failed-capsule", 0o400),
            (
                self.paths.failed_authority_state_path,
                b"failed-authority",
                0o600,
            ),
            (self.paths.failed_execution_journal_path, b"", 0o600),
            (self.paths.failed_execution_resources_path, b"", 0o600),
        ):
            self.write(path, raw, mode)
        raw = subject.canonical_json_bytes(self.contract_document())
        self.write(self.paths.durable_contract_path, raw, 0o400)
        self.expectation = subject.ReviewedStagedPrefixExpectation(
            contract_sha256=hashlib.sha256(raw).hexdigest(),
            disposition_id=DISPOSITION_ID,
            old_tag_ref=subject.PRODUCTION_OLD_TAG_REF,
            old_tag_commit=TAG_COMMIT,
            old_tag_tree=TAG_TREE,
            failed_package_manifest_sha256=FAILED_PACKAGE,
            failed_controller_runtime_receipt_sha256=FAILED_RUNTIME,
            corrected_generation="000005",
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
    def write(path: Path, raw: bytes, mode: int) -> None:
        path.write_bytes(raw)
        path.chmod(mode)

    @staticmethod
    def file_spec(role: str, path: Path) -> dict[str, object]:
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

    @staticmethod
    def directory_spec(role: str, path: Path) -> dict[str, object]:
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
            "entries": sorted(member.name for member in path.iterdir()),
        }

    def contract_document(self) -> dict[str, object]:
        reviewed = subject.ReviewedStagedPrefixExpectation(
            contract_sha256="0" * 64,
            disposition_id=DISPOSITION_ID,
            old_tag_ref=subject.PRODUCTION_OLD_TAG_REF,
            old_tag_commit=TAG_COMMIT,
            old_tag_tree=TAG_TREE,
            failed_package_manifest_sha256=FAILED_PACKAGE,
            failed_controller_runtime_receipt_sha256=FAILED_RUNTIME,
            corrected_generation="000005",
            corrected_package_manifest_sha256=CORRECTED_PACKAGE,
            corrected_controller_runtime_receipt_sha256=CORRECTED_RUNTIME,
        )
        return {
            "schema_version": subject.CONTRACT_SCHEMA,
            "disposition_id": DISPOSITION_ID,
            "repository_contract_source": (
                subject.PRODUCTION_REPOSITORY_CONTRACT_SOURCE
            ),
            "durable_contract_path": str(self.paths.durable_contract_path),
            "tombstone_path": str(self.paths.tombstone_path),
            "preserved_predecessor": {
                "generation": "000002",
                "tag": {
                    "ref": subject.PRODUCTION_PRESERVED_TAG_REF,
                    "object_type": "commit",
                    "commit": subject.PRODUCTION_PRESERVED_TAG_COMMIT,
                    "tree": subject.PRODUCTION_PRESERVED_TAG_TREE,
                },
                "evidence_files": [
                    self.file_spec(
                        "preserved_contract", self.paths.preserved_contract_path
                    ),
                    self.file_spec(
                        "preserved_permit", self.paths.preserved_permit_path
                    ),
                    self.file_spec(
                        "preserved_pre_effect_receipt",
                        self.paths.preserved_pre_effect_receipt_path,
                    ),
                    self.file_spec(
                        "preserved_staged_capsule",
                        self.paths.preserved_staged_capsule_path,
                    ),
                ],
                "evidence_directories": [
                    self.directory_spec(
                        "preserved_executions_v2",
                        self.paths.preserved_executions_root,
                    ),
                    self.directory_spec(
                        "preserved_store_secret",
                        self.paths.preserved_secret_root,
                    ),
                ],
            },
            "failed_attempt": {
                "generation": "000004",
                "tag": {
                    "ref": subject.PRODUCTION_OLD_TAG_REF,
                    "object_type": "commit",
                    "commit": TAG_COMMIT,
                    "tree": TAG_TREE,
                },
                "package_manifest_sha256": FAILED_PACKAGE,
                "controller_runtime_receipt_sha256": FAILED_RUNTIME,
                "evidence_files": [
                    self.file_spec("failed_contract", self.paths.old_contract_path),
                    self.file_spec("failed_permit", self.paths.old_permit_path),
                    self.file_spec(
                        "failed_store_spec_v3_tombstone",
                        self.paths.old_pre_effect_receipt_path,
                    ),
                    self.file_spec(
                        "failed_capsule_v5", self.paths.staged_capsule_path
                    ),
                    self.file_spec(
                        "failed_authority_state_v4",
                        self.paths.failed_authority_state_path,
                    ),
                    self.file_spec(
                        "failed_execution_journal",
                        self.paths.failed_execution_journal_path,
                    ),
                    self.file_spec(
                        "failed_execution_resources",
                        self.paths.failed_execution_resources_path,
                    ),
                ],
                "evidence_directories": [
                    self.directory_spec(
                        "failed_executions_v4", self.paths.executions_root
                    ),
                    self.directory_spec(
                        "failed_execution", self.paths.failed_execution_root
                    ),
                    self.directory_spec(
                        "failed_store_secret", self.paths.store_secret_root
                    ),
                ],
            },
            "corrected_successor": subject._corrected_successor_document(
                reviewed
            ),
            "absent_resources": subject._expected_absent_resources(self.paths),
        }

    def execute(self, runner: ClosedRunner | None = None) -> dict[str, object]:
        return dict(
            subject.execute_staged_prefix_disposition(
                self.paths,
                self.expectation,
                command_runner=runner or ClosedRunner(self.paths),
            )
        )

    def test_exact_two_generation_evidence_creates_replayable_fence(self) -> None:
        evidence_paths = tuple(subject._preserved_evidence_paths(self.paths).values()) + tuple(
            subject._failed_evidence_paths(self.paths).values()
        )
        before = {path: (path.read_bytes(), path.stat().st_ino) for path in evidence_paths}
        first = self.execute()
        inode = self.paths.tombstone_path.stat().st_ino
        second = self.execute()
        self.assertEqual(first, second)
        self.assertEqual(first["failed_generation"], "000004")
        self.assertEqual(first["corrected_generation"], "000005")
        self.assertTrue(first["failed_evidence_preserved_in_place"])
        self.assertFalse(first["deletion_performed"])
        self.assertEqual(self.paths.tombstone_path.stat().st_ino, inode)
        self.assertEqual(stat.S_IMODE(self.paths.tombstone_path.stat().st_mode), 0o400)
        for path, identity in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_ino), identity)

    def test_exact_fence_replay_allows_corrected_successor_preclaim_state(self) -> None:
        first = self.execute()
        tombstone_inode = self.paths.tombstone_path.stat().st_ino
        self.write(
            self.paths.corrected_authority_state_path,
            b"corrected-preclaim-authority",
            0o600,
        )
        self.directory("state/executions-v5")
        corrected_container = subject._DOCKER_IDENTITIES["docker_container"][-1]

        replay = self.execute(
            ClosedRunner(
                self.paths,
                docker_output=(corrected_container + "\n").encode("ascii"),
            )
        )

        self.assertEqual(replay, first)
        self.assertEqual(self.paths.tombstone_path.stat().st_ino, tombstone_inode)
        self.assertEqual(
            self.paths.corrected_authority_state_path.read_bytes(),
            b"corrected-preclaim-authority",
        )

    def test_exact_fence_replay_still_refuses_failed_evidence_drift(self) -> None:
        first = self.execute()
        failed = self.paths.failed_execution_journal_path
        failed.chmod(0o600)
        failed.write_bytes(b"drift")
        failed.chmod(0o600)

        with self.assertRaisesRegex(
            subject.StagedPrefixDispositionError,
            "evidence_mismatch|file_invalid",
        ):
            self.execute()

        self.assertTrue(self.paths.tombstone_path.exists())
        self.assertEqual(
            subject.verify_staged_prefix_tombstone(
                self.paths, self.expectation
            ),
            first,
        )

    def test_conflicting_existing_fence_never_falls_back_to_fresh_disposition(self) -> None:
        self.execute()
        self.paths.tombstone_path.chmod(0o600)
        self.paths.tombstone_path.write_bytes(b"conflict")
        self.paths.tombstone_path.chmod(0o400)

        with self.assertRaisesRegex(
            subject.StagedPrefixDispositionError,
            "tombstone_invalid|tombstone_conflict",
        ):
            self.execute()

        self.assertEqual(self.paths.tombstone_path.read_bytes(), b"conflict")

    def test_each_failed_file_hash_or_mode_drift_refuses_without_fence(self) -> None:
        for path in subject._failed_evidence_paths(self.paths).values():
            with self.subTest(path=path):
                original = path.read_bytes()
                original_mode = stat.S_IMODE(path.stat().st_mode)
                path.chmod(0o600)
                path.write_bytes(original + b"x")
                path.chmod(original_mode)
                with self.assertRaises(subject.StagedPrefixDispositionError):
                    self.execute()
                self.assertFalse(self.paths.tombstone_path.exists())
                path.chmod(0o600)
                path.write_bytes(original)
                path.chmod(original_mode)

    def test_failed_execution_tree_drift_refuses_without_deleting_it(self) -> None:
        foreign = self.paths.failed_execution_root / "foreign"
        self.write(foreign, b"preserve", 0o600)
        with self.assertRaisesRegex(
            subject.StagedPrefixDispositionIntegrityError,
            "directory_evidence_mismatch",
        ):
            self.execute()
        self.assertEqual(foreign.read_bytes(), b"preserve")
        self.assertFalse(self.paths.tombstone_path.exists())

    def test_failed_tag_or_absent_resource_drift_refuses(self) -> None:
        with self.assertRaisesRegex(
            subject.StagedPrefixDispositionIntegrityError,
            "git_tag_mismatch",
        ):
            self.execute(ClosedRunner(self.paths, failed_tag_commit="c" * 40))
        container = subject._DOCKER_IDENTITIES["docker_container"][-1]
        with self.assertRaisesRegex(
            subject.StagedPrefixDispositionHostStateError,
            "host_effect_present",
        ):
            self.execute(
                ClosedRunner(self.paths, docker_output=(container + "\n").encode())
            )
        self.assertFalse(self.paths.tombstone_path.exists())

    def test_contract_inode_drift_refuses(self) -> None:
        document = self.contract_document()
        document["failed_attempt"]["evidence_directories"][1]["inode"] += 1
        raw = subject.canonical_json_bytes(document)
        self.paths.durable_contract_path.chmod(0o600)
        self.paths.durable_contract_path.write_bytes(raw)
        self.paths.durable_contract_path.chmod(0o400)
        reviewed = replace(
            self.expectation, contract_sha256=hashlib.sha256(raw).hexdigest()
        )
        with self.assertRaisesRegex(
            subject.StagedPrefixDispositionIntegrityError,
            "directory_evidence_mismatch",
        ):
            subject.execute_staged_prefix_disposition(
                self.paths,
                reviewed,
                command_runner=ClosedRunner(self.paths),
            )


class ProductionContractFactsTests(unittest.TestCase):
    @unittest.skip("P5/R5/C5 are intentionally unsealed")
    def test_generator_binds_exact_failed_000004_tree_and_000005_namespace(self) -> None:
        document = generator.generate()
        generated = subject.canonical_json_bytes(document)
        self.assertEqual(CHECKED_IN_CONTRACT.read_bytes(), generated)
        self.assertEqual(
            hashlib.sha256(generated).hexdigest(),
            "daf64a4a6a17d6666d408f0beb216f44ba7d755efab4b43835ec0c7e3ad11f15",
        )
        failed = document["failed_attempt"]
        files = {item["role"]: item for item in failed["evidence_files"]}
        directories = {
            item["role"]: item for item in failed["evidence_directories"]
        }
        self.assertEqual(failed["generation"], "000004")
        self.assertEqual(
            failed["package_manifest_sha256"],
            "5addc8e4ab40b6bc700fde57b67579f8caa3d21077715bcdaf61d5714b52743e",
        )
        self.assertEqual(
            failed["controller_runtime_receipt_sha256"],
            "9dda4d93a1bcfecf4305736feffafb578e4629cd32594cec6b2d6d72cb90a4d3",
        )
        self.assertEqual(failed["tag"]["commit"], subject.PRODUCTION_OLD_TAG_COMMIT)
        self.assertEqual(files["failed_permit"]["inode"], 1652272)
        self.assertEqual(
            files["failed_store_spec_v3_tombstone"]["sha256"],
            "57e07d7ce65e79503982e3c87297f644a347b41394a40cf56127f3536524130d",
        )
        self.assertEqual(files["failed_authority_state_v4"]["mode"], 0o600)
        self.assertEqual(files["failed_execution_journal"]["size"], 0)
        self.assertEqual(
            files["failed_execution_journal"]["path"],
            str(subject.PRODUCTION_EXECUTION_ROOT / "journal.jsonl"),
        )
        self.assertEqual(
            files["failed_execution_resources"]["path"],
            str(subject.PRODUCTION_EXECUTION_ROOT / "resources.jsonl"),
        )
        self.assertEqual(
            directories["failed_executions_v4"]["entries"],
            [subject.PRODUCTION_EXECUTION_ID],
        )
        self.assertEqual(
            directories["failed_execution"]["entries"],
            ["journal.jsonl", "resources.jsonl"],
        )
        successor = document["corrected_successor"]
        self.assertEqual(successor["generation"], "000005")
        self.assertEqual(
            successor["package_manifest_sha256"],
            "634669dbca4f2ccfed929951bcdd0d555d19e53f9b736ee21b42217e8e8cd629",
        )
        self.assertEqual(
            successor["controller_runtime_receipt_sha256"],
            "ed0b3518484eec292f35f8b996bccefd01e13038105634016b4253a8c2a732a7",
        )
        self.assertEqual(
            successor["attempt_identity_sha256"],
            "7d36e9326e1b333b4203d167f776ef75759ff295f943f79ae5838b068c3b8ba5",
        )
        self.assertEqual(
            subject._TCP_IDENTITIES["127.0.0.1:55436@000005"], "55436"
        )
        self.assertEqual(
            subject._TCP_IDENTITIES["127.0.0.1:6347@000005"], "6347"
        )


if __name__ == "__main__":
    unittest.main()
