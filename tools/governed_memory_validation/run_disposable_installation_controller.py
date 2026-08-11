from __future__ import annotations

"""Hermetic Phase 8A proof for the synthetic inactive-install controller.

This runner cannot install anything.  It rejects process creation, sockets,
name resolution, and filesystem writes outside its one disposable root.  Its
only backend is the sealed in-memory SyntheticBackend.
"""

import sys

sys.dont_write_bytecode = True

from pathlib import Path

_IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))

from collections import Counter
import hashlib
import json
import os
import socket
import subprocess
import tempfile
from typing import Callable, Final


_IMPORT_PHASE_ACTIVE = True
_IMPORT_PHASE_BLOCKED: Counter[str] = Counter()


def _import_phase_audit(event: str, args: tuple[object, ...]) -> None:
    """Block mutations and external effects before local package imports."""

    if not _IMPORT_PHASE_ACTIVE:
        return
    process_events = {
        "subprocess.Popen",
        "os.system",
        "os.fork",
        "os.forkpty",
        "pty.spawn",
    }
    socket_events = {
        "socket.__new__",
        "socket.connect",
        "socket.connect_ex",
        "socket.bind",
        "socket.sendto",
    }
    dns_events = {
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
    }
    mutation_events = {
        "os.remove",
        "os.rmdir",
        "os.mkdir",
        "os.chmod",
        "os.chown",
        "os.truncate",
        "os.utime",
        "os.setxattr",
        "os.removexattr",
        "os.rename",
        "os.replace",
        "os.link",
        "os.symlink",
        "os.mknod",
        "os.mkfifo",
        "mmap.__new__",
    }
    category: str | None = None
    if (
        event in process_events
        or event.startswith("os.exec")
        or event.startswith("os.spawn")
        or event.startswith("os.posix_spawn")
    ):
        category = "process"
    elif event in socket_events:
        category = "socket"
    elif event in dns_events:
        category = "dns"
    elif event in mutation_events:
        category = "mutation"
    elif event == "open" and len(args) >= 3:
        mode, flags = args[1], args[2]
        mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if (
            isinstance(mode, str)
            and any(token in mode for token in "wax+")
        ) or (isinstance(flags, int) and bool(flags & mask)):
            category = "mutation"
    if category is not None:
        _IMPORT_PHASE_BLOCKED[category] += 1
        raise RuntimeError("phase8a_local_import_effect_forbidden:" + category)


sys.addaudithook(_import_phase_audit)

from tools.governed_memory_install.controller import (
    AuthorizationError,
    ControllerInterruption,
    ControllerReceipt,
    DisposableControllerAuthorization,
    INSTALL_STEPS,
    InstallationCompensatedError,
    InactiveInstallationController,
    OperationStateError,
    ROLLBACK_STEPS,
    RollbackGateError,
    StateDriftError,
    StepState,
)
from tools.governed_memory_install.journal import (
    FileJournal,
    JournalIntegrityError,
    parse_journal_bytes,
)
from tools.governed_memory_install.inactive_installation import (
    InstallationPackageError,
    verify_package,
)
from tools.governed_memory_install.synthetic_backend import (
    SyntheticBackend,
    SyntheticEffectFailure,
)

_IMPORT_PHASE_ACTIVE = False
IMPORT_PHASE_GUARD_COMPLETED: Final = True


REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
PLAN_PATH: Final = (
    REPOSITORY_ROOT
    / "ops/governed_memory/installation/controller_plan.json"
)
ROLLBACK_SQL_PATH: Final = (
    REPOSITORY_ROOT
    / "ops/governed_memory/installation/postgres/"
    "canonical_cluster_rollback.pgsql.in"
)
ARTIFACT_PATHS: Final = (
    "ops/governed_memory/installation/controller_plan.json",
    "ops/governed_memory/installation/postgres/"
    "canonical_cluster_rollback.pgsql.in",
    "tools/governed_memory_install/controller.py",
    "tools/governed_memory_install/journal.py",
    "tools/governed_memory_install/synthetic_backend.py",
    "tools/governed_memory_validation/"
    "run_disposable_installation_controller.py",
)
PROOF_SCHEMA_VERSION: Final = (
    "governed-memory-installation-controller-disposable-proof-v1"
)
EXPECTED_PACKAGE_ARTIFACT_COUNT: Final = 59
EXPECTED_RETAINED_EFFECTS: Final = (
    "I04_QUARANTINE_LEGACY_SECRET",
    "I05_CREATE_SERVICE_IDENTITY",
    "I29_SEAL_INACTIVE_POSTFLIGHT",
)
EXPECTED_RETAINED_RESOURCES: Final = (
    "inactive_postflight_receipt",
    "legacy_secret_quarantine",
    "root:backup_root",
    "root:environment_root",
    "root:install_root",
    "root:runtime_environment_root",
    "root:state_root",
    "service_identity",
)
HASH_ZERO: Final = "0" * 64


class ProofFailure(RuntimeError):
    pass


