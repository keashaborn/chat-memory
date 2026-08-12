from __future__ import annotations

"""Guarded synthetic-only disposable proof for the dormant-store installation controller.

The public entry point constructs the exact sealed in-memory backend itself.
It accepts no backend, command runner, host adapter, credentials, environment,
network endpoint, image, installation target, or activation option.  Modeled
interruptions remain in one Python process and are not durable, composite, or
live crash-recovery proof.
"""

from collections import Counter
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
import gc
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Final

from .controller import (
    CompletedStateError,
    ControllerReceipt,
    InstallationCompensatedError,
    DormantStoreInstallController,
    STORES_ONLY_PLAN,
    StepState,
    validate_plan,
)
from .execution_lock import GlobalExecutionLock
from .synthetic_backend import (
    FAULT_FAIL_AFTER_EFFECT,
    FAULT_FAIL_BEFORE_EFFECT,
    FAULT_INTERRUPT_AFTER_APPLIED_JOURNAL,
    FAULT_INTERRUPT_AFTER_COMPENSATION,
    FAULT_INTERRUPT_AFTER_EFFECT,
    FAULT_INTERRUPT_BEFORE_COMPENSATION,
    FAULT_INTERRUPT_BEFORE_EFFECT,
    FAULT_NONE,
    SyntheticBackend,
    SyntheticEffectFailure,
    SyntheticInterruption,
    SyntheticScenario,
    _construct_exact_synthetic_backend,
)


REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
CONTRACT_PATH: Final = (
    REPOSITORY_ROOT
    / "ops/governed_memory/installation/current/"
    "disposable_proof_contract.json"
)
RECEIPT_SCHEMA_PATH: Final = (
    REPOSITORY_ROOT
    / "ops/governed_memory/installation/current/"
    "disposable_proof_receipt.schema.json"
)
PACKAGE_MANIFEST_PATH: Final = (
    REPOSITORY_ROOT
    / "ops/governed_memory/installation/current/package_manifest.json"
)
CONTROLLER_PLAN_PATH: Final = (
    REPOSITORY_ROOT
    / "ops/governed_memory/installation/current/controller_plan.json"
)
BACKEND_SOURCE_PATH: Final = (
    REPOSITORY_ROOT
    / "tools/governed_memory_install/synthetic_backend.py"
)
CONTROLLER_SOURCE_PATH: Final = (
    REPOSITORY_ROOT / "tools/governed_memory_install/controller.py"
)
EXECUTION_LOCK_SOURCE_PATH: Final = (
    REPOSITORY_ROOT / "tools/governed_memory_install/execution_lock.py"
)
HARNESS_SOURCE_PATH: Final = Path(__file__).resolve()
RUNNER_SOURCE_PATH: Final = (
    REPOSITORY_ROOT
    / "tools/governed_memory_validation/run_installation_synthetic_proof.py"
)

PROOF_SCHEMA_VERSION: Final = (
    "governed-memory-dormant-store-install-disposable-synthetic-proof-v2"
)
RECEIPT_SCHEMA_VERSION: Final = (
    "governed-memory-dormant-store-install-disposable-proof-receipt-v2"
)
CONTRACT_SCHEMA_VERSION: Final = (
    "governed-memory-dormant-store-install-disposable-proof-contract-v2"
)
PROOF_SCOPE: Final = "synthetic_in_process_model_only"
PROOF_OUTCOME: Final = "synthetic_matrix_passed"
EXPECTED_PLAN_STEP_COUNT: Final = 19
EXPECTED_COMPENSABLE_STEP_COUNT: Final = 14
EXPECTED_SCENARIO_COUNT: Final = 124
HASH_ZERO: Final = "0" * 64

FAMILY_HAPPY_PATH: Final = "happy_path"
FAMILY_FAIL_BEFORE_EFFECT: Final = "ordinary_failure_before_effect"
FAMILY_FAIL_AFTER_EFFECT: Final = "ordinary_failure_after_effect"
FAMILY_INTERRUPT_BEFORE_EFFECT: Final = "interrupt_before_effect"
FAMILY_INTERRUPT_AFTER_EFFECT: Final = "interrupt_after_effect"
FAMILY_INTERRUPT_AFTER_APPLIED: Final = "interrupt_after_applied_journal"
FAMILY_INTERRUPT_BEFORE_COMPENSATION: Final = (
    "interrupt_before_compensation"
)
FAMILY_INTERRUPT_AFTER_COMPENSATION: Final = (
    "interrupt_after_compensation"
)

