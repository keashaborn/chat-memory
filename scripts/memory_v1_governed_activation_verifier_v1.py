#!/usr/bin/env python3
"""Content-free verifier for one owner's governed-memory activation status."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
from typing import Any
from uuid import UUID


CONTRACT_VERSION = "memory_v1_governed_activation_verifier_v1"
DEFAULT_CONFIGURATION = Path(
    "/etc/systemd/system/brains.service.d/70-memory-v1-universal-authenticated.conf"
)
MAX_CONFIGURATION_BYTES = 65_536
TARGET_KEYS = frozenset(
    {
        "MEMORY_V1_GOVERNED_ACTIVE",
        "MEMORY_V1_GOVERNED_ACTIVE_ALL_AUTHENTICATED",
        "MEMORY_V1_GOVERNED_ACTIVE_USER_IDS",
    }
)


class ActivationVerificationError(RuntimeError):
    pass


def _canonical_owner(value: str) -> UUID:
    try:
        owner = UUID(value)
    except ValueError as exc:
        raise ActivationVerificationError("owner UUID is invalid") from exc
    if str(owner) != value:
        raise ActivationVerificationError("owner UUID is not canonical")
    return owner


def parse_activation_configuration(value: bytes) -> dict[str, str]:
    if not value or len(value) > MAX_CONFIGURATION_BYTES or b"\x00" in value:
        raise ActivationVerificationError("activation configuration bytes are invalid")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ActivationVerificationError("activation configuration is not UTF-8") from exc

    observed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        prefix, separator, payload = line.partition("=")
        if not separator or prefix.strip() != "Environment":
            continue
        try:
            assignments = shlex.split(payload, posix=True)
        except ValueError as exc:
            raise ActivationVerificationError(
                "activation configuration quoting is invalid"
            ) from exc
        for assignment in assignments:
            key, assignment_separator, item = assignment.partition("=")
            if not assignment_separator or key not in TARGET_KEYS:
                continue
            if key in observed:
                raise ActivationVerificationError(
                    "activation configuration contains a duplicate target key"
                )
            observed[key] = item

    for required in (
        "MEMORY_V1_GOVERNED_ACTIVE",
        "MEMORY_V1_GOVERNED_ACTIVE_ALL_AUTHENTICATED",
    ):
        if observed.get(required) not in {"0", "1"}:
            raise ActivationVerificationError(
                "activation configuration is missing a binary target key"
            )
    observed.setdefault("MEMORY_V1_GOVERNED_ACTIVE_USER_IDS", "")
    return observed


def _configured_owners(raw: str) -> set[UUID]:
    values: set[UUID] = set()
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            owner = UUID(candidate)
        except ValueError as exc:
            raise ActivationVerificationError(
                "activation configuration contains a malformed owner UUID"
            ) from exc
        if str(owner) != candidate:
            raise ActivationVerificationError(
                "activation configuration contains a noncanonical owner UUID"
            )
        values.add(owner)
    return values


def verify_owner_activation(
    value: bytes,
    *,
    owner_user_id: UUID,
    configuration_sha256: str | None = None,
) -> dict[str, Any]:
    observed = parse_activation_configuration(value)
    configured_owners = _configured_owners(
        observed["MEMORY_V1_GOVERNED_ACTIVE_USER_IDS"]
    )
    governed_active = observed["MEMORY_V1_GOVERNED_ACTIVE"] == "1"
    all_authenticated = (
        observed["MEMORY_V1_GOVERNED_ACTIVE_ALL_AUTHENTICATED"] == "1"
    )
    owner_allowlisted = governed_active and (
        all_authenticated or owner_user_id in configured_owners
    )
    if not governed_active:
        scope = "inactive"
    elif all_authenticated:
        scope = "all_authenticated"
    else:
        scope = "explicit_owner_list"
    return {
        "activation_scope": scope,
        "configuration_sha256": configuration_sha256 or hashlib.sha256(value).hexdigest(),
        "contract_version": CONTRACT_VERSION,
        "governed_active": governed_active,
        "owner_allowlisted": owner_allowlisted,
        "provider_calls": 0,
        "secret_values_emitted": False,
    }


def read_configuration(path: Path) -> tuple[bytes, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ActivationVerificationError(
            "activation configuration cannot be opened safely"
        ) from exc
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != 0
            or before.st_gid != 0
            or mode & 0o022
            or before.st_size < 1
            or before.st_size > MAX_CONFIGURATION_BYTES
        ):
            raise ActivationVerificationError(
                "activation configuration identity is invalid"
            )
        chunks: list[bytes] = []
        remaining = MAX_CONFIGURATION_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
        )
        if identity_before != identity_after or len(value) != before.st_size:
            raise ActivationVerificationError(
                "activation configuration changed while read"
            )
        return value, hashlib.sha256(value).hexdigest()
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--configuration", type=Path, default=DEFAULT_CONFIGURATION)
    arguments = parser.parse_args(argv)
    try:
        owner = _canonical_owner(arguments.owner_user_id)
        value, digest = read_configuration(arguments.configuration)
        report = verify_owner_activation(
            value,
            owner_user_id=owner,
            configuration_sha256=digest,
        )
    except ActivationVerificationError:
        print(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "status": "verification_failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
