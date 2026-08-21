from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_legacy_memory_retirement_security_boundary import BoundaryError, verify


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ops/security/20260821_legacy_memory_retirement_security_boundary_v1/package.json"


class LegacyMemoryRetirementSecurityBoundaryTests(unittest.TestCase):
    def test_exact_candidate_package_passes(self) -> None:
        result = verify(PACKAGE)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["application_role"], "brains_app")
        self.assertEqual(result["inspection_role"], "lifeswitch_retirement_auditor")
        self.assertFalse(result["production_changed"])

    def _copy_package(self) -> tuple[tempfile.TemporaryDirectory[str], Path, dict[str, object]]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        copied_root = root / "repo"
        for entry in json.loads(PACKAGE.read_text(encoding="utf-8"))["inputs"].values():
            source = ROOT / entry["path"]
            target = copied_root / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        copied = copied_root / "ops/security/20260821_legacy_memory_retirement_security_boundary_v1/package.json"
        copied.parent.mkdir(parents=True, exist_ok=True)
        document = copy.deepcopy(json.loads(PACKAGE.read_text(encoding="utf-8")))
        copied.write_text(json.dumps(document), encoding="utf-8")
        return temporary, copied, document

    def test_policy_widening_is_rejected_even_when_rehashed(self) -> None:
        temporary, copied, document = self._copy_package()
        try:
            relative = document["inputs"]["recovery_role_identity_policy"]["path"]
            policy_path = copied.parents[3] / relative
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["Statement"][0]["Action"].append("secretsmanager:PutSecretValue")
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            document["inputs"]["recovery_role_identity_policy"]["sha256"] = hashlib.sha256(policy_path.read_bytes()).hexdigest()
            copied.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(BoundaryError, "identity_policy_invalid"):
                verify(copied)
        finally:
            temporary.cleanup()

    def test_database_base_table_grant_is_rejected_even_when_rehashed(self) -> None:
        temporary, copied, document = self._copy_package()
        try:
            relative = document["inputs"]["database_provision"]["path"]
            sql_path = copied.parents[3] / relative
            sql_path.write_text(
                sql_path.read_text(encoding="utf-8")
                + "\nGRANT SELECT ON public.chat_log TO lifeswitch_retirement_auditor;\n",
                encoding="utf-8",
            )
            document["inputs"]["database_provision"]["sha256"] = hashlib.sha256(sql_path.read_bytes()).hexdigest()
            copied.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(BoundaryError, "database_provision_forbidden"):
                verify(copied)
        finally:
            temporary.cleanup()

    def test_application_and_inspection_roles_cannot_be_substituted(self) -> None:
        temporary, copied, document = self._copy_package()
        try:
            document["identities"]["application_role"] = "sage"
            copied.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(BoundaryError, "identity_split_invalid"):
                verify(copied)
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
