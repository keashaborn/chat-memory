from __future__ import annotations

import unittest
from unittest import mock

from tools.governed_memory_validation import (
    execute_phase9_pre_effect_disposition as disposition_entrypoint,
)
from tools.governed_memory_validation import phase9_permitted_candidate as subject
from tools.governed_memory_validation import pre_effect_disposition
from tools.governed_memory_validation import (
    publish_phase9_pre_effect_permit as permit_publisher,
)


COMMIT = "a" * 40
TREE = "b" * 40
PACKAGE = disposition_entrypoint.PACKAGE_MANIFEST_SHA256
RUNTIME = disposition_entrypoint.CONTROLLER_RUNTIME_RECEIPT_SHA256
BLOBS = {path: f"{index + 1:x}" * 40 for index, path in enumerate(disposition_entrypoint._SOURCE_PATHS)}
SUCCESSOR = pre_effect_disposition.production_successor_attempt_identity_sha256(
    package_manifest_sha256=PACKAGE,
    controller_runtime_receipt_sha256=RUNTIME,
)


def permit(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "candidate_git_commit": COMMIT,
        "candidate_git_tree": TREE,
        "package_manifest_sha256": PACKAGE,
        "controller_runtime_receipt_sha256": RUNTIME,
        "contract_sha256": pre_effect_disposition.PRODUCTION_CONTRACT_SHA256,
        "predecessor_attempt_identity_sha256": (
            pre_effect_disposition.PRODUCTION_PREDECESSOR_ATTEMPT_IDENTITY_SHA256
        ),
        "successor_attempt_identity_sha256": SUCCESSOR,
        "authorization_text_sha256": (
            pre_effect_disposition.PRODUCTION_AUTHORIZATION_TEXT_SHA256
        ),
        "thread_id": disposition_entrypoint.THREAD_ID,
        "source_blobs": dict(BLOBS),
        "authorized_action": (
            "execute_pre_effect_disposition_and_disposable_live_proof_only"
        ),
    }
    value.update(changes)
    return value


def identity(
    *,
    commit: str = COMMIT,
    tree: str = TREE,
    blobs: dict[str, str] | None = None,
) -> disposition_entrypoint.CandidateIdentity:
    selected = BLOBS if blobs is None else blobs
    return disposition_entrypoint.CandidateIdentity(
        commit,
        tree,
        tuple((path, selected[path]) for path in disposition_entrypoint._SOURCE_PATHS),
    )