EXPECTED_FAMILY_COUNTS: Final = {
    FAMILY_HAPPY_PATH: 1,
    FAMILY_FAIL_BEFORE_EFFECT: 19,
    FAMILY_FAIL_AFTER_EFFECT: 19,
    FAMILY_INTERRUPT_BEFORE_EFFECT: 19,
    FAMILY_INTERRUPT_AFTER_EFFECT: 19,
    FAMILY_INTERRUPT_AFTER_APPLIED: 19,
    FAMILY_INTERRUPT_BEFORE_COMPENSATION: 14,
    FAMILY_INTERRUPT_AFTER_COMPENSATION: 14,
}
EXPECTED_OUTCOME_COUNTS: Final = {
    "inactive_complete": 58,
    "same_attempt_compensated": 65,
    "terminal_postflight_failure_blocks_compensation": 1,
}


class DisposableProofError(RuntimeError):
    """Content-free proof refusal."""


class DisposableProofAuditViolation(DisposableProofError):
    """A forbidden effect was attempted while the proof fence was active."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise DisposableProofError(code)


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as error:
        raise DisposableProofError("proof_json_invalid") from error


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_projection(value: object) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    except OSError as error:
        raise DisposableProofError("proof_artifact_read_failed") from error
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DisposableProofError("proof_document_invalid") from error
    if not isinstance(value, dict):
        raise DisposableProofError("proof_document_invalid")
    return value


@dataclass(slots=True)
class _RuntimeAuditState:
    active: bool = False
    repository_root: Path | None = None
    disposable_root: Path | None = None
    blocked_effect_count: int = 0
    environment_access_count: int = 0
    trusted_internal_openat_lock: bool = False


_RUNTIME_AUDIT = _RuntimeAuditState()


def _resolved_allowed(path_value: object, *, write: bool) -> bool:
    if isinstance(path_value, int):
        return bool(write and _RUNTIME_AUDIT.trusted_internal_openat_lock)
    if isinstance(path_value, os.PathLike):
        try:
            path_value = os.fspath(path_value)
        except TypeError:
            return False
    if isinstance(path_value, bytes):
        try:
            path_value = path_value.decode(sys.getfilesystemencoding())
        except UnicodeError:
            return False
    if not isinstance(path_value, str) or not path_value:
        return False
    candidate = Path(path_value)
    if not candidate.is_absolute():
        return bool(
            write
            and _RUNTIME_AUDIT.trusted_internal_openat_lock
            and path_value == "execution.lock"
        )
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return False
    roots: tuple[Path, ...]
    if write:
        roots = (
            (_RUNTIME_AUDIT.disposable_root,)
            if _RUNTIME_AUDIT.disposable_root is not None
            else ()
        )
    else:
        roots = tuple(
            root
            for root in (
                _RUNTIME_AUDIT.repository_root,
                _RUNTIME_AUDIT.disposable_root,
            )
            if root is not None
        )
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _deny_runtime_effect(code: str) -> None:
    _RUNTIME_AUDIT.blocked_effect_count += 1
    raise DisposableProofAuditViolation(code)


def _runtime_audit_hook(event: str, args: tuple[object, ...]) -> None:
    if not _RUNTIME_AUDIT.active:
        return
    if (
        event in {
            "subprocess.Popen",
            "os.system",
            "os.fork",
            "os.forkpty",
            "pty.spawn",
        }
        or event.startswith("os.exec")
        or event.startswith("os.spawn")
        or event.startswith("os.posix_spawn")
    ):
        _deny_runtime_effect("proof_process_effect_forbidden")
    if (
        event
        in {
            "socket.__new__",
            "socket.bind",
            "socket.connect",
            "socket.connect_ex",
            "socket.getaddrinfo",
            "socket.gethostbyaddr",
            "socket.gethostbyname",
            "socket.gethostbyname_ex",
            "socket.getnameinfo",
            "socket.sendto",
        }
        or event.startswith("http.client")
        or event.startswith("urllib")
    ):
        _deny_runtime_effect("proof_network_effect_forbidden")
    if event.startswith("ctypes.") or event == "import":
        _deny_runtime_effect("proof_dynamic_code_effect_forbidden")
    if event in {"os.putenv", "os.unsetenv"}:
        _deny_runtime_effect("proof_environment_effect_forbidden")
    if event == "open" and args:
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        write_mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        write = (
            isinstance(mode, str) and any(token in mode for token in "wax+")
        ) or (isinstance(flags, int) and bool(flags & write_mask))
        if not _resolved_allowed(args[0], write=write):
            _deny_runtime_effect(
                "proof_filesystem_write_forbidden"
                if write
                else "proof_filesystem_read_forbidden"
            )
    mutation_events = {
        "os.chmod",
        "os.chown",
        "os.link",
        "os.mkdir",
        "os.mkfifo",
        "os.mknod",
        "os.remove",
        "os.removexattr",
        "os.rename",
        "os.replace",
        "os.rmdir",
        "os.setxattr",
        "os.symlink",
        "os.truncate",
        "os.utime",
    }
    if event in mutation_events:
        path_arguments = args[:2] if event in {"os.rename", "os.replace"} else args[:1]
        if not path_arguments or any(
            not _resolved_allowed(value, write=True) for value in path_arguments
        ):
            _deny_runtime_effect("proof_filesystem_write_forbidden")
    if event in {"os.listdir", "os.scandir"} and args:
        if not _resolved_allowed(args[0], write=False):
            _deny_runtime_effect("proof_filesystem_read_forbidden")


sys.addaudithook(_runtime_audit_hook)


class _ForbiddenEnvironment(MutableMapping[object, object]):
    def _deny(self) -> None:
        _RUNTIME_AUDIT.environment_access_count += 1
        raise DisposableProofAuditViolation("proof_environment_access_forbidden")

    def __getitem__(self, key: object) -> object:
        self._deny()

    def __setitem__(self, key: object, value: object) -> None:
        self._deny()

    def __delitem__(self, key: object) -> None:
        self._deny()

    def __iter__(self) -> Iterator[object]:
        self._deny()

    def __len__(self) -> int:
        self._deny()


@contextmanager
def _runtime_fence(disposable_root: Path) -> Iterator[None]:
    if _RUNTIME_AUDIT.active:
        raise DisposableProofError("proof_fence_reentry_refused")
    if not isinstance(disposable_root, Path) or not disposable_root.is_absolute():
        raise DisposableProofError("proof_fence_setup_invalid")
    try:
        repository_root = REPOSITORY_ROOT.resolve(strict=True)
        resolved_disposable_root = disposable_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise DisposableProofError("proof_fence_setup_invalid") from error
    original_environ = os.environ
    original_environb = getattr(os, "environb", None)
    forbidden_environment = _ForbiddenEnvironment()
    garbage_collection_was_enabled = gc.isenabled()
    if garbage_collection_was_enabled:
        # Finalize cycles created by earlier in-process tests before the audit
        # fence is active. Otherwise an unrelated delayed finalizer can access
        # the environment during this proof and contaminate its scoped counts.
        gc.collect()
        gc.disable()
    try:
        _RUNTIME_AUDIT.active = True
        _RUNTIME_AUDIT.repository_root = repository_root
        _RUNTIME_AUDIT.disposable_root = resolved_disposable_root
        _RUNTIME_AUDIT.blocked_effect_count = 0
        _RUNTIME_AUDIT.environment_access_count = 0
        _RUNTIME_AUDIT.trusted_internal_openat_lock = False
        os.environ = forbidden_environment  # type: ignore[assignment]
        if original_environb is not None:
            os.environb = forbidden_environment  # type: ignore[assignment]
        yield
        _require(
            _RUNTIME_AUDIT.blocked_effect_count == 0,
            "proof_forbidden_effect_observed",
        )
        _require(
            _RUNTIME_AUDIT.environment_access_count == 0,
            "proof_environment_access_observed",
        )
    finally:
        os.environ = original_environ
        if original_environb is not None:
            os.environb = original_environb  # type: ignore[assignment]
        _RUNTIME_AUDIT.active = False
        _RUNTIME_AUDIT.repository_root = None
        _RUNTIME_AUDIT.disposable_root = None
        _RUNTIME_AUDIT.trusted_internal_openat_lock = False
        if garbage_collection_was_enabled:
            gc.enable()


def _validate_disposable_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise DisposableProofError("proof_disposable_root_invalid")
    try:
        resolved = root.resolve(strict=True)
        metadata = root.lstat()
    except OSError as error:
        raise DisposableProofError("proof_disposable_root_invalid") from error
    if (
        resolved != root
        or not root.name.startswith("dormant_store_install-disposable-proof-")
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
        or any(root.iterdir())
    ):
        raise DisposableProofError("proof_disposable_root_invalid")
    return resolved


def _scenario_matrix() -> tuple[SyntheticScenario, ...]:
    steps = tuple(step.step_id for step in STORES_ONLY_PLAN)
    scenarios: list[SyntheticScenario] = [
        SyntheticScenario(
            scenario_id="S000_HAPPY_PATH",
            family=FAMILY_HAPPY_PATH,
            fault_point=FAULT_NONE,
            target_step_id=None,
        )
    ]
    families = (
        (FAMILY_FAIL_BEFORE_EFFECT, FAULT_FAIL_BEFORE_EFFECT),
        (FAMILY_FAIL_AFTER_EFFECT, FAULT_FAIL_AFTER_EFFECT),
        (FAMILY_INTERRUPT_BEFORE_EFFECT, FAULT_INTERRUPT_BEFORE_EFFECT),
        (FAMILY_INTERRUPT_AFTER_EFFECT, FAULT_INTERRUPT_AFTER_EFFECT),
        (
            FAMILY_INTERRUPT_AFTER_APPLIED,
            FAULT_INTERRUPT_AFTER_APPLIED_JOURNAL,
        ),
    )
    sequence = 1
    for family, fault_point in families:
        for step_id in steps:
            scenarios.append(
                SyntheticScenario(
                    scenario_id=f"S{sequence:03d}_{family}_{step_id}",
                    family=family,
                    fault_point=fault_point,
                    target_step_id=step_id,
                )
            )
            sequence += 1
    trigger_step_id = steps[-1]
    for family, fault_point in (
        (
            FAMILY_INTERRUPT_BEFORE_COMPENSATION,
            FAULT_INTERRUPT_BEFORE_COMPENSATION,
        ),
        (
            FAMILY_INTERRUPT_AFTER_COMPENSATION,
            FAULT_INTERRUPT_AFTER_COMPENSATION,
        ),
    ):
        for step in STORES_ONLY_PLAN:
            if not step.compensable:
                continue
            scenarios.append(
                SyntheticScenario(
                    scenario_id=f"S{sequence:03d}_{family}_{step.step_id}",
                    family=family,
                    fault_point=fault_point,
                    target_step_id=step.step_id,
                    trigger_failure_step_id=trigger_step_id,
                )
            )
            sequence += 1
    return tuple(scenarios)


def _verify_scenario_matrix(
    scenarios: tuple[SyntheticScenario, ...],
) -> dict[str, int]:
    _require(len(STORES_ONLY_PLAN) == EXPECTED_PLAN_STEP_COUNT, "proof_plan_count_invalid")
    _require(len(scenarios) == EXPECTED_SCENARIO_COUNT, "proof_scenario_count_invalid")
    _require(
        len({scenario.scenario_id for scenario in scenarios}) == len(scenarios),
        "proof_scenario_id_duplicate",
    )
    observed_family_counts = Counter(scenario.family for scenario in scenarios)
    _require(
        dict(sorted(observed_family_counts.items()))
        == dict(sorted(EXPECTED_FAMILY_COUNTS.items())),
        "proof_scenario_family_count_invalid",
    )
    all_step_ids = {step.step_id for step in STORES_ONLY_PLAN}
    for family in (
        FAMILY_FAIL_BEFORE_EFFECT,
        FAMILY_FAIL_AFTER_EFFECT,
        FAMILY_INTERRUPT_BEFORE_EFFECT,
        FAMILY_INTERRUPT_AFTER_EFFECT,
        FAMILY_INTERRUPT_AFTER_APPLIED,
    ):
        covered = {
            scenario.target_step_id
            for scenario in scenarios
            if scenario.family == family
        }
        _require(covered == all_step_ids, "proof_step_coverage_invalid")
    compensable = {step.step_id for step in STORES_ONLY_PLAN if step.compensable}
    _require(
        len(compensable) == EXPECTED_COMPENSABLE_STEP_COUNT,
        "proof_compensable_count_invalid",
    )
    for family in (
        FAMILY_INTERRUPT_BEFORE_COMPENSATION,
        FAMILY_INTERRUPT_AFTER_COMPENSATION,
    ):
        covered = {
            scenario.target_step_id
            for scenario in scenarios
            if scenario.family == family
        }
        _require(covered == compensable, "proof_compensation_coverage_invalid")
    return dict(sorted(observed_family_counts.items()))


def _expected_compensation(applied: tuple[str, ...]) -> tuple[str, ...]:
    applied_set = set(applied)
    return tuple(
        step.step_id
        for step in reversed(STORES_ONLY_PLAN)
        if step.step_id in applied_set and step.compensable
    )


def _verify_states(
    backend: SyntheticBackend,
    *,
    applied: tuple[str, ...],
    compensated: tuple[str, ...],
) -> None:
    applied_set = set(applied)
    compensated_set = set(compensated)
    observed = dict(backend.state_projection)
    for step in STORES_ONLY_PLAN:
        expected = (
            StepState.AFTER.value
            if step.step_id in applied_set and step.step_id not in compensated_set
            else StepState.BEFORE.value
        )
        _require(observed[step.step_id] == expected, "proof_state_projection_invalid")


def _verify_complete_receipt(receipt: ControllerReceipt) -> None:
    expected = tuple(step.step_id for step in STORES_ONLY_PLAN)
    _require(receipt.outcome == "inactive_stores_installation_complete", "proof_complete_outcome_invalid")
    _require(receipt.applied_step_ids == expected, "proof_complete_steps_invalid")
    _require(receipt.compensated_step_ids == (), "proof_complete_compensation_invalid")


def _run_case(
    scenario: SyntheticScenario,
    *,
    case_index: int,
    disposable_root: Path,
) -> dict[str, object]:
    case_root = disposable_root / f"case-{case_index:03d}"
    case_root.mkdir(mode=0o700)
    os.chmod(case_root, 0o700)
    backend = _construct_exact_synthetic_backend(scenario)
    attempt_id = f"dormant_store_install-proof-{case_index:03d}"
    outcome: str
    applied: tuple[str, ...]
    compensated: tuple[str, ...]
    _RUNTIME_AUDIT.trusted_internal_openat_lock = True
    try:
        execution_lock = GlobalExecutionLock(case_root / "execution.lock")
    finally:
        _RUNTIME_AUDIT.trusted_internal_openat_lock = False
    with execution_lock:
        controller = DormantStoreInstallController(
            plan=STORES_ONLY_PLAN,
            backend=backend,
            held_lock=execution_lock.held_capability(),
        )
        if scenario.family == FAMILY_HAPPY_PATH:
            receipt = controller.run(attempt_id=attempt_id)
            _verify_complete_receipt(receipt)
            outcome = "inactive_complete"
            applied = receipt.applied_step_ids
            compensated = receipt.compensated_step_ids
        elif scenario.family in {
            FAMILY_FAIL_BEFORE_EFFECT,
            FAMILY_FAIL_AFTER_EFFECT,
        }:
            target_position = tuple(
                step.step_id for step in STORES_ONLY_PLAN
            ).index(scenario.target_step_id)
            if (
                scenario.family == FAMILY_FAIL_AFTER_EFFECT
                and target_position == len(STORES_ONLY_PLAN) - 1
            ):
                try:
                    controller.run(attempt_id=attempt_id)
                except CompletedStateError:
                    outcome = "terminal_postflight_failure_blocks_compensation"
                    applied = tuple(step.step_id for step in STORES_ONLY_PLAN)
                    compensated = ()
                else:
                    raise DisposableProofError(
                        "proof_terminal_postflight_failure_not_refused"
                    )
            else:
                try:
                    controller.run(attempt_id=attempt_id)
                except InstallationCompensatedError as error:
                    _require(
                        type(error.cause) is SyntheticEffectFailure,
                        "proof_failure_cause_invalid",
                    )
                    receipt = error.receipt
                    outcome = "same_attempt_compensated"
                    applied = receipt.applied_step_ids
                    compensated = receipt.compensated_step_ids
                    expected_count = target_position + (
                        1 if scenario.family == FAMILY_FAIL_AFTER_EFFECT else 0
                    )
                    _require(
                        applied
                        == tuple(
                            step.step_id
                            for step in STORES_ONLY_PLAN[:expected_count]
                        ),
                        "proof_failure_applied_steps_invalid",
                    )
                    _require(
                        compensated == _expected_compensation(applied),
                        "proof_failure_compensation_invalid",
                    )
                else:
                    raise DisposableProofError("proof_failure_not_observed")
        elif scenario.family in {
            FAMILY_INTERRUPT_BEFORE_EFFECT,
            FAMILY_INTERRUPT_AFTER_EFFECT,
            FAMILY_INTERRUPT_AFTER_APPLIED,
        }:
            try:
                controller.run(attempt_id=attempt_id)
            except SyntheticInterruption:
                pass
            else:
                raise DisposableProofError("proof_interruption_not_observed")
            is_terminal_postflight_resume = (
                scenario.target_step_id == STORES_ONLY_PLAN[-1].step_id
                and scenario.family
                in {
                    FAMILY_INTERRUPT_AFTER_EFFECT,
                    FAMILY_INTERRUPT_AFTER_APPLIED,
                }
            )
            if is_terminal_postflight_resume:
                receipt = controller.run(attempt_id=attempt_id)
                _verify_complete_receipt(receipt)
                outcome = "inactive_complete"
                applied = receipt.applied_step_ids
                compensated = receipt.compensated_step_ids
            else:
                receipt = controller.run(attempt_id=attempt_id)
                _verify_complete_receipt(receipt)
                outcome = "inactive_complete"
                applied = receipt.applied_step_ids
                compensated = receipt.compensated_step_ids
        elif scenario.family in {
            FAMILY_INTERRUPT_BEFORE_COMPENSATION,
            FAMILY_INTERRUPT_AFTER_COMPENSATION,
        }:
            try:
                controller.run(attempt_id=attempt_id)
            except SyntheticInterruption:
                pass
            else:
                raise DisposableProofError(
                    "proof_compensation_interruption_not_observed"
                )
            receipt = controller.run(attempt_id=attempt_id)
            applied = tuple(step.step_id for step in STORES_ONLY_PLAN[:-1])
            compensated = _expected_compensation(applied)
            _require(
                receipt.outcome == "same_attempt_compensation_complete",
                "proof_compensation_resume_outcome_invalid",
            )
            _require(receipt.applied_step_ids == applied, "proof_compensation_resume_steps_invalid")
            _require(
                receipt.compensated_step_ids == compensated,
                "proof_compensation_resume_order_invalid",
            )
            outcome = "same_attempt_compensated"
        else:
            raise DisposableProofError("proof_scenario_family_invalid")

    if scenario.fault_point != FAULT_NONE:
        _require(backend.fault_fired, "proof_synthetic_fault_not_fired")
    _verify_states(backend, applied=applied, compensated=compensated)
    records = backend.journal_records()
    journal_head = records[-1].record_sha256 if records else HASH_ZERO
    return {
        "scenario_sha256": _sha256_projection(scenario.projection()),
        "outcome": outcome,
        "applied_step_count": len(applied),
        "compensated_step_count": len(compensated),
        "journal_record_count": len(records),
        "journal_head_sha256": journal_head,
        "state_projection_sha256": _sha256_projection(
            backend.state_projection
        ),
        "operation_counts": backend.operation_counts,
    }


def _verify_contract_and_schema(
    *,
    family_counts: dict[str, int],
) -> tuple[dict[str, object], dict[str, object]]:
    contract = _load_json(CONTRACT_PATH)
    schema = _load_json(RECEIPT_SCHEMA_PATH)
    _require(
        contract.get("schema_version") == CONTRACT_SCHEMA_VERSION,
        "proof_contract_schema_invalid",
    )
    _require(
        contract.get("state")
        == (
            "repository-only-synthetic-harness-packaged-"
            "no-promoted-receipt-no-live-execution"
        ),
        "proof_contract_state_invalid",
    )
    _require(contract.get("proof_scope") == PROOF_SCOPE, "proof_contract_scope_invalid")
    matrix = contract.get("scenario_matrix")
    _require(isinstance(matrix, dict), "proof_contract_matrix_invalid")
    _require(matrix.get("scenario_count") == EXPECTED_SCENARIO_COUNT, "proof_contract_matrix_invalid")
    _require(matrix.get("family_counts") == family_counts, "proof_contract_matrix_invalid")
    _require(
        matrix.get("maximum_injected_events_per_scenario") == 2
        and matrix.get("maximum_interruptions_per_scenario") == 1
        and matrix.get(
            "compensation_interruption_scenarios_include_one_trigger_failure_and_one_interruption"
        )
        is True
        and matrix.get("operating_system_process_kills_performed") == 0
        and matrix.get("composite_step_partial_failure_scenarios_performed")
        == 0,
        "proof_contract_matrix_invalid",
    )
    claims = contract.get("claims")
    _require(isinstance(claims, dict), "proof_contract_claims_invalid")
    for key in (
        "live_execution_proven",
        "live_installation_proven",
        "live_rollback_proven",
        "durable_process_crash_recovery_proven",
        "composite_crash_recovery_proven",
        "docker_compatibility_proven",
        "systemd_compatibility_proven",
        "external_store_readiness_proven",
        "activation_proven",
    ):
        _require(claims.get(key) is False, "proof_contract_claims_invalid")
    _require(
        schema.get("$id")
        == "urn:governed-memory:dormant-store-install:disposable-proof-receipt:v2",
        "proof_receipt_schema_invalid",
    )
    properties = schema.get("properties")
    _require(isinstance(properties, dict), "proof_receipt_schema_invalid")
    schema_version = properties.get("schema_version")
    _require(
        isinstance(schema_version, dict)
        and schema_version.get("const") == RECEIPT_SCHEMA_VERSION,
        "proof_receipt_schema_invalid",
    )
    return contract, schema


def _verify_receipt_schema_projection(receipt: dict[str, object]) -> None:
    """Validate the closed receipt shape without a runtime schema dependency."""

    schema = _load_json(RECEIPT_SCHEMA_PATH)
    required = schema.get("required")
    properties = schema.get("properties")
    if (
        type(required) is not list
        or any(type(key) is not str for key in required)
        or type(properties) is not dict
        or set(receipt) != set(required)
        or set(receipt) - set(properties)
    ):
        raise DisposableProofError("proof_receipt_schema_projection_invalid")
    for key in required:
        rule = properties.get(key)
        if type(rule) is dict and "const" in rule and receipt.get(key) != rule["const"]:
            raise DisposableProofError("proof_receipt_schema_projection_invalid")
    for section in ("guard", "claims"):
        rule = properties.get(section)
        value = receipt.get(section)
        if type(rule) is not dict or type(value) is not dict:
            raise DisposableProofError("proof_receipt_schema_projection_invalid")
        nested_required = rule.get("required")
        nested_properties = rule.get("properties")
        if (
            type(nested_required) is not list
            or type(nested_properties) is not dict
            or set(value) != set(nested_required)
            or set(value) - set(nested_properties)
        ):
            raise DisposableProofError("proof_receipt_schema_projection_invalid")
        for key in nested_required:
            nested_rule = nested_properties.get(key)
            if (
                type(nested_rule) is dict
                and "const" in nested_rule
                and value.get(key) != nested_rule["const"]
            ):
                raise DisposableProofError(
                    "proof_receipt_schema_projection_invalid"
                )


def run_disposable_proof(*, disposable_root: Path) -> dict[str, object]:
    """Run the sole synthetic proof behavior and return a content-free receipt."""

    if not isinstance(disposable_root, Path) or not disposable_root.is_absolute():
        raise DisposableProofError("proof_disposable_root_invalid")
    root = _validate_disposable_root(disposable_root)
    with _runtime_fence(root):
        root = _validate_disposable_root(root)
        scenarios = _scenario_matrix()
        family_counts = _verify_scenario_matrix(scenarios)
        contract, receipt_schema = _verify_contract_and_schema(
            family_counts=family_counts
        )
        plan_sha256 = validate_plan(STORES_ONLY_PLAN)
        results = tuple(
            _run_case(
                scenario,
                case_index=index,
                disposable_root=root,
            )
            for index, scenario in enumerate(scenarios)
        )
        outcome_counts = dict(
            sorted(Counter(result["outcome"] for result in results).items())
        )
        _require(
            outcome_counts == EXPECTED_OUTCOME_COUNTS,
            "proof_outcome_count_invalid",
        )
        operation_counts: Counter[str] = Counter()
        for result in results:
            counts = result["operation_counts"]
            _require(isinstance(counts, dict), "proof_operation_counts_invalid")
            for key, value in counts.items():
                _require(
                    isinstance(key, str)
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0,
                    "proof_operation_counts_invalid",
                )
                operation_counts[key] += value
        source_hashes = {
            "backend": _sha256_file(BACKEND_SOURCE_PATH),
            "controller": _sha256_file(CONTROLLER_SOURCE_PATH),
            "execution_lock": _sha256_file(EXECUTION_LOCK_SOURCE_PATH),
            "harness": _sha256_file(HARNESS_SOURCE_PATH),
            "runner": _sha256_file(RUNNER_SOURCE_PATH),
        }
        receipt: dict[str, object] = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "proof_scope": PROOF_SCOPE,
            "outcome": PROOF_OUTCOME,
            "scenario_count": len(scenarios),
            "plan_step_count": len(STORES_ONLY_PLAN),
            "compensable_step_count": sum(
                1 for step in STORES_ONLY_PLAN if step.compensable
            ),
            "scenario_family_counts": family_counts,
            "outcome_counts": outcome_counts,
            "operation_counts": dict(sorted(operation_counts.items())),
            "journal_record_count": sum(
                int(result["journal_record_count"]) for result in results
            ),
            "scenario_matrix_sha256": _sha256_projection(
                tuple(scenario.projection() for scenario in scenarios)
            ),
            "scenario_result_set_sha256": _sha256_projection(results),
            "journal_head_set_sha256": _sha256_projection(
                tuple(result["journal_head_sha256"] for result in results)
            ),
            "state_result_set_sha256": _sha256_projection(
                tuple(result["state_projection_sha256"] for result in results)
            ),
            "controller_plan_model_sha256": plan_sha256,
            "controller_plan_document_sha256": _sha256_projection(
                _load_json(CONTROLLER_PLAN_PATH)
            ),
            "proof_contract_sha256": _sha256_projection(contract),
            "receipt_schema_sha256": _sha256_projection(receipt_schema),
            "proof_sources_sha256": _sha256_projection(source_hashes),
            "package_manifest_sha256": _sha256_file(PACKAGE_MANIFEST_PATH),
            "guard": {
                "instrumentation": "python_audit_hook_best_effort_not_kernel_confinement",
                "audit_hook_configured": True,
                "blocked_effect_count": _RUNTIME_AUDIT.blocked_effect_count,
                "environment_access_count": _RUNTIME_AUDIT.environment_access_count,
            },
            "claims": {
                "synthetic_atomic_step_plan_model_exercised": True,
                "single_interruption_resume_model_exercised": True,
                "same_attempt_compensation_sequence_model_exercised": True,
                "full_pre_attempt_state_restoration_proven": False,
                "empty_rollback_proven": False,
                "live_execution_proven": False,
                "live_installation_proven": False,
                "live_rollback_proven": False,
                "durable_process_crash_recovery_proven": False,
                "composite_crash_recovery_proven": False,
                "docker_compatibility_proven": False,
                "systemd_compatibility_proven": False,
                "external_store_readiness_proven": False,
                "activation_proven": False,
                "secrets_accessed": False,
                "images_staged_or_pulled": False,
                "kernel_confinement_proven": False,
                "hostile_same_process_replacement_resisted": False,
            },
        }
        receipt["receipt_sha256"] = _sha256_projection(receipt)
        _verify_receipt_schema_projection(receipt)
        return receipt
