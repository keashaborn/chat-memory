"""Conversation-to-Memory boundary tests; no conversation store is contacted."""

from __future__ import annotations

from datetime import timedelta
import json
import unittest

from rag_engine.governed_memory.auth import (
    ActorRole,
    ActorScope,
    VerifiedActor,
    require_owner,
    require_scope,
)
from rag_engine.governed_memory.contracts import ContractViolation, EligibilityDecision
from rag_engine.governed_memory.worker import process_ingest_item
from tests.memory._fixtures import (
    ATTACHMENT_A,
    ATTACHMENT_TEXT,
    CUTOVER,
    FIXTURE_PROVENANCE,
    MESSAGE_BEFORE_CUTOVER,
    NOW,
    OWNER_A,
    OWNER_B,
    make_bridge_lease,
    make_ingest_payload,
    make_worker_actor,
)


class IntakeBoundaryTests(unittest.TestCase):
    def process(self, payload: dict[str, object]) -> dict[str, object]:
        return process_ingest_item(
            payload,
            lease_envelope=make_bridge_lease(payload, ingest_after=CUTOVER),
            actor=make_worker_actor(),
            expected_owner_user_id=OWNER_A,
            transaction_time=NOW,
        )

    def test_post_cutover_user_message_produces_an_ordered_persistence_plan(self) -> None:
        result = self.process(make_ingest_payload())
        self.assertEqual(result["decision"], EligibilityDecision.SEND_EXTERNAL.value)
        self.assertEqual(result["bridge_state"], "completed")
        self.assertEqual(
            result["effects"],
            [
                "record_selected_evidence",
                "create_extraction_job",
                "complete_bridge",
            ],
        )
        self.assertIsNotNone(result["selected_evidence"])

    def test_ingest_has_one_dedicated_worker_scope(self) -> None:
        self.assertEqual(
            ActorScope.PROCESS_MEMORY_INGEST.value,
            "process_memory_ingest",
        )
        for wrong_scope in (
            ActorScope.LEASE_EXTRACTION,
            ActorScope.LEASE_PROJECTION,
        ):
            wrong_actor = VerifiedActor(
                owner_user_id=OWNER_A,
                actor_id=OWNER_B,
                session_id=OWNER_A,
                role=ActorRole.WORKER,
                scopes=(wrong_scope,),
                authentication_manifest_sha256="a" * 64,
                authenticated_at=NOW,
            )
            with self.subTest(scope=wrong_scope.value), self.assertRaises(
                ContractViolation
            ):
                process_ingest_item(
                    (payload := make_ingest_payload()),
                    lease_envelope=make_bridge_lease(
                        payload, ingest_after=CUTOVER
                    ),
                    actor=wrong_actor,
                    expected_owner_user_id=OWNER_A,
                    transaction_time=NOW,
                )

    def test_pre_cutover_message_cannot_enter_a_worker_lease(self) -> None:
        payload = make_ingest_payload(
            message_id=MESSAGE_BEFORE_CUTOVER,
            created_at=CUTOVER - timedelta(microseconds=1),
        )
        with self.assertRaises(ContractViolation):
            self.process(payload)

    def test_exact_cutover_timestamp_is_in_scope(self) -> None:
        payload = make_ingest_payload(created_at=CUTOVER)
        result = self.process(payload)
        self.assertNotEqual(result["bridge_state"], "expired")

    def test_content_hash_mismatch_fails_before_any_effect(self) -> None:
        payload = make_ingest_payload()
        payload["content_sha256"] = "0" * 64
        with self.assertRaises(ContractViolation):
            self.process(payload)

    def test_any_source_attachment_terminally_blocks_memory_intake(self) -> None:
        payload = make_ingest_payload(attachment_ids=(ATTACHMENT_A,))
        result = self.process(payload)
        serialized = json.dumps(result, sort_keys=True)
        self.assertEqual(result["decision"], EligibilityDecision.BLOCK_LOCAL.value)
        self.assertEqual(result["bridge_state"], "completed")
        self.assertEqual(result["effects"], ["complete_bridge"])
        self.assertFalse(result["provider_allowed"])
        self.assertIsNone(result["selected_evidence"])
        self.assertEqual(result["receipt"]["external_model_calls"], 0)
        self.assertNotIn(ATTACHMENT_TEXT, serialized)
        self.assertNotIn(str(ATTACHMENT_A), serialized)
        self.assertNotIn("read_attachment", result["effects"])

    def test_body_cannot_override_authenticated_owner(self) -> None:
        payload = make_ingest_payload()
        payload["requested_owner_user_id"] = str(OWNER_B)
        with self.assertRaises(ContractViolation):
            self.process(payload)

    def test_attachment_and_alias_content_keys_are_rejected_not_ignored(self) -> None:
        for forbidden_key in (
            "attachment_content",
            "attachment_text",
            "text",
            "raw",
        ):
            payload = make_ingest_payload()
            payload[forbidden_key] = "SYNTHETIC FORBIDDEN ALIAS CONTENT"
            with self.subTest(forbidden_key=forbidden_key), self.assertRaises(
                ContractViolation
            ):
                self.process(payload)

    def test_non_user_message_creates_no_evidence_or_job(self) -> None:
        payload = make_ingest_payload(role="assistant")
        result = self.process(payload)
        self.assertEqual(result["decision"], EligibilityDecision.BLOCK_LOCAL.value)
        self.assertNotIn("record_selected_evidence", result["effects"])
        self.assertNotIn("create_extraction_job", result["effects"])
        self.assertEqual(result["effects"], ["complete_bridge"])


class VerifiedActorBoundaryTests(unittest.TestCase):
    def verified_actor(self) -> VerifiedActor:
        return VerifiedActor(
            owner_user_id=OWNER_A,
            actor_id=OWNER_A,
            session_id=OWNER_A,
            role=ActorRole.OWNER,
            scopes=(ActorScope.READ_CLAIMS,),
            authentication_manifest_sha256="a" * 64,
            authenticated_at=NOW,
        )

    def test_owner_and_scope_are_derived_from_verified_actor(self) -> None:
        verified = self.verified_actor()
        require_owner(verified, OWNER_A)
        require_scope(verified, ActorScope.READ_CLAIMS)
        self.assertRegex(verified.binding_sha256, r"^[0-9a-f]{64}$")

    def test_cross_owner_and_missing_scope_fail_closed(self) -> None:
        verified = self.verified_actor()
        with self.assertRaises(ContractViolation):
            require_owner(verified, OWNER_B)
        with self.assertRaises(ContractViolation):
            require_scope(verified, ActorScope.MUTATE_CLAIMS)

    def test_owner_actor_id_must_equal_owner_user_id(self) -> None:
        with self.assertRaises(ContractViolation):
            VerifiedActor(
                owner_user_id=OWNER_A,
                actor_id=OWNER_B,
                session_id=OWNER_A,
                role=ActorRole.OWNER,
                scopes=(ActorScope.READ_CLAIMS,),
                authentication_manifest_sha256="a" * 64,
                authenticated_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
