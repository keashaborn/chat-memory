from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import importlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
import unicodedata
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.conversation_capture import (
    CAPTURE_MODE_ENV,
    CAPTURE_OWNER_ALLOWLIST_ENV,
    CAPTURE_PILOT_ROLLING_24H_LIMIT,
    CAPTURE_TEXT_AUTHORITY,
    CAPTURE_USER_SOURCE,
    CaptureConfigurationError,
    CaptureSettings,
    capture_auth_context_sha256,
    capture_decision_for_owner,
    capture_policy_sha256,
    capture_settings_from_environment,
    enqueue_captured_chat_log_message,
    normalize_capture_text,
)
from rag_engine.governed_memory.eligibility import EligibilityPolicy
from rag_engine.governed_memory.exclusive_cutover import (
    EXCLUSIVE_MODE_ENV,
)


OWNER_A = UUID("11111111-1111-4111-8111-111111111111")
OWNER_B = UUID("22222222-2222-4222-8222-222222222222")
SESSION_A = UUID("33333333-3333-4333-8333-333333333333")
MESSAGE_A = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OUTBOX_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CREATED_AT = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]
ELIGIBLE_INPUT = {
    "authority": CAPTURE_TEXT_AUTHORITY,
    "source": CAPTURE_USER_SOURCE,
    "has_attachments": False,
    "is_voice_turn": False,
    "no_store": False,
    "has_search_authorization": False,
}
PILOT_MODE = {
    CAPTURE_MODE_ENV: "pilot",
    EXCLUSIVE_MODE_ENV: "successor_pilot",
}


class _FakeConnection:
    def __init__(
        self,
        *,
        outcome: str = "enqueued",
        outbox_id: UUID | None = OUTBOX_A,
    ) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.outcome = outcome
        self.outbox_id = outbox_id

    async def fetchrow(self, query: str, *args: object) -> Mapping[str, object]:
        self.calls.append((query, args))
        return {"outcome": self.outcome, "outbox_id": self.outbox_id}