class AuditViolation(ProofFailure):
    pass


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ProofFailure(code)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _remove_disposable_tree(root: Path) -> None:
    """Remove only the already-resolved disposable root with absolute paths."""

    _require(root.is_absolute() and root.name.startswith("phase8a-controller-proof-"), "cleanup_root_invalid")
    descendants = sorted(
        root.rglob("*"),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in descendants:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
        else:
            raise ProofFailure("cleanup_entry_type_invalid")
    root.rmdir()


class ProofAudit:
    """Fail-closed interpreter audit policy for the disposable proof."""

    _SIMPLE_PATH_EVENTS: Final = {
        "os.remove": (0, 1),
        "os.rmdir": (0, 1),
        "os.mkdir": (0, 2),
        "os.chmod": (0, 2),
        "os.chown": (0, 3),
        "os.truncate": (0, None),
        "os.utime": (0, 3),
        "os.setxattr": (0, None),
        "os.removexattr": (0, None),
        "os.mknod": (0, 3),
        "os.mkfifo": (0, 2),
    }

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        self.blocked_attempts: Counter[str] = Counter()
        self._protected: frozenset[Path] = frozenset()

    def protect(self, paths: tuple[Path, ...]) -> None:
        self._protected = frozenset(
            path.resolve(strict=True) for path in paths
        )

    def release_protection(self) -> None:
        self._protected = frozenset()

    def _fd_directory(self, descriptor: int) -> Path:
        for prefix in (Path("/proc/self/fd"), Path("/dev/fd")):
            try:
                return Path(os.readlink(prefix / str(descriptor))).resolve()
            except OSError:
                continue
        raise AuditViolation("audit_dir_fd_unresolvable")

    def _resolve_path(
        self,
        raw_path: object,
        directory_fd: object = None,
    ) -> Path | None:
        if isinstance(raw_path, int):
            if raw_path < 0:
                raise AuditViolation("audit_fd_invalid")
            return self._fd_directory(raw_path)
        try:
            path = Path(os.fsdecode(os.fspath(raw_path)))
        except (TypeError, ValueError) as error:
            raise AuditViolation("audit_path_invalid") from error
        if not path.is_absolute():
            if isinstance(directory_fd, int) and directory_fd >= 0:
                path = self._fd_directory(directory_fd) / path
            else:
                path = Path.cwd() / path
        return path.resolve(strict=False)

    def _inside_root(self, path: Path) -> bool:
        return path == self.root or self.root in path.parents

    def _touches_protected(self, path: Path) -> bool:
        return any(
            path == protected or path in protected.parents
            for protected in self._protected
        )

    def _check_mutation_path(
        self,
        raw_path: object,
        directory_fd: object = None,
    ) -> None:
        path = self._resolve_path(raw_path, directory_fd)
        if path is None:
            return
        if not self._inside_root(path):
            self.blocked_attempts["write_outside_temp_root"] += 1
            raise AuditViolation("write_outside_disposable_root")
        if self._touches_protected(path):
            self.blocked_attempts["sentinel_mutation"] += 1
            raise AuditViolation("protected_sentinel_mutation")

    @staticmethod
    def _open_is_mutating(mode: object, flags: object) -> bool:
        if isinstance(mode, str) and any(token in mode for token in "wax+"):
            return True
        if isinstance(flags, int):
            mask = (
                os.O_WRONLY
                | os.O_RDWR
                | os.O_CREAT
                | os.O_TRUNC
                | os.O_APPEND
            )
            return bool(flags & mask)
        return False

    def __call__(self, event: str, args: tuple[object, ...]) -> None:
        if event in {
            "subprocess.Popen",
            "os.system",
            "os.fork",
            "os.forkpty",
            "os.posix_spawn",
            "os.posix_spawnp",
            "pty.spawn",
        } or event.startswith("os.exec") or event.startswith("os.spawn"):
            self.blocked_attempts["subprocess"] += 1
            raise AuditViolation("subprocess_forbidden")
        if event in {
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyname_ex",
            "socket.gethostbyaddr",
            "socket.getnameinfo",
        }:
            self.blocked_attempts["dns"] += 1
            raise AuditViolation("dns_forbidden")
        if event in {
            "socket.__new__",
            "socket.connect",
            "socket.connect_ex",
            "socket.bind",
            "socket.sendto",
        }:
            self.blocked_attempts["socket"] += 1
            raise AuditViolation("socket_forbidden")
        if event == "mmap.__new__":
            self.blocked_attempts["mmap"] += 1
            raise AuditViolation("mmap_forbidden")
        if event == "open" and len(args) >= 3:
            if self._open_is_mutating(args[1], args[2]):
                self._check_mutation_path(args[0])
            return
        if event in self._SIMPLE_PATH_EVENTS:
            path_index, fd_index = self._SIMPLE_PATH_EVENTS[event]
            directory_fd = (
                args[fd_index]
                if fd_index is not None and len(args) > fd_index
                else None
            )
            self._check_mutation_path(args[path_index], directory_fd)
            return
        if event in {"os.rename", "os.replace", "os.link"}:
            source_fd = args[2] if len(args) > 2 else None
            target_fd = args[3] if len(args) > 3 else None
            self._check_mutation_path(args[0], source_fd)
            self._check_mutation_path(args[1], target_fd)
            return
        if event == "os.symlink":
            target_fd = args[2] if len(args) > 2 else None
            self._check_mutation_path(args[1], target_fd)


class ScenarioMatrix:
    def __init__(self, sentinel_check: Callable[[], None]) -> None:
        self.entries: list[dict[str, str]] = []
        self._labels: set[str] = set()
        self._sentinel_check = sentinel_check

    def record(
        self,
        category: str,
        label: str,
        outcome: str,
        receipt_sha256: str = HASH_ZERO,
    ) -> None:
        _require(label not in self._labels, "scenario_label_duplicate")
        _require(len(receipt_sha256) == 64, "scenario_receipt_hash_invalid")
        self._sentinel_check()
        self._labels.add(label)
        self.entries.append(
            {
                "category": category,
                "label": label,
                "outcome": outcome,
                "receipt_sha256": receipt_sha256,
            }
        )

    def count(self, category: str) -> int:
        return sum(entry["category"] == category for entry in self.entries)

    @property
    def sha256(self) -> str:
        return _sha256_bytes(_canonical_bytes(self.entries))


def _load_and_verify_plan() -> dict[str, object]:
    document = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    _require(isinstance(document, dict), "controller_plan_not_object")
    install_ids = tuple(
        step["id"] for step in document.get("install_steps", [])
    )
    rollback_ids = tuple(
        step["id"] for step in document.get("rollback_steps", [])
    )
    _require(install_ids == INSTALL_STEPS, "controller_plan_install_steps_drift")
    _require(
        rollback_ids == ROLLBACK_STEPS,
        "controller_plan_rollback_steps_drift",
    )
    i29 = next(
        (
            step
            for step in document.get("install_steps", [])
            if isinstance(step, dict)
            and step.get("id") == "I29_SEAL_INACTIVE_POSTFLIGHT"
        ),
        None,
    )
    _require(
        isinstance(i29, dict)
        and i29.get("artifact_refs")
        == [
            "ops/governed_memory/installation/authority/"
            "installation_execution_receipt.schema.json",
            "ops/governed_memory/installation/receipt.schema.json",
        ],
        "controller_plan_postflight_receipt_schema_drift",
    )
    scope = document.get("scope")
    _require(isinstance(scope, dict), "controller_plan_scope_invalid")
    _require(
        scope.get("source_postgresql_steps") == [],
        "controller_plan_source_postgresql_not_empty",
    )
    _require(
        scope.get("source_bridge_migrations") == [],
        "controller_plan_source_bridge_not_empty",
    )
    canonical = _canonical_bytes(document)
    _require(b"0002_conversation_bridge" not in canonical, "migration_0002_in_plan")
    _require(b"source_cluster_roles.pgsql" not in canonical, "source_roles_in_plan")
    missing = document.get("required_adapter_capabilities_without_packaged_artifacts")
    _require(isinstance(missing, list), "controller_plan_adapter_blockers_invalid")
    blockers = {
        item.get("typed_blocker") for item in missing if isinstance(item, dict)
    }
    _require(
        blockers
        == {
            "store_supervisor_artifact_not_packaged",
            "encrypted_backup_restore_adapter_artifact_not_packaged",
            "trusted_clock_and_atomic_single_use_nonce_claim_not_packaged",
            "canonical_global_execution_lock_not_packaged",
            "external_journal_seal_anchor_not_packaged",
            "exact_live_probe_adapter_not_packaged",
            "same_filesystem_quarantine_preflight_adapter_not_packaged",
            "canonical_cluster_rollback_not_disposable_postgresql_executed",
            "linux_execution_backend_hard_disabled",
        },
        "controller_plan_adapter_blockers_drift",
    )
    return document


def _verify_rollback_sql() -> None:
    sql = ROLLBACK_SQL_PATH.read_text(encoding="utf-8")
    upper = sql.upper()
    _require("CASCADE" not in upper, "canonical_rollback_cascade_present")
    _require("DROP OWNED" not in upper, "canonical_rollback_drop_owned_present")
    _require(
        upper.count("DROP DATABASE GOVERNED_MEMORY;") == 1,
        "canonical_rollback_database_drop_drift",
    )
    _require(
        "GOVERNED_MEMORY_ROLES_ONLY_RECOVERY_PREFLIGHT" in upper,
        "canonical_rollback_recovery_branch_absent",
    )
    _require(
        "PG_CATALOG.PG_SHDEPEND" in upper,
        "canonical_rollback_dependency_guard_absent",
    )
    _require(
        "BEGIN;" in upper and "COMMIT;" in upper,
        "canonical_rollback_role_transaction_absent",
    )


def _artifact_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in ARTIFACT_PATHS:
        path = REPOSITORY_ROOT / relative
        _require(path.is_file() and not path.is_symlink(), "proof_artifact_invalid")
        hashes[relative] = _sha256_file(path)
    return hashes


def _scenario_directory(root: Path, label: str) -> Path:
    safe = "".join(character if character.isalnum() else "_" for character in label)
    path = root / "scenarios" / safe
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(0o700)
    return path


def _journal_seal(path: Path, binding_sha256: str) -> tuple[str, int]:
    entries = parse_journal_bytes(
        path.read_bytes(),
        binding_sha256=binding_sha256,
    )
    return (entries[-1].entry_sha256 if entries else HASH_ZERO, len(entries))


def _call_controller(
    *,
    backend: SyntheticBackend,
    journal_path: Path,
    binding_sha256: str,
    authorization: DisposableControllerAuthorization,
    attempt_id: str,
    expected_seal: tuple[str, int] | None = None,
) -> ControllerReceipt:
    journal_arguments: dict[str, object] = {
        "binding_sha256": binding_sha256,
    }
    if expected_seal is not None:
        journal_arguments.update(
            {
                "expected_head_sha256": expected_seal[0],
                "expected_sequence": expected_seal[1],
            }
        )
    with FileJournal(journal_path, **journal_arguments) as journal:
        controller = InactiveInstallationController(
            backend=backend,
            journal=journal,
            binding_sha256=binding_sha256,
        )
        if authorization.operation == "install":
            return controller.install(authorization, attempt_id=attempt_id)
        return controller.rollback(authorization, attempt_id=attempt_id)


def _authorization(
    operation: str,
    binding_sha256: str,
) -> DisposableControllerAuthorization:
    proof_hash = _sha256_bytes(
        ("phase8a-disposable-only:" + operation + ":" + binding_sha256).encode(
            "ascii"
        )
    )
    return DisposableControllerAuthorization(
        operation=operation,
        binding_sha256=binding_sha256,
        proof_receipt_sha256=proof_hash,
    )


def _assert_synthetic_zero_external(backend: SyntheticBackend) -> None:
    _require(backend.controller_mode == "phase8a_synthetic_disposable_only", "backend_mode_drift")
    _require(backend.provider_calls == 0, "synthetic_provider_call_nonzero")
    _require(
        backend.source_postgresql_actions == 0,
        "synthetic_source_postgresql_action_nonzero",
    )
    _require(
        backend.runtime_application_starts == 0,
        "synthetic_runtime_application_start_nonzero",
    )
    _require(backend.network_calls == 0, "synthetic_network_call_nonzero")


def _assert_effect_once(
    backend: SyntheticBackend,
    operation: str,
    steps: tuple[str, ...],
) -> None:
    for step in steps:
        _require(
            backend.effect_count(operation, step) == 1,
            "synthetic_effect_count_drift:" + operation + ":" + step,
        )


def _assert_rollback_terminal(backend: SyntheticBackend) -> None:
    _require(backend.resources_remaining == (), "synthetic_resource_retained")
    _require(
        backend.retained_effects == EXPECTED_RETAINED_EFFECTS,
        "synthetic_retained_effect_set_drift",
    )
    _require(
        backend.retained_resources == EXPECTED_RETAINED_RESOURCES,
        "synthetic_retained_resource_set_drift",
    )
    _assert_synthetic_zero_external(backend)


def _run_happy_paths(
    root: Path,
    binding: str,
    matrix: ScenarioMatrix,
) -> tuple[str, str]:
    install_authority = _authorization("install", binding)
    rollback_authority = _authorization("rollback", binding)
    first_install: dict[str, object] | None = None
    first_rollback: dict[str, object] | None = None
    for index in range(2):
        directory = _scenario_directory(root, "happy_" + str(index + 1))
        journal_path = directory / "controller.journal"
        backend = SyntheticBackend()
        install_receipt = _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=install_authority,
            attempt_id="happy-install",
        )
        rollback_receipt = _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=rollback_authority,
            attempt_id="happy-rollback",
            expected_seal=(
                install_receipt.journal_head_sha256,
                install_receipt.journal_sequence,
            ),
        )
        _assert_effect_once(backend, "install", INSTALL_STEPS)
        _assert_effect_once(backend, "rollback", ROLLBACK_STEPS)
        _assert_rollback_terminal(backend)
        if first_install is None:
            first_install = install_receipt.as_dict()
            first_rollback = rollback_receipt.as_dict()
        else:
            _require(
                install_receipt.as_dict() == first_install,
                "happy_install_not_deterministic",
            )
            _require(
                rollback_receipt.as_dict() == first_rollback,
                "happy_rollback_not_deterministic",
            )
        matrix.record(
            "happy_path",
            "happy_" + str(index + 1),
            "install_and_empty_only_rollback_complete",
            rollback_receipt.receipt_sha256,
        )
    _require(first_install is not None and first_rollback is not None, "happy_receipt_absent")
    return (
        _sha256_bytes(_canonical_bytes(first_install)),
        _sha256_bytes(_canonical_bytes(first_rollback)),
    )


