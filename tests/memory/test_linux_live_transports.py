from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
import json
from pathlib import Path
import unittest

import tools.governed_memory_install.linux_live_transports as contracts
from tools.governed_memory_install.linux_live_transports import (
    DOCKER_REQUESTS,
    DockerArgvContract,
    DockerRequestId,
    LiveTransportContractError,
    ObservationState,
    QDRANT_REQUESTS,
    QdrantRequestBytes,
    QdrantRequestId,
    ROOT_FILE_SLOTS,
    RootFileSlot,
    RootRemovalIdentity,
    SystemdPrefixObservation,
    SystemdPrefixState,
    TypedObservation,
)


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "governed_memory_install" / "linux_live_transports.py"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _absent() -> TypedObservation:
    return TypedObservation(
        ObservationState.ABSENT,
        "verified_absent",
        HASH_A,
    )


def _exact() -> TypedObservation:
    return TypedObservation(
        ObservationState.EXACT,
        "verified_exact",
        HASH_A,
        HASH_B,
    )


def _drift() -> TypedObservation:
    return TypedObservation(
        ObservationState.DRIFT,
        "verified_drift",
        HASH_A,
        HASH_C,
    )


def _unknown() -> TypedObservation:
    return TypedObservation(
        ObservationState.UNKNOWN,
        "transport_unavailable",
        None,
    )


class PureContractBoundaryTests(unittest.TestCase):
    def test_module_has_no_live_primitive_or_effect_surface(self) -> None:
        source = MODULE.read_text(encoding="utf-8")
        for forbidden in (
            "import subprocess",
            "import socket",
            "http.client",
            "os.open",
            "os.unlink",
            "connection_factory",
            "api_key=",
        ):
            self.assertNotIn(forbidden, source)

        self.assertFalse(any("Primitive" in name for name in contracts.__all__))
        self.assertFalse(any(name.endswith("Transport") for name in contracts.__all__))
        for contract_type in (
            contracts.DockerArgvContract,
            contracts.RootFileSlotContract,
            contracts.RootRemovalIdentity,
            contracts.QdrantRequestBytes,
        ):
            for effect_method in ("run", "execute", "create", "remove", "write"):
                self.assertFalse(hasattr(contract_type, effect_method))

    def test_observations_and_removal_identity_capture_no_raw_output(self) -> None:
        self.assertEqual(
            {field.name for field in fields(TypedObservation)},
            {
                "state",
                "reason_code",
                "evidence_sha256",
                "observed_identity_sha256",
            },
        )
        self.assertEqual(
            {field.name for field in fields(RootRemovalIdentity)},
            {"slot", "device", "inode", "content_sha256", "execution_id"},
        )


class TypedObservationTests(unittest.TestCase):
    def test_verified_absence_is_not_transport_unknown(self) -> None:
        absent = _absent()
        unknown = _unknown()
        self.assertIs(absent.state, ObservationState.ABSENT)
        self.assertEqual(absent.evidence_sha256, HASH_A)
        self.assertIsNone(absent.observed_identity_sha256)
        self.assertFalse(absent.manual_review_required)
        self.assertIs(unknown.state, ObservationState.UNKNOWN)
        self.assertIsNone(unknown.evidence_sha256)
        self.assertTrue(unknown.manual_review_required)
        self.assertNotEqual(absent, unknown)

    def test_exact_and_drift_require_hashed_observed_identity(self) -> None:
        self.assertFalse(_exact().manual_review_required)
        self.assertTrue(_drift().manual_review_required)
        with self.assertRaisesRegex(
            LiveTransportContractError, "identity_invalid"
        ):
            TypedObservation(
                ObservationState.EXACT,
                "verified_exact",
                HASH_A,
                None,
            )
        with self.assertRaisesRegex(
            LiveTransportContractError, "absence_proof_invalid"
        ):
            TypedObservation(
                ObservationState.ABSENT,
                "verified_absent",
                None,
            )
        with self.assertRaisesRegex(
            LiveTransportContractError, "unknown_has_identity"
        ):
            TypedObservation(
                ObservationState.UNKNOWN,
                "transport_unavailable",
                HASH_A,
            )


