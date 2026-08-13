from __future__ import annotations

"""Durable root-owned controller-authority marker for empty-store rollback.

The transport has no CLI and accepts no caller-selected path.  It persists a
claim-bound acquisition record before the live semantic-empty recheck and a
second create-once record for that recheck.  These files are durable
authority/evidence markers; they do not independently prevent a
privileged process or a direct PostgreSQL/Qdrant client from writing.  The
rollback trust boundary therefore also requires the separately verified
absence of installed application writers, zero active clients, and fresh
root-controlled store credentials.

A failed rollback leaves the records in place; only the controller's
post-receipt release path removes the exact records.  Release removes the
acquisition record first.  If the process then crashes, replay recreates that
exact record, recovers the semantic proof, and safely retries the release.
"""

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Final, Protocol

from .linux_live_adapters import ClosedRootFileEffects
from .linux_live_transports import (
    ObservationState,
    RootFileSlot,
    RootRemovalIdentity,
)
from .receipts import canonical_json_bytes
from .rollback_entrypoint import (
    RollbackOperationRequest,
    canonical_live_rollback_controller_authority_marker_sha256,
)
from .rollback_live_adapter import DurableLiveRollbackMarkerObservation


class LiveRollbackMarkerTransportError(RuntimeError):
    """Content-free refusal from durable controller-authority-marker transport."""


ACQUISITION_FILENAME: Final = "empty-rollback-controller-authority-marker.json"
SEMANTIC_EMPTY_FILENAME: Final = (
    "empty-rollback-semantic-empty-proof.json"
)
MAX_CONTROLLER_AUTHORITY_MARKER_DOCUMENT_BYTES: Final = 8192
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ACQUISITION_KEYS: Final = frozenset(
    {
        "schema_version",
        "execution_id",
        "attempt_id",
        "plan_sha256",
        "eligibility_receipt_sha256",
        "exact_targets_sha256",
        "controller_runtime_tree_sha256",
        "controller_release_tree_sha256",
        "marker_bound_postgres_identity_sha256",
        "marker_bound_qdrant_identity_sha256",
        "controller_authority_marker_sha256",
    }
)
_SEMANTIC_KEYS: Final = frozenset(
    {
        "schema_version",
        "execution_id",
        "attempt_id",
        "controller_authority_marker_sha256",
        "semantic_empty_state_sha256",
    }
)


@dataclass(frozen=True, slots=True)
class RootControllerAuthorityMarkerFileObservation:
    content: bytes
    regular_no_follow: bool
    mode: int
    uid: int
    gid: int
    file_fsynced: bool
    parent_fsynced: bool

    def __post_init__(self) -> None:
        if (
            type(self.content) is not bytes
            or not self.content
            or len(self.content) > MAX_CONTROLLER_AUTHORITY_MARKER_DOCUMENT_BYTES
            or self.regular_no_follow is not True
            or self.mode != 0o400
            or self.uid != 0
            or self.gid != 0
            or self.file_fsynced is not True
            or self.parent_fsynced is not True
        ):
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_file_observation_invalid"
            )


class RootControllerAuthorityMarkerFileStore(Protocol):
    def create_or_read_exact(
        self,
        execution_id: str,
        filename: str,
        expected: bytes,
    ) -> RootControllerAuthorityMarkerFileObservation: ...

    def read_optional_exact(
        self,
        execution_id: str,
        filename: str,
        expected: bytes | None,
    ) -> RootControllerAuthorityMarkerFileObservation | None: ...

    def remove_exact_execution(
        self,
        execution_id: str,
        acquisition: bytes,
        semantic_empty: bytes,
    ) -> None: ...