def _expect_interruption(call: Callable[[], object], code: str) -> None:
    try:
        call()
    except ControllerInterruption:
        return
    raise ProofFailure(code)


def _run_install_crash_matrix(
    root: Path,
    binding: str,
    matrix: ScenarioMatrix,
) -> None:
    install_authority = _authorization("install", binding)
    rollback_authority = _authorization("rollback", binding)
    boundaries = {
        "after_intent": "crash_after_intent_step",
        "after_effect": "crash_after_effect_step",
        "after_journal_commit": "crash_after_journal_commit_step",
    }
    for step in INSTALL_STEPS:
        for boundary, argument in boundaries.items():
            label = "install_crash_" + boundary + "_" + step
            directory = _scenario_directory(root, label)
            journal_path = directory / "controller.journal"
            backend = SyntheticBackend(**{argument: step})
            _expect_interruption(
                lambda: _call_controller(
                    backend=backend,
                    journal_path=journal_path,
                    binding_sha256=binding,
                    authorization=install_authority,
                    attempt_id="crash-install",
                ),
                "install_crash_not_injected:" + boundary + ":" + step,
            )
            seal = _journal_seal(journal_path, binding)
            backend.clear_faults()
            receipt = _call_controller(
                backend=backend,
                journal_path=journal_path,
                binding_sha256=binding,
                authorization=install_authority,
                attempt_id="crash-install",
                expected_seal=seal,
            )
            _assert_effect_once(backend, "install", INSTALL_STEPS)
            expected_recovered = (step,) if boundary == "after_effect" else ()
            _require(
                receipt.recovered_step_ids == expected_recovered,
                "install_recovered_steps_drift:" + boundary + ":" + step,
            )
            rollback_receipt = _call_controller(
                backend=backend,
                journal_path=journal_path,
                binding_sha256=binding,
                authorization=rollback_authority,
                attempt_id="crash-cleanup",
                expected_seal=(receipt.journal_head_sha256, receipt.journal_sequence),
            )
            _assert_effect_once(backend, "rollback", ROLLBACK_STEPS)
            _assert_rollback_terminal(backend)
            matrix.record(
                "crash_resume",
                label,
                "install_resumed_exactly_once",
                rollback_receipt.receipt_sha256,
            )