class DockerRequestContractTests(unittest.TestCase):
    def test_observations_are_narrow_and_never_project_config_env(self) -> None:
        observations = tuple(
            request for request in DOCKER_REQUESTS.values() if request.is_observation
        )
        self.assertEqual(len(observations), 5)
        for request in observations:
            self.assertEqual(request.argv[0], "/usr/bin/docker")
            self.assertIn("inspect", request.argv)
            self.assertIn("--format", request.argv)
            template = request.argv[request.argv.index("--format") + 1]
            self.assertNotIn("{{json .}}", template)
            self.assertNotIn(".Config.Env", template)
            self.assertNotIn(".Config.Env", request.projected_fields)
            self.assertNotIn(".Config", request.projected_fields)
            self.assertTrue(all(field in template for field in request.projected_fields))

        container = DOCKER_REQUESTS[
            DockerRequestId.OBSERVE_POSTGRES_CONTAINER
        ]
        self.assertIn(".Config.Image", container.projected_fields)
        self.assertIn(".HostConfig.CapDrop", container.projected_fields)
        self.assertIn("(len .NetworkSettings.Ports)", container.projected_fields)
        for broad in (
            ".HostConfig",
            ".HostConfig.PortBindings",
            ".HostConfig.RestartPolicy",
            ".HostConfig.Tmpfs",
            ".Mounts",
            ".NetworkSettings.Ports",
            ".NetworkSettings.Networks",
            ".State",
            ".Config.Labels",
        ):
            self.assertNotIn(broad, container.projected_fields)
        volume = DOCKER_REQUESTS[DockerRequestId.OBSERVE_POSTGRES_VOLUME]
        self.assertNotIn(".Options", volume.projected_fields)
        self.assertIn("(len .Options)", volume.projected_fields)

    def test_public_constructor_rejects_noncanonical_effect_argv(self) -> None:
        with self.assertRaisesRegex(
            LiveTransportContractError, "docker_request_not_canonical"
        ):
            DockerArgvContract(
                DockerRequestId.START_POSTGRES_CONTAINER,
                ("/usr/bin/docker", "pull", "attacker/image"),
            )

    def test_stop_argv_uses_current_exact_timeout_flag(self) -> None:
        self.assertEqual(
            DOCKER_REQUESTS[DockerRequestId.STOP_POSTGRES_CONTAINER].argv,
            (
                "/usr/bin/docker",
                "stop",
                "--timeout=10",
                "governed-memory-postgres-9a54cf123493-000005",
            ),
        )
        self.assertEqual(
            DOCKER_REQUESTS[DockerRequestId.STOP_QDRANT_CONTAINER].argv,
            (
                "/usr/bin/docker",
                "stop",
                "--timeout=10",
                "governed-memory-qdrant-9a54cf123493-000005",
            ),
        )
        self.assertFalse(
            any("--time=10" in request.argv for request in DOCKER_REQUESTS.values())
        )

    def test_fixed_requests_are_immutable_data_not_effect_methods(self) -> None:
        with self.assertRaises(TypeError):
            DOCKER_REQUESTS[DockerRequestId.REMOVE_NETWORK] = DOCKER_REQUESTS[
                DockerRequestId.REMOVE_NETWORK
            ]  # type: ignore[index]
        request = DOCKER_REQUESTS[DockerRequestId.START_POSTGRES_CONTAINER]
        with self.assertRaises(FrozenInstanceError):
            request.argv = ("/bin/sh",)  # type: ignore[misc]
        self.assertFalse(hasattr(request, "execute"))
        self.assertFalse(hasattr(request, "run"))


class RootFileContractTests(unittest.TestCase):
    def test_every_root_slot_has_root_owned_non_writable_ancestors(self) -> None:
        self.assertEqual(set(ROOT_FILE_SLOTS), set(RootFileSlot))
        for contract in ROOT_FILE_SLOTS.values():
            self.assertTrue(contract.path_template.startswith("/"))
            for ancestor in contract.ancestors:
                self.assertEqual(ancestor.required_uid, 0)
                self.assertEqual(ancestor.required_gid, 0)
                self.assertTrue(ancestor.allowed_modes)
                self.assertTrue(all(mode & 0o022 == 0 for mode in ancestor.allowed_modes))

        postgres = ROOT_FILE_SLOTS[RootFileSlot.POSTGRES_SECRET]
        self.assertEqual(postgres.required_mode, 0o600)
        self.assertEqual(postgres.ancestors[-1].allowed_modes, frozenset({0o700}))
        receipt = ROOT_FILE_SLOTS[RootFileSlot.TERMINAL_POSTFLIGHT_RECEIPT]
        self.assertEqual(receipt.ancestors[-1].allowed_modes, frozenset({0o700}))

    def test_removal_identity_binds_device_inode_content_and_execution(self) -> None:
        identity = RootRemovalIdentity(
            RootFileSlot.TERMINAL_POSTFLIGHT_RECEIPT,
            device=2049,
            inode=92817,
            content_sha256=HASH_A,
            execution_id=HASH_B,
        )
        self.assertEqual(
            identity.path,
            "/var/lib/governed-memory-controller/executions-v5/"
            f"{HASH_B}/terminal-postflight-receipt.json",
        )
        with self.assertRaises(FrozenInstanceError):
            identity.inode = 1  # type: ignore[misc]
        for replacement in (
            {"device": 0},
            {"inode": 0},
            {"content_sha256": "not-a-hash"},
            {"execution_id": "not-a-hash"},
        ):
            values = {
                "slot": RootFileSlot.POSTGRES_SECRET,
                "device": 1,
                "inode": 2,
                "content_sha256": HASH_A,
                "execution_id": HASH_B,
            }
            values.update(replacement)
            with self.subTest(replacement=replacement), self.assertRaisesRegex(
                LiveTransportContractError, "root_removal_identity_invalid"
            ):
                RootRemovalIdentity(**values)  # type: ignore[arg-type]

    def test_systemd_enablement_contract_has_exact_target(self) -> None:
        enablement = ROOT_FILE_SLOTS[RootFileSlot.SYSTEMD_ENABLEMENT]
        self.assertEqual(
            enablement.path_template,
            "/etc/systemd/system/multi-user.target.wants/"
            "governed-memory-stores-v5.service",
        )
        self.assertEqual(
            enablement.symlink_target,
            "/etc/systemd/system/governed-memory-stores-v5.service",
        )


