from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / "seebx_platform_clean_install_verification_v1.json"
if not EVIDENCE.is_file():
    EVIDENCE = ROOT.parent / "ops" / "database" / EVIDENCE.name


class SeebxPlatformCleanInstallEvidenceV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    def test_receipt_is_pass_only_and_non_authoritative(self) -> None:
        self.assertEqual(
            self.evidence["schema_version"],
            "seebx-platform-clean-install-evidence-v1",
        )
        self.assertEqual(self.evidence["status"], "pass")
        self.assertTrue(all(value is False for value in self.evidence["authority"].values()))
        self.assertEqual(
            self.evidence["production_health"]["production_database_writes"],
            0,
        )

    def test_every_binding_is_an_exact_digest_or_commit(self) -> None:
        sha256 = re.compile(r"^[0-9a-f]{64}$")
        commit = re.compile(r"^[0-9a-f]{40}$")
        source = self.evidence["source_candidate"]
        self.assertRegex(source["baseline_generator_commit"], commit)
        self.assertRegex(source["verifier_commit"], commit)
        for section, names in (
            ("disposable_migration", (
                "receipt_sha256",
                "baseline_table_manifest_sha256",
                "forward_table_manifest_sha256",
                "forward_platform_audit_sha256",
                "forward_platform_disposition_sha256",
            )),
            ("clean_baseline", ("receipt_sha256", "canonical_schema_sha256")),
            ("work_runner_install", ("receipt_sha256", "tool_sha256", "catalog_sha256")),
        ):
            for name in names:
                self.assertRegex(self.evidence[section][name], sha256)

    def test_clean_install_counts_and_cleanup_are_exact(self) -> None:
        catalog = self.evidence["work_runner_install"]["catalog"]
        self.assertEqual(
            catalog,
            {
                "schema_count": 9,
                "role_count": 6,
                "membership_count": 1,
                "relation_count": 21,
                "function_count": 17,
                "policy_count": 20,
                "rls_enabled_count": 17,
                "rls_forced_count": 17,
                "extension_count": 2,
                "table_row_count": 0,
            },
        )
        self.assertEqual(self.evidence["clean_baseline"]["product_object_count"], 38)
        self.assertEqual(self.evidence["clean_baseline"]["retained_index_entries"], 39)
        cleanup = self.evidence["cleanup"]
        self.assertEqual(cleanup["status"], "pass")
        self.assertTrue(all(value is True for key, value in cleanup.items() if key != "status"))

    def test_sandbox_is_networkless_rootless_and_unprivileged(self) -> None:
        sandbox = self.evidence["work_runner_install"]["sandbox"]
        self.assertTrue(sandbox["rootless"])
        self.assertEqual(sandbox["network_mode"], "none")
        self.assertEqual(sandbox["published_ports"], 0)
        self.assertTrue(sandbox["read_only_root"])
        self.assertFalse(sandbox["privileged"])
        self.assertTrue(sandbox["no_new_privileges"])
        self.assertEqual(sandbox["capabilities_dropped"], "all")


if __name__ == "__main__":
    unittest.main()
