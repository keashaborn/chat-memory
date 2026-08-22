from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_lifeswitch_forms_disposable_v1.py"
SPEC = importlib.util.spec_from_file_location("forms_disposable_v1", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def encryption_evidence(*, encrypted: bool = True, observed_at: str) -> dict[str, object]:
    return {
        "schema_version": module.ENCRYPTION_SCHEMA_VERSION,
        "observed_at": observed_at,
        "region": module.AWS_REGION,
        "caller_identity": {
            "Account": module.AWS_ACCOUNT_ID,
            "Arn": (
                "arn:aws:sts::339712834334:assumed-role/"
                "AWSReservedSSO_LifeSwitchAdministrator/example"
            ),
            "UserId": "example",
        },
        "describe_instances": {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": module.INSTANCE_ID,
                            "State": {"Name": "running"},
                            "RootDeviceName": "/dev/sda1",
                            "BlockDeviceMappings": [
                                {
                                    "DeviceName": "/dev/sda1",
                                    "Ebs": {"VolumeId": "vol-0123456789abcdef0"},
                                }
                            ],
                        }
                    ]
                }
            ]
        },
        "describe_volumes": {
            "Volumes": [
                {
                    "VolumeId": "vol-0123456789abcdef0",
                    "Encrypted": encrypted,
                    "State": "in-use",
                    "Attachments": [
                        {
                            "InstanceId": module.INSTANCE_ID,
                            "State": "attached",
                        }
                    ],
                }
            ]
        },
    }


class LifeSwitchFormsDisposableV1Tests(unittest.TestCase):
    def test_run_id_and_target_database_are_bounded(self) -> None:
        self.assertEqual(
            module.target_database("forms-20260822t1200z"),
            "lifeswitch_forms_verify_forms_20260822t1200z",
        )
        for invalid in ("short", "../escape", "UPPERCASE01", "a" * 40):
            with self.subTest(invalid=invalid):
                with self.assertRaises(module.FormsVerificationContractError):
                    module.target_database(invalid)

    def test_encryption_evidence_binds_account_instance_root_volume_and_filesystem(self) -> None:
        now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "evidence.json"
            path.write_text(
                json.dumps(encryption_evidence(observed_at=now.isoformat())),
                encoding="utf-8",
            )
            with patch.object(module, "_require_private_regular_file"):
                result = module.validate_encryption_evidence(
                    path,
                    now=now,
                    filesystem={
                        "target": "/",
                        "source": "/dev/root",
                        "fstype": "ext4",
                    },
                )
        self.assertTrue(result["volume_encrypted"])
        self.assertEqual(result["volume_id"], "vol-0123456789abcdef0")
        self.assertEqual(result["filesystem"]["target"], "/")

    def test_encryption_evidence_fails_closed_when_unencrypted_or_stale(self) -> None:
        now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
        cases = (
            encryption_evidence(encrypted=False, observed_at=now.isoformat()),
            encryption_evidence(
                observed_at=(now - timedelta(hours=25)).isoformat()
            ),
        )
        for evidence in cases:
            with self.subTest(evidence=evidence):
                with tempfile.TemporaryDirectory() as raw:
                    path = Path(raw) / "evidence.json"
                    path.write_text(json.dumps(evidence), encoding="utf-8")
                    with patch.object(module, "_require_private_regular_file"):
                        with self.assertRaises(module.FormsVerificationContractError):
                            module.validate_encryption_evidence(
                                path,
                                now=now,
                                filesystem={
                                    "target": "/",
                                    "source": "/dev/root",
                                    "fstype": "ext4",
                                },
                            )

    def test_destination_dsn_preserves_secret_and_changes_only_database(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "lifeswitch.env"
            path.write_text(
                "LIFESWITCH_POSTGRES_DSN="
                "postgresql://lifeswitch_app_login:secret@127.0.0.1:55433/lifeswitch\n",
                encoding="utf-8",
            )
            with patch.object(module, "CONFIG_PATH", path), patch.object(
                module, "_require_private_regular_file"
            ):
                result = module.read_lifeswitch_dsn("lifeswitch_forms_verify_test")
        self.assertEqual(
            result,
            "postgresql://lifeswitch_app_login:secret@127.0.0.1:55433/"
            "lifeswitch_forms_verify_test",
        )

    def test_platform_source_dsn_is_bound_to_restricted_local_memory_role(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "platform.env"
            path.write_text(
                "POSTGRES_DSN="
                "postgresql://brains_app:secret@127.0.0.1:5432/memory\n",
                encoding="utf-8",
            )
            with patch.object(module, "SOURCE_CONFIG_PATH", path), patch.object(
                module, "_require_private_regular_file"
            ):
                result = module.read_platform_source_dsn()
        self.assertEqual(
            result,
            "postgresql://brains_app:secret@127.0.0.1:5432/memory",
        )

    def test_verifier_never_invokes_a_shell(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("subprocess.call", source)

    def test_main_failure_is_content_free(self) -> None:
        with patch.object(
            module,
            "execute",
            side_effect=module.FormsVerificationExecutionError("sensitive detail"),
        ), patch("builtins.print") as output:
            status = module.main(
                [
                    "--run-id",
                    "forms-20260822t1200z",
                    "--candidate-commit",
                    "a" * 40,
                    "--repository",
                    str(ROOT),
                    "--encryption-evidence",
                    "/root/private.json",
                    "--approval-id",
                    "authorized-test",
                ]
            )
        self.assertEqual(status, 1)
        rendered = json.loads(output.call_args.args[0])
        self.assertEqual(rendered["status"], "failed")
        self.assertNotIn("sensitive detail", json.dumps(rendered))


if __name__ == "__main__":
    unittest.main()
