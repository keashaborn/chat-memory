from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import memory_v1_qdrant_bridge_config_v1 as bridge


OPERATOR_GIT = {"head": "a" * 40, "tree": "b" * 40}
PRODUCTION_GIT = {"head": "c" * 40, "tree": "d" * 40}
INTEGRATED_GIT = {"head": "e" * 40, "tree": "f" * 40}
LEASE = {
    "lease_id": "bridge-config-test-lease",
    "task_id": "bridge-config-test-task",
    "thread_id": "bridge-config-test-thread",
    "acquire_event_sha256": "d" * 64,
    "registry_revision": 44,
}
RUN_ID = "memory-qdrant-bridge-config-20260802T080000Z-abcdef123456"
RESTORE_RUN_ID = "memory-qdrant-bridge-config-20260802T081000Z-123456abcdef"
SECRET = "synthetic-sensitive-marker-that-must-never-appear"
ORIGINAL = (
    f"OPENAI_API_KEY={SECRET}\n"
    "UNRELATED=preserve this exactly\n"
    "MEMORY_V1_V5_SHADOW=1\n"
    "AFTER=unchanged\n"
).encode()
APPLIED = (
    f"OPENAI_API_KEY={SECRET}\n"
    "UNRELATED=preserve this exactly\n"
    "MEMORY_V1_COLLECTION=memory_claim_v1_active\n"
    "MEMORY_V1_V5_SHADOW=1\n"
    "AFTER=unchanged\n"
).encode()


def identity(path: pathlib.Path) -> dict[str, int]:
    info = path.stat()
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "nlink": info.st_nlink,
        "size": info.st_size,
    }


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def parse_audit(path: pathlib.Path) -> list[dict]:
    events = [json.loads(line) for line in path.read_bytes().splitlines()]
    prior = None
    for sequence, event in enumerate(events, 1):
        assert event["audit_sequence"] == sequence
        assert event["prior_event_sha256"] == prior
        body = dict(event)
        stored = body.pop("event_sha256")
        assert digest(bridge.canonical_bytes(body)) == stored
        prior = stored
    return events


class BridgeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.repo = self.root / "chat-memory"
        self.repo.mkdir(mode=0o700)
        self.operator_repo = self.root / "candidate"
        (self.operator_repo / "scripts").mkdir(parents=True, mode=0o700)
        (self.repo / "scripts").mkdir(mode=0o700)
        tool_bytes = pathlib.Path(bridge.__file__).read_bytes()
        self.operator_tool = self.operator_repo / bridge.TOOL_RELATIVE_PATH
        self.production_tool = self.repo / bridge.TOOL_RELATIVE_PATH
        self.operator_tool.write_bytes(tool_bytes)
        self.production_tool.write_bytes(tool_bytes)
        self.operator_tool.chmod(0o644)
        self.production_tool.chmod(0o644)
        self.env_path = self.repo / ".env"
        self.env_path.write_bytes(ORIGINAL)
        self.env_path.chmod(0o600)
        self.snapshot = self.root / "snapshots"
        self.snapshot.mkdir(mode=0o700)
        self.apply_activation = self.snapshot / "phase5-apply"
        self.apply_activation.mkdir(mode=0o700)
        self.apply_bridge = self.apply_activation / "bridge-config"
        self.apply_bridge.mkdir(mode=0o700)
        self.run = self.apply_bridge / RUN_ID
        self.run.mkdir(mode=0o700)
        self.restore_activation = self.snapshot / "phase5-restore"
        self.restore_activation.mkdir(mode=0o700)
        self.restore_bridge = self.restore_activation / "bridge-config"
        self.restore_bridge.mkdir(mode=0o700)
        self.restore_run = self.restore_bridge / RESTORE_RUN_ID
        self.restore_run.mkdir(mode=0o700)
        self.apply_spec_path = self.apply_activation / "bridge-config.spec.json"
        self.restore_spec_path = self.restore_activation / "bridge-config.spec.json"
        self.lock_path = self.root / "bridge.lock"
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.constants = mock.patch.multiple(
            bridge,
            EXPECTED_UID=self.uid,
            EXPECTED_GID=self.gid,
            ROOT_UID=self.uid,
            ROOT_GID=self.gid,
            PRODUCTION_REPOSITORY=self.repo,
            PREINTEGRATION_OPERATOR_REPOSITORY=self.operator_repo,
            ALLOWED_OPERATOR_REPOSITORIES=frozenset(
                {self.operator_repo, self.repo}
            ),
            ENV_PATH=self.env_path,
            SNAPSHOT_ROOT=self.snapshot,
            LOCK_PATH=self.lock_path,
        )
        self.constants.start()
        self.addCleanup(self.constants.stop)
        self.exchange = mock.patch.object(
            bridge, "_rename_exchange", side_effect=self.fake_exchange
        )
        self.exchange.start()
        self.addCleanup(self.exchange.stop)
        self.tool_sha = bridge.tool_sha256(self.operator_tool)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def fake_exchange(parent: int, first: str, second: str) -> None:
        temporary = ".bridge-config-unit-exchange"
        os.rename(first, temporary, src_dir_fd=parent, dst_dir_fd=parent)
        os.rename(second, first, src_dir_fd=parent, dst_dir_fd=parent)
        os.rename(temporary, second, src_dir_fd=parent, dst_dir_fd=parent)

    def fake_git_identity(self, repository: pathlib.Path) -> dict[str, str]:
        if repository == self.operator_repo:
            return dict(OPERATOR_GIT)
        if repository == self.repo:
            return dict(PRODUCTION_GIT)
        raise AssertionError(f"unexpected repository: {repository}")

    def value(
        self,
        *,
        operation: str = "apply",
        run_id: str = RUN_ID,
        env_raw: bytes | None = None,
        backup_path: pathlib.Path | None = None,
        backup_identity: dict | None = None,
        backup_sha: str | None = None,
        operator_repository: pathlib.Path | None = None,
        operator_git: dict[str, str] | None = None,
        production_git: dict[str, str] | None = None,
    ) -> dict:
        if env_raw is None:
            env_raw = self.env_path.read_bytes()
        if backup_path is None:
            backup_path = self.run / "bridge-config.env.backup"
        run = self.run if run_id == RUN_ID else self.restore_run
        result = bridge.apply_transform(env_raw) if operation == "apply" else ORIGINAL
        apply_report = self.run / "bridge-config.report.json"
        operator_repository = operator_repository or self.operator_repo
        operator_git = operator_git or OPERATOR_GIT
        production_git = production_git or PRODUCTION_GIT
        return {
            "schema_version": bridge.SPEC_VERSION,
            "run_id": run_id,
            "operation": operation,
            "operator_repository": str(operator_repository),
            "expected_operator_git": dict(operator_git),
            "expected_production_git": dict(production_git),
            "expected_tool_path": str(
                operator_repository / bridge.TOOL_RELATIVE_PATH
            ),
            "expected_tool_sha256": self.tool_sha,
            "lease": dict(LEASE),
            "env_path": str(self.env_path),
            "expected_env_identity": identity(self.env_path),
            "expected_env_sha256": digest(env_raw),
            "expected_result_env_sha256": digest(result),
            "backup_path": str(backup_path),
            "expected_backup_identity": backup_identity,
            "expected_backup_sha256": backup_sha,
            "apply_spec_path": (
                str(self.apply_spec_path) if operation == "restore" else None
            ),
            "apply_spec_sha256": (
                digest(self.apply_spec_path.read_bytes())
                if operation == "restore" and self.apply_spec_path.exists()
                else ("1" * 64 if operation == "restore" else None)
            ),
            "apply_report_path": str(apply_report) if operation == "restore" else None,
            "apply_report_sha256": (
                digest(apply_report.read_bytes())
                if operation == "restore" and apply_report.exists()
                else ("2" * 64 if operation == "restore" else None)
            ),
            "audit_path": str(run / "bridge-config.audit.jsonl"),
            "report_path": str(run / "bridge-config.report.json"),
        }

    def load(self, value: dict) -> bridge.Spec:
        raw = bridge.canonical_bytes(value)
        path = self.apply_spec_path if value["operation"] == "apply" else self.restore_spec_path
        path.write_bytes(raw)
        path.chmod(0o600)
        return bridge.Spec.load(path, digest(raw))

    def execute(self, spec: bridge.Spec) -> dict:
        operator_repository = pathlib.Path(spec.value["operator_repository"])

        def expected_git(repository: pathlib.Path) -> dict[str, str]:
            if repository == operator_repository:
                return dict(spec.value["expected_operator_git"])
            if repository == self.repo:
                return dict(spec.value["expected_production_git"])
            raise AssertionError(f"unexpected repository: {repository}")

        with (
            mock.patch.object(
                bridge,
                "current_tool_path",
                return_value=pathlib.Path(spec.value["expected_tool_path"]),
            ),
            mock.patch.object(
                bridge, "git_identity", side_effect=expected_git
            ),
            mock.patch.object(bridge, "require_guard_as_uid1000") as guard,
            mock.patch.dict(os.environ, {"SUDO_UID": str(self.uid)}, clear=False),
        ):
            report = bridge._execute_child(spec)
        self.assertEqual(guard.call_count, 2)
        return report


