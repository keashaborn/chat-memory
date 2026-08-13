from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from tools.governed_memory_install.controller import STORES_ONLY_PLAN
from tools.governed_memory_install.host_boundary import (
    HostOperationProfile,
    HostOperationRequest,
)
from tools.governed_memory_install.linux_plan import (
    ExecutionBinding,
    bind_store_spec,
    canonical_labels_sha256,
    load_store_spec,
)
from tools.governed_memory_install.linux_store_effects import (
    BoundLinuxStoreTransports,
    BoundRetainedRootDirectorySnapshot,
    BoundResourceSnapshot,
    BoundSystemdSupervisorSnapshot,
    EffectPresence,
    ExactInstallArtifacts,
    ExactSystemdSupervisor,
    FilesystemNodeKind,
    FreshSecret,
    LinuxStoreEffectsError,
    LinuxStoreHostOperations,
    LivePreflightSnapshot,
    SecretEnvironmentDocument,
    CONTROLLER_CONFIG_DIRECTORY,
    POSTGRES_STORE_SECRET_PATH,
    QDRANT_STORE_SECRET_PATH,
    SYSTEMD_ENABLEMENT_PATH,
    SYSTEMD_ENABLEMENT_TARGET,
    SYSTEMD_UNIT_PATH,
    STORE_SECRET_DIRECTORY,
    secret_environment_public_id,
    _ExecutionIdentity,
)
from tools.governed_memory_install.linux_store_readiness import (
    CanonicalPostgreSQLRole,
    ClosedStoreReadinessProbe,
    PostgreSQLCatalogIdentity,
    PostgreSQLRoleMembership,
    PrebootstrapPostgreSQLSnapshot,
    PrebootstrapQdrantSnapshot,
    QdrantCollectionConfiguration,
    TerminalPostgreSQLSnapshot,
    TerminalQdrantSnapshot,
)
from tools.governed_memory_install.store_readiness import (
    COLLECTION,
    POSTGRES_BIND,
    QDRANT_BIND,
    REQUIRED_ROLE_NAMES,
    TERMINAL_MIGRATION_IDS,
    POSTGRES_SERVER_VERSION,
    QDRANT_SERVER_VERSION,
)
from tools.governed_memory_install.rollback_live_adapter import (
    ClosedLinuxPhysicalRollbackDriver,
    ExactPhysicalEmptyRollbackOperations,
    LedgerBoundPhysicalTarget,
)


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SHA256 = "d" * 64
EXECUTION_ID = "7" * 64
ATTEMPT_ID = "install-" + "8" * 40
BINDING_SHA256 = "9" * 64
RUNTIME_RECEIPT_SHA256 = "a" * 64
RUNTIME_TREE_SHA256 = "b" * 64
RELEASE_TREE_SHA256 = "c" * 64
INTERPRETER_SHA256 = "1" * 64
INVENTORY_SHA256 = "2" * 64
LOCK_SHA256 = "3" * 64
LAUNCHER_SHA256 = "4" * 64
POSTGRES_ID = "5" * 64
QDRANT_ID = "6" * 64
POSTGRES_IMAGE_ID = "sha256:" + "a" * 64
QDRANT_IMAGE_ID = "sha256:" + "b" * 64


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()


def _artifacts() -> ExactInstallArtifacts:
    manifest = json.loads(
        (ROOT / "ops/governed_memory/installation/current/package_manifest.json")
        .read_text(encoding="utf-8")
    )
    values = {
        path: (ROOT / path).read_bytes()
        for path in manifest["artifacts"]
        if (ROOT / path).is_file()
    }
    return ExactInstallArtifacts.from_verified_mapping(values)


def _resolved_spec() -> dict[str, object]:
    static = load_store_spec(
        ROOT / "ops/governed_memory/installation/store_spec.json"
    )
    return bind_store_spec(
        static,
        ExecutionBinding(
            binding_sha256=BINDING_SHA256,
            authorization_id="phase9d-test-authorization",
            authorization_nonce_sha256="e" * 64,
            execution_id=EXECUTION_ID,
            package_manifest_sha256=PACKAGE_SHA256,
        ),
    )