class SystemdPrefixContractTests(unittest.TestCase):
    def test_absent_and_exact_are_terminal_states(self) -> None:
        absent = SystemdPrefixObservation(_absent(), _absent())
        exact = SystemdPrefixObservation(_exact(), _exact())
        self.assertIs(absent.state, SystemdPrefixState.ABSENT)
        self.assertFalse(absent.manual_review_required)
        self.assertIs(exact.state, SystemdPrefixState.EXACT)
        self.assertFalse(exact.manual_review_required)

    def test_every_partial_unknown_or_drift_state_requires_manual_review(self) -> None:
        cases = (
            ((_exact(), _absent()), SystemdPrefixState.UNIT_ONLY_PARTIAL),
            ((_absent(), _exact()), SystemdPrefixState.NON_PREFIX_PARTIAL),
            ((_drift(), _exact()), SystemdPrefixState.DRIFT),
            ((_exact(), _unknown()), SystemdPrefixState.UNKNOWN),
        )
        for pair, expected in cases:
            with self.subTest(expected=expected):
                observation = SystemdPrefixObservation(*pair)
                self.assertIs(observation.state, expected)
                self.assertTrue(observation.manual_review_required)


class QdrantRequestContractTests(unittest.TestCase):
    def test_requests_are_exact_immutable_bytes_without_credentials(self) -> None:
        self.assertEqual(set(QDRANT_REQUESTS), set(QdrantRequestId))
        for request in QDRANT_REQUESTS.values():
            self.assertIs(type(request.method), bytes)
            self.assertIs(type(request.target), bytes)
            self.assertTrue(request.target.startswith(b"/"))
            if request.body is not None:
                self.assertIs(type(request.body), bytes)
                json.loads(request.body)
            self.assertFalse(hasattr(request, "execute"))
            self.assertFalse(hasattr(request, "api_key"))

        create = QDRANT_REQUESTS[QdrantRequestId.CREATE_COLLECTION]
        self.assertEqual(create.method, b"PUT")
        self.assertEqual(
            create.target,
            b"/collections/governed_memory_9a54cf123493_000005",
        )
        self.assertEqual(
            create.body,
            b'{"on_disk_payload":true,"replication_factor":1,'
            b'"vectors":{"distance":"Dot","size":3072}}',
        )
        self.assertEqual(
            QDRANT_REQUESTS[QdrantRequestId.OBSERVE_ALIAS].target,
            (
                b"/collections/"
                b"governed_memory_9a54cf123493_000005/aliases"
            ),
        )
        self.assertNotIn(
            b"/aliases/governed_memory_active",
            {
                request.target
                for request in QDRANT_REQUESTS.values()
            },
        )
        with self.assertRaises(TypeError):
            QDRANT_REQUESTS[QdrantRequestId.OBSERVE_ROOT] = create  # type: ignore[index]

    def test_public_constructor_rejects_noncanonical_method_target_and_body(self) -> None:
        with self.assertRaisesRegex(
            LiveTransportContractError, "qdrant_request_not_canonical"
        ):
            QdrantRequestBytes(
                QdrantRequestId.OBSERVE_ROOT,
                b"DELETE",
                b"/collections/unrelated",
                None,
                frozenset({200}),
            )


if __name__ == "__main__":
    unittest.main()