class CaptureGateTests(unittest.TestCase):
    def test_capture_settings_are_startup_validated_and_immutable(self) -> None:
        self.assertEqual(CAPTURE_PILOT_ROLLING_24H_LIMIT, 20)
        off = capture_settings_from_environment({})
        self.assertEqual(
            off,
            CaptureSettings(mode="off", owner_user_ids=()),
        )
        pilot = capture_settings_from_environment(
            {
                **PILOT_MODE,
                CAPTURE_OWNER_ALLOWLIST_ENV: str(OWNER_A),
            }
        )
        self.assertEqual(pilot.mode, "pilot")
        self.assertEqual(pilot.owner_user_ids, (OWNER_A,))
        for invalid in (
            ("off", (OWNER_A,)),
            ("pilot", ()),
            ("pilot", (OWNER_A, OWNER_B)),
            ("unexpected", ()),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                CaptureConfigurationError
            ):
                CaptureSettings(
                    mode=invalid[0],
                    owner_user_ids=invalid[1],
                )

    def test_default_off_ignores_unrelated_allowlist_and_disables_owner(self) -> None:
        decision = capture_decision_for_owner(
            str(OWNER_A),
            authority="active_voice_session_lease_v1",
            source="frontend/chat:assistant",
            has_attachments=True,
            is_voice_turn=True,
            no_store=True,
            has_search_authorization=True,
            environ={CAPTURE_OWNER_ALLOWLIST_ENV: "not-a-uuid"},
        )
        self.assertEqual(decision.mode, "off")
        self.assertFalse(decision.enabled)

    def test_pilot_is_exact_owner_allowlist_only(self) -> None:
        environ = {
            **PILOT_MODE,
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

    def test_pilot_requires_the_exact_exclusive_successor_mode(self) -> None:
        for exclusive_mode in (None, "legacy", "SUCCESSOR_PILOT", " successor_pilot"):
            environ = {
                CAPTURE_MODE_ENV: "pilot",
                CAPTURE_OWNER_ALLOWLIST_ENV: str(OWNER_A),
            }
            if exclusive_mode is not None:
                environ[EXCLUSIVE_MODE_ENV] = exclusive_mode
            with self.subTest(exclusive_mode=exclusive_mode):
                with self.assertRaises(CaptureConfigurationError):
                    capture_decision_for_owner(
                        str(OWNER_A),
                        **ELIGIBLE_INPUT,
                        environ=environ,
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
                            **PILOT_MODE,
                            CAPTURE_OWNER_ALLOWLIST_ENV: allowlist,
                        },
                    )

    def test_pilot_excludes_voice_non_user_sources_and_attachments(self) -> None:
        environ = {
            **PILOT_MODE,
            CAPTURE_OWNER_ALLOWLIST_ENV: str(OWNER_A),
        }
        excluded_inputs = (
            {
                "authority": "active_voice_session_lease_v1",
                "source": CAPTURE_USER_SOURCE,
                "has_attachments": False,
                "is_voice_turn": True,
                "no_store": False,
                "has_search_authorization": False,
            },
            {
                "authority": CAPTURE_TEXT_AUTHORITY,
                "source": "frontend/chat:assistant",
                "has_attachments": False,
                "is_voice_turn": False,
                "no_store": False,
                "has_search_authorization": False,
            },
            {
                "authority": CAPTURE_TEXT_AUTHORITY,
                "source": "trusted-web",
                "has_attachments": False,
                "is_voice_turn": False,
                "no_store": False,
                "has_search_authorization": True,
            },
            {
                "authority": CAPTURE_TEXT_AUTHORITY,
                "source": "frontend/identity",
                "has_attachments": False,
                "is_voice_turn": False,
                "no_store": False,
                "has_search_authorization": False,
            },
            {
                "authority": CAPTURE_TEXT_AUTHORITY,
                "source": "no_store",
                "has_attachments": False,
                "is_voice_turn": False,
                "no_store": True,
                "has_search_authorization": False,
            },
            {
                "authority": CAPTURE_TEXT_AUTHORITY,
                "source": CAPTURE_USER_SOURCE,
                "has_attachments": True,
                "is_voice_turn": False,
                "no_store": False,
                "has_search_authorization": False,
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
                session_id=SESSION_A,
                authentication_manifest_sha256="a" * 64,
                authority="supabase_access_token_v1",
                request_id="request-a",
            ),
            capture_auth_context_sha256(
                owner_user_id=OWNER_A,
                session_id=SESSION_A,
                authentication_manifest_sha256="a" * 64,
                authority="supabase_access_token_v1",
                request_id="request-a",
            ),
        )
        self.assertNotEqual(
            capture_auth_context_sha256(
                owner_user_id=OWNER_A,
                session_id=SESSION_A,
                authentication_manifest_sha256="a" * 64,
                authority="supabase_access_token_v1",
                request_id="request-a",
            ),
            capture_auth_context_sha256(
                owner_user_id=OWNER_B,
                session_id=SESSION_A,
                authentication_manifest_sha256="a" * 64,
                authority="supabase_access_token_v1",
                request_id="request-a",
            ),
        )
        self.assertNotEqual(
            capture_auth_context_sha256(
                owner_user_id=OWNER_A,
                session_id=SESSION_A,
                authentication_manifest_sha256="a" * 64,
                authority="supabase_access_token_v1",
                request_id="request-a",
            ),
            capture_auth_context_sha256(
                owner_user_id=OWNER_A,
                session_id=OWNER_B,
                authentication_manifest_sha256="a" * 64,
                authority="supabase_access_token_v1",
                request_id="request-a",
            ),
        )
        self.assertNotEqual(
            capture_auth_context_sha256(
                owner_user_id=OWNER_A,
                session_id=SESSION_A,
                authentication_manifest_sha256="a" * 64,
                authority="supabase_access_token_v1",
                request_id="request-a",
            ),
            capture_auth_context_sha256(
                owner_user_id=OWNER_A,
                session_id=SESSION_A,
                authentication_manifest_sha256="b" * 64,
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
        self.assertIn("is_voice_turn=voice_turn_id is not None", route)
        self.assertIn("no_store=no_store", route)
        self.assertIn("has_search_authorization=bool(", route)
        self.assertIn("settings=GOVERNED_MEMORY_CAPTURE_SETTINGS", route)
        self.assertIn("if governed_memory_capture.enabled:", route)
        self.assertIn("require_memory_actor_context_v1(", route)
        self.assertIn("require_live_authority=True", route)
        self.assertIn("if no_store:", route)
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
        identity_return_at = route.index("legacy_identity_memory_retired")
        decision_at = route.index("capture_decision_for_owner(")
        attachment_replay_at = route.index('"replayed": True')
        self.assertLess(identity_return_at, decision_at)
        self.assertLess(attachment_replay_at, enqueue_at)
        self.assertLess(attachment_binding_at, enqueue_at)
        self.assertLess(enqueue_at, commit_at)
        self.assertIn(
            "memory_capture_outcome = capture_receipt.outcome",
            route,
        )
        self.assertIn(
            'response_payload["memory_capture_outcome"] = memory_capture_outcome',
            route,
        )
        self.assertNotIn("capture_receipt.outbox_id", route)
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
    @staticmethod
    def decision():
        return capture_decision_for_owner(
            str(OWNER_A),
            **ELIGIBLE_INPUT,
            environ={
                **PILOT_MODE,
                CAPTURE_OWNER_ALLOWLIST_ENV: str(OWNER_A),
            },
        )

    async def test_enqueue_uses_only_message_id_and_server_policy_hash(self) -> None:
        decision = self.decision()
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

    async def test_pilot_limit_is_typed_success_without_outbox_identifier(self) -> None:
        receipt = await enqueue_captured_chat_log_message(
            _FakeConnection(
                outcome="pilot_limit_reached",
                outbox_id=None,
            ),
            decision=self.decision(),
            message_id=MESSAGE_A,
            source_created_at=CREATED_AT,
        )
        self.assertEqual(receipt.outcome, "pilot_limit_reached")
        self.assertIsNone(receipt.outbox_id)

    async def test_capture_outcome_and_outbox_shape_must_match(self) -> None:
        invalid = (
            ("enqueued", None),
            ("replayed", None),
            ("pilot_limit_reached", OUTBOX_A),
            ("unexpected", OUTBOX_A),
        )
        for outcome, outbox_id in invalid:
            with self.subTest(outcome=outcome, outbox_id=outbox_id):
                with self.assertRaises(ContractViolation):
                    await enqueue_captured_chat_log_message(
                        _FakeConnection(
                            outcome=outcome,
                            outbox_id=outbox_id,
                        ),
                        decision=self.decision(),
                        message_id=MESSAGE_A,
                        source_created_at=CREATED_AT,
                    )

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


def _log_request(body: Mapping[str, object]) -> Request:
    from starlette.requests import Request

    encoded = json.dumps(body).encode("utf-8")
    delivered = False

    async def receive() -> Mapping[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {
            "type": "http.request",
            "body": encoded,
            "more_body": False,
        }

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/log",
            "headers": [(b"authorization", b"Bearer a.b.c")],
        },
        receive,
    )


def _live_authority(
    *,
    session_present: bool = True,
    missing_session: bool = False,
    unavailable: bool = False,
) -> LiveSupabaseAuthorityVerifier:
    from rag_engine.governed_memory.runtime.live_supabase import (
        LiveSupabaseAuthorityConfig,
        LiveSupabaseAuthorityVerifier,
        LiveSupabaseSessionVerifier,
        LiveSupabaseUserVerifier,
    )

    config = LiveSupabaseAuthorityConfig(
        issuer="https://synthetic.supabase.co/auth/v1",
        api_key="synthetic-publishable-key",
    )

    def fetch_user(*_args: object) -> bytes:
        if unavailable:
            raise OSError("synthetic live authority unavailable")
        return json.dumps({"id": str(OWNER_A)}).encode("utf-8")

    def fetch_session(*_args: object) -> bytes:
        if missing_session:
            return b"[]"
        return json.dumps(
            [
                {
                    "owner_user_id": str(OWNER_A),
                    "session_id": str(SESSION_A),
                    "session_present": session_present,
                }
            ]
        ).encode("utf-8")

    return LiveSupabaseAuthorityVerifier(
        user_verifier=LiveSupabaseUserVerifier(config, fetcher=fetch_user),
        session_verifier=LiveSupabaseSessionVerifier(
            config,
            fetcher=fetch_session,
        ),
    )


_APP_RUNTIME_AVAILABLE = all(
    importlib.util.find_spec(module) is not None
    for module in ("asyncpg", "fastapi", "jwt", "openai", "qdrant_client")
)


@unittest.skipUnless(
    _APP_RUNTIME_AVAILABLE,
    "full Brains runtime dependencies are unavailable",
)
class CaptureRouteLiveAuthorityTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "POSTGRES_DSN": (
                        "postgresql://brains_app:synthetic@127.0.0.1:5432/memory"
                    ),
                    EXCLUSIVE_MODE_ENV: "successor_pilot",
                    CAPTURE_MODE_ENV: "pilot",
                    CAPTURE_OWNER_ALLOWLIST_ENV: str(OWNER_A),
                },
                clear=False,
            ),
        ):
            cls.backend_app = importlib.import_module("app")

    def pilot_body(self) -> dict[str, object]:
        return {
            "user_id": str(OWNER_A),
            "thread_id": str(MESSAGE_A),
            "source": "frontend/chat:user",
            "text": "Synthetic durable fact.",
            "tags": ["user", "chat"],
        }

    async def assert_live_refusal(
        self,
        verifier: LiveSupabaseAuthorityVerifier,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        from seebx.adapters.supabase import VerifiedSupabaseIdentity

        identity = VerifiedSupabaseIdentity(
            actor_user_id=str(OWNER_A),
            session_id=str(SESSION_A),
            authentication_manifest_sha256="a" * 64,
        )
        connect = AsyncMock()
        pilot_settings = CaptureSettings(
            mode="pilot",
            owner_user_ids=(OWNER_A,),
        )
        with (
            patch.object(
                self.backend_app,
                "GOVERNED_MEMORY_CAPTURE_SETTINGS",
                pilot_settings,
            ),
            patch.object(
                self.backend_app,
                "require_memory_actor_v1",
                new=AsyncMock(return_value=str(OWNER_A)),
            ),
            patch.object(
                self.backend_app,
                "SUCCESSOR_LIVE_AUTHORITY_FACTORY",
                return_value=verifier,
            ),
            patch(
                "rag_engine.memory_actor_auth_v1.require_verified_supabase_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch.object(self.backend_app.asyncpg, "connect", connect),
        ):
            response = await self.backend_app.log_chat(
                _log_request(self.pilot_body())
            )
        self.assertEqual(response.status_code, status_code)
        self.assertEqual(json.loads(response.body)["detail"], detail)
        self.assertIn("no-store", response.headers["cache-control"])
        connect.assert_not_awaited()

    async def test_missing_and_revoked_sessions_stop_before_transcript_io(
        self,
    ) -> None:
        for verifier in (
            _live_authority(missing_session=True),
            _live_authority(session_present=False),
        ):
            with self.subTest(verifier=verifier):
                await self.assert_live_refusal(
                    verifier,
                    status_code=401,
                    detail="successor_live_authority_denied",
                )

    async def test_authority_outage_stops_before_transcript_io(self) -> None:
        await self.assert_live_refusal(
            _live_authority(unavailable=True),
            status_code=503,
            detail="successor_live_authority_unavailable",
        )

    async def test_unconfigured_authority_stops_before_transcript_io(self) -> None:
        connect = AsyncMock()
        pilot_settings = CaptureSettings(
            mode="pilot",
            owner_user_ids=(OWNER_A,),
        )
        with (
            patch.object(
                self.backend_app,
                "GOVERNED_MEMORY_CAPTURE_SETTINGS",
                pilot_settings,
            ),
            patch.object(
                self.backend_app,
                "require_memory_actor_v1",
                new=AsyncMock(return_value=str(OWNER_A)),
            ),
            patch.object(
                self.backend_app,
                "SUCCESSOR_LIVE_AUTHORITY_FACTORY",
                side_effect=self.backend_app.SuccessorLiveAuthorityConfigurationError,
            ),
            patch.object(self.backend_app.asyncpg, "connect", connect),
        ):
            response = await self.backend_app.log_chat(
                _log_request(self.pilot_body())
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            json.loads(response.body)["detail"],
            "successor_live_authority_unconfigured",
        )
        connect.assert_not_awaited()

    async def test_explicit_no_store_constructs_no_successor_or_database_io(
        self,
    ) -> None:
        factory = Mock()
        connect = AsyncMock()
        body = self.pilot_body()
        body["no_store"] = True
        with (
            patch.object(
                self.backend_app,
                "require_memory_actor_v1",
                new=AsyncMock(return_value=str(OWNER_A)),
            ),
            patch.object(
                self.backend_app,
                "SUCCESSOR_LIVE_AUTHORITY_FACTORY",
                factory,
            ),
            patch.object(self.backend_app.asyncpg, "connect", connect),
        ):
            response = await self.backend_app.log_chat(_log_request(body))
        self.assertEqual(response["status"], "no_store")
        factory.assert_not_called()
        connect.assert_not_awaited()

    async def test_retired_identity_refuses_before_auth_or_resource_io(self) -> None:
        authenticate = AsyncMock()
        connect = AsyncMock()
        factory = Mock()
        with (
            patch.object(
                self.backend_app,
                "require_memory_actor_v1",
                new=authenticate,
            ),
            patch.object(
                self.backend_app,
                "SUCCESSOR_LIVE_AUTHORITY_FACTORY",
                factory,
            ),
            patch.object(self.backend_app.asyncpg, "connect", connect),
        ):
            response = await self.backend_app.log_chat(
                _log_request(
                    {
                        "user_id": str(OWNER_A),
                        "source": "frontend/identity",
                        "text": "FULL_NAME: Synthetic Name",
                    }
                )
            )
        self.assertEqual(response.status_code, 410)
        self.assertEqual(
            json.loads(response.body)["detail"],
            "legacy_identity_memory_retired",
        )
        authenticate.assert_not_awaited()
        factory.assert_not_called()
        connect.assert_not_awaited()
