from __future__ import annotations

"""Test-only construction of canonical resource-identity ledger bytes."""

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from tools.governed_memory_install.resource_identity import (
    SCHEMA_VERSION,
    ZERO_HEAD,
    ResourceIdentityRecord,
    parse_ledger_bytes,
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def resource_identity_ledger_bytes(
    entries: tuple[Mapping[str, object], ...],
    *,
    binding_sha256: str,
) -> bytes:
    """Serialize caller-supplied synthetic entries with a valid hash chain."""

    lines: list[bytes] = []
    previous_entry_sha256 = ZERO_HEAD
    for sequence, entry in enumerate(entries, start=1):
        payload: dict[str, object] = {
            "binding_sha256": binding_sha256,
            "event": entry["event"],
            "image_id": entry.get("image_id"),
            "image_repo_digest": entry.get("image_repo_digest"),
            "ownership_sha256": entry["ownership_sha256"],
            "resource_labels_sha256": entry.get("resource_labels_sha256"),
            "previous_entry_sha256": previous_entry_sha256,
            "resource_id": entry["resource_id"],
            "resource_kind": entry["resource_kind"],
            "resource_name": entry["resource_name"],
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
        }
        value = dict(payload)
        entry_sha256 = hashlib.sha256(_canonical_json(payload)).hexdigest()
        value["entry_sha256"] = entry_sha256
        lines.append(_canonical_json(value))
        previous_entry_sha256 = entry_sha256
    return b"".join(line + b"\n" for line in lines)


def write_resource_identity_ledger(
    path: Path,
    entries: tuple[Mapping[str, object], ...],
    *,
    binding_sha256: str,
) -> tuple[ResourceIdentityRecord, ...]:
    raw = resource_identity_ledger_bytes(
        entries,
        binding_sha256=binding_sha256,
    )
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return parse_ledger_bytes(raw, expected_binding_sha256=binding_sha256)


def append_resource_identity(
    path: Path,
    *,
    binding_sha256: str,
    event: str,
    resource_kind: str,
    resource_name: str,
    resource_id: str,
    ownership_sha256: str,
    resource_labels_sha256: str | None = None,
    image_id: str | None = None,
    image_repo_digest: str | None = None,
) -> ResourceIdentityRecord:
    existing = parse_ledger_bytes(
        path.read_bytes() if path.exists() else b"",
        expected_binding_sha256=binding_sha256,
    )
    entries = tuple(
        {
            "event": record.event,
            "image_id": record.image_id,
            "image_repo_digest": record.image_repo_digest,
            "ownership_sha256": record.ownership_sha256,
            "resource_labels_sha256": record.resource_labels_sha256,
            "resource_id": record.resource_id,
            "resource_kind": record.resource_kind,
            "resource_name": record.resource_name,
        }
        for record in existing
    ) + (
        {
            "event": event,
            "image_id": image_id,
            "image_repo_digest": image_repo_digest,
            "ownership_sha256": ownership_sha256,
            "resource_labels_sha256": resource_labels_sha256,
            "resource_id": resource_id,
            "resource_kind": resource_kind,
            "resource_name": resource_name,
        },
    )
    return write_resource_identity_ledger(
        path,
        entries,
        binding_sha256=binding_sha256,
    )[-1]