class _Secrets:
    def generate_postgres_password(self) -> FreshSecret:
        return FreshSecret(b"p" * 43)

    def generate_qdrant_api_key(self) -> FreshSecret:
        return FreshSecret(b"q" * 43)


class _Invariants:
    def global_execution_lock_held(self) -> bool:
        return True

    def inspect_live_preflight(self) -> LivePreflightSnapshot:
        return LivePreflightSnapshot(True, True, 0, 0)


class _Files:
    def __init__(self, spec_sha256: str) -> None:
        self.spec_sha256 = spec_sha256
        self.spec = False
        self.postgres = False
        self.qdrant = False
        self.postgres_resource_id = secret_environment_public_id(
            POSTGRES_STORE_SECRET_PATH, EXECUTION_ID
        )
        self.qdrant_resource_id = secret_environment_public_id(
            QDRANT_STORE_SECRET_PATH, EXECUTION_ID
        )
        self.postflight: bytes | None = None
        self.secret_document_reprs: list[str] = []
        self.rendered_secret_documents: list[bytes] = []
        self.parent_directories = {
            CONTROLLER_CONFIG_DIRECTORY: ("root", "root", 0o700, True, False),
            STORE_SECRET_DIRECTORY: ("root", "root", 0o700, True, False),
        }

    def observe_retained_parent_directories(
        self,
    ) -> tuple[BoundRetainedRootDirectorySnapshot, ...]:
        return tuple(
            BoundRetainedRootDirectorySnapshot(path, *self.parent_directories[path])
            for path in (CONTROLLER_CONFIG_DIRECTORY, STORE_SECRET_DIRECTORY)
        )

    def observe_resolved_store_spec(self) -> BoundResourceSnapshot:
        return (
            BoundResourceSnapshot(EffectPresence.EXACT, self.spec_sha256)
            if self.spec
            else BoundResourceSnapshot(EffectPresence.ABSENT)
        )

    def observe_postgres_secret(self) -> BoundResourceSnapshot:
        return (
            BoundResourceSnapshot(EffectPresence.EXACT, self.postgres_resource_id)
            if self.postgres
            else BoundResourceSnapshot(EffectPresence.ABSENT)
        )

    def observe_qdrant_secret(self) -> BoundResourceSnapshot:
        return (
            BoundResourceSnapshot(EffectPresence.EXACT, self.qdrant_resource_id)
            if self.qdrant
            else BoundResourceSnapshot(EffectPresence.ABSENT)
        )

    def create_resolved_store_spec(self, canonical_document: bytes) -> None:
        self.spec = True
        self.assert_canonical = json.loads(canonical_document.decode("ascii"))

    def _accept_secret(
        self, expected: str, document: SecretEnvironmentDocument
    ) -> None:
        if document.logical_name != expected:
            raise AssertionError("wrong secret environment")
        rendered = document._render_for_root_writer_only()
        expected_prefix = (
            b"# governed-memory-execution-id="
            + EXECUTION_ID.encode("ascii")
            + b"\n"
        )
        if document.execution_id != EXECUTION_ID or not rendered.startswith(
            expected_prefix
        ):
            raise AssertionError("secret execution binding malformed")
        if expected == "postgres":
            if b"POSTGRES_DB=postgres\n" not in rendered:
                raise AssertionError("postgres environment malformed")
        elif b"QDRANT__SERVICE__API_KEY=" not in rendered:
            raise AssertionError("qdrant environment malformed")
        self.secret_document_reprs.append(repr(document))
        self.rendered_secret_documents.append(rendered)

    def create_postgres_secret(self, document: SecretEnvironmentDocument) -> None:
        self._accept_secret("postgres", document)
        self.postgres = True

    def create_qdrant_secret(self, document: SecretEnvironmentDocument) -> None:
        self._accept_secret("qdrant", document)
        self.qdrant = True

    def remove_resolved_store_spec(self) -> None:
        self.spec = False

    def remove_postgres_secret(self) -> None:
        self.postgres = False

    def remove_qdrant_secret(self) -> None:
        self.qdrant = False

    def read_terminal_postflight_receipt(self) -> bytes | None:
        return self.postflight

    def create_terminal_postflight_receipt(self, canonical_document: bytes) -> None:
        if self.postflight is not None:
            raise AssertionError("postflight overwrite")
        self.postflight = canonical_document