def _run_rollback_crash_matrix(
    root: Path,
    binding: str,
    matrix: ScenarioMatrix,
) -> None:
    install_authority = _authorization("install", binding)
    rollback_authority = _authorization("rollback", binding)
    boundaries = {
        "after_intent": "crash_after_intent_step",
        "after_effect": "crash_after_effect_step",
        "after_journal_commit": "crash_after_journal_commit_step",
    }
    for step in ROLLBACK_STEPS:
        for boundary, argument in boundaries.items():
            label = "rollback_crash_" + boundary + "_" + step
            directory = _scenario_directory(root, label)
            journal_path = directory / "controller.journal"
            backend = SyntheticBackend(**{argument: step})
            install_receipt = _call_controller(
                backend=backend,
                journal_path=journal_path,
                binding_sha256=binding,
                authorization=install_authority,
                attempt_id="rollback-crash-install",
            )
            _expect_interruption(
                lambda: _call_controller(
                    backend=backend,
                    journal_path=journal_path,
                    binding_sha256=binding,
                    authorization=rollback_authority,
                    attempt_id="rollback-crash",
                    expected_seal=(
                        install_receipt.journal_head_sha256,
                        install_receipt.journal_sequence,
                    ),
                ),
                "rollback_crash_not_injected:" + boundary + ":" + step,
            )
            seal = _journal_seal(journal_path, binding)
            backend.clear_faults()
            receipt = _call_controller(
                backend=backend,
                journal_path=journal_path,
                binding_sha256=binding,
                authorization=rollback_authority,
                attempt_id="rollback-crash",
                expected_seal=seal,
            )
            _assert_effect_once(backend, "install", INSTALL_STEPS)
            _assert_effect_once(backend, "rollback", ROLLBACK_STEPS)
            expected_recovered = (step,) if boundary == "after_effect" else ()
            _require(
                receipt.recovered_step_ids == expected_recovered,
                "rollback_recovered_steps_drift:" + boundary + ":" + step,
            )
            _assert_rollback_terminal(backend)
            matrix.record(
                "crash_resume",
                label,
                "rollback_resumed_exactly_once",
                receipt.receipt_sha256,
            )


def _run_install_failure_matrix(
    root: Path,
    binding: str,
    matrix: ScenarioMatrix,
) -> None:
    authorization = _authorization("install", binding)
    boundaries = {
        "before_effect": "fail_effect_step",
        "after_effect": "fail_after_effect_step",
    }
    for step in INSTALL_STEPS:
        for boundary, argument in boundaries.items():
            label = "install_failure_" + boundary + "_" + step
            directory = _scenario_directory(root, label)
            journal_path = directory / "controller.journal"
            backend = SyntheticBackend(**{argument: step})
            try:
                _call_controller(
                    backend=backend,
                    journal_path=journal_path,
                    binding_sha256=binding,
                    authorization=authorization,
                    attempt_id="failure-install",
                )
            except InstallationCompensatedError as error:
                _require(
                    error.receipt.outcome == "same_attempt_compensation_complete",
                    "install_compensation_outcome_drift",
                )
            else:
                raise ProofFailure("install_failure_not_compensated:" + step)
            _require(backend.resources_remaining == (), "install_compensation_incomplete")
            _assert_synthetic_zero_external(backend)
            seal = _journal_seal(journal_path, binding)
            backend.clear_faults()
            resume_receipt = _call_controller(
                backend=backend,
                journal_path=journal_path,
                binding_sha256=binding,
                authorization=authorization,
                attempt_id="failure-install",
                expected_seal=seal,
            )
            _require(
                resume_receipt.outcome == "same_attempt_compensated",
                "install_compensation_resume_drift",
            )
            matrix.record(
                "partial_failure",
                label,
                "same_attempt_compensated",
                resume_receipt.receipt_sha256,
            )


def _run_compensation_crash_matrix(
    root: Path,
    binding: str,
    matrix: ScenarioMatrix,
) -> None:
    authorization = _authorization("install", binding)
    boundaries = {
        "after_intent": "crash_after_intent_step",
        "after_effect": "crash_after_effect_step",
        "after_journal_commit": "crash_after_journal_commit_step",
    }
    for step in ROLLBACK_STEPS:
        for boundary, argument in boundaries.items():
            label = "compensation_crash_" + boundary + "_" + step
            directory = _scenario_directory(root, label)
            journal_path = directory / "controller.journal"
            backend = SyntheticBackend(
                fail_after_effect_step=INSTALL_STEPS[-1],
                **{argument: step},
            )
            _expect_interruption(
                lambda: _call_controller(
                    backend=backend,
                    journal_path=journal_path,
                    binding_sha256=binding,
                    authorization=authorization,
                    attempt_id="compensation-crash",
                ),
                "compensation_crash_not_injected:"
                + boundary
                + ":"
                + step,
            )
            seal = _journal_seal(journal_path, binding)
            backend.clear_faults()
            receipt = _call_controller(
                backend=backend,
                journal_path=journal_path,
                binding_sha256=binding,
                authorization=authorization,
                attempt_id="compensation-crash",
                expected_seal=seal,
            )
            _require(
                receipt.outcome == "same_attempt_compensated",
                "compensation_crash_resume_outcome_drift",
            )
            _assert_effect_once(backend, "install", INSTALL_STEPS)
            _assert_effect_once(backend, "compensation", ROLLBACK_STEPS)
            expected_recovered = (step,) if boundary == "after_effect" else ()
            _require(
                receipt.recovered_step_ids == expected_recovered,
                "compensation_recovered_steps_drift:"
                + boundary
                + ":"
                + step,
            )
            _assert_rollback_terminal(backend)
            matrix.record(
                "compensation_crash_resume",
                label,
                "same_attempt_compensation_resumed_exactly_once",
                receipt.receipt_sha256,
            )


def _run_partial_install_authorized_rollback_matrix(
    root: Path,
    binding: str,
    matrix: ScenarioMatrix,
) -> None:
    install_authority = _authorization("install", binding)
    rollback_authority = _authorization("rollback", binding)
    for index, step in enumerate(INSTALL_STEPS):
        label = "partial_install_authorized_rollback_" + step
        directory = _scenario_directory(root, label)
        journal_path = directory / "controller.journal"
        backend = SyntheticBackend(crash_after_effect_step=step)
        _expect_interruption(
            lambda: _call_controller(
                backend=backend,
                journal_path=journal_path,
                binding_sha256=binding,
                authorization=install_authority,
                attempt_id="partial-install",
            ),
            "partial_install_crash_not_injected:" + step,
        )
        seal = _journal_seal(journal_path, binding)
        backend.clear_faults()
        receipt = _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=rollback_authority,
            attempt_id="partial-install-rollback",
            expected_seal=seal,
        )
        _require(
            receipt.outcome == "authorized_empty_only_rollback_complete",
            "partial_install_rollback_outcome_drift:" + step,
        )
        for position, install_step in enumerate(INSTALL_STEPS):
            _require(
                backend.effect_count("install", install_step)
                == (1 if position <= index else 0),
                "partial_install_effect_count_drift:" + install_step,
            )
        _require(
            receipt.completed_step_ids[0]
            == "R01_VERIFY_EMPTY_EXACT_OWNERSHIP"
            and receipt.completed_step_ids[-1]
            == "R20_VERIFY_FINAL_ABSENCE",
            "partial_install_rollback_bounds_drift:" + step,
        )
        _require(
            backend.resources_remaining == (),
            "partial_install_rollback_resources_remain:" + step,
        )
        _assert_synthetic_zero_external(backend)
        matrix.record(
            "partial_install_authorized_rollback",
            label,
            "separately_authorized_empty_only_rollback_complete",
            receipt.receipt_sha256,
        )