class Phase9PermittedCandidateTests(unittest.TestCase):
    def call(
        self,
        *,
        document: dict[str, object] | None = None,
        current: disposition_entrypoint.CandidateIdentity | None = None,
        selected: tuple[str, str, dict[str, str]] | None = None,
        final_document: dict[str, object] | None = None,
        final_current: disposition_entrypoint.CandidateIdentity | None = None,
        final_selected: tuple[str, str, dict[str, str]] | None = None,
        **expectations: object,
    ) -> subject.PermittedCandidateAuthority:
        exact_document = permit() if document is None else document
        exact_current = identity() if current is None else current
        exact_selected = (
            (COMMIT, TREE, dict(BLOBS)) if selected is None else selected
        )
        exact_final_document = (
            exact_document if final_document is None else final_document
        )
        exact_final_current = (
            exact_current if final_current is None else final_current
        )
        exact_final_selected = (
            exact_selected if final_selected is None else final_selected
        )
        with (
            mock.patch.object(
                disposition_entrypoint,
                "_read_and_verify_permit",
                side_effect=(exact_document, exact_final_document),
            ),
            mock.patch.object(
                disposition_entrypoint,
                "_verified_candidate_identity",
                side_effect=(exact_current, exact_final_current),
            ),
            mock.patch.object(
                disposition_entrypoint,
                "_reverify_candidate",
            ) as reverify,
            mock.patch.object(
                permit_publisher,
                "_verify_selected_candidate",
                side_effect=(exact_selected, exact_final_selected),
            ),
        ):
            authority = subject.require_exact_permitted_candidate(
                **expectations
            )
        self.assertEqual(
            reverify.call_args_list,
            [
                mock.call(exact_document, exact_current),
                mock.call(exact_final_document, exact_final_current),
            ],
        )
        return authority

    def test_exact_permit_head_tag_sources_and_expectations_are_accepted(self) -> None:
        observed = self.call(
            expected_candidate_git_commit=COMMIT,
            expected_candidate_git_tree=TREE,
            expected_package_manifest_sha256=PACKAGE,
            expected_controller_runtime_receipt_sha256=RUNTIME,
            expected_thread_id=disposition_entrypoint.THREAD_ID,
            expected_authorization_text_sha256=(
                disposition_entrypoint.AUTHORIZATION_TEXT_SHA256
            ),
        )
        self.assertEqual(observed.candidate_git_commit, COMMIT)
        self.assertEqual(observed.candidate_git_tree, TREE)
        self.assertEqual(dict(observed.source_blobs), BLOBS)
        self.assertEqual(observed.successor_attempt_identity_sha256, SUCCESSOR)

    def test_permit_head_tag_or_source_drift_is_refused(self) -> None:
        changed_blobs = dict(BLOBS)
        changed_blobs[next(iter(changed_blobs))] = "f" * 40
        cases = (
            {"document": permit(candidate_git_commit="c" * 40)},
            {"current": identity(commit="c" * 40)},
            {"selected": ("c" * 40, TREE, dict(BLOBS))},
            {"selected": (COMMIT, "c" * 40, dict(BLOBS))},
            {"selected": (COMMIT, TREE, changed_blobs)},
        )
        for values in cases:
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(
                    subject.Phase9PermittedCandidateError,
                    "phase9_permitted_candidate_required",
                ),
            ):
                self.call(**values)

    def test_contract_successor_thread_authorization_and_pr_drift_are_refused(self) -> None:
        document_cases = (
            permit(contract_sha256="f" * 64),
            permit(predecessor_attempt_identity_sha256="f" * 64),
            permit(successor_attempt_identity_sha256="f" * 64),
            permit(authorization_text_sha256="f" * 64),
        )
        for document in document_cases:
            with (
                self.subTest(document=document),
                self.assertRaises(subject.Phase9PermittedCandidateError),
            ):
                self.call(document=document)
        expectation_cases = (
            {"expected_candidate_git_commit": "c" * 40},
            {"expected_candidate_git_tree": "c" * 40},
            {"expected_package_manifest_sha256": "f" * 64},
            {"expected_controller_runtime_receipt_sha256": "f" * 64},
            {"expected_thread_id": "foreign"},
            {"expected_authorization_text_sha256": "f" * 64},
        )
        for expectations in expectation_cases:
            with (
                self.subTest(expectations=expectations),
                self.assertRaises(subject.Phase9PermittedCandidateError),
            ):
                self.call(**expectations)

    def test_tag_change_between_initial_and_final_selection_is_refused(self) -> None:
        with self.assertRaisesRegex(
            subject.Phase9PermittedCandidateError,
            "^phase9_permitted_candidate_required$",
        ):
            self.call(
                final_selected=("c" * 40, TREE, dict(BLOBS)),
            )

    def test_upstream_permit_refusal_is_content_free(self) -> None:
        failure = disposition_entrypoint.Phase9PreEffectDispositionEntrypointError(
            "private detail"
        )
        with (
            mock.patch.object(
                disposition_entrypoint,
                "_read_and_verify_permit",
                side_effect=failure,
            ),
            self.assertRaisesRegex(
                subject.Phase9PermittedCandidateError,
                "^phase9_permitted_candidate_required$",
            ),
        ):
            subject.require_exact_permitted_candidate()


if __name__ == "__main__":
    unittest.main()
