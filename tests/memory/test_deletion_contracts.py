from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import inspect
import unittest
from uuid import UUID

from rag_engine.governed_memory.auth import (
    ActorRole,
    ActorScope,
    VerifiedActor,
)
from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.deletion_contracts import (
    ALLOWED_RECENT_WINDOW_SECONDS,
    CONVERSATIONAL_ERASURE_DOMAIN,
    DELETION_REQUEST_CONTRACT_VERSION,
    ConversationDeletionRequestV1,
    ConversationErasureTarget,
    DeletionSelectorKind,
    bind_conversation_deletion,
    conversation_deletion_confirmation_sha256,
    conversation_deletion_request_from_body,
    erasure_target_manifest_sha256,
    source_erasure_target_sha256,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
SESSION = UUID("22222222-2222-4222-8222-222222222222")
OPERATION = UUID("33333333-3333-4333-8333-333333333333")
MESSAGE = UUID("44444444-4444-4444-8444-444444444444")
THREAD = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
def actor() -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=OWNER,
        actor_id=OWNER,
        session_id=SESSION,
        role=ActorRole.OWNER,
        scopes=(ActorScope.ERASE_CONVERSATIONS,),
        authentication_manifest_sha256="a" * 64,
        authenticated_at=NOW,
    )


def body(kind: str, **values: object) -> dict[str, object]:
    try:
        selector_kind = DeletionSelectorKind(kind)
        confirmation = conversation_deletion_confirmation_sha256(
            operation_id=OPERATION,
            selector_kind=selector_kind,
            thread_id=(
                UUID(str(values["thread_id"]))
                if "thread_id" in values
                else None
            ),
            anchor_message_id=(
                UUID(str(values["anchor_message_id"]))
                if "anchor_message_id" in values
                else None
            ),
            recent_window_seconds=values.get("recent_window_seconds"),
        )
    except (ContractViolation, ValueError):
        confirmation = "c" * 64
    return {
        "contract_version": DELETION_REQUEST_CONTRACT_VERSION,
        "data_domain": CONVERSATIONAL_ERASURE_DOMAIN,
        "confirmation_sha256": confirmation,
        "operation_id": str(OPERATION),
        "selector_kind": kind,
        **values,
    }