def _run_rollback_failure_matrix(
    root: Path,
    binding: str,
    matrix: ScenarioMatrix,
) -> None:
    install_authority = _authorization("install", binding)
    rollback_authority = _authorization("rollback", binding)
    boundaries = {
        "before_effect": "fail_effect_step",
        "after_effect": "fail_after_effect_step",
    }
    for step in ROLLBACK_STEPS:
        for boundary, argument in boundaries.items():
            label = "rollback_failure_" + boundary + "_" + step
            directory = _scenario_directory(root, label)
            journal_path = directory / "controller.journal"
            backend = SyntheticBackend(**{argument: step})
            install_receipt = _call_controller(
                backend=backend,
                journal_path=journal_path,
                binding_sha256=binding,
                authorization=install_authority,
                attempt_id="rollback-failure-install",
            )
            try:
                _call_controller(
                    backend=backend,
                    journal_path=journal_path,
                    binding_sha256=binding,
                    authorization=rollback_authority,
                    attempt_id="rollback-failure",
                    expected_seal=(
                        install_receipt.journal_head_sha256,
                        install_receipt.journal_sequence,
                    ),
                )
            except SyntheticEffectFailure:
                pass
            else:
                raise ProofFailure("rollback_failure_not_injected:" + step)
            seal = _journal_seal(journal_path, binding)
            backend.clear_faults()
            receipt = _call_controller(
                backend=backend,
                journal_path=journal_path,
                binding_sha256=binding,
                authorization=rollback_authority,
                attempt_id="rollback-failure",
                expected_seal=seal,
            )
            _assert_effect_once(backend, "rollback", ROLLBACK_STEPS)
            expected_recovered = (step,) if boundary == "after_effect" else ()
            _require(
                receipt.recovered_step_ids == expected_recovered,
                "rollback_failure_recovery_drift:" + boundary + ":" + step,
            )
            _assert_rollback_terminal(backend)
            matrix.record(
                "partial_failure",
                label,
                "rollback_resumed_exactly_once",
                receipt.receipt_sha256,
            )


def _expect_exception(
    expected: type[BaseException],
    call: Callable[[], object],
    code: str,
) -> BaseException:
    try:
        call()
    except expected as error:
        return error
    raise ProofFailure(code)


def _run_tamper_and_partial_refusals(
    root: Path,
    binding: str,
    matrix: ScenarioMatrix,
) -> None:
    install_authority = _authorization("install", binding)
    rollback_authority = _authorization("rollback", binding)

    label = "tamper_journal_hash"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    backend = SyntheticBackend(crash_after_intent_step=INSTALL_STEPS[0])
    _expect_interruption(
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=install_authority,
            attempt_id="tamper-hash",
        ),
        "tamper_seed_interrupt_absent",
    )
    content = journal_path.read_bytes()
    changed = content.replace(b'"event":"intent"', b'"event":"intenx"', 1)
    _require(changed != content and len(changed) == len(content), "tamper_edit_failed")
    journal_path.write_bytes(changed)
    _expect_exception(
        JournalIntegrityError,
        lambda: FileJournal(journal_path, binding_sha256=binding),
        "tampered_journal_accepted",
    )
    matrix.record("tamper_refusal", label, "journal_hash_refused")

    label = "tamper_partial_final_line"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    backend = SyntheticBackend()
    _call_controller(
        backend=backend,
        journal_path=journal_path,
        binding_sha256=binding,
        authorization=install_authority,
        attempt_id="tamper-partial",
    )
    journal_path.write_bytes(journal_path.read_bytes() + b"{")
    _expect_exception(
        JournalIntegrityError,
        lambda: FileJournal(journal_path, binding_sha256=binding),
        "partial_journal_line_accepted",
    )
    matrix.record("tamper_refusal", label, "partial_line_refused")

    label = "tamper_clean_suffix_removal"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    backend = SyntheticBackend()
    receipt = _call_controller(
        backend=backend,
        journal_path=journal_path,
        binding_sha256=binding,
        authorization=install_authority,
        attempt_id="tamper-suffix",
    )
    lines = journal_path.read_bytes().splitlines(keepends=True)
    _require(len(lines) > 1, "tamper_suffix_seed_invalid")
    journal_path.write_bytes(b"".join(lines[:-1]))
    _expect_exception(
        JournalIntegrityError,
        lambda: FileJournal(
            journal_path,
            binding_sha256=binding,
            expected_head_sha256=receipt.journal_head_sha256,
            expected_sequence=receipt.journal_sequence,
        ),
        "clean_suffix_removal_accepted",
    )
    matrix.record("tamper_refusal", label, "sealed_suffix_refused")

    label = "tamper_authorization_binding"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    backend = SyntheticBackend()
    wrong_authority = DisposableControllerAuthorization(
        operation="install",
        binding_sha256="f" * 64,
        proof_receipt_sha256="e" * 64,
    )
    _expect_exception(
        AuthorizationError,
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=wrong_authority,
            attempt_id="tamper-authority",
        ),
        "wrong_authorization_binding_accepted",
    )
    _require(not backend.effects, "authorization_refusal_mutated_backend")
    matrix.record("tamper_refusal", label, "authorization_binding_refused")

    label = "tamper_controller_binding"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    with FileJournal(journal_path, binding_sha256=binding) as journal:
        _expect_exception(
            OperationStateError,
            lambda: InactiveInstallationController(
                backend=SyntheticBackend(),
                journal=journal,
                binding_sha256="d" * 64,
            ),
            "wrong_controller_binding_accepted",
        )
    matrix.record("tamper_refusal", label, "controller_binding_refused")

    label = "tamper_compensation_resume_authorization_binding"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    backend = SyntheticBackend(
        fail_effect_step="I12_CREATE_POSTGRES_CONTAINER"
    )
    _expect_exception(
        InstallationCompensatedError,
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=install_authority,
            attempt_id="tamper-compensation-binding",
        ),
        "compensation_binding_seed_failure_absent",
    )
    sealed_before = _journal_seal(journal_path, binding)
    effects_before = tuple(backend.effects)
    different_proof_authority = DisposableControllerAuthorization(
        operation="install",
        binding_sha256=binding,
        proof_receipt_sha256=_sha256_bytes(
            b"phase8a-different-compensation-proof"
        ),
    )
    _expect_exception(
        AuthorizationError,
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=different_proof_authority,
            attempt_id="tamper-compensation-binding",
            expected_seal=sealed_before,
        ),
        "compensation_resume_authorization_mismatch_accepted",
    )
    _require(
        _journal_seal(journal_path, binding) == sealed_before,
        "compensation_authorization_refusal_changed_journal",
    )
    _require(
        tuple(backend.effects) == effects_before,
        "compensation_authorization_refusal_changed_effects",
    )
    matrix.record(
        "tamper_refusal",
        label,
        "compensation_resume_authorization_binding_refused",
    )

    label = "partial_unowned_install_effect"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    backend = SyntheticBackend()
    backend.seed_unowned_effect("install", INSTALL_STEPS[0])
    _expect_exception(
        StateDriftError,
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=install_authority,
            attempt_id="partial-install",
        ),
        "unowned_install_effect_accepted",
    )
    _require(not backend.effects, "unowned_install_refusal_executed_effect")
    matrix.record("partial_refusal", label, "unowned_effect_refused")

    unowned_step = "I10_CREATE_POSTGRES_VOLUME"
    label = "rollback_empty_journal_unowned_install_effect"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    backend = SyntheticBackend()
    backend.seed_unowned_effect("install", unowned_step)
    effects_before = tuple(backend.effects)
    _expect_exception(
        StateDriftError,
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=rollback_authority,
            attempt_id="empty-journal-unowned-rollback",
        ),
        "empty_journal_unowned_install_rollback_accepted",
    )
    _require(
        _journal_seal(journal_path, binding) == (HASH_ZERO, 0),
        "empty_journal_unowned_install_refusal_changed_journal",
    )
    _require(
        tuple(backend.effects) == effects_before,
        "empty_journal_unowned_install_refusal_executed_effect",
    )
    matrix.record(
        "partial_refusal",
        label,
        "unowned_install_rollback_refused",
    )

    label = "rollback_partial_journal_unowned_later_install_effect"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    backend = SyntheticBackend(
        crash_after_journal_commit_step=INSTALL_STEPS[0]
    )
    _expect_interruption(
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=install_authority,
            attempt_id="partial-journal-unowned-install",
        ),
        "partial_journal_unowned_install_seed_interrupt_absent",
    )
    seal = _journal_seal(journal_path, binding)
    backend.clear_faults()
    backend.seed_unowned_effect("install", unowned_step)
    effects_before = tuple(backend.effects)
    _expect_exception(
        StateDriftError,
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=rollback_authority,
            attempt_id="partial-journal-unowned-rollback",
            expected_seal=seal,
        ),
        "partial_journal_unowned_install_rollback_accepted",
    )
    _require(
        _journal_seal(journal_path, binding) == seal,
        "partial_journal_unowned_install_refusal_changed_journal",
    )
    _require(
        tuple(backend.effects) == effects_before,
        "partial_journal_unowned_install_refusal_executed_effect",
    )
    matrix.record(
        "partial_refusal",
        label,
        "unowned_later_install_rollback_refused",
    )

    label = "partial_probe_drift"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    backend = SyntheticBackend()
    backend.set_probe_override("install", INSTALL_STEPS[0], StepState.DRIFT)
    _expect_exception(
        StateDriftError,
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=install_authority,
            attempt_id="partial-drift",
        ),
        "probe_drift_accepted",
    )
    _require(not backend.effects, "probe_drift_refusal_executed_effect")
    matrix.record("partial_refusal", label, "probe_drift_refused")

    label = "partial_resume_probe_drift"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    backend = SyntheticBackend(crash_after_intent_step=INSTALL_STEPS[0])
    _expect_interruption(
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=install_authority,
            attempt_id="partial-resume-drift",
        ),
        "resume_drift_seed_interrupt_absent",
    )
    seal = _journal_seal(journal_path, binding)
    backend.clear_faults()
    backend.set_probe_override("install", INSTALL_STEPS[0], StepState.DRIFT)
    _expect_exception(
        StateDriftError,
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=install_authority,
            attempt_id="partial-resume-drift",
            expected_seal=seal,
        ),
        "resume_probe_drift_accepted",
    )
    _require(not backend.effects, "resume_probe_drift_executed_effect")
    matrix.record("partial_refusal", label, "resume_probe_drift_refused")

    label = "partial_unowned_rollback_effect"
    directory = _scenario_directory(root, label)
    journal_path = directory / "controller.journal"
    backend = SyntheticBackend()
    install_receipt = _call_controller(
        backend=backend,
        journal_path=journal_path,
        binding_sha256=binding,
        authorization=install_authority,
        attempt_id="partial-rollback-install",
    )
    backend.seed_unowned_effect("rollback", ROLLBACK_STEPS[0])
    _expect_exception(
        StateDriftError,
        lambda: _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=rollback_authority,
            attempt_id="partial-rollback",
            expected_seal=(
                install_receipt.journal_head_sha256,
                install_receipt.journal_sequence,
            ),
        ),
        "unowned_rollback_effect_accepted",
    )
    _require(
        not any(operation == "rollback" for operation, unused in backend.effects),
        "unowned_rollback_refusal_executed_effect",
    )
    matrix.record("partial_refusal", label, "unowned_rollback_refused")