class _Docker:
    def __init__(self, spec: dict[str, object], prerequisites: object) -> None:
        resources = spec["resources"]
        self.labels = {
            "network": canonical_labels_sha256(resources["network"]["labels"]),
            "postgres_volume": canonical_labels_sha256(
                resources["volumes"]["postgres"]["labels"]
            ),
            "qdrant_volume": canonical_labels_sha256(
                resources["volumes"]["qdrant"]["labels"]
            ),
            "postgres_container": canonical_labels_sha256(
                resources["containers"]["postgres"]["labels"]
            ),
            "qdrant_container": canonical_labels_sha256(
                resources["containers"]["qdrant"]["labels"]
            ),
        }
        self.prerequisites = prerequisites
        self.present: set[str] = set()
        self.running = {"postgres_container": False, "qdrant_container": False}

    def _plain(self, key: str, resource_id: str) -> BoundResourceSnapshot:
        if key not in self.present:
            return BoundResourceSnapshot(EffectPresence.ABSENT)
        return BoundResourceSnapshot(
            EffectPresence.EXACT,
            resource_id,
            resource_labels_sha256=self.labels[key],
        )

    def observe_network(self) -> BoundResourceSnapshot:
        return self._plain("network", "docker-network-0001")

    def observe_postgres_volume(self) -> BoundResourceSnapshot:
        return self._plain("postgres_volume", "docker-volume-postgres-0001")

    def observe_qdrant_volume(self) -> BoundResourceSnapshot:
        return self._plain("qdrant_volume", "docker-volume-qdrant-0001")

    def _container(self, logical_name: str) -> BoundResourceSnapshot:
        key = logical_name + "_container"
        if key not in self.present:
            return BoundResourceSnapshot(EffectPresence.ABSENT)
        expected = getattr(self.prerequisites.local_images, logical_name)
        return BoundResourceSnapshot(
            EffectPresence.EXACT,
            POSTGRES_ID if logical_name == "postgres" else QDRANT_ID,
            resource_labels_sha256=self.labels[key],
            image_id=expected.image_id,
            image_repo_digest=expected.repo_digest,
            running=self.running[key],
        )

    def observe_postgres_container(self) -> BoundResourceSnapshot:
        return self._container("postgres")

    def observe_qdrant_container(self) -> BoundResourceSnapshot:
        return self._container("qdrant")

    def create_network(self) -> None:
        self.present.add("network")

    def create_postgres_volume(self) -> None:
        self.present.add("postgres_volume")

    def create_qdrant_volume(self) -> None:
        self.present.add("qdrant_volume")

    def create_postgres_container(self) -> None:
        self.present.add("postgres_container")

    def create_qdrant_container(self) -> None:
        self.present.add("qdrant_container")

    def start_postgres_container(self) -> None:
        self.running["postgres_container"] = True

    def start_qdrant_container(self) -> None:
        self.running["qdrant_container"] = True

    def stop_postgres_container(self) -> None:
        self.running["postgres_container"] = False

    def stop_qdrant_container(self) -> None:
        self.running["qdrant_container"] = False

    def remove_postgres_container(self) -> None:
        self.present.discard("postgres_container")

    def remove_qdrant_container(self) -> None:
        self.present.discard("qdrant_container")

    def remove_postgres_volume(self) -> None:
        self.present.discard("postgres_volume")

    def remove_qdrant_volume(self) -> None:
        self.present.discard("qdrant_volume")

    def remove_network(self) -> None:
        self.present.discard("network")