class ClosedLinuxRootControllerAuthorityMarkerFileStore:
    """Bind the controller-authority-marker protocol to the selected descriptor-safe Linux lane."""

    def __init__(
        self,
        effects: ClosedRootFileEffects,
        execution_id: str,
    ) -> None:
        if (
            type(effects) is not ClosedRootFileEffects
            or _HASH_RE.fullmatch(execution_id) is None
        ):
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_linux_store_invalid"
            )
        self._effects = effects
        self._execution_id = execution_id

    @staticmethod
    def _slot(filename: str) -> RootFileSlot:
        if filename == ACQUISITION_FILENAME:
            return RootFileSlot.ROLLBACK_CONTROLLER_AUTHORITY_MARKER
        if filename == SEMANTIC_EMPTY_FILENAME:
            return RootFileSlot.ROLLBACK_SEMANTIC_EMPTY_PROOF
        raise LiveRollbackMarkerTransportError(
            "live_rollback_controller_authority_marker_selector_invalid"
        )

    def _require_execution(self, execution_id: str) -> None:
        if execution_id != self._execution_id:
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_execution_mismatch"
            )

    @staticmethod
    def _observation(content: bytes) -> RootControllerAuthorityMarkerFileObservation:
        return RootControllerAuthorityMarkerFileObservation(
            content=content,
            regular_no_follow=True,
            mode=0o400,
            uid=0,
            gid=0,
            file_fsynced=True,
            parent_fsynced=True,
        )

    def create_or_read_exact(
        self,
        execution_id: str,
        filename: str,
        expected: bytes,
    ) -> RootControllerAuthorityMarkerFileObservation:
        self._require_execution(execution_id)
        slot = self._slot(filename)
        expected_sha256 = hashlib.sha256(expected).hexdigest()
        observed = self._effects.observe_execution_record(
            slot,
            expected_sha256,
        )
        if observed.observation.state is ObservationState.ABSENT:
            self._effects.create_execution_record(slot, expected)
        elif observed.observation.state is not ObservationState.EXACT:
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_existing_file_mismatch"
            )
        content = self._effects.read_execution_record(
            slot,
            expected_sha256,
        )
        if content != expected:
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_file_mismatch"
            )
        return self._observation(content)

    def read_optional_exact(
        self,
        execution_id: str,
        filename: str,
        expected: bytes | None,
    ) -> RootControllerAuthorityMarkerFileObservation | None:
        self._require_execution(execution_id)
        if type(expected) is not bytes:
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_expected_bytes_required"
            )
        slot = self._slot(filename)
        content = self._effects.read_execution_record(
            slot,
            hashlib.sha256(expected).hexdigest(),
        )
        if content is None:
            return None
        if content != expected:
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_optional_file_mismatch"
            )
        return self._observation(content)

    def remove_exact_execution(
        self,
        execution_id: str,
        acquisition: bytes,
        semantic_empty: bytes,
    ) -> None:
        self._require_execution(execution_id)
        observations: dict[str, RootRemovalIdentity | None] = {}
        for filename, expected in (
            (ACQUISITION_FILENAME, acquisition),
            (SEMANTIC_EMPTY_FILENAME, semantic_empty),
        ):
            slot = self._slot(filename)
            observed = self._effects.observe_execution_record(
                slot,
                hashlib.sha256(expected).hexdigest(),
            )
            if observed.observation.state is ObservationState.DRIFT:
                raise LiveRollbackMarkerTransportError(
                    "live_rollback_controller_authority_marker_release_file_mismatch"
                )
            observations[filename] = observed.identity
        acquisition_identity = observations[ACQUISITION_FILENAME]
        semantic_identity = observations[SEMANTIC_EMPTY_FILENAME]
        if acquisition_identity is None:
            if semantic_identity is not None:
                raise LiveRollbackMarkerTransportError(
                    "live_rollback_controller_authority_marker_release_prefix_invalid"
                )
            return
        self._effects.remove_execution_record(acquisition_identity)
        if semantic_identity is not None:
            self._effects.remove_execution_record(semantic_identity)


def _load_document(raw: bytes, keys: frozenset[str]) -> dict[str, object]:
    try:
        document = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LiveRollbackMarkerTransportError(
            "live_rollback_controller_authority_marker_document_invalid"
        ) from None
    if type(document) is not dict or set(document) != set(keys):
        raise LiveRollbackMarkerTransportError(
            "live_rollback_controller_authority_marker_document_invalid"
        )
    if canonical_json_bytes(document) + b"\n" != raw:
        raise LiveRollbackMarkerTransportError(
            "live_rollback_controller_authority_marker_document_not_canonical"
        )
    return document


