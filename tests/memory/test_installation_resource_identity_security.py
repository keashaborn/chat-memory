from __future__ import annotations

from contextlib import contextmanager
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
from tools.governed_memory_install.execution_capability import (
    _claimed_execution_binding_evidence,
)
from tools.governed_memory_install.host_boundary import (
    CommandRunner,
    DOCKER_BINARY,
    HostBoundaryError,
    IMAGE_INSPECT_TEMPLATE,
    SUPERVISOR_INSPECT_TEMPLATES,
)
from tools.governed_memory_install.secure_file import (
    SecureFileError,
    read_root_owned_regular_file,
)
from tests.memory.test_installation_durable_journal import _Fixture


OWNERSHIP = "b" * 64
RESOURCE_LABELS = "c" * 64
CONTAINER_ID = "1" * 64
OTHER_CONTAINER_ID = "2" * 64
IMAGE_ID = "sha256:" + "3" * 64
REPO_DIGEST = "postgres@sha256:" + "4" * 64


@contextmanager
def _anchored_ledger(root: Path):
    fixture = _Fixture(root / "anchored-fixture")
    try:
        claimed = fixture.claimed_binding()
        evidence = _claimed_execution_binding_evidence(claimed)
        path = Path(evidence.resource_identity_ledger_path)
        path.parent.mkdir(parents=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        ledger = ResourceIdentityLedger(
            path,
            claimed_execution_binding=claimed,
            authority_state=fixture.state,
            held_lock=fixture.execution_lock.held_capability(),
            create=True,
        )
        yield fixture, ledger, path
    finally:
        fixture.close()


def _append_created(ledger: ResourceIdentityLedger) -> None:
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


class DormantStoreInstallResourceIdentitySecurityTests(unittest.TestCase):
    def test_unanchored_ledger_constructor_is_not_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TypeError, "binding_sha256"):
                ResourceIdentityLedger(  # type: ignore[call-arg]
                    Path(directory) / "ledger.jsonl",
                    binding_sha256="a" * 64,
                )

    def test_secure_authority_file_rejects_mode_hardlink_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "store_spec-v6.json"
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
                IMAGE_INSPECT_TEMPLATE,
                "postgres:16-alpine@sha256:" + "5" * 64,
            )
        )
        with self.assertRaisesRegex(HostBoundaryError, "profile_refused"):
            image_runner._validate_profile(
                (DOCKER_BINARY, "create", "--privileged", "postgres")
            )

        supervisor = CommandRunner(command_profile="store_supervisor")
        for template in SUPERVISOR_INSPECT_TEMPLATES:
            supervisor._validate_profile(
                (
                    DOCKER_BINARY,
                    "container",
                    "inspect",
                    "--format",
                    template,
                    CONTAINER_ID,
                )
            )
        supervisor._validate_profile(
            (DOCKER_BINARY, "stop", "--timeout=10", CONTAINER_ID)
        )
        with self.assertRaisesRegex(HostBoundaryError, "profile_refused"):
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
        with self.assertRaisesRegex(HostBoundaryError, "profile_refused"):
            supervisor._validate_profile((DOCKER_BINARY, "rm", CONTAINER_ID))

    def test_rejects_permissive_mode_hard_link_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _anchored_ledger(root) as (_fixture, ledger, ledger_path):
                _append_created(ledger)

                ledger_path.chmod(0o666)
                with self.assertRaisesRegex(ResourceIdentityError, "file_invalid"):
                    load_ledger(
                        ledger_path,
                        expected_binding_sha256=ledger.binding_sha256,
                    )
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
                alias = ledger_path.with_name("ledger-hardlink.jsonl")
                os.link(ledger_path, alias)
                with self.assertRaisesRegex(ResourceIdentityError, "file_invalid"):
                    load_ledger(
                        ledger_path,
                        expected_binding_sha256=ledger.binding_sha256,
                    )
                alias.unlink()

                symlink = ledger_path.with_name("ledger-symlink.jsonl")
                symlink.symlink_to(ledger_path.name)
                with self.assertRaises(ResourceIdentityError):
                    load_ledger(
                        symlink,
                        expected_binding_sha256=ledger.binding_sha256,
                    )

    def test_rejects_unsafe_parent_and_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _anchored_ledger(root) as (_fixture, ledger, ledger_path):
                _append_created(ledger)

                ledger_path.parent.chmod(0o755)
                with self.assertRaisesRegex(
                    ResourceIdentityError, "directory_invalid"
                ):
                    load_ledger(
                        ledger_path,
                        expected_binding_sha256=ledger.binding_sha256,
                    )
                ledger_path.parent.chmod(0o700)

                with self.assertRaisesRegex(
                    ResourceIdentityError, "resource_drift"
                ):
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
                with self.assertRaisesRegex(
                    ResourceIdentityError, "resource_drift"
                ):
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
            with _anchored_ledger(Path(directory)) as (_fixture, ledger, _path):
                with self.assertRaisesRegex(
                    ResourceIdentityError, "resource_labels_invalid"
                ):
                    ledger.append(
                        event="created",
                        resource_kind="network",
                        resource_name="governed-memory-net-9a54cf123493-000006",
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
            with _anchored_ledger(Path(directory)) as (_fixture, ledger, path):
                _append_created(ledger)
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
                with self.assertRaisesRegex(
                    ResourceIdentityError, "transition_invalid"
                ):
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
                with self.assertRaisesRegex(
                    ResourceIdentityError, "transition_invalid"
                ):
                    parse_ledger_bytes(
                        b"\n".join(lines) + b"\n",
                        expected_binding_sha256=ledger.binding_sha256,
                    )

    def test_exact_install_and_rollback_resource_kinds_are_secret_free(self) -> None:
        cases = (
            ("database", "governed_memory"),
            ("migration", "0001_foundation"),
            ("network", "governed-memory-net-9a54cf123493-000006"),
            ("qdrant_alias", "governed_memory_active"),
            ("qdrant_collection", "governed_memory_9a54cf123493_000006"),
            ("resolved_store_spec", "/etc/governed-memory-controller/store_spec-v6.json"),
            ("secret_file", "/etc/governed-memory-stores/9a54cf123493-000006/postgres.env"),
            ("systemd_unit", "/etc/systemd/system/governed-memory-stores-v6.service"),
            ("volume", "governed-memory-postgres-data-9a54cf123493-000006"),
        )
        with tempfile.TemporaryDirectory() as directory:
            with _anchored_ledger(Path(directory)) as (_fixture, ledger, path):
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
                records = load_ledger(
                    path,
                    expected_binding_sha256=ledger.binding_sha256,
                )
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
