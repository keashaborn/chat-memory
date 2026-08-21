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
    ARCHIVE_VERSION, EXPECTED_COUNTS, EXPECTED_HASHES, ReconciliationContractError,
    canonical_bytes, decode_key, encrypt_quarantine, load_key_custody_receipt,
    prepare_output, validate_dsn,
)


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "ops/migrations/20260820_legacy_attestation_reconciliation_v1"


def custody_document(key: bytes) -> dict[str, object]:
    return {
        "schema_version": "seebx-legacy-attestation-key-custody-receipt-v1",
        "status": "pass", "provider": "aws_secrets_manager", "region": "us-east-2",
        "secret_arn": "arn:aws:secretsmanager:us-east-2:339712834334:secret:lifeswitch/seebx/legacy-attestation-quarantine-v1-a1b2c3",
        "secret_version_id": "12345678-1234-1234-1234-123456789012",
        "retrieval_principal_arn": "arn:aws:iam::339712834334:role/LifeSwitchLegacyMemoryRecoveryV1",
        "key_algorithm": "AES-256-GCM", "key_bytes": 32,
        "key_fingerprint_sha256": hashlib.sha256(key).hexdigest(),
        "recovery_tested_at_utc": "2026-08-21T12:00:00Z",
        "secret_value_persisted_in_receipt": False,
    }


class LegacyAttestationReconciliationV1Tests(unittest.TestCase):
    def test_package_binds_exact_inputs_and_preserves_source(self) -> None:
        package = json.loads((PACKAGE_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["status"], "candidate_not_applied")
        self.assertEqual(package["expected"]["counts"], {**EXPECTED_COUNTS, "destination_conflicts": 0})
        self.assertEqual(package["expected"]["sha256"]["source"], EXPECTED_HASHES["source"])
        self.assertFalse(package["execution"]["migration_deletes_source_rows"])
        self.assertFalse(package["execution"]["schema_retirement_authorized"])
        self.assertEqual(package["key_custody"]["provider"], "aws_secrets_manager")
        for record in package["inputs"].values():
            path = ROOT / record["path"] if record["path"].startswith(("scripts/", "ops/")) else PACKAGE_ROOT / record["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), record["sha256"])

    def test_forward_is_exactly_bounded_and_non_destructive(self) -> None:
        sql = (PACKAGE_ROOT / "forward.pgsql").read_text(encoding="utf-8").lower()
        for text in ("source_rows <> 190", "eligible_rows <> 32", "orphan_rows <> 158", "destination_conflicts <> 0", "legacy_attestation_key_custody_receipt_sha256"):
            self.assertIn(text, sql)
        self.assertIn("insert into chat_integrity.assistant_transcript_attestation_v1", sql)
        for forbidden in ("drop schema", "truncate", "delete from memory."):
            self.assertNotIn(forbidden, sql)

    def test_quarantine_uses_authenticated_encryption_and_roundtrips(self) -> None:
        key = bytes(range(32))
        plaintext = canonical_bytes({"rows": [{"answer_id": "a"}]})
        encoded = encrypt_quarantine(plaintext, key)
        envelope = json.loads(encoded)
        nonce = base64.urlsafe_b64decode(envelope["nonce"])
        ciphertext = base64.urlsafe_b64decode(envelope["ciphertext"])
        self.assertEqual(AESGCM(key).decrypt(nonce, ciphertext, ARCHIVE_VERSION.encode("ascii")), plaintext)

    def test_key_custody_receipt_binds_exact_recoverable_key_without_storing_it(self) -> None:
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "custody.json"
            raw = canonical_bytes(custody_document(key))
            path.write_bytes(raw)
            path.chmod(0o600)
            document, receipt_sha = load_key_custody_receipt(path, key)
            self.assertEqual(receipt_sha, hashlib.sha256(raw).hexdigest())
            self.assertFalse(document["secret_value_persisted_in_receipt"])
            self.assertNotIn(base64.urlsafe_b64encode(key).decode("ascii"), raw.decode("utf-8"))
            document["key_fingerprint_sha256"] = "0" * 64
            path.write_bytes(canonical_bytes(document))
            path.chmod(0o600)
            with self.assertRaisesRegex(ReconciliationContractError, "fingerprint_mismatch"):
                load_key_custody_receipt(path, key)

    def test_key_dsn_and_output_root_fail_closed(self) -> None:
        with self.assertRaises(ReconciliationContractError):
            decode_key(base64.urlsafe_b64encode(b"short").decode("ascii"))
        with self.assertRaises(ReconciliationContractError):
            validate_dsn("postgresql://sage:placeholder@example.com:5432/memory")
        validate_dsn("postgresql://sage:placeholder@127.0.0.1:5432/memory")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            output = prepare_output(root, "run-20260820")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