class DurableRootLiveRollbackMarkerTransport:
    """Concrete durable controller-marker transport over a root file store."""

    def __init__(self, store: RootControllerAuthorityMarkerFileStore) -> None:
        if not all(
            callable(getattr(store, name, None))
            for name in (
                "create_or_read_exact",
                "read_optional_exact",
                "remove_exact_execution",
            )
        ):
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_store_invalid"
            )
        self._store = store

    @staticmethod
    def _acquisition(
        request: RollbackOperationRequest,
        postgres_identity: str,
        qdrant_identity: str,
    ) -> tuple[dict[str, object], bytes]:
        controller_authority_marker_sha256 = (
            canonical_live_rollback_controller_authority_marker_sha256(
                request,
                marker_bound_postgres_identity_sha256=postgres_identity,
                marker_bound_qdrant_identity_sha256=qdrant_identity,
            )
        )
        document: dict[str, object] = {
            "schema_version": "governed-memory-live-rollback-controller-authority-marker-v1",
            "execution_id": request.execution_id,
            "attempt_id": request.attempt_id,
            "plan_sha256": request.plan_sha256,
            "eligibility_receipt_sha256": (
                request.eligibility_receipt_sha256
            ),
            "exact_targets_sha256": request.exact_targets_sha256,
            "controller_runtime_tree_sha256": (
                request.controller_runtime_tree_sha256
            ),
            "controller_release_tree_sha256": (
                request.controller_release_tree_sha256
            ),
            "marker_bound_postgres_identity_sha256": postgres_identity,
            "marker_bound_qdrant_identity_sha256": qdrant_identity,
            "controller_authority_marker_sha256": controller_authority_marker_sha256,
        }
        return document, canonical_json_bytes(document) + b"\n"

    @staticmethod
    def _semantic(
        request: RollbackOperationRequest,
        controller_authority_marker_sha256: str,
        semantic_sha256: str,
    ) -> tuple[dict[str, object], bytes]:
        if (
            _HASH_RE.fullmatch(controller_authority_marker_sha256) is None
            or _HASH_RE.fullmatch(semantic_sha256) is None
        ):
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_semantic_identity_invalid"
            )
        document: dict[str, object] = {
            "schema_version": (
                "governed-memory-live-rollback-controller-authority-marker-semantic-empty-v1"
            ),
            "execution_id": request.execution_id,
            "attempt_id": request.attempt_id,
            "controller_authority_marker_sha256": controller_authority_marker_sha256,
            "semantic_empty_state_sha256": semantic_sha256,
        }
        return document, canonical_json_bytes(document) + b"\n"

    @staticmethod
    def _observation(
        acquisition: dict[str, object],
        file_observation: RootControllerAuthorityMarkerFileObservation,
        semantic_sha256: str | None,
    ) -> DurableLiveRollbackMarkerObservation:
        return DurableLiveRollbackMarkerObservation(
            controller_authority_marker_sha256=str(acquisition["controller_authority_marker_sha256"]),
            marker_bound_postgres_identity_sha256=str(
                acquisition[
                    "marker_bound_postgres_identity_sha256"
                ]
            ),
            marker_bound_qdrant_identity_sha256=str(
                acquisition["marker_bound_qdrant_identity_sha256"]
            ),
            semantic_empty_state_sha256=semantic_sha256,
            durable_root_file_regular_no_follow=(
                file_observation.regular_no_follow
            ),
            durable_root_file_mode=file_observation.mode,
            durable_root_file_uid=file_observation.uid,
            durable_root_file_gid=file_observation.gid,
            durable_root_file_fsynced=file_observation.file_fsynced,
            durable_parent_fsynced=file_observation.parent_fsynced,
        )

    def acquire_or_recover(
        self,
        request: RollbackOperationRequest,
        *,
        marker_bound_postgres_identity_sha256: str,
        marker_bound_qdrant_identity_sha256: str,
        expected_semantic_empty_state_sha256: str,
    ) -> DurableLiveRollbackMarkerObservation:
        if (
            type(request) is not RollbackOperationRequest
            or request.step.step_id
            != "R04_ACQUIRE_ROLLBACK_CONTROLLER_AUTHORITY_MARKER"
        ):
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_request_invalid"
            )
        acquisition, raw = self._acquisition(
            request,
            marker_bound_postgres_identity_sha256,
            marker_bound_qdrant_identity_sha256,
        )
        acquired_file = self._store.create_or_read_exact(
            request.execution_id,
            ACQUISITION_FILENAME,
            raw,
        )
        parsed = _load_document(acquired_file.content, _ACQUISITION_KEYS)
        if parsed != acquisition:
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_acquisition_mismatch"
            )
        _expected_semantic, expected_semantic_raw = self._semantic(
            request,
            str(acquisition["controller_authority_marker_sha256"]),
            expected_semantic_empty_state_sha256,
        )
        semantic_file = self._store.read_optional_exact(
            request.execution_id,
            SEMANTIC_EMPTY_FILENAME,
            expected_semantic_raw,
        )
        semantic_sha256: str | None = None
        if semantic_file is not None:
            semantic = _load_document(semantic_file.content, _SEMANTIC_KEYS)
            if (
                semantic["execution_id"] != request.execution_id
                or semantic["attempt_id"] != request.attempt_id
                or semantic["controller_authority_marker_sha256"] != acquisition["controller_authority_marker_sha256"]
                or _HASH_RE.fullmatch(
                    str(semantic["semantic_empty_state_sha256"])
                ) is None
                or semantic["semantic_empty_state_sha256"]
                != expected_semantic_empty_state_sha256
            ):
                raise LiveRollbackMarkerTransportError(
                    "live_rollback_controller_authority_marker_semantic_mismatch"
                )
            semantic_sha256 = str(
                semantic["semantic_empty_state_sha256"]
            )
        return self._observation(
            acquisition,
            acquired_file,
            semantic_sha256,
        )

    def persist_semantic_empty_observation(
        self,
        request: RollbackOperationRequest,
        held: DurableLiveRollbackMarkerObservation,
        semantic_empty_state_sha256: str,
    ) -> DurableLiveRollbackMarkerObservation:
        self.assert_held(request, held)
        semantic, raw = self._semantic(
            request,
            held.controller_authority_marker_sha256,
            semantic_empty_state_sha256,
        )
        semantic_file = self._store.create_or_read_exact(
            request.execution_id,
            SEMANTIC_EMPTY_FILENAME,
            raw,
        )
        if _load_document(semantic_file.content, _SEMANTIC_KEYS) != semantic:
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_semantic_persistence_mismatch"
            )
        acquisition, acquired_raw = self._acquisition(
            request,
            held.marker_bound_postgres_identity_sha256,
            held.marker_bound_qdrant_identity_sha256,
        )
        acquired_file = self._store.read_optional_exact(
            request.execution_id,
            ACQUISITION_FILENAME,
            acquired_raw,
        )
        if acquired_file is None:
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_acquisition_missing"
            )
        return self._observation(
            acquisition,
            acquired_file,
            semantic_empty_state_sha256,
        )

    def assert_held(
        self,
        request: RollbackOperationRequest,
        held: DurableLiveRollbackMarkerObservation,
    ) -> DurableLiveRollbackMarkerObservation:
        if (
            type(request) is not RollbackOperationRequest
            or type(held) is not DurableLiveRollbackMarkerObservation
        ):
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_assertion_invalid"
            )
        acquisition, acquired_raw = self._acquisition(
            request,
            held.marker_bound_postgres_identity_sha256,
            held.marker_bound_qdrant_identity_sha256,
        )
        acquired_file = self._store.read_optional_exact(
            request.execution_id,
            ACQUISITION_FILENAME,
            acquired_raw,
        )
        if acquired_file is None:
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_acquisition_missing"
            )
        semantic_sha256 = held.semantic_empty_state_sha256
        if semantic_sha256 is None:
            pass
        else:
            _semantic, semantic_raw = self._semantic(
                request,
                held.controller_authority_marker_sha256,
                semantic_sha256,
            )
            semantic_file = self._store.read_optional_exact(
                request.execution_id,
                SEMANTIC_EMPTY_FILENAME,
                semantic_raw,
            )
            if semantic_file is None:
                raise LiveRollbackMarkerTransportError(
                    "live_rollback_controller_authority_marker_semantic_record_missing"
                )
        observed = self._observation(
            acquisition,
            acquired_file,
            semantic_sha256,
        )
        if observed != held:
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_observation_drift"
            )
        return observed

    def release_after_receipt(
        self,
        request: RollbackOperationRequest,
        held: DurableLiveRollbackMarkerObservation,
    ) -> None:
        self.assert_held(request, held)
        semantic_sha256 = held.semantic_empty_state_sha256
        if semantic_sha256 is None:
            raise LiveRollbackMarkerTransportError(
                "live_rollback_controller_authority_marker_release_without_semantic_proof"
            )
        _acquisition, acquired_raw = self._acquisition(
            request,
            held.marker_bound_postgres_identity_sha256,
            held.marker_bound_qdrant_identity_sha256,
        )
        _semantic, semantic_raw = self._semantic(
            request,
            held.controller_authority_marker_sha256,
            semantic_sha256,
        )
        self._store.remove_exact_execution(
            request.execution_id,
            acquired_raw,
            semantic_raw,
        )


def production_live_rollback_marker_transport(
    *,
    root_file_effects: ClosedRootFileEffects,
    execution_id: str,
) -> DurableRootLiveRollbackMarkerTransport:
    """Bind the selected fixed-path Linux transport without effects."""

    return DurableRootLiveRollbackMarkerTransport(
        ClosedLinuxRootControllerAuthorityMarkerFileStore(root_file_effects, execution_id)
    )


__all__ = [
    "ACQUISITION_FILENAME",
    "ClosedLinuxRootControllerAuthorityMarkerFileStore",
    "DurableRootLiveRollbackMarkerTransport",
    "LiveRollbackMarkerTransportError",
    "RootControllerAuthorityMarkerFileObservation",
    "RootControllerAuthorityMarkerFileStore",
    "SEMANTIC_EMPTY_FILENAME",
    "production_live_rollback_marker_transport",
]
