#!/usr/bin/env python3
from __future__ import annotations

"""Create encrypted, hash-bound evidence for legacy attestation reconciliation."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import asyncpg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SCHEMA_VERSION = "seebx-legacy-attestation-reconciliation-receipt-v1"
ARCHIVE_VERSION = "seebx-legacy-attestation-encrypted-quarantine-v1"
EXPECTED_DATABASE = "memory"
EXPECTED_ROLE = "sage"
EXPECTED_PORT = 5432
EXPECTED_COUNTS = {"source": 190, "eligible": 32, "quarantine": 158}
EXPECTED_HASHES = {
    "source": "c07967305707d83e258bd441939359b472b95bb414eeb739a7edd99a5f657831",
    "eligible": "4b3917cac82fc5a4522a2bb8e0a9296651daedc3d94fb6b02840d9b5f6b64678",
    "quarantine": "38f20b7df5cbd1d3a8d413825eb76a6a7a682f0e78b491e1aa09a315113d90e1",
}
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,47}$")
ATTESTATION_COLUMNS = (
    "answer_id", "owner_user_id", "thread_id", "chat_log_id",
    "request_id_sha256", "conversation_snapshot_sha256", "trusted_plan_sha256",
    "provider_request_sha256", "provider_response_sha256", "provider_response_id",
    "output_kind", "assistant_text_sha256", "attestation_sha256", "created_at",
)


class ReconciliationContractError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value) if value is not None and not isinstance(value, (str, int, float, bool)) else value


def normalize_rows(rows: list[Any]) -> list[dict[str, Any]]:
    normalized = [
        {column: json_value(row[column]) for column in ATTESTATION_COLUMNS}
        for row in rows
    ]
    return sorted(normalized, key=lambda row: row["answer_id"])


def decode_key(raw: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception as error:
        raise ReconciliationContractError("quarantine_key_invalid") from error
    if len(key) != 32:
        raise ReconciliationContractError("quarantine_key_must_be_32_bytes")
    return key


def encrypt_quarantine(plaintext: bytes, key: bytes) -> bytes:
    nonce = os.urandom(12)
    aad = ARCHIVE_VERSION.encode("ascii")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    envelope = {
        "schema_version": ARCHIVE_VERSION,
        "algorithm": "AES-256-GCM",
        "aad": base64.urlsafe_b64encode(aad).decode("ascii"),
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }
    encoded = canonical_bytes(envelope)
    decoded = AESGCM(key).decrypt(nonce, ciphertext, aad)
    if decoded != plaintext:
        raise ReconciliationContractError("quarantine_encryption_roundtrip_failed")
    return encoded


def atomic_write(path: Path, value: bytes) -> None:
    temporary = path.with_name(path.name + ".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def prepare_output(root: Path, run_id: str) -> Path:
    if RUN_ID.fullmatch(run_id) is None:
        raise ReconciliationContractError("run_id_invalid")
    metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReconciliationContractError("output_root_invalid")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ReconciliationContractError("output_root_permissions_invalid")
    output = root / run_id
    output.mkdir(mode=0o700, exist_ok=False)
    return output


def validate_dsn(dsn: str) -> None:
    try:
        parsed = urlsplit(dsn)
        port = parsed.port or 5432
    except ValueError as error:
        raise ReconciliationContractError("dsn_invalid") from error
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ReconciliationContractError("dsn_scheme_invalid")
    if (parsed.hostname or "") not in {"127.0.0.1", "localhost", "::1"}:
        raise ReconciliationContractError("dsn_host_must_be_local")
    if port != EXPECTED_PORT or unquote(parsed.username or "") != EXPECTED_ROLE or unquote(parsed.path.removeprefix("/")) != EXPECTED_DATABASE:
        raise ReconciliationContractError("dsn_target_identity_mismatch")


async def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    dsn = (os.getenv(arguments.dsn_env) or "").strip()
    raw_key = (os.getenv(arguments.key_env) or "").strip()
    if not dsn:
        raise ReconciliationContractError("dsn_environment_missing")
    if not raw_key:
        raise ReconciliationContractError("quarantine_key_environment_missing")
    validate_dsn(dsn)
    key = decode_key(raw_key)
    connection = await asyncpg.connect(dsn, command_timeout=30)
    try:
        transaction = connection.transaction(isolation="repeatable_read", readonly=True)
        await transaction.start()
        try:
            identity = await connection.fetchrow("select current_database() as database, current_user as role")
            if identity["database"] != EXPECTED_DATABASE or identity["role"] != EXPECTED_ROLE:
                raise ReconciliationContractError("database_identity_mismatch")
            rows = await connection.fetch(
                """
                SELECT a.*,
                       (l.id IS NOT NULL) AS eligible
                FROM memory.assistant_transcript_attestation_v1 AS a
                LEFT JOIN public.chat_log AS l
                  ON l.id = a.answer_id
                 AND l.id = a.chat_log_id
                 AND l.owner_user_id = a.owner_user_id
                 AND l.thread_id = a.thread_id
                 AND encode(public.digest(l.text, 'sha256'), 'hex') = a.assistant_text_sha256
                ORDER BY a.answer_id
                """
            )
        finally:
            await transaction.rollback()
    finally:
        await connection.close()

    source = normalize_rows(list(rows))
    eligible = normalize_rows([row for row in rows if row["eligible"]])
    quarantine = normalize_rows([row for row in rows if not row["eligible"]])
    counts = {"source": len(source), "eligible": len(eligible), "quarantine": len(quarantine)}
    hashes = {
        "source": sha256_bytes(canonical_bytes(source)),
        "eligible": sha256_bytes(canonical_bytes(eligible)),
        "quarantine": sha256_bytes(canonical_bytes(quarantine)),
    }
    if counts != EXPECTED_COUNTS:
        raise ReconciliationContractError("attestation_counts_drifted")
    if hashes != EXPECTED_HASHES:
        raise ReconciliationContractError("attestation_hashes_drifted")

    output = prepare_output(arguments.output_root, arguments.run_id)
    archive_plaintext = canonical_bytes({"schema_version": ARCHIVE_VERSION, "rows": quarantine})
    archive = encrypt_quarantine(archive_plaintext, key)
    archive_path = output / "legacy-attestation-quarantine.aesgcm.json"
    atomic_write(archive_path, archive)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "run_id": arguments.run_id,
        "source": {"database": EXPECTED_DATABASE, "role": EXPECTED_ROLE},
        "classification": {"counts": counts, "sha256": hashes},
        "quarantine": {
            "path": archive_path.name,
            "algorithm": "AES-256-GCM",
            "plaintext_sha256": sha256_bytes(archive_plaintext),
            "ciphertext_sha256": sha256_bytes(archive),
            "mode": "0600",
            "key_persisted": False,
        },
    }
    receipt_bytes = canonical_bytes(receipt)
    receipt_path = output / "reconciliation-receipt.json"
    atomic_write(receipt_path, receipt_bytes)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "output_directory": str(output),
        "receipt_sha256": sha256_bytes(receipt_bytes),
        "quarantine_ciphertext_sha256": receipt["quarantine"]["ciphertext_sha256"],
        "classification": receipt["classification"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn-env", default="LEGACY_MEMORY_ADMIN_DSN")
    parser.add_argument("--key-env", default="LEGACY_ATTESTATION_QUARANTINE_KEY_B64")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(execute(arguments))
        code = 0
    except ReconciliationContractError as error:
        result = {"schema_version": SCHEMA_VERSION, "status": "error", "error": "reconciliation_contract_error", "reason": str(error)}
        code = 2
    except Exception:
        result = {"schema_version": SCHEMA_VERSION, "status": "error", "error": "reconciliation_unexpected_error"}
        code = 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
