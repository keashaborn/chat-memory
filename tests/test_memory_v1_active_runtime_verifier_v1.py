from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.memory_v1_active_runtime_verifier_v1 import (
    BINDING_CONTRACT,
    RuntimeVerificationError,
    build_binding,
    decode_json,
    git_identity,
    parse_environment_file,
    parse_environment_value,
    sha256_bytes,
    validate_manifest,
    verify_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "ops/systemd/memory-v1-active-runtime-manifest-v1.json"


class ActiveRuntimeVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.manifest_sha256 = sha256_bytes(MANIFEST.read_bytes())
        source_hashes = {
            item["path"]: item["sha256"]
            for item in self.manifest["source_components"]
        }
        unit_hashes: dict[str, str] = {}
        units: dict[str, dict[str, str]] = {}
        for item in self.manifest["systemd_units"]:
            if "source_path" in item:
                source_hashes[item["source_path"]] = item["source_sha256"]
            unit_hashes[item["name"]] = item["installed_sha256"]
            units[item["name"]] = copy.deepcopy(
                item["expected"]["installed_inactive"]
            )
            units[item["name"]]["fragment_path"] = (
                f"/etc/systemd/system/{item['name']}"
            )
        function_hashes = {
            item["signature"]: item["sha256"]
            for item in self.manifest["catalog_contract"]["functions"]
        }
        relation_states = {
            name: {"forced_rls": True, "rls": True}
            for name in self.manifest["catalog_contract"]["forced_rls_relations"]
        }
        self.snapshot = {
            "catalog": {
                "actor_role": copy.deepcopy(
                    self.manifest["catalog_contract"]["actor_role"]
                ),
                "function_sha256": function_hashes,
                "relations": relation_states,
            },
            "catalog_environment": {
                "sha256": "e" * 64,
                "state": "root_owned_0600_regular_single_link",
                "size": 1024,
            },
            "config": {
                "sha256": "d" * 64,
                "state": "root_owned_0600_regular_single_link",
                "size": 512,
            },
            "discovered_memory_units": copy.deepcopy(
                self.manifest["exact_installed_memory_unit_set"]
            ),
            "installed_unit_sha256": unit_hashes,
            "openai_sdk_version": self.manifest["verification"][
                "openai_sdk_version"
            ],
            "python_executable_sha256": "a" * 64,
            "repository": {
                "commit": "b" * 40,
                "tree": "c" * 40,
                "tracked_clean": True,
            },
            "sentinels": {
                item["path"]: item["expected"]["installed_inactive"]
                for item in self.manifest["timer_sentinels"]
            },
            "source_sha256": source_hashes,
            "units": units,
        }
        self.binding = {
            "catalog_environment_sha256": self.snapshot[
                "catalog_environment"
            ]["sha256"],
            "catalog_function_sha256": function_hashes,
            "contract_version": BINDING_CONTRACT,
            "manifest_sha256": self.manifest_sha256,
            "openai_sdk_version": self.snapshot["openai_sdk_version"],
            "phase": "installed_inactive",
            "python_executable_sha256": self.snapshot[
                "python_executable_sha256"
            ],
            "repository_commit": self.snapshot["repository"]["commit"],
            "repository_tree": self.snapshot["repository"]["tree"],
            "runtime_config_sha256": self.snapshot["config"]["sha256"],
            "source_sha256": source_hashes,
            "unit_sha256": unit_hashes,
        }

    def verify(self, snapshot: dict, binding: dict | None = None) -> dict:
        return verify_snapshot(
            self.manifest,
            snapshot,
            manifest_sha256=self.manifest_sha256,
            phase="installed_inactive",
            binding=self.binding if binding is None else binding,
        )

    def test_exact_complete_snapshot_and_binding_pass(self) -> None:
        report = self.verify(self.snapshot)
        self.assertTrue(report["runtime_matches_manifest"])
        self.assertTrue(report["catalog_matches_manifest"])
        self.assertTrue(report["successor_exclusivity_verified"])
        self.assertEqual(report["installed_memory_unit_count"], 39)
        self.assertEqual(report["blocker_count"], 3)
        self.assertEqual(report["provider_calls"], 0)
        self.assertIsNotNone(report["binding_sha256"])

    def test_binding_generation_is_exact_and_content_free(self) -> None:
        generated = build_binding(
            self.manifest,
            self.snapshot,
            manifest_sha256=self.manifest_sha256,
            phase="installed_inactive",
        )
        self.assertEqual(generated, self.binding)
        self.assertNotIn("POSTGRES_DSN", generated)
        self.assertNotIn("runtime_config", generated)

    def test_unlisted_or_missing_installed_memory_unit_fails_closed(self) -> None:
        extra = copy.deepcopy(self.snapshot)
        extra["discovered_memory_units"].append("memory-v1-unknown.timer")
        with self.assertRaises(RuntimeVerificationError):
            self.verify(extra)

        missing = copy.deepcopy(self.snapshot)
        missing["discovered_memory_units"].pop()
        with self.assertRaises(RuntimeVerificationError):
            self.verify(missing)

    def test_state_source_unit_and_binding_drift_fail_closed(self) -> None:
        openai = copy.deepcopy(self.snapshot)
        openai["units"]["memory-v1-openai-extraction.timer"][
            "active_state"
        ] = "active"
        with self.assertRaises(RuntimeVerificationError):
            self.verify(openai)

        source = copy.deepcopy(self.snapshot)
        first_source = next(iter(source["source_sha256"]))
        source["source_sha256"][first_source] = "e" * 64
        with self.assertRaises(RuntimeVerificationError):
            self.verify(source)

        installed = copy.deepcopy(self.snapshot)
        first_unit = next(iter(installed["installed_unit_sha256"]))
        installed["installed_unit_sha256"][first_unit] = "f" * 64
        with self.assertRaises(RuntimeVerificationError):
            self.verify(installed)

        wrong_binding = copy.deepcopy(self.binding)
        wrong_binding["repository_commit"] = "e" * 40
        with self.assertRaises(RuntimeVerificationError):
            self.verify(self.snapshot, binding=wrong_binding)

        wrong_catalog_environment = copy.deepcopy(self.binding)
        wrong_catalog_environment["catalog_environment_sha256"] = "f" * 64
        with self.assertRaises(RuntimeVerificationError):
            self.verify(self.snapshot, binding=wrong_catalog_environment)

    def test_catalog_function_role_and_rls_drift_fail_closed(self) -> None:
        function = copy.deepcopy(self.snapshot)
        first_function = next(iter(function["catalog"]["function_sha256"]))
        function["catalog"]["function_sha256"][first_function] = "e" * 64
        with self.assertRaises(RuntimeVerificationError):
            self.verify(function)

        role = copy.deepcopy(self.snapshot)
        role["catalog"]["actor_role"]["bypass_rls"] = True
        with self.assertRaises(RuntimeVerificationError):
            self.verify(role)

        relation = copy.deepcopy(self.snapshot)
        first_relation = next(iter(relation["catalog"]["relations"]))
        relation["catalog"]["relations"][first_relation]["forced_rls"] = False
        with self.assertRaises(RuntimeVerificationError):
            self.verify(relation)

    def test_binding_is_mandatory_and_covers_complete_hash_maps(self) -> None:
        with self.assertRaises(RuntimeVerificationError):
            verify_snapshot(
                self.manifest,
                self.snapshot,
                manifest_sha256=self.manifest_sha256,
                phase="installed_inactive",
                binding=None,
            )
        wrong_source = copy.deepcopy(self.binding)
        wrong_source["source_sha256"] = dict(wrong_source["source_sha256"])
        wrong_source["source_sha256"].pop(next(iter(wrong_source["source_sha256"])))
        with self.assertRaises(RuntimeVerificationError):
            self.verify(self.snapshot, binding=wrong_source)

    def test_manifest_rejects_hidden_required_blocker_or_handoff(self) -> None:
        blocker = copy.deepcopy(self.manifest)
        blocker["blockers"] = blocker["blockers"][1:]
        with self.assertRaises(RuntimeVerificationError):
            validate_manifest(blocker)

        handoff = copy.deepcopy(self.manifest)
        handoff["runtime_handoffs"] = handoff["runtime_handoffs"][1:]
        with self.assertRaises(RuntimeVerificationError):
            validate_manifest(handoff)

    def test_environment_parser_is_bounded_and_does_not_expand(self) -> None:
        parsed = parse_environment_file(
            b'# comment\nPOSTGRES_DSN="postgresql://example/db"\nOTHER=value\n'
        )
        self.assertEqual(parsed["POSTGRES_DSN"], "postgresql://example/db")
        self.assertEqual(parsed["OTHER"], "value")
        with self.assertRaises(RuntimeVerificationError):
            parse_environment_file(b"POSTGRES_DSN=one\nPOSTGRES_DSN=two\n")
        with self.assertRaises(RuntimeVerificationError):
            parse_environment_file(b"POSTGRES_DSN=$UNEXPANDED value\n")

    def test_catalog_environment_reads_only_exact_dsn_assignment(self) -> None:
        value = (
            b"OTHER=unquoted value with spaces\n"
            b'POSTGRES_DSN="postgresql://example/db"\n'
            b"ANOTHER=$UNEXPANDED value\n"
        )
        self.assertEqual(
            parse_environment_value(value, key="POSTGRES_DSN"),
            "postgresql://example/db",
        )
        with self.assertRaises(RuntimeVerificationError):
            parse_environment_value(
                value + b"POSTGRES_DSN=postgresql://duplicate/db\n",
                key="POSTGRES_DSN",
            )
        with self.assertRaises(RuntimeVerificationError):
            parse_environment_value(value, key="MISSING_DSN")

    def test_git_identity_uses_exact_command_scoped_safe_directory(self) -> None:
        root = Path("/opt/chat-memory")
        responses = iter(["b" * 40, "c" * 40, ""])
        calls: list[list[str]] = []

        def fixed(arguments: list[str], **_kwargs: object) -> str:
            calls.append(arguments)
            return next(responses)

        with mock.patch(
            "scripts.memory_v1_active_runtime_verifier_v1.run_fixed",
            side_effect=fixed,
        ):
            observed = git_identity(root)

        prefix = [
            "/usr/bin/git",
            "-c",
            "safe.directory=/opt/chat-memory",
            "-C",
            "/opt/chat-memory",
        ]
        self.assertEqual(
            calls,
            [
                [*prefix, "rev-parse", "HEAD"],
                [*prefix, "rev-parse", "HEAD^{tree}"],
                [*prefix, "status", "--porcelain=v1", "--untracked-files=all"],
            ],
        )
        self.assertEqual(observed["commit"], "b" * 40)
        self.assertTrue(observed["tracked_clean"])

    def test_duplicate_json_and_symlink_inputs_are_rejected(self) -> None:
        with self.assertRaises(RuntimeVerificationError):
            decode_json(b'{"a":1,"a":2}', label="fixture")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(target)
            from scripts.memory_v1_active_runtime_verifier_v1 import read_regular

            with self.assertRaises(RuntimeVerificationError):
                read_regular(link, label="fixture")


if __name__ == "__main__":
    unittest.main()
