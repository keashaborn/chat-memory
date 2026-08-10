from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
import unittest
import unicodedata
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.conversation_capture import (
    CAPTURE_MODE_ENV,
    CAPTURE_OWNER_ALLOWLIST_ENV,
    CAPTURE_TEXT_AUTHORITY,
    CAPTURE_USER_SOURCE,
    CaptureConfigurationError,
    capture_auth_context_sha256,
    capture_decision_for_owner,
    capture_policy_sha256,
    enqueue_captured_chat_log_message,
    normalize_capture_text,
)
from rag_engine.governed_memory.eligibility import EligibilityPolicy


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = UUID("22222222-2222-4222-8222-222222222222")
MESSAGE_A = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OUTBOX_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CREATED_AT = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]
ELIGIBLE_INPUT = {
    "authority": CAPTURE_TEXT_AUTHORITY,
    "source": CAPTURE_USER_SOURCE,
    "has_attachments": False,
}


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *args: object) -> Mapping[str, object]:
        self.calls.append((query, args))
        return {"outcome": "enqueued", "outbox_id": OUTBOX_A}


class CaptureGateTests(unittest.TestCase):
    def test_default_off_ignores_unrelated_allowlist_and_disables_owner(self) -> None:
        decision = capture_decision_for_owner(
            str(OWNER_A),
            authority="active_voice_session_lease_v1",
            source="frontend/chat:assistant",
            has_attachments=True,
            environ={CAPTURE_OWNER_ALLOWLIST_ENV: "not-a-uuid"},
        )
        self.assertEqual(decision.mode, "off")
        self.assertFalse(decision.enabled)

    def test_pilot_is_exact_owner_allowlist_only(self) -> None:
        environ = {
            CAPTURE_MODE_ENV: "pilot",
            CAPTURE_OWNER_ALLOWLIST_ENV: str(OWNER_A),
        }
        self.assertTrue(
            capture_decision_for_owner(
                str(OWNER_A), **ELIGIBLE_INPUT, environ=environ
            ).enabled
        )
        self.assertFalse(
            capture_decision_for_owner(
                "33333333-3333-4333-8333-333333333333",
                **ELIGIBLE_INPUT,
                environ=environ,
            ).enabled
        )

    def test_pilot_rejects_empty_duplicate_or_noncanonical_allowlist(self) -> None:
        invalid = (
            "",
            f"{OWNER_A},{OWNER_A}",
            f"{OWNER_A},{OWNER_B}",
            "{" + str(OWNER_A) + "}",
        )
        for allowlist in invalid:
            with self.subTest(allowlist=allowlist):
                with self.assertRaises(CaptureConfigurationError):
                    capture_decision_for_owner(
                        str(OWNER_A),
                        **ELIGIBLE_INPUT,
                        environ={
                            CAPTURE_MODE_ENV: "pilot",
                            CAPTURE_OWNER_ALLOWLIST_ENV: allowlist,
                        },
                    )

    def test_pilot_excludes_voice_non_user_sources_and_attachments(self) -> None:
        environ = {
            CAPTURE_MODE_ENV: "pilot",
            CAPTURE_OWNER_ALLOWLIST_ENV: str(OWNER_A),
        }
        excluded_inputs = (
            {
                "authority": "active_voice_session_lease_v1",
                "source": CAPTURE_USER_SOURCE,
                "has_attachments": False,
            },
            {
                "authority": CAPTURE_TEXT_AUTHORITY,
                "source": "frontend/chat:assistant",
                "has_attachments": False,
            },
            {
                "authority": CAPTURE_TEXT_AUTHORITY,
                "source": "trusted-web",
                "has_attachments": False,
            },
            {
                "authority": CAPTURE_TEXT_AUTHORITY,
                "source": "frontend/identity",
                "has_attachments": False,
            },
            {
                "authority": CAPTURE_TEXT_AUTHORITY,
                "source": "no_store",
                "has_attachments": False,
            },
            {
                "authority": CAPTURE_TEXT_AUTHORITY,
                "source": CAPTURE_USER_SOURCE,
                "has_attachments": True,
            },
        )
        for excluded in excluded_inputs:
            with self.subTest(excluded=excluded):
                self.assertFalse(
                    capture_decision_for_owner(
                        str(OWNER_A), **excluded, environ=environ
                    ).enabled
                )

    def test_capture_text_is_nfc_and_policy_hash_is_server_derived(self) -> None:
        decomposed = "Synthetic cafe\u0301 preference"
        normalized = normalize_capture_text(decomposed)
        self.assertEqual(normalized, unicodedata.normalize("NFC", decomposed))
        self.assertEqual(
            capture_policy_sha256(CREATED_AT),
            EligibilityPolicy(ingest_after=CREATED_AT).policy_sha256,
        )
        self.assertEqual(
            capture_auth_context_sha256(
                owner_user_id=OWNER_A,
                authority="supabase_access_token_v1",
                request_id="request-a",
            ),
            capture_auth_context_sha256(
                owner_user_id=OWNER_A,
                authority="supabase_access_token_v1",
                request_id="request-a",
            ),
        )
        self.assertNotEqual(
            capture_auth_context_sha256(
                owner_user_id=OWNER_A,
                authority="supabase_access_token_v1",
                request_id="request-a",
            ),
            capture_auth_context_sha256(
                owner_user_id=OWNER_B,
                authority="supabase_access_token_v1",
                request_id="request-a",
            ),
        )

    def test_app_default_off_branch_retains_old_insert_and_gates_enqueue(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        route = source.split('@app.post("/log")', 1)[1].split(
            '@app.post("/threads/new")', 1
        )[0]
        self.assertIn("capture_decision_for_owner(", route)
        self.assertIn("authority=memory_actor_authority_v1(req)", route)
        self.assertIn("source=source", route)
        self.assertIn("has_attachments=bool(attachment_ids)", route)
        self.assertIn("if governed_memory_capture.enabled:", route)
        self.assertIn("capture_auth_context_sha256(", route)
        self.assertIn(
            '"SELECT set_config(\'app.auth_context_sha256\',$1,true)"',
            route,
        )
        self.assertIn(
            "created_dt = None if governed_memory_capture.enabled else datetime.utcnow()",
            route,
        )
        self.assertIn(
            '") VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)"',
            route,
        )
        enqueue_at = route.index("await enqueue_captured_chat_log_message(")
        attachment_binding_at = route.index("UPDATE public.chat_attachments")
        commit_at = route.index("await transaction.commit()", enqueue_at)
        identity_return_at = route.index('"note": "identity_card"')
        decision_at = route.index("capture_decision_for_owner(")
        attachment_replay_at = route.index('"replayed": True')
        self.assertLess(identity_return_at, decision_at)
        self.assertLess(attachment_replay_at, enqueue_at)
        self.assertLess(attachment_binding_at, enqueue_at)
        self.assertLess(enqueue_at, commit_at)
        self.assertIn(
            "SELECT id,message_id,status,deleted_at",
            route,
        )
        self.assertNotIn(
            "SELECT id,message_id,status,deleted_at,content",
            route,
        )
        before_log, after_log = source.split('@app.post("/log")', 1)
        after_log = after_log.split('@app.post("/threads/new")', 1)[1]
        self.assertNotIn("enqueue_captured_chat_log_message(", before_log)
        self.assertNotIn("enqueue_captured_chat_log_message(", after_log)


class CaptureAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_uses_only_message_id_and_server_policy_hash(self) -> None:
        decision = capture_decision_for_owner(
            str(OWNER_A),
            **ELIGIBLE_INPUT,
            environ={
                CAPTURE_MODE_ENV: "pilot",
                CAPTURE_OWNER_ALLOWLIST_ENV: str(OWNER_A),
            },
        )
        connection = _FakeConnection()
        receipt = await enqueue_captured_chat_log_message(
            connection,
            decision=decision,
            message_id=MESSAGE_A,
            source_created_at=CREATED_AT,
        )
        self.assertEqual(receipt.outbox_id, OUTBOX_A)
        self.assertEqual(receipt.policy_sha256, capture_policy_sha256(CREATED_AT))
        self.assertEqual(len(connection.calls), 1)
        query, args = connection.calls[0]
        self.assertEqual(
            query,
            "SELECT outcome,outbox_id FROM "
            "memory_ingest_private.enqueue_chat_log_message($1::uuid,$2::text)",
        )
        self.assertEqual(args, (MESSAGE_A, receipt.policy_sha256))

    async def test_disabled_owner_cannot_reach_enqueue_adapter(self) -> None:
        decision = capture_decision_for_owner(
            str(OWNER_A), **ELIGIBLE_INPUT, environ={}
        )
        connection = _FakeConnection()
        with self.assertRaisesRegex(
            ContractViolation, "governed_memory_capture_not_enabled"
        ):
            await enqueue_captured_chat_log_message(
                connection,
                decision=decision,
                message_id=MESSAGE_A,
                source_created_at=CREATED_AT,
            )
        self.assertEqual(connection.calls, [])