class _Postgres:
    bind = POSTGRES_BIND

    def __init__(self, artifacts: ExactInstallArtifacts) -> None:
        self.artifacts = artifacts
        self.bootstrap = False
        self.migrations: set[str] = set()

    def _state(self, present: bool, resource_id: str) -> BoundResourceSnapshot:
        return (
            BoundResourceSnapshot(EffectPresence.EXACT, resource_id)
            if present
            else BoundResourceSnapshot(EffectPresence.ABSENT)
        )

    def observe_canonical_bootstrap(self) -> BoundResourceSnapshot:
        return self._state(self.bootstrap, self.artifacts.canonical_bootstrap.sha256)

    def observe_migration_0001(self) -> BoundResourceSnapshot:
        return self._state(
            "0001" in self.migrations,
            self.artifacts.migration_0001_forward.sha256,
        )

    def observe_migration_0003(self) -> BoundResourceSnapshot:
        return self._state(
            "0003" in self.migrations,
            self.artifacts.migration_0003_forward.sha256,
        )

    def observe_migration_0004(self) -> BoundResourceSnapshot:
        return self._state(
            "0004" in self.migrations,
            self.artifacts.migration_0004_forward.sha256,
        )

    def apply_canonical_bootstrap(self, bootstrap: object, preflight: object) -> None:
        if bootstrap is not self.artifacts.canonical_bootstrap:
            raise AssertionError("bootstrap artifact substitution")
        if preflight is not self.artifacts.roles_preflight:
            raise AssertionError("preflight artifact substitution")
        self.bootstrap = True

    def apply_migration_0001(self, artifact: object) -> None:
        self.migrations.add("0001")

    def apply_migration_0003(self, artifact: object) -> None:
        self.migrations.add("0003")

    def apply_migration_0004(self, artifact: object) -> None:
        self.migrations.add("0004")

    def rollback_migration_0004(self, artifact: object) -> None:
        self.migrations.discard("0004")

    def rollback_migration_0003(self, artifact: object) -> None:
        self.migrations.discard("0003")

    def rollback_migration_0001(self, artifact: object) -> None:
        self.migrations.discard("0001")

    def rollback_canonical_bootstrap(self, artifact: object) -> None:
        self.bootstrap = False

    def inspect_prebootstrap(self) -> PrebootstrapPostgreSQLSnapshot:
        return PrebootstrapPostgreSQLSnapshot(
            POSTGRES_BIND,
            16,
            POSTGRES_SERVER_VERSION,
            "postgres",
            self.bootstrap,
            REQUIRED_ROLE_NAMES if self.bootstrap else (),
            0,
        )

    def inspect_terminal(self) -> TerminalPostgreSQLSnapshot:
        roles = tuple(
            CanonicalPostgreSQLRole(
                name, False, False, False, False, False, False, False
            )
            for name in REQUIRED_ROLE_NAMES
        )
        return TerminalPostgreSQLSnapshot(
            POSTGRES_BIND,
            16,
            POSTGRES_SERVER_VERSION,
            "governed_memory",
            TERMINAL_MIGRATION_IDS,
            roles,
            (
                PostgreSQLRoleMembership(
                    "governed_memory_owner", "governed_memory_bootstrap"
                ),
            ),
            (
                PostgreSQLCatalogIdentity(
                    "schema",
                    "memory",
                    "memory",
                    "governed_memory_owner",
                    "f" * 64,
                ),
            ),
            0,
            0,
            0,
        )


class _Qdrant:
    bind = QDRANT_BIND

    def __init__(self, artifacts: ExactInstallArtifacts) -> None:
        self.artifacts = artifacts
        self.collection = False
        self.alias = False

    def observe_collection(self) -> BoundResourceSnapshot:
        return (
            BoundResourceSnapshot(
                EffectPresence.EXACT, self.artifacts.qdrant_collection_sha256
            )
            if self.collection
            else BoundResourceSnapshot(EffectPresence.ABSENT)
        )

    def observe_alias(self) -> BoundResourceSnapshot:
        return (
            BoundResourceSnapshot(
                EffectPresence.EXACT, self.artifacts.qdrant_alias_sha256
            )
            if self.alias
            else BoundResourceSnapshot(EffectPresence.ABSENT)
        )

    def create_collection(self) -> None:
        self.collection = True

    def create_alias(self) -> None:
        self.alias = True

    def remove_alias(self) -> None:
        self.alias = False

    def remove_collection(self) -> None:
        self.collection = False

    def inspect_prebootstrap(self) -> PrebootstrapQdrantSnapshot:
        return PrebootstrapQdrantSnapshot(
            QDRANT_BIND, QDRANT_SERVER_VERSION, self.collection, 0, 0
        )

    def inspect_terminal(self) -> TerminalQdrantSnapshot:
        return TerminalQdrantSnapshot(
            QDRANT_BIND,
            QDRANT_SERVER_VERSION,
            self.collection,
            COLLECTION if self.alias else "",
            QdrantCollectionConfiguration(3072, "Dot", True, 1),
            0,
            0,
            0,
        )


