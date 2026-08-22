from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "verify_platform_clean_install_v1.py"
if not SCRIPT.is_file():
    SCRIPT = ROOT.parent / "scripts" / "verify_platform_clean_install_v1.py"
SPEC = importlib.util.spec_from_file_location("platform_clean_install", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def plan_fixture() -> dict[str, object]:
    schemas = [
        {"name": name, "owner": owner, "source": source}
        for name, owner, source in (
            ("ai_operations", "ai_operations_store_v1", "ai_operations"),
            ("conversation", "seebx_platform_owner_v1", "public"),
            ("conversation_integrity", "seebx_platform_owner_v1", "chat_integrity"),
            ("conversation_private", "seebx_platform_owner_v1", "chat_history_private"),
            ("telemetry", "seebx_platform_owner_v1", "public"),
            ("trusted_web", "seebx_trusted_web_owner_v1", "trusted_web"),
            ("usage", "seebx_platform_owner_v1", "lifeswitch_usage"),
            ("user_settings", "seebx_platform_owner_v1", "user_settings"),
            ("voice", "seebx_platform_owner_v1", "public"),
        )
    ]
    roles = [
        {"name": name, "can_login": name == "seebx_platform_app_v1", "purpose": "test"}
        for name in (
            "ai_operations_store_v1",
            "seebx_platform_app_v1",
            "seebx_platform_owner_v1",
            "seebx_trusted_web_owner_v1",
            "seebx_usage_admin_v1",
            "seebx_usage_writer_v1",
        )
    ]
    targets = []
    for index in range(21):
        targets.append({
            "object_type": "relation",
            "source_identity": f"public.relation_{index}",
            "target_identity": f"conversation.relation_{index}",
            "target_owner": "seebx_platform_owner_v1",
        })
    for index in range(17):
        targets.append({
            "object_type": "function",
            "source_identity": f"public.function_{index}()",
            "target_identity": f"conversation.function_{index}()",
            "target_owner": "seebx_platform_owner_v1",
        })
    return {
        "extensions": {"pgcrypto": "1.3", "plpgsql": "1.0"},
        "target_memberships": [
            {"granted": "seebx_usage_writer_v1", "member": "seebx_platform_app_v1"}
        ],
        "target_roles": roles,
        "target_schemas": schemas,
        "targets": targets,
    }


def baseline_fixture(root: Path, commit: str) -> tuple[Path, str]:
    baseline = root / "baseline"
    baseline.mkdir(mode=0o700)
    artifacts = {}
    for name in module.ARTIFACT_NAMES:
        path = baseline / name
        path.write_text("-- " + name + "\n", encoding="utf-8")
        path.chmod(0o600)
        artifacts[name] = {
            "bytes": path.stat().st_size,
            "mode": "0o600",
            "sha256": module.sha256_file(path),
        }
    receipt = {
        "artifacts": artifacts,
        "candidate_commit": commit,
        "data_copy_authority": False,
        "deletion_authority": False,
        "installation_authority": False,
        "object_plan": plan_fixture(),
        "production_change_authority": False,
        "schema_only": True,
        "schema_version": module.BASELINE_SCHEMA_VERSION,
        "status": "candidate_schema_ready",
    }
    receipt_path = baseline / "platform-clean-baseline-receipt-v1.json"
    receipt_path.write_bytes(module.canonical_bytes(receipt) + b"\n")
    receipt_path.chmod(0o600)
    return baseline, module.sha256_file(receipt_path)


class PlatformCleanInstallV1Tests(unittest.TestCase):
    def test_baseline_is_exact_hash_bound_and_non_authorizing(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as raw:
            baseline, receipt_sha256 = baseline_fixture(Path(raw), commit)
            receipt, artifacts = module.verify_baseline(baseline, receipt_sha256, commit)
            self.assertEqual(receipt["status"], "candidate_schema_ready")
            self.assertEqual(set(artifacts), module.ARTIFACT_NAMES)
            with self.assertRaisesRegex(module.InstallContractError, "sha256_mismatch"):
                module.verify_baseline(baseline, "b" * 64, commit)

    def test_baseline_rejects_group_readable_artifact(self) -> None:
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as raw:
            baseline, receipt_sha256 = baseline_fixture(Path(raw), commit)
            artifact = baseline / "platform-clean-schema-v1.sql"
            artifact.chmod(0o640)
            with self.assertRaisesRegex(module.InstallContractError, "permissions"):
                module.verify_baseline(baseline, receipt_sha256, commit)

    def test_names_are_bounded_and_deterministic(self) -> None:
        names = module.sandbox_names("clean-proof-20260822")
        self.assertEqual(names["container"], "lwr-platform-clean-clean-proof-20260822")
        self.assertNotIn("-", names["data_volume"])
        with self.assertRaisesRegex(module.InstallContractError, "run_id"):
            module.sandbox_names("BAD")

    def test_container_command_is_digest_pinned_and_hardened(self) -> None:
        names = module.sandbox_names("clean-proof-20260822")
        command = module.build_create_command(names=names, baseline=Path("/private/input"))
        self.assertEqual(command[:2], [module.PODMAN, "create"])
        for required in (
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--memory=4g",
            "--cpus=2",
            "--user=999:999",
            module.POSTGRES_IMAGE,
        ):
            self.assertIn(required, command)
        self.assertFalse(any("publish" in item for item in command))
        self.assertIn("--volume=/private/input:/baseline:ro,rprivate", command)
        self.assertEqual(len(module.PODMAN_DEFAULT_CAPABILITIES), 11)
        self.assertIn("sys_chroot", module.PODMAN_DEFAULT_CAPABILITIES)

    def test_catalog_accepts_only_exact_clean_contract(self) -> None:
        plan = plan_fixture()
        roles = sorted(
            ({
                "name": item["name"],
                "can_login": item["can_login"],
                "superuser": False,
                "create_db": False,
                "create_role": False,
                "replication": False,
                "bypass_rls": False,
            } for item in plan["target_roles"]),
            key=lambda item: item["name"],
        )
        relations = [
            {
                "identity": item["target_identity"],
                "owner": item["target_owner"],
                "kind": "r",
                "rls_enabled": index == 0,
                "rls_forced": index == 0,
            }
            for index, item in enumerate(plan["targets"])
            if item["object_type"] == "relation"
        ]
        functions = [
            {
                "identity": item["target_identity"],
                "owner": item["target_owner"],
                "security_definer": False,
                "configuration": "",
            }
            for item in plan["targets"]
            if item["object_type"] == "function"
        ]
        catalog = {
            "schemas": sorted(
                ({"name": item["name"], "owner": item["owner"]} for item in plan["target_schemas"]),
                key=lambda item: item["name"],
            ),
            "roles": roles,
            "memberships": plan["target_memberships"],
            "relations": relations,
            "functions": functions,
            "extensions": plan["extensions"],
            "policies": [{"identity": "conversation.relation_0.owner", "roles": ["seebx_platform_app_v1"]}],
            "public_privilege_count": 0,
            "forbidden_schema_count": 0,
            "forbidden_role_count": 0,
            "table_rows": [{"identity": item["identity"], "rows": 0} for item in relations],
            "expected_rls_enabled": [relations[0]["identity"]],
            "expected_rls_forced": [relations[0]["identity"]],
        }
        summary = module.validate_catalog(catalog, plan)
        self.assertEqual(summary["relation_count"], 21)
        self.assertEqual(summary["function_count"], 17)
        self.assertEqual(summary["table_row_count"], 0)
        catalog["forbidden_role_count"] = 1
        with self.assertRaisesRegex(module.InstallExecutionError, "isolation"):
            module.validate_catalog(catalog, plan)

    def test_catalog_rejects_policy_role_and_rls_drift(self) -> None:
        plan = plan_fixture()
        with self.assertRaisesRegex(module.InstallExecutionError, "policy_role"):
            module.validate_catalog(
                {
                    "schemas": sorted(
                        ({"name": item["name"], "owner": item["owner"]} for item in plan["target_schemas"]),
                        key=lambda item: item["name"],
                    ),
                    "roles": sorted(
                        ({
                            "name": item["name"], "can_login": item["can_login"],
                            "superuser": False, "create_db": False, "create_role": False,
                            "replication": False, "bypass_rls": False,
                        } for item in plan["target_roles"]),
                        key=lambda item: item["name"],
                    ),
                    "memberships": plan["target_memberships"],
                    "relations": [
                        {"identity": item["target_identity"], "owner": item["target_owner"], "kind": "r", "rls_enabled": False, "rls_forced": False}
                        for item in plan["targets"] if item["object_type"] == "relation"
                    ],
                    "functions": [
                        {"identity": item["target_identity"], "owner": item["target_owner"], "security_definer": False, "configuration": ""}
                        for item in plan["targets"] if item["object_type"] == "function"
                    ],
                    "extensions": plan["extensions"],
                    "policies": [{"identity": "conversation.relation_0.owner", "roles": ["sage"]}],
                    "public_privilege_count": 0,
                    "forbidden_schema_count": 0,
                    "forbidden_role_count": 0,
                    "table_rows": [],
                    "expected_rls_enabled": [],
                    "expected_rls_forced": [],
                },
                plan,
            )

    def test_subprocess_execution_never_uses_shell(self) -> None:
        calls = []
        original = module.subprocess.run
        try:
            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                return type("Result", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
            module.subprocess.run = fake_run
            self.assertEqual(module.run_checked(["podman", "version"], label="test"), "ok")
        finally:
            module.subprocess.run = original
        self.assertNotIn("shell", calls[0][1])
        self.assertIs(calls[0][1]["stdin"], module.subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