def _run_empty_only_refusals(
    root: Path,
    binding: str,
    matrix: ScenarioMatrix,
) -> None:
    install_authority = _authorization("install", binding)
    rollback_authority = _authorization("rollback", binding)
    cases: tuple[tuple[str, object, str], ...] = (
        ("pilot_ever_started", True, "pilot_ever_started"),
        (
            "successor_user_memory_row_count",
            1,
            "successor_user_memory_row_count_not_zero",
        ),
        (
            "successor_projection_queue_row_count",
            1,
            "successor_projection_queue_row_count_not_zero",
        ),
        ("qdrant_point_count", 1, "qdrant_point_count_not_zero"),
        ("active_client_count", 1, "active_client_count_not_zero"),
        ("legacy_import_count", 1, "legacy_import_count_not_zero"),
    )
    for field, value, expected_code in cases:
        label = "empty_only_" + field
        directory = _scenario_directory(root, label)
        journal_path = directory / "controller.journal"
        backend = SyntheticBackend()
        install_receipt = _call_controller(
            backend=backend,
            journal_path=journal_path,
            binding_sha256=binding,
            authorization=install_authority,
            attempt_id="empty-gate-install",
        )
        backend.set_rollback_facts(**{field: value})
        error = _expect_exception(
            RollbackGateError,
            lambda: _call_controller(
                backend=backend,
                journal_path=journal_path,
                binding_sha256=binding,
                authorization=rollback_authority,
                attempt_id="empty-gate-rollback",
                expected_seal=(
                    install_receipt.journal_head_sha256,
                    install_receipt.journal_sequence,
                ),
            ),
            "empty_only_gate_accepted:" + field,
        )
        _require(
            isinstance(error, RollbackGateError)
            and error.refusal_codes == (expected_code,),
            "empty_only_refusal_code_drift:" + field,
        )
        _require(
            not any(operation == "rollback" for operation, unused in backend.effects),
            "empty_only_refusal_executed_rollback",
        )
        _require(
            _journal_seal(journal_path, binding)
            == (
                install_receipt.journal_head_sha256,
                install_receipt.journal_sequence,
            ),
            "empty_only_refusal_changed_journal",
        )
        _assert_synthetic_zero_external(backend)
        matrix.record("empty_only_refusal", label, expected_code)


def _audit_self_tests(audit: ProofAudit, protected_path: Path) -> None:
    def expect_audit(call: Callable[[], object], code: str) -> None:
        try:
            call()
        except AuditViolation:
            return
        raise ProofFailure(code)

    expect_audit(
        lambda: subprocess.run(["/usr/bin/true"], check=True),
        "audit_subprocess_not_blocked",
    )
    expect_audit(
        lambda: audit("os.fork", ()),
        "audit_fork_policy_not_blocked",
    )
    expect_audit(lambda: socket.socket(), "audit_socket_not_blocked")
    expect_audit(
        lambda: socket.getaddrinfo("localhost", 0),
        "audit_dns_not_blocked",
    )
    expect_audit(
        lambda: Path("/dev/null").open("wb"),
        "audit_external_write_not_blocked",
    )
    with Path("/dev/null").open("rb") as outside_handle:
        expect_audit(
            lambda: audit("os.truncate", (outside_handle.fileno(), 0)),
            "audit_external_fd_write_not_blocked",
        )
    with protected_path.open("rb") as protected_handle:
        expect_audit(
            lambda: audit(
                "os.chmod",
                (protected_handle.fileno(), 0o600, -1),
            ),
            "audit_protected_fd_write_not_blocked",
        )
    expect_audit(
        lambda: audit("mmap.__new__", ()),
        "audit_mmap_policy_not_blocked",
    )
    _require(
        dict(audit.blocked_attempts)
        == {
            "subprocess": 2,
            "socket": 1,
            "dns": 1,
            "write_outside_temp_root": 2,
            "sentinel_mutation": 1,
            "mmap": 1,
        },
        "audit_self_test_count_drift",
    )