class SpecAndTransformTests(BridgeFixture):
    def test_canonical_apply_spec_loads(self) -> None:
        spec = self.load(self.value())
        self.assertEqual(spec.value["operation"], "apply")
        self.assertEqual(spec.sha256, digest(spec.raw))

    def test_noncanonical_and_extra_field_specs_fail(self) -> None:
        value = self.value()
        path = self.apply_spec_path
        raw = json.dumps(value, indent=2).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        with self.assertRaises(bridge.BridgeConfigError):
            bridge.Spec.load(path, digest(raw))
        value["extra"] = True
        raw = bridge.canonical_bytes(value)
        path.write_bytes(raw)
        with self.assertRaises(bridge.BridgeConfigError):
            bridge.Spec.load(path, digest(raw))

    def test_spec_rejects_wrong_env_and_evidence_paths(self) -> None:
        value = self.value()
        value["env_path"] = str(self.root / "other.env")
        with self.assertRaises(bridge.BridgeConfigError):
            self.load(value)
        value = self.value()
        value["expected_tool_path"] = str(self.repo / "scripts/other.py")
        with self.assertRaises(bridge.BridgeConfigError):
            self.load(value)
        value = self.value()
        value["report_path"] = str(self.root / "outside.json")
        with self.assertRaises(bridge.BridgeConfigError):
            self.load(value)

    def test_spec_rejects_unapproved_operator_and_split_git_mismatch(self) -> None:
        value = self.value()
        value["operator_repository"] = str(self.root / "unapproved")
        value["expected_tool_path"] = str(
            self.root / "unapproved" / bridge.TOOL_RELATIVE_PATH
        )
        with self.assertRaisesRegex(bridge.BridgeConfigError, "operator repository"):
            self.load(value)
        value = self.value(
            operator_repository=self.repo,
            operator_git=OPERATOR_GIT,
            production_git=PRODUCTION_GIT,
        )
        with self.assertRaisesRegex(bridge.BridgeConfigError, "identities differ"):
            self.load(value)

    def test_apply_rejects_predeclared_backup_identity(self) -> None:
        value = self.value()
        value["expected_backup_sha256"] = digest(ORIGINAL)
        value["expected_backup_identity"] = identity(self.env_path)
        with self.assertRaises(bridge.BridgeConfigError):
            self.load(value)

    def test_restore_requires_exact_backup_contract(self) -> None:
        backup = self.run / "bridge-config.env.backup"
        backup.write_bytes(ORIGINAL)
        backup.chmod(0o600)
        self.env_path.write_bytes(APPLIED)
        value = self.value(
            operation="restore",
            run_id=RESTORE_RUN_ID,
            env_raw=APPLIED,
            backup_path=backup,
            backup_identity=identity(backup),
            backup_sha=digest(ORIGINAL),
        )
        self.assertEqual(self.load(value).value["operation"], "restore")
        value["expected_backup_sha256"] = "e" * 64
        with self.assertRaises(bridge.BridgeConfigError):
            self.load(value)

    def test_spec_mode_and_symlink_are_rejected(self) -> None:
        value = self.value()
        raw = bridge.canonical_bytes(value)
        real = self.root / "real-spec.json"
        self.apply_spec_path.write_bytes(raw)
        self.apply_spec_path.chmod(0o644)
        with self.assertRaises(bridge.BridgeConfigError):
            bridge.Spec.load(self.apply_spec_path, digest(raw))
        self.apply_spec_path.unlink()
        real.write_bytes(raw)
        real.chmod(0o600)
        self.apply_spec_path.symlink_to(real)
        with self.assertRaises(bridge.BridgeConfigError):
            bridge.Spec.load(self.apply_spec_path, digest(raw))

    def test_apply_transform_is_one_exact_insertion(self) -> None:
        self.assertEqual(bridge.apply_transform(ORIGINAL), APPLIED)
        before = ORIGINAL.splitlines(keepends=True)
        after = APPLIED.splitlines(keepends=True)
        anchor = after.index(bridge.ANCHOR_LINE)
        self.assertEqual(after[anchor - 1], bridge.SETTING_LINE)
        self.assertEqual(after[: anchor - 1] + after[anchor:], before)

    def test_apply_transform_rejects_ambiguous_or_malformed_files(self) -> None:
        cases = [
            ORIGINAL + bridge.ANCHOR_LINE,
            APPLIED,
            ORIGINAL.replace(b"\n", b"\r\n"),
            ORIGINAL[:-1],
            b"A=1\nMEMORY_V1_COLLECTION=wrong\n" + bridge.ANCHOR_LINE,
            b"A=1\n export MEMORY_V1_COLLECTION = wrong\n" + bridge.ANCHOR_LINE,
            b"A=1\nexport MEMORY_V1_V5_SHADOW=1\n" + bridge.ANCHOR_LINE,
            b"A=1\n",
            b"A=\x00bad\n" + bridge.ANCHOR_LINE,
        ]
        for raw in cases:
            with self.subTest(raw=raw[:40]), self.assertRaises(bridge.BridgeConfigError):
                bridge.apply_transform(raw)

    def test_restore_requires_byte_exact_applied_state(self) -> None:
        self.assertEqual(bridge.restore_transform(APPLIED, ORIGINAL), ORIGINAL)
        with self.assertRaises(bridge.BridgeConfigError):
            bridge.restore_transform(APPLIED.replace(b"AFTER=", b"CHANGED="), ORIGINAL)