class _Systemd:
    def __init__(self) -> None:
        self.unit_kind = FilesystemNodeKind.ABSENT
        self.unit_sha256: str | None = None
        self.enablement_kind = FilesystemNodeKind.ABSENT
        self.enablement_target: str | None = None

    def observe_supervisor(self) -> BoundSystemdSupervisorSnapshot:
        return BoundSystemdSupervisorSnapshot(
            SYSTEMD_UNIT_PATH,
            self.unit_kind,
            self.unit_sha256,
            SYSTEMD_ENABLEMENT_PATH,
            self.enablement_kind,
            self.enablement_target,
        )

    def install_and_enable_supervisor(
        self, exact: ExactSystemdSupervisor
    ) -> None:
        if type(exact) is not ExactSystemdSupervisor:
            raise AssertionError("unbound supervisor install")
        self.unit_kind = FilesystemNodeKind.REGULAR_FILE
        self.unit_sha256 = hashlib.sha256(exact.unit_content).hexdigest()
        self.enablement_kind = FilesystemNodeKind.SYMLINK
        self.enablement_target = exact.enablement_target

    def disable_and_remove_supervisor(
        self, exact: ExactSystemdSupervisor
    ) -> None:
        if type(exact) is not ExactSystemdSupervisor:
            raise AssertionError("unbound supervisor removal")
        self.unit_kind = FilesystemNodeKind.ABSENT
        self.unit_sha256 = None
        self.enablement_kind = FilesystemNodeKind.ABSENT
        self.enablement_target = None


