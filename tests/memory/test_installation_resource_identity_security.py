from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from tools.governed_memory_install.resource_identity import (
    ResourceIdentityError,
    ResourceIdentityLedger,
    load_ledger,
    parse_ledger_bytes,
)
from tools.governed_memory_install.host_boundary import (
    CommandRunner,
    DOCKER_BINARY,
    HostBoundaryError,
)
from tools.governed_memory_install.secure_file import (
    SecureFileError,
    read_root_owned_regular_file,
)


BINDING = "a" * 64
OWNERSHIP = "b" * 64
RESOURCE_LABELS = "c" * 64
CONTAINER_ID = "1" * 64
OTHER_CONTAINER_ID = "2" * 64
IMAGE_ID = "sha256:" + "3" * 64
REPO_DIGEST = "postgres@sha256:" + "4" * 64


def _append_created(path: Path) -> ResourceIdentityLedger:
    ledger = ResourceIdentityLedger(path, binding_sha256=BINDING)
    ledger.append(
        event="created",
        resource_kind="container",
        resource_name="governed-memory-postgres-security-test",
        resource_id=CONTAINER_ID,
        image_id=IMAGE_ID,
        image_repo_digest=REPO_DIGEST,
        ownership_sha256=OWNERSHIP,
        resource_labels_sha256=RESOURCE_LABELS,
    )
    return ledger