class ExecutionTests(BridgeFixture):
    def test_environment_parent_must_be_uid1000_and_not_writable(self) -> None:
        spec = self.load(self.value())
        self.repo.chmod(0o722)
        with self.assertRaisesRegex(bridge.BridgeConfigError, "parent identity"):
            self.execute(spec)
        self.assertEqual(self.env_path.read_bytes(), ORIGINAL)
        self.assertFalse((self.run / "bridge-config.env.backup").exists())

    def test_apply_creates_durable_backup_audit_report_and_exact_env(self) -> None:
        spec = self.load(self.value())
        report = self.execute(spec)
        backup = self.run / "bridge-config.env.backup"
        audit_path = self.run / "bridge-config.audit.jsonl"
        report_path = self.run / "bridge-config.report.json"
        self.assertEqual(self.env_path.read_bytes(), APPLIED)
        self.assertEqual(backup.read_bytes(), ORIGINAL)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        events = parse_audit(audit_path)
        self.assertEqual(
            [event["state"] for event in events],
            ["prepared", "backup_durable", "completed"],
        )
        self.assertEqual(json.loads(report_path.read_text()), report)
        all_evidence = audit_path.read_bytes() + report_path.read_bytes()
        self.assertNotIn(SECRET.encode(), all_evidence)
        self.assertNotIn(b"OPENAI_API_KEY", all_evidence)
        self.assertFalse(report["raw_values_recorded"])
        self.assertFalse(report["service_action_performed"])
        self.assertEqual(set(report), bridge.REPORT_FIELDS)
        self.assertIsNone(report["failure_stage"])

    def test_atomic_exchange_preserves_concurrent_environment_replacement(self) -> None:
        opening_identity = identity(self.env_path)
        concurrent = ORIGINAL.replace(b"UNRELATED=preserve", b"UNRELATED=parallel")
        calls = 0

        def exchange_after_drift(parent: int, first: str, second: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                replacement = self.repo / ".env.concurrent"
                replacement.write_bytes(concurrent)
                replacement.chmod(0o600)
                os.replace(replacement, self.env_path)
            self.fake_exchange(parent, first, second)

        with (
            mock.patch.object(
                bridge, "_rename_exchange", side_effect=exchange_after_drift
            ),
            self.assertRaisesRegex(
                bridge.BridgeConfigError, "changed at atomic exchange"
            ),
        ):
            bridge._replace_env(
                APPLIED,
                expected_identity=opening_identity,
                expected_sha256=digest(ORIGINAL),
                run_id=RUN_ID,
            )
        self.assertEqual(calls, 2)
        self.assertEqual(self.env_path.read_bytes(), concurrent)
        self.assertFalse((self.repo / f".env.{RUN_ID}.tmp").exists())

    def test_restore_is_exact_and_keeps_backup(self) -> None:
        apply_spec = self.load(self.value())
        self.execute(apply_spec)
        backup = self.run / "bridge-config.env.backup"
        restore_value = self.value(
            operation="restore",
            run_id=RESTORE_RUN_ID,
            env_raw=APPLIED,
            backup_path=backup,
            backup_identity=identity(backup),
            backup_sha=digest(ORIGINAL),
        )
        restore_spec = self.load(restore_value)
        report = self.execute(restore_spec)
        self.assertEqual(self.env_path.read_bytes(), ORIGINAL)
        self.assertEqual(backup.read_bytes(), ORIGINAL)
        self.assertEqual(report["operation"], "restore")
        self.assertEqual(
            [event["state"] for event in parse_audit(self.restore_run / "bridge-config.audit.jsonl")],
            ["prepared", "completed"],
        )

    def test_restore_can_switch_from_candidate_operator_to_installed_operator(self) -> None:
        self.execute(self.load(self.value()))
        backup = self.run / "bridge-config.env.backup"
        restore_value = self.value(
            operation="restore",
            run_id=RESTORE_RUN_ID,
            env_raw=APPLIED,
            backup_path=backup,
            backup_identity=identity(backup),
            backup_sha=digest(ORIGINAL),
            operator_repository=self.repo,
            operator_git=INTEGRATED_GIT,
            production_git=INTEGRATED_GIT,
        )
        report = self.execute(self.load(restore_value))
        self.assertEqual(report["operator_repository"], str(self.repo))
        self.assertEqual(report["operator_git_head"], INTEGRATED_GIT["head"])
        self.assertEqual(self.env_path.read_bytes(), ORIGINAL)

    def test_preexisting_backup_is_no_clobber(self) -> None:
        backup = self.run / "bridge-config.env.backup"
        backup.write_bytes(b"do-not-overwrite")
        backup.chmod(0o600)
        spec = self.load(self.value())
        with self.assertRaises(bridge.BridgeConfigError):
            self.execute(spec)
        self.assertEqual(backup.read_bytes(), b"do-not-overwrite")
        self.assertEqual(self.env_path.read_bytes(), ORIGINAL)

    def test_stale_audit_or_report_stops_before_backup_creation(self) -> None:
        for name in ("bridge-config.audit.jsonl", "bridge-config.report.json"):
            with self.subTest(name=name):
                path = self.run / name
                path.write_bytes(b"stale")
                path.chmod(0o600)
                spec = self.load(self.value())
                with self.assertRaises(bridge.BridgeConfigError):
                    self.execute(spec)
                self.assertFalse((self.run / "bridge-config.env.backup").exists())
                self.assertEqual(self.env_path.read_bytes(), ORIGINAL)
                for cleanup_name in (
                    "bridge-config.audit.jsonl",
                    "bridge-config.report.json",
                ):
                    cleanup = self.run / cleanup_name
                    if cleanup.exists():
                        cleanup.unlink()

    def test_env_mode_and_symlink_are_rejected_before_evidence(self) -> None:
        spec = self.load(self.value())
        self.env_path.chmod(0o640)
        with self.assertRaises(bridge.BridgeConfigError):
            self.execute(spec)
        self.assertFalse((self.run / "bridge-config.env.backup").exists())
        self.env_path.unlink()
        target = self.repo / "target"
        target.write_bytes(ORIGINAL)
        target.chmod(0o600)
        self.env_path.symlink_to(target)
        value = self.value()
        value["expected_env_identity"] = identity(target)
        with self.assertRaises(bridge.BridgeConfigError):
            self.execute(bridge.Spec(value=value, raw=bridge.canonical_bytes(value)))

    def test_failure_before_replace_is_durably_failed_no_change(self) -> None:
        spec = self.load(self.value())
        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(bridge, "git_identity", side_effect=self.fake_git_identity),
            mock.patch.object(bridge, "require_guard_as_uid1000"),
            mock.patch.object(bridge, "_replace_env", side_effect=bridge.BridgeConfigError("synthetic")),
            mock.patch.dict(os.environ, {"SUDO_UID": str(self.uid)}, clear=False),
            self.assertRaisesRegex(bridge.BridgeConfigError, "failed_no_change"),
        ):
            bridge._execute_child(spec)
        self.assertEqual(self.env_path.read_bytes(), ORIGINAL)
        events = parse_audit(self.run / "bridge-config.audit.jsonl")
        self.assertEqual(events[-1]["state"], "failed_no_change")
        report = json.loads((self.run / "bridge-config.report.json").read_text())
        self.assertEqual(report["state"], "failed_no_change")
        self.assertEqual(set(report), bridge.REPORT_FIELDS)
        self.assertEqual(report["failure_stage"], "mutation_or_evidence")

    def test_commit_then_exception_is_rolled_back_and_verified(self) -> None:
        spec = self.load(self.value())
        original_replace = bridge._replace_env
        calls = 0

        def commit_then_raise(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original_replace(*args, **kwargs)
            if calls == 1:
                raise bridge.BridgeConfigError("synthetic post-rename failure")
            return result

        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(bridge, "git_identity", side_effect=self.fake_git_identity),
            mock.patch.object(bridge, "require_guard_as_uid1000") as guard,
            mock.patch.object(bridge, "_replace_env", side_effect=commit_then_raise),
            mock.patch.dict(os.environ, {"SUDO_UID": str(self.uid)}, clear=False),
            self.assertRaisesRegex(bridge.BridgeConfigError, "rolled_back_verified"),
        ):
            bridge._execute_child(spec)
        self.assertEqual(self.env_path.read_bytes(), ORIGINAL)
        self.assertEqual(guard.call_count, 3)
        self.assertEqual(parse_audit(self.run / "bridge-config.audit.jsonl")[-1]["state"], "rolled_back_verified")

    def test_displaced_read_failure_uses_distinct_rollback_temporary(self) -> None:
        spec = self.load(self.value())
        original_stable_read_at = bridge._stable_read_at
        calls = 0

        def fail_first_displaced_read(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise bridge.BridgeConfigError("synthetic displaced read failure")
            return original_stable_read_at(*args, **kwargs)

        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(bridge, "git_identity", side_effect=self.fake_git_identity),
            mock.patch.object(bridge, "require_guard_as_uid1000") as guard,
            mock.patch.object(
                bridge,
                "_stable_read_at",
                side_effect=fail_first_displaced_read,
            ),
            mock.patch.dict(os.environ, {"SUDO_UID": str(self.uid)}, clear=False),
            self.assertRaisesRegex(bridge.BridgeConfigError, "rolled_back_verified"),
        ):
            bridge._execute_child(spec)
        self.assertEqual(self.env_path.read_bytes(), ORIGINAL)
        self.assertEqual(guard.call_count, 3)
        self.assertEqual(
            list(self.repo.glob(f".env.{RUN_ID}*.tmp")),
            [],
        )
        self.assertEqual(
            parse_audit(self.run / "bridge-config.audit.jsonl")[-1]["state"],
            "rolled_back_verified",
        )
        report = json.loads((self.run / "bridge-config.report.json").read_text())
        self.assertEqual(report["state"], "rolled_back_verified")

    def test_report_write_failure_after_replace_rolls_back(self) -> None:
        spec = self.load(self.value())
        original_rewrite = bridge._rewrite_descriptor
        calls = 0

        def fail_first(descriptor, raw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise bridge.BridgeConfigError("synthetic report failure")
            return original_rewrite(descriptor, raw)

        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(bridge, "git_identity", side_effect=self.fake_git_identity),
            mock.patch.object(bridge, "require_guard_as_uid1000"),
            mock.patch.object(bridge, "_rewrite_descriptor", side_effect=fail_first),
            mock.patch.dict(os.environ, {"SUDO_UID": str(self.uid)}, clear=False),
            self.assertRaisesRegex(bridge.BridgeConfigError, "rolled_back_verified"),
        ):
            bridge._execute_child(spec)
        self.assertEqual(self.env_path.read_bytes(), ORIGINAL)
        self.assertEqual(parse_audit(self.run / "bridge-config.audit.jsonl")[-1]["state"], "rolled_back_verified")

    def test_completed_audit_failure_rolls_back_then_records_rollback(self) -> None:
        spec = self.load(self.value())
        original_append = bridge.Audit.append
        failed = False

        def fail_completed(audit, state, fields):
            nonlocal failed
            if state == "completed" and not failed:
                failed = True
                raise bridge.BridgeConfigError("synthetic completed audit failure")
            return original_append(audit, state, fields)

        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(bridge, "git_identity", side_effect=self.fake_git_identity),
            mock.patch.object(bridge, "require_guard_as_uid1000"),
            mock.patch.object(bridge.Audit, "append", autospec=True, side_effect=fail_completed),
            mock.patch.dict(os.environ, {"SUDO_UID": str(self.uid)}, clear=False),
            self.assertRaisesRegex(bridge.BridgeConfigError, "rolled_back_verified"),
        ):
            bridge._execute_child(spec)
        self.assertEqual(self.env_path.read_bytes(), ORIGINAL)
        self.assertEqual(parse_audit(self.run / "bridge-config.audit.jsonl")[-1]["state"], "rolled_back_verified")

    def test_restore_post_replace_failure_never_reapplies_bridge(self) -> None:
        self.execute(self.load(self.value()))
        backup = self.run / "bridge-config.env.backup"
        value = self.value(
            operation="restore",
            run_id=RESTORE_RUN_ID,
            env_raw=APPLIED,
            backup_path=backup,
            backup_identity=identity(backup),
            backup_sha=digest(ORIGINAL),
        )
        spec = self.load(value)
        original_replace = bridge._replace_env
        calls = 0

        def commit_then_raise(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original_replace(*args, **kwargs)
            if calls == 1:
                raise bridge.BridgeConfigError("synthetic restore failure")
            return result

        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(bridge, "git_identity", side_effect=self.fake_git_identity),
            mock.patch.object(bridge, "require_guard_as_uid1000"),
            mock.patch.object(bridge, "_replace_env", side_effect=commit_then_raise),
            mock.patch.dict(os.environ, {"SUDO_UID": str(self.uid)}, clear=False),
        ):
            report = bridge._execute_child(spec)
        self.assertEqual(report["state"], "completed")
        self.assertEqual(self.env_path.read_bytes(), ORIGINAL)
        self.assertEqual(calls, 1)
        self.assertEqual(
            [event["state"] for event in parse_audit(self.restore_run / "bridge-config.audit.jsonl")],
            ["prepared", "completed"],
        )

    def test_restore_rejects_tampered_apply_report_before_env_change(self) -> None:
        self.execute(self.load(self.value()))
        backup = self.run / "bridge-config.env.backup"
        value = self.value(
            operation="restore",
            run_id=RESTORE_RUN_ID,
            env_raw=APPLIED,
            backup_path=backup,
            backup_identity=identity(backup),
            backup_sha=digest(ORIGINAL),
        )
        spec = self.load(value)
        report_path = self.run / "bridge-config.report.json"
        report_path.write_bytes(report_path.read_bytes() + b" ")
        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(bridge, "git_identity", side_effect=self.fake_git_identity),
            mock.patch.object(bridge, "require_guard_as_uid1000"),
            mock.patch.dict(os.environ, {"SUDO_UID": str(self.uid)}, clear=False),
            self.assertRaisesRegex(bridge.BridgeConfigError, "lineage digest"),
        ):
            bridge._execute_child(spec)
        self.assertEqual(self.env_path.read_bytes(), APPLIED)
        self.assertFalse((self.restore_run / "bridge-config.audit.jsonl").exists())

    def test_lock_symlink_and_nonprivate_file_are_rejected(self) -> None:
        target = self.root / "target-lock"
        target.write_text("x")
        self.lock_path.symlink_to(target)
        with self.assertRaises(bridge.BridgeConfigError):
            bridge._lock()
        self.lock_path.unlink()
        self.lock_path.write_text("x")
        self.lock_path.chmod(0o644)
        with self.assertRaises(bridge.BridgeConfigError):
            bridge._lock()

    def test_child_rechecks_tool_git_and_guard(self) -> None:
        spec = self.load(self.value())
        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(bridge, "tool_sha256", return_value="e" * 64),
            mock.patch.dict(os.environ, {"SUDO_UID": str(self.uid)}, clear=False),
            self.assertRaisesRegex(bridge.BridgeConfigError, "tool identity"),
        ):
            bridge._execute_child(spec)
        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(bridge, "tool_sha256", return_value=self.tool_sha),
            mock.patch.object(
                bridge,
                "git_identity",
                side_effect=[{"head": "0" * 40, "tree": "1" * 40}, PRODUCTION_GIT],
            ),
            mock.patch.dict(os.environ, {"SUDO_UID": str(self.uid)}, clear=False),
            self.assertRaisesRegex(bridge.BridgeConfigError, "operator checkout"),
        ):
            bridge._execute_child(spec)

    def test_child_rejects_production_drift_independently(self) -> None:
        spec = self.load(self.value())
        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(
                bridge,
                "git_identity",
                side_effect=[OPERATOR_GIT, {"head": "0" * 40, "tree": "1" * 40}],
            ),
            mock.patch.dict(os.environ, {"SUDO_UID": str(self.uid)}, clear=False),
            self.assertRaisesRegex(bridge.BridgeConfigError, "production checkout"),
        ):
            bridge._execute_child(spec)