class LinuxStoreEffectsTests(unittest.TestCase):
    def test_absent_systemd_snapshot_is_valid_and_classifies_absent(self) -> None:
        snapshot = BoundSystemdSupervisorSnapshot.absent()
        self.assertIs(snapshot.unit_kind, FilesystemNodeKind.ABSENT)
        self.assertIs(snapshot.enablement_kind, FilesystemNodeKind.ABSENT)
        self.assertFalse(snapshot.daemon_reload_pending)
        exact = ExactSystemdSupervisor.from_rendered_unit(
            b"[Unit]\nDescription=absent snapshot regression\n"
        )
        self.assertIs(snapshot.classify(exact).presence, EffectPresence.ABSENT)

    def setUp(self) -> None:
        self.spec = _resolved_spec()
        self.spec_sha256 = _canonical_sha(self.spec)
        self.artifacts = _artifacts()
        postgres_repo = self.spec["resources"]["containers"]["postgres"]["image"][
            "repo_digest"
        ]
        qdrant_repo = self.spec["resources"]["containers"]["qdrant"]["image"][
            "repo_digest"
        ]
        self.prerequisites = SimpleNamespace(
            local_images=SimpleNamespace(
                postgres=SimpleNamespace(
                    image_id=POSTGRES_IMAGE_ID,
                    repo_digest=postgres_repo,
                ),
                qdrant=SimpleNamespace(
                    image_id=QDRANT_IMAGE_ID,
                    repo_digest=qdrant_repo,
                ),
            )
        )
        self.files = _Files(self.spec_sha256)
        self.docker = _Docker(self.spec, self.prerequisites)
        self.postgres = _Postgres(self.artifacts)
        self.qdrant = _Qdrant(self.artifacts)
        self.systemd = _Systemd()
        self.transports = BoundLinuxStoreTransports(
            _Invariants(),
            self.files,
            self.docker,
            self.postgres,
            self.qdrant,
            self.systemd,
        )
        self.readiness = ClosedStoreReadinessProbe(
            postgres=self.postgres,
            qdrant=self.qdrant,
            expected_postgres_catalog_sha256=(
                "b03adbc71a48465b61b5388e9e2a85090f81c3045aa7af733480c186c1936077"
            ),
        )
        runtime_root = (
            "/opt/governed-memory-controller/runtimes/" + RUNTIME_RECEIPT_SHA256
        )
        release_root = (
            "/opt/governed-memory-controller/releases/" + PACKAGE_SHA256
        )
        self.identity = _ExecutionIdentity(
            EXECUTION_ID,
            ATTEMPT_ID,
            BINDING_SHA256,
            PACKAGE_SHA256,
            self.spec_sha256,
            RUNTIME_RECEIPT_SHA256,
            runtime_root,
            RUNTIME_TREE_SHA256,
            release_root,
            RELEASE_TREE_SHA256,
            release_root
            + "/ops/governed_memory/installation/current/package_manifest.json",
            runtime_root + "/bin/python",
            INTERPRETER_SHA256,
            runtime_root + "/controller-distributions.json",
            INVENTORY_SHA256,
            LOCK_SHA256,
            release_root
            + "/tools/governed_memory_install/store_supervisor_launcher.py",
            LAUNCHER_SHA256,
        )
        self.operations = LinuxStoreHostOperations(
            identity=self.identity,
            resolved_store_spec=self.spec,
            artifacts=self.artifacts,
            prerequisites=self.prerequisites,
            transports=self.transports,
            readiness_probe=self.readiness,
            secret_source=_Secrets(),
        )

    def test_closed_physical_driver_revalidates_revision_and_fixed_dispatch(self) -> None:
        self.docker.create_network()
        exact_supervisor = ExactSystemdSupervisor.from_rendered_unit(
            self.artifacts.render_supervisor_unit(
                execution_id=EXECUTION_ID,
                runtime_receipt_sha256=RUNTIME_RECEIPT_SHA256,
                package_manifest_sha256=PACKAGE_SHA256,
            )
        )
        driver = ClosedLinuxPhysicalRollbackDriver(
            transports=self.transports,
            resolved_store_spec=self.spec,
            exact_supervisor=exact_supervisor,
        )
        target = LedgerBoundPhysicalTarget(
            resource_key="network",
            resource_kind="network",
            resource_name=self.spec["resources"]["network"]["name"],
            resource_id="docker-network-0001",
            ownership_sha256="a" * 64,
            resource_identity_sha256="b" * 64,
            resource_labels_sha256=canonical_labels_sha256(
                self.spec["resources"]["network"]["labels"]
            ),
            image_id=None,
            image_repo_digest=None,
        )
        observed = driver.observe(target)
        self.assertEqual(observed.state, "present")
        self.assertEqual(
            canonical_labels_sha256(dict(observed.labels)),
            target.resource_labels_sha256,
        )
        with self.assertRaisesRegex(
            Exception, "revision_drift"
        ):
            driver.remove_network(target, "0" * 64)
        driver.remove_network(
            target,
            ExactPhysicalEmptyRollbackOperations._revision(observed),
        )
        self.assertEqual(driver.observe(target).state, "absent")

    def _request(
        self, step_id: str, profile: HostOperationProfile
    ) -> HostOperationRequest:
        return HostOperationRequest(
            profile=profile,
            step_id=step_id,
            execution_id=EXECUTION_ID,
            attempt_id=ATTEMPT_ID,
            execution_binding_sha256=BINDING_SHA256,
            package_manifest_sha256=PACKAGE_SHA256,
            resolved_store_spec_sha256=self.spec_sha256,
            controller_runtime_receipt_sha256=RUNTIME_RECEIPT_SHA256,
            controller_runtime_root=self.identity.controller_runtime_root,
            controller_runtime_tree_sha256=RUNTIME_TREE_SHA256,
            controller_release_root=self.identity.controller_release_root,
            controller_release_tree_sha256=RELEASE_TREE_SHA256,
            controller_release_package_manifest_path=(
                self.identity.controller_release_package_manifest_path
            ),
            controller_runtime_interpreter_path=(
                self.identity.controller_runtime_interpreter_path
            ),
            controller_runtime_interpreter_sha256=INTERPRETER_SHA256,
            controller_runtime_inventory_path=(
                self.identity.controller_runtime_inventory_path
            ),
            controller_runtime_inventory_sha256=INVENTORY_SHA256,
            controller_requirements_lock_sha256=LOCK_SHA256,
            supervisor_launcher_path=self.identity.supervisor_launcher_path,
            supervisor_launcher_sha256=LAUNCHER_SHA256,
            postflight_receipt_path=(
                "/var/lib/governed-memory-controller/executions/"
                + EXECUTION_ID
                + "/terminal-postflight-receipt.json"
            ),
            resource_targets=self.operations._expected_targets(step_id),
        )

    def test_all_nineteen_profiles_are_closed_exact_and_crash_reobservable(self) -> None:
        for step in STORES_ONLY_PLAN:
            with self.subTest(step=step.step_id):
                request = self._request(
                    step.step_id, HostOperationProfile(step.effect)
                )
                before = self.operations.observe(request)
                self.assertEqual(
                    before.state,
                    "after" if step.invariant_only else "before",
                )
                result = self.operations.apply(request, before)
                after = self.operations.observe(request)
                self.assertEqual(after.state, "after")
                self.assertEqual(result.identities, after.identities)
                if step.step_id == "I10_START_AND_VERIFY_EMPTY_STORES":
                    fresh = self.readiness.verify_fresh_empty_stores()
                    self.assertFalse(fresh.postgres.target_database_exists)
                    self.assertEqual(fresh.postgres.present_target_role_names, ())
                if step.step_id == "I19_COLD_RESTART_AND_VERIFY_TERMINAL_POSTFLIGHT":
                    self.assertIsNotNone(result.postflight_receipt_sha256)
                    self.assertEqual(
                        result.postflight_receipt_sha256,
                        after.postflight_receipt_sha256,
                    )
                    self.assertEqual(self.operations.observe(request), after)
        self.assertEqual(len(self.files.secret_document_reprs), 2)
        self.assertTrue(
            all("<redacted>" in value for value in self.files.secret_document_reprs)
        )
        self.assertTrue(
            all(
                (
                    "# governed-memory-execution-id=" + EXECUTION_ID
                ).encode("ascii")
                in document
                for document in self.files.rendered_secret_documents
            )
        )
        public = repr(
            (
                self.operations.observe(
                    self._request(
                        STORES_ONLY_PLAN[3].step_id,
                        HostOperationProfile(STORES_ONLY_PLAN[3].effect),
                    )
                ),
                json.loads(self.files.postflight.decode("ascii")),
            )
        )
        self.assertNotIn("p" * 43, public)
        self.assertNotIn("q" * 43, public)

    def test_partial_file_step_recovers_without_overwriting_existing_secret(self) -> None:
        self.files.qdrant = True
        request = self._request(
            STORES_ONLY_PLAN[3].step_id,
            HostOperationProfile(STORES_ONLY_PLAN[3].effect),
        )
        before = self.operations.observe(request)
        self.assertEqual(before.state, "recoverable")
        self.operations.apply(request, before)
        self.assertTrue(self.files.qdrant)
        self.assertTrue(self.files.postgres)
        self.assertTrue(self.files.spec)
        self.assertEqual(len(self.files.secret_document_reprs), 1)
        self.assertIn("postgres", self.files.secret_document_reprs[0])

    def test_secret_bytes_cannot_be_persisted_as_public_resource_identity(
        self,
    ) -> None:
        step = STORES_ONLY_PLAN[3]
        request = self._request(step.step_id, HostOperationProfile(step.effect))
        self.operations.apply(request, self.operations.observe(request))
        self.files.postgres_resource_id = "p" * 43
        with self.assertRaisesRegex(
            LinuxStoreEffectsError, "linux_store_resource_identity_mismatch"
        ):
            self.operations.observe(request)

    def test_exact_compensation_uses_closed_reverse_profile(self) -> None:
        create = self._request(
            STORES_ONLY_PLAN[4].step_id,
            HostOperationProfile(STORES_ONLY_PLAN[4].effect),
        )
        self.operations.apply(create, self.operations.observe(create))
        remove = self._request(
            STORES_ONLY_PLAN[4].step_id,
            HostOperationProfile(STORES_ONLY_PLAN[4].rollback),
        )
        observed = self.operations.observe(remove)
        self.assertEqual(observed.state, "after")
        self.operations.compensate(remove, observed)
        self.assertEqual(self.operations.observe(remove).state, "before")

    def test_supervisor_recovers_exact_partial_prefix_and_refuses_drift(
        self,
    ) -> None:
        step = next(
            item
            for item in STORES_ONLY_PLAN
            if item.step_id == "I18_INSTALL_AND_ENABLE_STORES_SUPERVISOR"
        )
        install = self._request(
            step.step_id, HostOperationProfile(step.effect)
        )
        exact = self.operations._supervisor
        self.systemd.unit_kind = FilesystemNodeKind.REGULAR_FILE
        self.systemd.unit_sha256 = exact.unit_sha256
        partial = self.operations.observe(install)
        self.assertEqual(partial.state, "recoverable")
        self.operations.apply(install, partial)
        self.assertEqual(self.operations.observe(install).state, "after")

        self.systemd.enablement_target = "/etc/systemd/system/wrong.service"
        drift = self.operations.observe(install)
        self.assertEqual(drift.state, "drift")
        with self.assertRaisesRegex(
            LinuxStoreEffectsError, "linux_store_apply_state_invalid"
        ):
            self.operations.apply(install, drift)

        self.systemd.disable_and_remove_supervisor(exact)
        result = self.operations.apply(install, self.operations.observe(install))
        self.assertEqual(
            result.identities[0].resource_id,
            exact.composite_identity_sha256,
        )
        self.assertEqual(self.systemd.enablement_target, SYSTEMD_ENABLEMENT_TARGET)
        remove = self._request(
            step.step_id, HostOperationProfile(step.rollback)
        )
        self.operations.compensate(remove, self.operations.observe(remove))
        self.assertEqual(self.operations.observe(remove).state, "before")

    def test_leaf_rollback_retains_preinstalled_root_directories(self) -> None:
        step = STORES_ONLY_PLAN[3]
        install = self._request(step.step_id, HostOperationProfile(step.effect))
        before_parents = dict(self.files.parent_directories)
        self.operations.apply(install, self.operations.observe(install))
        remove = self._request(step.step_id, HostOperationProfile(step.rollback))
        self.operations.compensate(remove, self.operations.observe(remove))
        self.assertEqual(self.files.parent_directories, before_parents)

        self.files.parent_directories[STORE_SECRET_DIRECTORY] = (
            "root",
            "root",
            0o755,
            True,
            False,
        )
        self.assertEqual(self.operations.observe(install).state, "drift")

    def test_wrong_profile_or_changed_observation_is_refused_content_free(self) -> None:
        request = self._request(
            STORES_ONLY_PLAN[4].step_id,
            HostOperationProfile(STORES_ONLY_PLAN[4].effect),
        )
        observed = self.operations.observe(request)
        self.docker.present.add("network")
        with self.assertRaisesRegex(
            LinuxStoreEffectsError, "linux_store_observation_changed"
        ) as caught:
            self.operations.apply(request, observed)
        self.assertIsNone(caught.exception.__cause__)
        wrong = self._request(
            STORES_ONLY_PLAN[4].step_id,
            HostOperationProfile.CREATE_EXACT_POSTGRES_VOLUME,
        )
        with self.assertRaisesRegex(
            LinuxStoreEffectsError, "linux_store_request_profile_invalid"
        ):
            self.operations.observe(wrong)


if __name__ == "__main__":
    unittest.main()