class DormantStoreInstallResourceIdentitySecurityTests(unittest.TestCase):
    def test_secure_authority_file_rejects_mode_hardlink_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "store_spec.json"
            path.write_bytes(b"{}\n")
            path.chmod(0o600)
            kwargs = {
                "expected_file_mode": 0o600,
                "expected_parent_mode": 0o700,
                "expected_uid": os.geteuid(),
            }
            self.assertEqual(read_root_owned_regular_file(path, **kwargs), b"{}\n")

            path.chmod(0o644)
            with self.assertRaisesRegex(SecureFileError, "secure_file_invalid"):
                read_root_owned_regular_file(path, **kwargs)
            path.chmod(0o600)
            alias = root / "alias.json"
            os.link(path, alias)
            with self.assertRaisesRegex(SecureFileError, "secure_file_invalid"):
                read_root_owned_regular_file(path, **kwargs)
            alias.unlink()
            symlink = root / "symlink.json"
            symlink.symlink_to(path.name)
            with self.assertRaises(SecureFileError):
                read_root_owned_regular_file(symlink, **kwargs)

    def test_host_command_profiles_are_closed(self) -> None:
        image_runner = CommandRunner(command_profile="image_inspect")
        image_runner._validate_profile(
            (
                DOCKER_BINARY,
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                "postgres:16-alpine@sha256:" + "5" * 64,
            )
        )
        with self.assertRaisesRegex(HostBoundaryError, "profile_refused"):
            image_runner._validate_profile(
                (DOCKER_BINARY, "create", "--privileged", "postgres")
            )

        supervisor = CommandRunner(command_profile="store_supervisor")
        supervisor._validate_profile(
            (
                DOCKER_BINARY,
                "container",
                "inspect",
                "--format",
                "{{json .}}",
                CONTAINER_ID,
            )
        )
        supervisor._validate_profile(
            (DOCKER_BINARY, "stop", "--time=10", CONTAINER_ID)
        )
        with self.assertRaisesRegex(HostBoundaryError, "profile_refused"):
            supervisor._validate_profile((DOCKER_BINARY, "rm", CONTAINER_ID))

    def test_rejects_permissive_mode_hard_link_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "ledger.jsonl"
            ledger = _append_created(ledger_path)

            ledger_path.chmod(0o666)
            with self.assertRaisesRegex(ResourceIdentityError, "file_invalid"):
                load_ledger(ledger_path, expected_binding_sha256=BINDING)
            with self.assertRaisesRegex(ResourceIdentityError, "file_invalid"):
                ledger.append(
                    event="observed",
                    resource_kind="container",
                    resource_name="governed-memory-postgres-security-test",
                    resource_id=CONTAINER_ID,
                    image_id=IMAGE_ID,
                    image_repo_digest=REPO_DIGEST,
                    ownership_sha256=OWNERSHIP,
                    resource_labels_sha256=RESOURCE_LABELS,
                )

            ledger_path.chmod(0o600)
            alias = root / "ledger-hardlink.jsonl"
            os.link(ledger_path, alias)
            with self.assertRaisesRegex(ResourceIdentityError, "file_invalid"):
                load_ledger(ledger_path, expected_binding_sha256=BINDING)
            alias.unlink()

            symlink = root / "ledger-symlink.jsonl"
            symlink.symlink_to(ledger_path.name)
            with self.assertRaises(ResourceIdentityError):
                load_ledger(symlink, expected_binding_sha256=BINDING)

    def test_rejects_unsafe_parent_and_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "ledger.jsonl"
            ledger = _append_created(ledger_path)

            root.chmod(0o755)
            with self.assertRaisesRegex(ResourceIdentityError, "directory_invalid"):
                load_ledger(ledger_path, expected_binding_sha256=BINDING)
            root.chmod(0o700)

            with self.assertRaisesRegex(ResourceIdentityError, "resource_drift"):
                ledger.append(
                    event="observed",
                    resource_kind="container",
                    resource_name="governed-memory-postgres-security-test",
                    resource_id=OTHER_CONTAINER_ID,
                    image_id=IMAGE_ID,
                    image_repo_digest=REPO_DIGEST,
                    ownership_sha256=OWNERSHIP,
                    resource_labels_sha256=RESOURCE_LABELS,
                )
            with self.assertRaisesRegex(ResourceIdentityError, "resource_drift"):
                ledger.append(
                    event="observed",
                    resource_kind="container",
                    resource_name="governed-memory-postgres-security-test",
                    resource_id=CONTAINER_ID,
                    image_id=IMAGE_ID,
                    image_repo_digest=REPO_DIGEST,
                    ownership_sha256=OWNERSHIP,
                    resource_labels_sha256="d" * 64,
                )

    def test_labels_are_required_only_for_docker_backed_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = ResourceIdentityLedger(
                Path(directory) / "ledger.jsonl",
                binding_sha256=BINDING,
            )
            with self.assertRaisesRegex(
                ResourceIdentityError, "resource_labels_invalid"
            ):
                ledger.append(
                    event="created",
                    resource_kind="network",
                    resource_name="governed-memory-net-9a54cf123493-000001",
                    resource_id="network-id-001",
                    ownership_sha256=OWNERSHIP,
                )
            with self.assertRaisesRegex(
                ResourceIdentityError, "resource_labels_forbidden"
            ):
                ledger.append(
                    event="created",
                    resource_kind="database",
                    resource_name="governed_memory",
                    resource_id="database-id-001",
                    ownership_sha256=OWNERSHIP,
                    resource_labels_sha256=RESOURCE_LABELS,
                )

    def test_removed_identity_cannot_be_resurrected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            ledger = _append_created(path)
            common = {
                "resource_kind": "container",
                "resource_name": "governed-memory-postgres-security-test",
                "resource_id": CONTAINER_ID,
                "image_id": IMAGE_ID,
                "image_repo_digest": REPO_DIGEST,
                "ownership_sha256": OWNERSHIP,
                "resource_labels_sha256": RESOURCE_LABELS,
            }
            ledger.append(event="removed", **common)
            with self.assertRaisesRegex(ResourceIdentityError, "transition_invalid"):
                ledger.append(event="observed", **common)

            lines = path.read_bytes().splitlines()
            import json
            import hashlib

            prior = json.loads(lines[-1])
            tampered = dict(prior)
            tampered["sequence"] = 3
            tampered["previous_entry_sha256"] = prior["entry_sha256"]
            tampered["event"] = "started"
            payload = dict(tampered)
            payload.pop("entry_sha256")
            canonical = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
            tampered["entry_sha256"] = hashlib.sha256(canonical).hexdigest()
            lines.append(json.dumps(
                tampered, sort_keys=True, separators=(",", ":")
            ).encode("ascii"))
            with self.assertRaisesRegex(ResourceIdentityError, "transition_invalid"):
                parse_ledger_bytes(
                    b"\n".join(lines) + b"\n",
                    expected_binding_sha256=BINDING,
                )

    def test_exact_install_and_rollback_resource_kinds_are_secret_free(self) -> None:
        cases = (
            ("database", "governed_memory"),
            ("migration", "0001_foundation"),
            ("network", "governed-memory-net-9a54cf123493-000001"),
            ("qdrant_alias", "governed_memory_active"),
            ("qdrant_collection", "governed_memory_9a54cf123493_000001"),
            ("resolved_store_spec", "/etc/governed-memory-controller/store_spec.json"),
            ("secret_file", "/etc/governed-memory-stores/9a54cf123493-000001/postgres.env"),
            ("systemd_unit", "/etc/systemd/system/governed-memory-stores.service"),
            ("volume", "governed-memory-postgres-data-9a54cf123493-000001"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            ledger = ResourceIdentityLedger(path, binding_sha256=BINDING)
            for index, (kind, name) in enumerate(cases, start=1):
                ledger.append(
                    event="created",
                    resource_kind=kind,
                    resource_name=name,
                    resource_id=f"resource-{index}",
                    ownership_sha256=hashlib.sha256(
                        f"owner-{index}".encode("ascii")
                    ).hexdigest(),
                    resource_labels_sha256=(
                        hashlib.sha256(
                            f"labels-{index}".encode("ascii")
                        ).hexdigest()
                        if kind in {"network", "volume"}
                        else None
                    ),
                )
            records = load_ledger(path, expected_binding_sha256=BINDING)
            self.assertEqual(
                {record.resource_kind for record in records},
                {kind for kind, _name in cases},
            )
            self.assertNotIn("secret_value", path.read_text().lower())

            with self.assertRaisesRegex(
                ResourceIdentityError, "forbidden_content"
            ):
                ledger.append(
                    event="created",
                    resource_kind="volume",
                    resource_name="governed-memory-extra-volume",
                    resource_id="tokenmaterialmustnotenterledger",
                    ownership_sha256=OWNERSHIP,
                    resource_labels_sha256=RESOURCE_LABELS,
                )


if __name__ == "__main__":
    unittest.main()