class OuterAndStaticBoundaryTests(BridgeFixture):
    def test_outer_uses_fixed_no_shell_sudo_argv(self) -> None:
        value = self.value()
        raw = bridge.canonical_bytes(value)
        path = self.apply_spec_path
        path.write_bytes(raw)
        path.chmod(0o600)
        spec = bridge.Spec.load(path, digest(raw))
        prior_identity = dict(spec.value["expected_env_identity"])
        backup_identity = {
            **prior_identity,
            "inode": prior_identity["inode"] + 1,
        }
        result_identity = {
            **prior_identity,
            "inode": prior_identity["inode"] + 2,
            "size": prior_identity["size"] + len(bridge.SETTING_LINE),
        }
        report = bridge._report(
            spec,
            state="completed",
            common=bridge._common_fields(spec, spec.value["expected_env_sha256"]),
            prior_identity=prior_identity,
            result_identity=result_identity,
            backup_identity=backup_identity,
        )
        completed = subprocess.CompletedProcess([], 0, bridge.canonical_bytes(report), b"")
        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(bridge, "tool_sha256", return_value=self.tool_sha),
            mock.patch.object(bridge, "git_identity", side_effect=self.fake_git_identity),
            mock.patch.object(bridge, "require_guard") as guard,
            mock.patch.object(bridge, "_run", return_value=completed) as run,
        ):
            result = bridge._execute_outer(path, digest(raw), "apply")
        self.assertEqual(result, report)
        guard.assert_called_once_with(LEASE)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0:3], [bridge.SUDO, "--non-interactive", "--preserve-env=CHAT_MEMORY_LEASE_ID,CODEX_TASK_ID,CODEX_THREAD_ID"])
        self.assertEqual(argv[3], bridge.PYTHON)
        self.assertIn("--privileged-child", argv)
        self.assertIn("--execute-apply", argv)
        self.assertNotIn("--execute-config", argv)
        self.assertNotIn("sh", [pathlib.Path(item).name for item in argv])

    def test_outer_rejects_incomplete_or_mismatched_child_report(self) -> None:
        spec = self.load(self.value())
        incomplete = {
            "contract_version": bridge.REPORT_VERSION,
            "spec_sha256": spec.sha256,
            "state": "completed",
            "raw_values_recorded": False,
            "service_action_performed": False,
        }
        with self.assertRaisesRegex(bridge.BridgeConfigError, "report rejected"):
            bridge._parse_child_report(bridge.canonical_bytes(incomplete), spec)
        prior_identity = dict(spec.value["expected_env_identity"])
        valid = bridge._report(
            spec,
            state="completed",
            common=bridge._common_fields(spec, spec.value["expected_env_sha256"]),
            prior_identity=prior_identity,
            result_identity={
                **prior_identity,
                "inode": prior_identity["inode"] + 2,
                "size": prior_identity["size"] + len(bridge.SETTING_LINE),
            },
            backup_identity={**prior_identity, "inode": prior_identity["inode"] + 1},
        )
        valid["production_git_head"] = "0" * 40
        with self.assertRaisesRegex(bridge.BridgeConfigError, "report rejected"):
            bridge._parse_child_report(bridge.canonical_bytes(valid), spec)

    def test_candidate_and_installed_operator_identities_are_both_supported(self) -> None:
        candidate = self.load(self.value())
        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(bridge, "git_identity", side_effect=self.fake_git_identity),
        ):
            self.assertEqual(
                bridge.verify_execution_identity(candidate), self.operator_tool
            )
        installed_value = self.value(
            operator_repository=self.repo,
            operator_git=INTEGRATED_GIT,
            production_git=INTEGRATED_GIT,
        )
        installed = self.load(installed_value)
        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.production_tool),
            mock.patch.object(
                bridge, "git_identity", return_value=dict(INTEGRATED_GIT)
            ),
        ):
            self.assertEqual(
                bridge.verify_execution_identity(installed), self.production_tool
            )

    def test_wrong_executed_tool_and_dirty_operator_fail_before_guard(self) -> None:
        spec = self.load(self.value())
        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.production_tool),
            self.assertRaisesRegex(bridge.BridgeConfigError, "tool identity"),
        ):
            bridge.verify_execution_identity(spec)
        with (
            mock.patch.object(bridge, "current_tool_path", return_value=self.operator_tool),
            mock.patch.object(
                bridge,
                "git_identity",
                side_effect=bridge.BridgeConfigError("checkout identity rejected"),
            ),
            self.assertRaisesRegex(bridge.BridgeConfigError, "checkout identity"),
        ):
            bridge.verify_execution_identity(spec)

    def test_operation_is_bound_before_sudo(self) -> None:
        value = self.value()
        raw = bridge.canonical_bytes(value)
        self.apply_spec_path.write_bytes(raw)
        self.apply_spec_path.chmod(0o600)
        with self.assertRaisesRegex(bridge.BridgeConfigError, "operation differs"):
            bridge._execute_outer(self.apply_spec_path, digest(raw), "restore")

    def test_root_child_runs_guard_as_exact_uid1000_no_shell_argv(self) -> None:
        guard_file = self.root / "guard.py"
        guard_file.write_text("pass\n")
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        lease_env = {
            "CHAT_MEMORY_LEASE_ID": LEASE["lease_id"],
            "CODEX_TASK_ID": LEASE["task_id"],
            "CODEX_THREAD_ID": LEASE["thread_id"],
        }
        with (
            mock.patch.object(bridge, "LEASE_GUARD", guard_file),
            mock.patch.object(bridge, "_load_lease_event"),
            mock.patch.object(bridge, "_run", return_value=completed) as run,
            mock.patch.dict(os.environ, lease_env, clear=False),
        ):
            bridge.require_guard_as_uid1000(LEASE)
        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            [
                bridge.SUDO,
                "--non-interactive",
                "--user",
                f"#{self.uid}",
                "--preserve-env=CHAT_MEMORY_LEASE_ID,CODEX_TASK_ID,CODEX_THREAD_ID",
                bridge.PYTHON,
                str(guard_file),
                "--operation",
                "production-write",
                "--worktree",
                str(self.repo),
            ],
        )

    def test_guard_rejects_uid1000_account_mapping_drift(self) -> None:
        account = mock.Mock(pw_uid=self.uid, pw_gid=self.gid + 1)
        guard_file = self.root / "guard-mapping.py"
        guard_file.write_text("pass\n")
        with (
            mock.patch.object(bridge, "LEASE_GUARD", guard_file),
            mock.patch.object(bridge.pwd, "getpwuid", return_value=account),
            mock.patch.object(bridge, "_load_lease_event"),
            mock.patch.dict(
                os.environ,
                {
                    "CHAT_MEMORY_LEASE_ID": LEASE["lease_id"],
                    "CODEX_TASK_ID": LEASE["task_id"],
                    "CODEX_THREAD_ID": LEASE["thread_id"],
                },
                clear=False,
            ),
            self.assertRaisesRegex(bridge.BridgeConfigError, "mapping rejected"),
        ):
            bridge.require_guard_as_uid1000(LEASE)

    def test_outer_rejects_uid_before_loading_spec(self) -> None:
        with mock.patch.object(bridge.os, "getuid", return_value=self.uid + 1), self.assertRaisesRegex(bridge.BridgeConfigError, "outer uid"):
            bridge._execute_outer(self.root / "missing", "e" * 64, "apply")

    def test_source_declares_sudo_assumption_and_no_service_action(self) -> None:
        source = pathlib.Path(bridge.__file__).read_text()
        self.assertIn("not described as a security", source)
        self.assertIn("NOPASSWD root-equivalent", source)
        self.assertIn("shell=False", source)
        self.assertNotIn("systemctl", source)
        self.assertNotIn("service restart", source.lower())
        self.assertEqual(bridge.PYTHON, "/usr/bin/python3.12")


if __name__ == "__main__":
    unittest.main()
