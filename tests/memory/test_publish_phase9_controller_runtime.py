from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from types import MappingProxyType, SimpleNamespace
import tempfile
import unittest
from unittest import mock

from tools.governed_memory_validation import (
    publish_phase9_controller_runtime as subject,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class PackageSnapshotTests(unittest.TestCase):
    def test_exact_manifest_bytes_and_every_indexed_artifact_are_snapshotted(self):
        artifacts = {
            "a/file.txt": b"alpha",
            "b/file.txt": b"beta",
        }
        manifest = (
            json.dumps(
                {
                    "schema_version": "synthetic-v1",
                    "state": "repository-only",
                    "artifacts": {
                        path: _sha(raw) for path, raw in sorted(artifacts.items())
                    },
                },
                indent=2,
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
        reads = {"manifest.json": manifest, **artifacts}
        package = SimpleNamespace(
            MANIFEST_RELATIVE="manifest.json",
            verify=lambda: {
                "package_manifest_sha256": _sha(manifest),
                "artifact_count": len(artifacts),
            },
            _read_repository_file=lambda relative: reads[relative],
        )

        snapshot = subject._load_package_snapshot(package)

        self.assertEqual(snapshot.manifest, manifest)
        self.assertEqual(dict(snapshot.artifacts), artifacts)
        self.assertEqual(
            snapshot.verification["package_manifest_sha256"], _sha(manifest)
        )

    def test_changed_artifact_is_refused(self):
        manifest = json.dumps(
            {
                "schema_version": "synthetic-v1",
                "state": "repository-only",
                "artifacts": {"a": "0" * 64},
            }
        ).encode("ascii")
        package = SimpleNamespace(
            MANIFEST_RELATIVE="manifest.json",
            verify=lambda: {
                "package_manifest_sha256": _sha(manifest),
                "artifact_count": 1,
            },
            _read_repository_file=lambda relative: (
                manifest if relative == "manifest.json" else b"changed"
            ),
        )
        with self.assertRaisesRegex(
            subject.Phase9RuntimePublicationError,
            "phase9_runtime_publication_package_changed",
        ):
            subject._load_package_snapshot(package)


class BuildIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = subject.CandidateIdentity("1" * 40, "2" * 40, "3" * 40)
        self.arguments = {
            "identity": self.identity,
            "package_manifest_sha256": "4" * 64,
            "runtime_contract_sha256": "5" * 64,
            "requirements_lock_sha256": "6" * 64,
            "build_plan_sha256": "7" * 64,
        }

    def test_selected_substrate_specification_has_frozen_identity(self):
        raw = subject._selected_substrate_specification()
        self.assertEqual(_sha(raw), subject.SUBSTRATE_SPECIFICATION_SHA256)
        self.assertEqual(raw, subject._canonical(json.loads(raw)))

    def test_nonce_and_intent_are_deterministic_canonical_and_fully_bound(self):
        first_nonce, first = subject._build_intent(**self.arguments)
        second_nonce, second = subject._build_intent(**self.arguments)
        changed = dict(self.arguments)
        changed["build_plan_sha256"] = "8" * 64
        changed_nonce, changed_intent = subject._build_intent(**changed)

        self.assertEqual(first_nonce, second_nonce)
        self.assertEqual(first, second)
        self.assertEqual(first, subject._canonical(json.loads(first)))
        self.assertNotEqual(first, changed_intent)
        # The nonce binds immutable source/package/input identities, while the
        # intent separately binds the resulting plan identity.
        self.assertEqual(first_nonce, changed_nonce)
        document = json.loads(first)
        self.assertEqual(document["schema_version"], subject.BUILD_INTENT_SCHEMA)
        self.assertEqual(document["build_nonce"], first_nonce)
        self.assertEqual(document["build_plan_sha256"], "7" * 64)
        self.assertFalse(document["activation_performed"])
        self.assertFalse(document["production_data_read"])

    def test_exact_intent_create_replay_and_owned_prefix_recovery(self):
        unused_nonce, expected = subject._build_intent(**self.arguments)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)

            def rename_no_replace(
                descriptor: int, source: str, destination: str
            ) -> None:
                self.assertEqual(descriptor, parent_fd)
                if (root / destination).exists():
                    raise FileExistsError
                os.rename(
                    source,
                    destination,
                    src_dir_fd=descriptor,
                    dst_dir_fd=descriptor,
                )

            try:
                with (
                    mock.patch.object(subject, "BUILD_INTENT_ROOT", PurePosixPath(root)),
                    mock.patch.object(subject, "ROOT_UID", os.getuid()),
                    mock.patch.object(subject, "ROOT_GID", os.getgid()),
                    mock.patch.object(
                        subject,
                        "_rename_intent_no_replace",
                        side_effect=rename_no_replace,
                    ),
                ):
                    first = subject._publish_build_intent(
                        parent_fd=parent_fd,
                        package_manifest_sha256="4" * 64,
                        expected=expected,
                    )
                    replay = subject._publish_build_intent(
                        parent_fd=parent_fd,
                        package_manifest_sha256="4" * 64,
                        expected=expected,
                    )
                    self.assertFalse(first[2])
                    self.assertTrue(replay[2])
                    self.assertEqual(first[:2], replay[:2])

                    final_name, staging_name = subject._intent_names("9" * 64)
                    (root / staging_name).write_bytes(expected[:37])
                    os.chmod(root / staging_name, 0o400)
                    recovered = subject._publish_build_intent(
                        parent_fd=parent_fd,
                        package_manifest_sha256="9" * 64,
                        expected=expected,
                    )
                    self.assertFalse(recovered[2])
                    self.assertEqual((root / final_name).read_bytes(), expected)
                    self.assertFalse((root / staging_name).exists())
            finally:
                os.close(parent_fd)

    def test_foreign_partial_intent_is_refused_without_removal(self):
        unused_nonce, expected = subject._build_intent(**self.arguments)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            unused_final, staging = subject._intent_names("4" * 64)
            foreign = b"not-an-expected-prefix"
            (root / staging).write_bytes(foreign)
            os.chmod(root / staging, 0o400)
            try:
                with (
                    mock.patch.object(subject, "BUILD_INTENT_ROOT", PurePosixPath(root)),
                    mock.patch.object(subject, "ROOT_UID", os.getuid()),
                    mock.patch.object(subject, "ROOT_GID", os.getgid()),
                ):
                    with self.assertRaisesRegex(
                        subject.Phase9RuntimePublicationError,
                        "phase9_runtime_publication_build_intent_mismatch",
                    ):
                        subject._publish_build_intent(
                            parent_fd=parent_fd,
                            package_manifest_sha256="4" * 64,
                            expected=expected,
                        )
                self.assertEqual((root / staging).read_bytes(), foreign)
            finally:
                os.close(parent_fd)


class CandidateIdentityTests(unittest.TestCase):
    def test_dirty_candidate_is_refused_before_identity_is_minted(self):
        def git(*arguments: str, **unused: object) -> bytes:
            if arguments[:2] == ("rev-parse", "--show-toplevel"):
                return (str(subject.ROOT) + "\n").encode()
            if arguments[:2] == ("status", "--porcelain=v1"):
                return b" M tracked.py\n"
            raise AssertionError(arguments)

        with mock.patch.object(subject, "_git", side_effect=git):
            with self.assertRaisesRegex(
                subject.Phase9RuntimePublicationError,
                "phase9_runtime_publication_candidate_not_clean",
            ):
                subject._verified_candidate_identity()


class PublicationControllerTests(unittest.TestCase):
    def test_closed_controller_orders_intent_before_staging_and_build(self):
        events: list[str] = []
        identity = subject.CandidateIdentity("1" * 40, "2" * 40, "3" * 40)
        substrate = b"exact-substrate"
        runtime_contract = b"runtime-contract"
        requirements_lock = b"requirements-lock"
        manifest_sha = "4" * 64
        snapshot = subject.PackageSnapshot(
            MappingProxyType(
                {"package_manifest_sha256": manifest_sha, "artifact_count": 74}
            ),
            b"manifest",
            MappingProxyType(
                {
                    subject.RUNTIME_CONTRACT_RELATIVE: runtime_contract,
                    subject.REQUIREMENTS_LOCK_RELATIVE: requirements_lock,
                }
            ),
        )
        substrate_identity = SimpleNamespace(
            specification_sha256=_sha(substrate),
            archive_sha256=subject.SUBSTRATE_ARCHIVE_SHA256,
            payload_tree_sha256="5" * 64,
        )
        wheelhouse_identity = SimpleNamespace(
            tree_sha256=subject.WHEELHOUSE_TREE_SHA256
        )
        plan = SimpleNamespace(
            build_plan_sha256="6" * 64,
            substrate=substrate_identity,
            wheelhouse=wheelhouse_identity,
        )
        result = SimpleNamespace(
            controller_runtime_receipt_sha256="7" * 64,
            runtime_root="/opt/governed-memory-controller/runtimes/" + "7" * 64,
            runtime_tree_sha256="8" * 64,
            release_root="/opt/governed-memory-controller/releases/" + manifest_sha,
            release_tree_sha256="9" * 64,
            receipt_path=(
                "/var/lib/governed-memory-controller/runtime-receipts/"
                + "7" * 64
                + ".json"
            ),
        )
        bootstrap = SimpleNamespace(created_roots=("a",), normalized_roots=())
        stage = SimpleNamespace(replayed=False)
        case = self

        class Primitives:
            def bootstrap_fixed_root_directories(self):
                events.append("bootstrap")
                return bootstrap

        primitives = Primitives()

        class Builder:
            @staticmethod
            def create_controller_runtime_build_plan(**arguments):
                events.append("plan")
                self.assertEqual(arguments["package_manifest_json"], b"manifest")
                self.assertEqual(
                    arguments["controller_runtime_contract_json"], runtime_contract
                )
                self.assertEqual(
                    arguments["controller_requirements_lock"], requirements_lock
                )
                self.assertRegex(arguments["build_nonce"], r"^[0-9a-f]{64}$")
                return plan

            @staticmethod
            def execute_controller_runtime_build(unused_plan, **arguments):
                events.append("build")
                self.assertEqual(arguments["package_artifacts"], dict(snapshot.artifacts))
                return result

        class Stager:
            def __init__(self, selected):
                case.assertIs(selected, primitives)

            def stage_selected_cpython_archive(self, unused_plan):
                events.append("stage-cpython")
                return stage

            def stage_exact_wheelhouse(self, unused_plan):
                events.append("stage-wheelhouse")
                return stage

        class Publication:
            def __init__(self, selected):
                case.assertIs(selected, primitives)
                events.append("transport")

        modules = subject.RuntimeModules(
            package=SimpleNamespace(),
            builder=Builder,
            stager=SimpleNamespace(ClosedRuntimeInputStager=Stager),
            publication=SimpleNamespace(ClosedRuntimePublicationTransport=Publication),
            primitives=SimpleNamespace(
                LinuxRuntimePublicationPrimitives=lambda: primitives
            ),
            inspector=SimpleNamespace(
                inspect_selected_standalone_archive=lambda unused: (
                    events.append("inspect") or substrate
                )
            ),
        )

        @contextmanager
        def locked():
            events.append("lock")
            yield 19

        def publish_intent(**unused):
            events.append("intent")
            return "/fixed/intent", "a" * 64, False

        def load_wheels(unused):
            events.append("wheels")
            return {"wheel.whl": b"wheel"}

        with (
            mock.patch.object(subject.sys, "platform", "linux"),
            mock.patch.object(subject.os, "geteuid", return_value=0),
            mock.patch.object(subject.os, "getegid", return_value=0),
            mock.patch.object(
                subject, "_closed_python_invocation", return_value=True
            ),
            mock.patch.object(
                subject,
                "_selected_substrate_specification",
                return_value=substrate,
            ),
            mock.patch.object(
                subject, "_verified_candidate_identity", return_value=identity
            ),
            mock.patch.object(subject, "_load_modules", return_value=modules),
            mock.patch.object(
                subject, "_load_package_snapshot", return_value=snapshot
            ),
            mock.patch.object(subject, "_reverify_candidate") as reverify,
            mock.patch.object(
                subject, "_load_exact_wheelhouse", side_effect=load_wheels
            ),
            mock.patch.object(subject, "_publication_lock", side_effect=locked),
            mock.patch.object(
                subject,
                "_ensure_build_intent_root",
                side_effect=lambda: events.append("intent-root"),
            ),
            mock.patch.object(
                subject, "_publish_build_intent", side_effect=publish_intent
            ),
        ):
            receipt = dict(subject.publish_phase9_controller_runtime())

        self.assertEqual(
            events,
            [
                "wheels",
                "plan",
                "intent-root",
                "lock",
                "intent",
                "inspect",
                "bootstrap",
                "stage-cpython",
                "stage-wheelhouse",
                "transport",
                "build",
            ],
        )
        self.assertEqual(reverify.call_count, 3)
        self.assertTrue(receipt["runtime_build_receipt_verified_by_builder"])
        self.assertFalse(receipt["authority_bound_runtime_capability_verified"])
        self.assertTrue(
            receipt[
                "authority_bound_runtime_capability_deferred_to_exact_proof_runner"
            ]
        )
        self.assertFalse(receipt["persistent_store_resources_created"])
        self.assertFalse(receipt["secrets_touched"])
        expected_receipt_hash = _sha(
            subject._canonical(
                {key: value for key, value in receipt.items() if key != "receipt_sha256"}
            )
        )
        self.assertEqual(receipt["receipt_sha256"], expected_receipt_hash)

    def test_nonisolated_or_nonroot_invocation_is_refused_before_git(self):
        with (
            mock.patch.object(subject.sys, "platform", "linux"),
            mock.patch.object(subject.os, "geteuid", return_value=0),
            mock.patch.object(subject.os, "getegid", return_value=0),
            mock.patch.object(
                subject, "_closed_python_invocation", return_value=False
            ),
            mock.patch.object(subject, "_verified_candidate_identity") as identity,
        ):
            with self.assertRaisesRegex(
                subject.Phase9RuntimePublicationError,
                "phase9_runtime_publication_root_linux_required",
            ):
                subject.publish_phase9_controller_runtime()
        identity.assert_not_called()

    def test_cli_has_no_operational_arguments(self):
        with (
            mock.patch("sys.stderr"),
            self.assertRaises(SystemExit),
        ):
            subject._parser().parse_args(("--source", "/tmp/foreign"))


if __name__ == "__main__":
    unittest.main()