def _validate_receipt(receipt: dict[str, object]) -> None:
    required = {
        "schema_version",
        "proof_scope",
        "result",
        "artifact_sha256",
        "package_manifest_sha256",
        "package_artifact_count",
        "package_artifact_sha256",
        "exact_targets_sha256",
        "local_import_guard",
        "controller_binding_sha256",
        "scenario_matrix_sha256",
        "scenario_counts",
        "happy_install_receipt_sha256",
        "happy_rollback_receipt_sha256",
        "sentinel_sha256",
        "sentinels_unchanged",
        "retained_effects_after_empty_rollback",
        "retained_resources_after_empty_rollback",
        "external_effect_counts",
        "audit_blocked_self_test_attempts",
        "temp_root_removed",
    }
    _require(set(receipt) == required, "proof_receipt_shape_invalid")
    _require(receipt["schema_version"] == PROOF_SCHEMA_VERSION, "proof_schema_drift")
    _require(
        receipt["proof_scope"]
        == "phase8a_synthetic_controller_only_not_live_installation_"
        "not_installation_authority",
        "proof_scope_drift",
    )
    _require(receipt["result"] == "pass", "proof_result_not_pass")
    _require(
        receipt["local_import_guard"]
        == {
            "installed_before_local_artifact_imports": True,
            "local_artifact_imports_completed": True,
            "blocked_effects_during_import": {},
        },
        "proof_local_import_guard_invalid",
    )
    package_artifacts = receipt["package_artifact_sha256"]
    proof_artifacts = receipt["artifact_sha256"]
    _require(
        isinstance(package_artifacts, dict)
        and receipt["package_artifact_count"]
        == EXPECTED_PACKAGE_ARTIFACT_COUNT
        and len(package_artifacts) == EXPECTED_PACKAGE_ARTIFACT_COUNT
        and all(
            isinstance(path, str)
            and isinstance(digest, str)
            and len(digest) == 64
            for path, digest in package_artifacts.items()
        )
        and isinstance(receipt["package_manifest_sha256"], str)
        and len(receipt["package_manifest_sha256"]) == 64
        and isinstance(receipt["exact_targets_sha256"], str)
        and len(receipt["exact_targets_sha256"]) == 64,
        "proof_package_binding_invalid",
    )
    _require(
        isinstance(proof_artifacts, dict)
        and set(proof_artifacts) == set(ARTIFACT_PATHS)
        and all(
            isinstance(digest, str) and len(digest) == 64
            for digest in proof_artifacts.values()
        )
        and all(
            package_artifacts.get(path) == digest
            for path, digest in proof_artifacts.items()
        ),
        "proof_artifact_binding_invalid",
    )
    for key in (
        "controller_binding_sha256",
        "scenario_matrix_sha256",
        "happy_install_receipt_sha256",
        "happy_rollback_receipt_sha256",
    ):
        _require(
            isinstance(receipt[key], str) and len(receipt[key]) == 64,
            "proof_hash_invalid:" + key,
        )
    expected_counts = {
        "happy_path": 2,
        "crash_resume": 3 * (len(INSTALL_STEPS) + len(ROLLBACK_STEPS)),
        "partial_failure": 2 * (len(INSTALL_STEPS) + len(ROLLBACK_STEPS)),
        "compensation_crash_resume": 3 * len(ROLLBACK_STEPS),
        "partial_install_authorized_rollback": len(INSTALL_STEPS),
        "tamper_refusal": 6,
        "partial_refusal": 6,
        "empty_only_refusal": 6,
    }
    scenario_counts = receipt["scenario_counts"]
    _require(
        isinstance(scenario_counts, dict)
        and set(scenario_counts) == {"total", *expected_counts}
        and all(
            scenario_counts.get(category) == expected
            for category, expected in expected_counts.items()
        )
        and scenario_counts.get("total") == sum(expected_counts.values()),
        "proof_scenario_counts_invalid",
    )
    sentinels = receipt["sentinel_sha256"]
    _require(
        isinstance(sentinels, dict)
        and set(sentinels)
        == {"accounts", "chat", "lifeswitch_structured_data"}
        and all(
            isinstance(digest, str) and len(digest) == 64
            for digest in sentinels.values()
        ),
        "proof_sentinel_binding_invalid",
    )
    _require(receipt["sentinels_unchanged"] is True, "proof_sentinel_result_false")
    _require(
        receipt["retained_effects_after_empty_rollback"]
        == list(EXPECTED_RETAINED_EFFECTS),
        "proof_retained_effects_invalid",
    )
    _require(
        receipt["retained_resources_after_empty_rollback"]
        == list(EXPECTED_RETAINED_RESOURCES),
        "proof_retained_resources_invalid",
    )
    _require(receipt["temp_root_removed"] is True, "proof_temp_cleanup_false")
    counts = receipt["external_effect_counts"]
    _require(
        isinstance(counts, dict)
        and set(counts)
        == {
            "completed_subprocess_calls",
            "completed_socket_calls",
            "completed_dns_calls",
            "audited_path_writes_outside_temp_root",
            "completed_mmap_calls",
            "live_backend_calls",
            "live_mutations",
            "provider_calls",
            "source_postgresql_connections",
            "source_postgresql_reads",
            "source_postgresql_writes",
            "production_postgresql_calls",
            "qdrant_calls",
            "service_manager_calls",
            "runtime_application_starts",
        }
        and all(value == 0 for value in counts.values()),
        "proof_external_effect_counts_not_zero",
    )
    _require(
        receipt["audit_blocked_self_test_attempts"]
        == {
            "subprocess": 2,
            "socket": 1,
            "dns": 1,
            "write_outside_temp_root": 2,
            "sentinel_mutation": 1,
            "mmap": 1,
        },
        "proof_audit_self_test_counts_invalid",
    )


