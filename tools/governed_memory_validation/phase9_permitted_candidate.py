from __future__ import annotations

"""Verify the external Phase 9 permit against the exact tagged candidate."""

from dataclasses import dataclass
from typing import Final

from tools.governed_memory_validation import (
    execute_phase9_staged_prefix_disposition as disposition_entrypoint,
)
from tools.governed_memory_validation import staged_prefix_disposition
from tools.governed_memory_validation import (
    publish_phase9_staged_prefix_permit as permit_publisher,
)


_HASH_LENGTH: Final = 64
_GIT_LENGTH: Final = 40
_PERMITTED_AUTHORIZED_ACTION: Final = (
    "execute_staged_prefix_disposition_and_disposable_live_proof_only"
)


class Phase9PermittedCandidateError(RuntimeError):
    """Content-free refusal from the external candidate authority."""


@dataclass(frozen=True, slots=True)
class PermittedCandidateAuthority:
    candidate_git_commit: str
    candidate_git_tree: str
    package_manifest_sha256: str
    controller_runtime_receipt_sha256: str
    contract_sha256: str
    successor_attempt_identity_sha256: str
    source_blobs: tuple[tuple[str, str], ...]


def require_exact_permitted_candidate(
    *,
    expected_candidate_git_commit: str | None = None,
    expected_candidate_git_tree: str | None = None,
    expected_package_manifest_sha256: str | None = None,
    expected_controller_runtime_receipt_sha256: str | None = None,
    expected_thread_id: str | None = None,
    expected_authorization_text_sha256: str | None = None,
) -> PermittedCandidateAuthority:
    """Require permit, clean HEAD, fixed tag, sources, and sealed bindings."""

    optional_hashes = (
        expected_package_manifest_sha256,
        expected_controller_runtime_receipt_sha256,
        expected_authorization_text_sha256,
    )
    optional_git = (
        expected_candidate_git_commit,
        expected_candidate_git_tree,
    )
    if (
        any(value is not None and (type(value) is not str or len(value) != _HASH_LENGTH)
            for value in optional_hashes)
        or any(value is not None and (type(value) is not str or len(value) != _GIT_LENGTH)
               for value in optional_git)
        or (expected_thread_id is not None and type(expected_thread_id) is not str)
    ):
        raise Phase9PermittedCandidateError(
            "phase9_permitted_candidate_expectation_invalid"
        )
    try:
        permit = disposition_entrypoint._read_and_verify_permit()
        current = disposition_entrypoint._verified_candidate_identity(permit)
        tag_commit, tag_tree, tag_blobs = (
            permit_publisher._verify_selected_candidate()
        )
        successor = (
            staged_prefix_disposition.production_corrected_attempt_identity_sha256(
                package_manifest_sha256=str(
                    permit["package_manifest_sha256"]
                ),
                controller_runtime_receipt_sha256=str(
                    permit["controller_runtime_receipt_sha256"]
                ),
            )
        )
        disposition_entrypoint._reverify_candidate(permit, current)
        permit_snapshot = disposition_entrypoint._canonical(dict(permit))
        tag_blob_snapshot = tuple(sorted(tag_blobs.items()))
    except (
        disposition_entrypoint.Phase9StagedPrefixDispositionEntrypointError,
        permit_publisher.Phase9StagedPrefixPermitPublicationError,
        staged_prefix_disposition.StagedPrefixDispositionError,
        KeyError,
    ) as error:
        raise Phase9PermittedCandidateError(
            "phase9_permitted_candidate_required"
        ) from error

    current_blobs = dict(current.source_blobs)
    exact = {
        "contract_sha256": staged_prefix_disposition.PRODUCTION_CONTRACT_SHA256,
        "predecessor_attempt_identity_sha256": (
            staged_prefix_disposition.production_failed_prefix_identity_sha256()
        ),
        "authorization_text_sha256": (
            permit_publisher.AUTHORIZATION_TEXT_SHA256
        ),
        "successor_attempt_identity_sha256": successor,
        "authorized_action": _PERMITTED_AUTHORIZED_ACTION,
    }
    if (
        current.commit != tag_commit
        or current.tree != tag_tree
        or current_blobs != tag_blobs
        or permit.get("candidate_git_commit") != current.commit
        or permit.get("candidate_git_tree") != current.tree
        or permit.get("source_blobs") != current_blobs
        or any(permit.get(key) != value for key, value in exact.items())
        or (
            expected_candidate_git_commit is not None
            and current.commit != expected_candidate_git_commit
        )
        or (
            expected_candidate_git_tree is not None
            and current.tree != expected_candidate_git_tree
        )
        or (
            expected_package_manifest_sha256 is not None
            and permit.get("package_manifest_sha256")
            != expected_package_manifest_sha256
        )
        or (
            expected_controller_runtime_receipt_sha256 is not None
            and permit.get("controller_runtime_receipt_sha256")
            != expected_controller_runtime_receipt_sha256
        )
        or (
            expected_thread_id is not None
            and permit.get("thread_id") != expected_thread_id
        )
        or (
            expected_authorization_text_sha256 is not None
            and permit.get("authorization_text_sha256")
            != expected_authorization_text_sha256
        )
    ):
        raise Phase9PermittedCandidateError(
            "phase9_permitted_candidate_required"
        )

    # Re-read every external authority surface at the final return boundary.
    # The second fixed-tag selection closes a tag/HEAD/permit change between
    # initial validation and authority delivery without tracking final C/T.
    try:
        final_permit = disposition_entrypoint._read_and_verify_permit()
        final_current = disposition_entrypoint._verified_candidate_identity(
            final_permit
        )
        final_tag_commit, final_tag_tree, final_tag_blobs = (
            permit_publisher._verify_selected_candidate()
        )
        final_successor = (
            staged_prefix_disposition.production_corrected_attempt_identity_sha256(
                package_manifest_sha256=str(
                    final_permit["package_manifest_sha256"]
                ),
                controller_runtime_receipt_sha256=str(
                    final_permit["controller_runtime_receipt_sha256"]
                ),
            )
        )
        disposition_entrypoint._reverify_candidate(
            final_permit, final_current
        )
        final_permit_snapshot = disposition_entrypoint._canonical(
            dict(final_permit)
        )
        final_tag_blob_snapshot = tuple(sorted(final_tag_blobs.items()))
    except (
        disposition_entrypoint.Phase9StagedPrefixDispositionEntrypointError,
        permit_publisher.Phase9StagedPrefixPermitPublicationError,
        staged_prefix_disposition.StagedPrefixDispositionError,
        KeyError,
    ) as error:
        raise Phase9PermittedCandidateError(
            "phase9_permitted_candidate_required"
        ) from error
    if (
        final_permit_snapshot != permit_snapshot
        or final_current != current
        or final_tag_commit != tag_commit
        or final_tag_tree != tag_tree
        or final_tag_blob_snapshot != tag_blob_snapshot
        or final_successor != successor
        or final_current.commit != final_tag_commit
        or final_current.tree != final_tag_tree
        or dict(final_current.source_blobs) != final_tag_blobs
    ):
        raise Phase9PermittedCandidateError(
            "phase9_permitted_candidate_required"
        )
    return PermittedCandidateAuthority(
        candidate_git_commit=final_current.commit,
        candidate_git_tree=final_current.tree,
        package_manifest_sha256=str(
            final_permit["package_manifest_sha256"]
        ),
        controller_runtime_receipt_sha256=str(
            final_permit["controller_runtime_receipt_sha256"]
        ),
        contract_sha256=str(final_permit["contract_sha256"]),
        successor_attempt_identity_sha256=final_successor,
        source_blobs=tuple(final_current.source_blobs),
    )


__all__ = [
    "PermittedCandidateAuthority",
    "Phase9PermittedCandidateError",
    "require_exact_permitted_candidate",
]