class DeletionRequestContractTests(unittest.TestCase):
    def test_request_has_no_owner_or_clock_authority(self) -> None:
        self.assertEqual(
            {field.name for field in fields(ConversationDeletionRequestV1)},
            {
                "operation_id",
                "selector_kind",
                "anchor_message_id",
                "thread_id",
                "recent_window_seconds",
                "confirmation_sha256",
                "contract_version",
                "data_domain",
            },
        )
        self.assertEqual(
            tuple(inspect.signature(bind_conversation_deletion).parameters),
            ("actor", "request"),
        )
        for prohibited in (
            "owner_user_id",
            "user_id",
            "requested_at",
            "cutoff_at",
            "before",
            "not_before",
        ):
            with self.subTest(prohibited=prohibited):
                with self.assertRaises(ContractViolation) as raised:
                    conversation_deletion_request_from_body(
                        body(
                            "all_conversations",
                            **{prohibited: str(OWNER)},
                        )
                    )
                self.assertEqual(
                    raised.exception.code,
                    "deletion_request_authority_field_prohibited",
                )

    def test_only_chat_source_scopes_exist(self) -> None:
        self.assertEqual(
            {item.value for item in DeletionSelectorKind},
            {"message_tail", "thread", "recent", "all_conversations"},
        )
        for prohibited in ("all_memory", "all_governed_memory", "structured"):
            with self.subTest(prohibited=prohibited):
                with self.assertRaises(ContractViolation):
                    conversation_deletion_request_from_body(body(prohibited))

    def test_fixed_domain_excludes_structured_lifeswitch_data(self) -> None:
        self.assertEqual(
            CONVERSATIONAL_ERASURE_DOMAIN,
            "chat_source_and_derived_governed_conversational_memory_v1",
        )
        for prohibited_domain in (
            "lifeswitch_library",
            "workouts",
            "weightlifting_sessions",
            "daily_food_logs",
            "measurements",
            "accounts",
            "all_data",
        ):
            candidate = body("all_conversations")
            candidate["data_domain"] = prohibited_domain
            with self.subTest(prohibited_domain=prohibited_domain):
                with self.assertRaises(ContractViolation) as raised:
                    conversation_deletion_request_from_body(candidate)
                self.assertEqual(
                    raised.exception.code, "invalid_deletion_data_domain"
                )

    def test_each_selector_has_one_exact_shape_and_bounded_recent_values(self) -> None:
        valid = (
            body("thread", thread_id=str(THREAD)),
            body(
                "message_tail",
                thread_id=str(THREAD),
                anchor_message_id=str(MESSAGE),
            ),
            *(
                body("recent", recent_window_seconds=value)
                for value in ALLOWED_RECENT_WINDOW_SECONDS
            ),
            body("all_conversations"),
        )
        for candidate in valid:
            with self.subTest(candidate=candidate):
                self.assertIsInstance(
                    conversation_deletion_request_from_body(candidate),
                    ConversationDeletionRequestV1,
                )
        invalid = (
            body("message_tail", anchor_message_id=str(MESSAGE)),
            body("thread", anchor_message_id=str(MESSAGE)),
            body("recent", recent_window_seconds=60),
            body("all_conversations", thread_id=str(THREAD)),
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ContractViolation):
                    conversation_deletion_request_from_body(candidate)

    def test_confirmation_is_content_free_and_replay_bound(self) -> None:
        request = conversation_deletion_request_from_body(
            body("all_conversations")
        )
        command = bind_conversation_deletion(actor=actor(), request=request)
        self.assertEqual(command.authority.owner_user_id, OWNER)
        expected = conversation_deletion_confirmation_sha256(
            operation_id=OPERATION,
            selector_kind=DeletionSelectorKind.ALL_CONVERSATIONS,
        )
        self.assertEqual(command.request.confirmation_sha256, expected)
        self.assertNotIn("DELETE CHAT DATA", expected)
        with self.assertRaises(ContractViolation) as raised:
            conversation_deletion_request_from_body(
                {
                    **body("all_conversations"),
                    "confirmation_sha256": "d" * 64,
                }
            )
        self.assertEqual(
            raised.exception.code, "deletion_confirmation_sha256_mismatch"
        )

    def test_confirmation_binds_operation_selector_targets_and_phrase(self) -> None:
        values = {
            "operation_id": OPERATION,
            "selector_kind": DeletionSelectorKind.MESSAGE_TAIL,
            "thread_id": THREAD,
            "anchor_message_id": MESSAGE,
        }
        exact = conversation_deletion_confirmation_sha256(**values)
        self.assertNotEqual(
            exact,
            conversation_deletion_confirmation_sha256(
                **{**values, "operation_id": UUID(int=1)}
            ),
        )
        self.assertNotEqual(
            exact,
            conversation_deletion_confirmation_sha256(
                operation_id=OPERATION,
                selector_kind=DeletionSelectorKind.THREAD,
                thread_id=THREAD,
            ),
        )

    def test_owner_without_erasure_scope_cannot_become_authority(self) -> None:
        denied = VerifiedActor(
            owner_user_id=OWNER,
            actor_id=OWNER,
            session_id=SESSION,
            role=ActorRole.OWNER,
            scopes=(ActorScope.MUTATE_CLAIMS,),
            authentication_manifest_sha256="a" * 64,
            authenticated_at=NOW,
        )
        request = conversation_deletion_request_from_body(
            body("all_conversations")
        )
        with self.assertRaises(ContractViolation) as raised:
            bind_conversation_deletion(actor=denied, request=request)
        self.assertEqual(raised.exception.code, "actor_scope_denied")

    def test_worker_actor_cannot_become_deletion_authority(self) -> None:
        worker = VerifiedActor(
            owner_user_id=OWNER,
            actor_id=SESSION,
            session_id=SESSION,
            role=ActorRole.WORKER,
            scopes=(ActorScope.READ_CLAIMS,),
            authentication_manifest_sha256="a" * 64,
            authenticated_at=NOW,
        )
        request = conversation_deletion_request_from_body(
            body("all_conversations")
        )
        with self.assertRaises(ContractViolation) as raised:
            bind_conversation_deletion(actor=worker, request=request)
        self.assertEqual(
            raised.exception.code, "deletion_owner_authority_required"
        )

    def test_target_and_manifest_match_postgres_framing(self) -> None:
        def frame(name: str, value: str) -> str:
            return f"{name}:{len(value.encode('utf-8'))}:{value}\n"

        preimage = "governed_memory.source_erasure_target.v1\n" + "".join(
            (
                frame("owner_user_id", str(OWNER)),
                frame("operation_id", str(OPERATION)),
                frame("message_id", str(MESSAGE)),
                frame("thread_id", str(THREAD)),
                frame("source_created_at", "2030-01-02T12:00:00.000000Z"),
            )
        )
        expected = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
        self.assertEqual(
            source_erasure_target_sha256(
                owner_user_id=OWNER,
                operation_id=OPERATION,
                message_id=MESSAGE,
                thread_id=THREAD,
                source_created_at=NOW,
            ),
            expected,
        )
        target = ConversationErasureTarget(
            owner_user_id=OWNER,
            operation_id=OPERATION,
            message_id=MESSAGE,
            thread_id=THREAD,
            source_created_at=NOW,
            target_sha256=expected,
        )
        expected_manifest = hashlib.sha256(
            (
                "governed_memory.source_erasure_target_manifest.v1\n"
                + expected
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            erasure_target_manifest_sha256(
                owner_user_id=OWNER,
                operation_id=OPERATION,
                targets=(target,),
            ),
            expected_manifest,
        )
        self.assertEqual(
            erasure_target_manifest_sha256(
                owner_user_id=OWNER,
                operation_id=OPERATION,
                targets=(),
            ),
            hashlib.sha256(
                b"governed_memory.source_erasure_target_manifest.v1\n"
            ).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
