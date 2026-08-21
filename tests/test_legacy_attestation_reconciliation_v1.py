
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from scripts.prepare_legacy_memory_attestation_reconciliation import (
    ARCHIVE_VERSION,
    EXPECTED_COUNTS,
    EXPECTED_HASHES,
    ReconciliationContractError,
    canonical_bytes,
    decode_key,
    encrypt_quarantine,
    prepare_output,
    validate_dsn,
)


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "ops/migrations/20260820_legacy_attestation_reconciliation_v1"


class LegacyAttestationReconciliationV1Tests(unittest.TestCase):
    def test_package_binds_exact_inputs_and_preserves_source(self) -> None:
        package = json.loads((PACKAGE_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["status"], "candidate_not_applied")
        self.assertEqual(package["expected"]["counts"], {**EXPECTED_COUNTS, "destination_conflicts": 0})
        self.assertEqual(package["expected"]["sha256"]["source"], EXPECTED_HASHES["source"])
        self.assertFalse(package["execution"]["migration_deletes_source_rows"])
        self.assertFalse(package["execution"]["schema_retirement_authorized"])
        for record in package["inputs"].values():
            path = ROOT / record["path"] if record["path"].startswith("scripts/") else PACKAGE_ROOT / record["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), record["sha256"])

    def test_forward_is_exactly_bounded_and_non_destructive(self) -> None:
        sql = (PACKAGE_ROOT / "forward.pgsql").read_text(encoding="utf-8").lower()
        self.assertIn("source_rows <> 190", sql)
        self.assertIn("eligible_rows <> 32", sql)
        self.assertIn("orphan_rows <> 158", sql)
        self.assertIn("destination_conflicts <> 0", sql)
        self.assertIn("insert into chat_integrity.assistant_transcript_attestation_v1", sql)
        for forbidden in ("drop schema", "truncate", "delete from memory."):
            self.assertNotIn(forbidden, sql)

    def test_quarantine_uses_authenticated_encryption_and_roundtrips(self) -> None:
        key = bytes(range(32))
        plaintext = canonical_bytes({"rows": [{"answer_id": "a"}]})
        encoded = encrypt_quarantine(plaintext, key)
        envelope = json.loads(encoded)
        self.assertEqual(envelope["algorithm"], "AES-256-GCM")
        nonce = base64.urlsafe_b64decode(envelope["nonce"])
        ciphertext = base64.urlsafe_b64decode(envelope["ciphertext"])
        self.assertEqual(AESGCM(key).decrypt(nonce, ciphertext, ARCHIVE_VERSION.encode("ascii")), plaintext)

    def test_key_dsn_and_output_root_fail_closed(self) -> None:
        with self.assertRaises(ReconciliationContractError):
            decode_key(base64.urlsafe_b64encode(b"short").decode("ascii"))
        with self.assertRaises(ReconciliationContractError):
            validate_dsn("postgresql://sage:" + "placeholder" + "@example.com:5432/memory")
        validate_dsn("postgresql://sage:" + "placeholder" + "@127.0.0.1:5432/memory")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            output = prepare_output(root, "run-20260820")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