def _run(root: Path, audit: ProofAudit) -> dict[str, object]:
    _load_and_verify_plan()
    _verify_rollback_sql()
    try:
        package = verify_package()
    except InstallationPackageError as error:
        raise ProofFailure("phase8a_package_verification_failed") from error
    package_artifacts = package.get("artifact_sha256")
    package_manifest_sha256 = package.get("package_manifest_sha256")
    exact_targets_sha256 = package.get("exact_targets_sha256")
    _require(
        isinstance(package_artifacts, dict)
        and len(package_artifacts) == EXPECTED_PACKAGE_ARTIFACT_COUNT
        and isinstance(package_manifest_sha256, str)
        and len(package_manifest_sha256) == 64
        and isinstance(exact_targets_sha256, str)
        and len(exact_targets_sha256) == 64,
        "phase8a_package_verification_result_invalid",
    )
    artifact_hashes = _artifact_hashes()
    _require(
        all(
            package_artifacts.get(relative) == digest
            for relative, digest in artifact_hashes.items()
        ),
        "proof_artifact_not_bound_by_package_manifest",
    )
    binding = _sha256_bytes(
        _canonical_bytes(
            {
                "package_manifest_sha256": package_manifest_sha256,
                "package_artifact_sha256": package_artifacts,
                "exact_targets_sha256": exact_targets_sha256,
                "proof_artifact_sha256": artifact_hashes,
            }
        )
    )

    sentinel_root = root / "protected_sentinels"
    sentinel_root.mkdir(mode=0o700)
    sentinel_root.chmod(0o700)
    sentinel_material = {
        "accounts": b"phase8a-opaque-account-sentinel-v1\n",
        "chat": b"phase8a-opaque-chat-sentinel-v1\n",
        "lifeswitch_structured_data": (
            b"phase8a-opaque-lifeswitch-structured-data-sentinel-v1\n"
        ),
    }
    sentinel_paths: dict[str, Path] = {}
    sentinel_hashes: dict[str, str] = {}
    for name, content in sentinel_material.items():
        path = sentinel_root / (name + ".sentinel")
        path.write_bytes(content)
        path.chmod(0o400)
        sentinel_paths[name] = path
        sentinel_hashes[name] = _sha256_file(path)

    def assert_sentinels() -> None:
        _require(
            {path.name for path in sentinel_root.iterdir()}
            == {path.name for path in sentinel_paths.values()},
            "sentinel_path_set_drift",
        )
        for name, path in sentinel_paths.items():
            _require(path.is_file() and not path.is_symlink(), "sentinel_file_drift")
            _require(_sha256_file(path) == sentinel_hashes[name], "sentinel_hash_drift")

    audit.protect(tuple(sentinel_paths.values()))
    _audit_self_tests(audit, sentinel_paths["accounts"])
    matrix = ScenarioMatrix(assert_sentinels)
    happy_install_hash, happy_rollback_hash = _run_happy_paths(
        root, binding, matrix
    )
    _run_install_crash_matrix(root, binding, matrix)
    _run_rollback_crash_matrix(root, binding, matrix)
    _run_install_failure_matrix(root, binding, matrix)
    _run_compensation_crash_matrix(root, binding, matrix)
    _run_partial_install_authorized_rollback_matrix(root, binding, matrix)
    _run_rollback_failure_matrix(root, binding, matrix)
    _run_tamper_and_partial_refusals(root, binding, matrix)
    _run_empty_only_refusals(root, binding, matrix)
    assert_sentinels()

    expected_crashes = 3 * (len(INSTALL_STEPS) + len(ROLLBACK_STEPS))
    expected_failures = 2 * (len(INSTALL_STEPS) + len(ROLLBACK_STEPS))
    expected_compensation_crashes = 3 * len(ROLLBACK_STEPS)
    expected_partial_install_rollbacks = len(INSTALL_STEPS)
    _require(matrix.count("happy_path") == 2, "happy_scenario_count_drift")
    _require(
        matrix.count("crash_resume") == expected_crashes,
        "crash_scenario_count_drift",
    )
    _require(
        matrix.count("partial_failure") == expected_failures,
        "failure_scenario_count_drift",
    )
    _require(
        matrix.count("compensation_crash_resume")
        == expected_compensation_crashes,
        "compensation_crash_scenario_count_drift",
    )
    _require(
        matrix.count("partial_install_authorized_rollback")
        == expected_partial_install_rollbacks,
        "partial_install_rollback_scenario_count_drift",
    )
    _require(matrix.count("tamper_refusal") == 6, "tamper_scenario_count_drift")
    _require(matrix.count("partial_refusal") == 6, "partial_refusal_count_drift")
    _require(matrix.count("empty_only_refusal") == 6, "empty_refusal_count_drift")
    _require(
        dict(audit.blocked_attempts)
        == {
            "subprocess": 2,
            "socket": 1,
            "dns": 1,
            "write_outside_temp_root": 2,
            "sentinel_mutation": 1,
            "mmap": 1,
        },
        "unexpected_forbidden_operation_attempted",
    )

    return {
        "schema_version": PROOF_SCHEMA_VERSION,
        "proof_scope": (
            "phase8a_synthetic_controller_only_not_live_installation_"
            "not_installation_authority"
        ),
        "result": "pass",
        "artifact_sha256": artifact_hashes,
        "package_manifest_sha256": package_manifest_sha256,
        "package_artifact_count": len(package_artifacts),
        "package_artifact_sha256": dict(sorted(package_artifacts.items())),
        "exact_targets_sha256": exact_targets_sha256,
        "local_import_guard": {
            "installed_before_local_artifact_imports": True,
            "local_artifact_imports_completed": IMPORT_PHASE_GUARD_COMPLETED,
            "blocked_effects_during_import": dict(_IMPORT_PHASE_BLOCKED),
        },
        "controller_binding_sha256": binding,
        "scenario_matrix_sha256": matrix.sha256,
        "scenario_counts": {
            "total": len(matrix.entries),
            "happy_path": matrix.count("happy_path"),
            "crash_resume": matrix.count("crash_resume"),
            "partial_failure": matrix.count("partial_failure"),
            "compensation_crash_resume": matrix.count(
                "compensation_crash_resume"
            ),
            "partial_install_authorized_rollback": matrix.count(
                "partial_install_authorized_rollback"
            ),
            "tamper_refusal": matrix.count("tamper_refusal"),
            "partial_refusal": matrix.count("partial_refusal"),
            "empty_only_refusal": matrix.count("empty_only_refusal"),
        },
        "happy_install_receipt_sha256": happy_install_hash,
        "happy_rollback_receipt_sha256": happy_rollback_hash,
        "sentinel_sha256": sentinel_hashes,
        "sentinels_unchanged": True,
        "retained_effects_after_empty_rollback": list(EXPECTED_RETAINED_EFFECTS),
        "retained_resources_after_empty_rollback": list(
            EXPECTED_RETAINED_RESOURCES
        ),
        "external_effect_counts": {
            "completed_subprocess_calls": 0,
            "completed_socket_calls": 0,
            "completed_dns_calls": 0,
            "audited_path_writes_outside_temp_root": 0,
            "completed_mmap_calls": 0,
            "live_backend_calls": 0,
            "live_mutations": 0,
            "provider_calls": 0,
            "source_postgresql_connections": 0,
            "source_postgresql_reads": 0,
            "source_postgresql_writes": 0,
            "production_postgresql_calls": 0,
            "qdrant_calls": 0,
            "service_manager_calls": 0,
            "runtime_application_starts": 0,
        },
        "audit_blocked_self_test_attempts": dict(audit.blocked_attempts),
        "temp_root_removed": False,
    }


def main() -> int:
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="phase8a-controller-proof-")).resolve()
    root.chmod(0o700)
    audit = ProofAudit(root)
    sys.addaudithook(audit)
    receipt: dict[str, object] | None = None
    failure: BaseException | None = None
    try:
        receipt = _run(root, audit)
    except BaseException as error:
        failure = error
    finally:
        audit.release_protection()
        try:
            _remove_disposable_tree(root)
        except BaseException as cleanup_error:
            if failure is None:
                failure = cleanup_error
    if failure is not None:
        print(
            "PHASE8A_CONTROLLER_PROOF_REFUSED="
            + failure.__class__.__name__
            + ":"
            + str(failure),
            file=sys.stderr,
        )
        return 1
    _require(receipt is not None, "proof_receipt_absent")
    receipt["temp_root_removed"] = not root.exists()
    _validate_receipt(receipt)
    print(_canonical_bytes(receipt).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
