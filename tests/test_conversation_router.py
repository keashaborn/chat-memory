from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, Response
from pydantic import ValidationError
from starlette.requests import Request

from seebx.capabilities.conversation.memory_contracts import (
    MEMORY_MODE_ZEP,
    MemoryNotApplicableReason,
    MemoryResponseConfigurationError,
    build_memory_exposed_provenance_v1,
    build_memory_no_selection_provenance_v1,
    build_memory_not_applicable_provenance_v1,
)
from seebx.capabilities.conversation import router as response_router
from seebx.core.identity import ActorContext, TEXT_AUTHORITY
from seebx.adapters.postgres import PostgresConnectionProvider
from seebx.capabilities.conversation.router import (
    NO_STORE_HEADERS,
    ConversationResponseRequestV1,
    apply_no_store_headers,
    response_memory_provenance_for_mode,
    conversation_response_query,
    memory_not_applicable_reason,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_ACTOR = UUID("2240822d-ac9a-4096-95aa-e2b24d36ef50")
SESSION = UUID("3240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_SESSION = UUID("4240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("5240822d-ac9a-4096-95aa-e2b24d36ef50")
ROOT = Path(__file__).resolve().parents[1]


class ConversationRouterTests(unittest.TestCase):
    def test_normal_chat_generation_is_backend_owned(self) -> None:
        source = (ROOT / "seebx/capabilities/conversation/router.py").read_text()
        self.assertIn("generation_config = OpenAIChatGenerationConfigV1()", source)
        self.assertIn("generation_config=generation_config", source)
        self.assertNotIn("OPENAI_CHAT_MODEL", source)
        self.assertNotIn("normalize_chat_model", source)
        self.assertIn("CONVERSATION_SAFETY_CLASSIFIER_MODEL", source)
        self.assertNotIn("RESSE_CLASSIFIER_MODEL", source)
        self.assertNotIn("[resse_response]", source)

    def test_response_connection_lifetime_is_adapter_owned_and_injected(self) -> None:
        source = (ROOT / "seebx/capabilities/conversation/router.py").read_text()
        app_source = (ROOT / "app.py").read_text()
        self.assertNotIn("import asyncpg", source)
        self.assertNotIn("asyncpg.connect", source)
        self.assertNotIn("await conn.close()", source)
        self.assertIn("async with postgres.connection() as conn:", source)
        self.assertIn("create_conversation_response_router(", app_source)
        self.assertIn('connect_kwargs={"command_timeout": 90}', app_source)

    def test_attachment_sql_is_owned_by_postgres_adapter(self) -> None:
        source = (ROOT / "seebx/capabilities/conversation/router.py").read_text()
        adapter = (
            ROOT / "seebx/adapters/conversation_attachments.py"
        ).read_text()
        self.assertNotIn("public.chat_attachments", source)
        self.assertNotIn("set_config('app.user_id'", source)
        self.assertIn("fetch_ready_message_attachments(", source)
        self.assertIn("public.chat_attachments", adapter)
        self.assertIn("set_config('app.user_id'", adapter)

    def test_public_request_rejects_client_policy_controls(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationResponseRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "message": "Explain Fractal Monism.",
                    "response_mode": "FM_EXPLICIT",
                    "fm_level": "EXPLICIT",
                    "vantage_id": "RESSE",
                    "model": "gpt-5.1",
                }
            )

    def test_search_capability_is_header_derived_and_not_public_payload(self) -> None:
        source = (ROOT / "seebx/capabilities/conversation/router.py").read_text()
        self.assertIn("require_web_search_actor_v1(", source)
        self.assertIn("SearchCapabilityManifestV1.create(", source)
        self.assertIn("req.headers.get(VOICE_SEARCH_AUTHORIZATION_HEADER)", source)
        self.assertNotIn("payload.search_capability", source)
        with self.assertRaises(ValidationError):
            ConversationResponseRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "message": "Hello.",
                    "no_store": True,
                    "search_capability_manifest": {
                        "available_routes": ["unbounded_web"],
                    },
                }
            )

    def test_retired_response_preferences_are_absent_from_successor_route(self) -> None:
        source = (ROOT / "seebx/capabilities/conversation/router.py").read_text()
        self.assertNotIn("assistant_response_preferences", source)
        self.assertNotIn("SUCCESSOR_RESPONSE_DEFAULTS", source)
        self.assertNotIn("payload.assistant_name", source)
        with self.assertRaises(ValidationError):
            ConversationResponseRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "message": "Hello.",
                    "no_store": True,
                    "assistant_name": "Sage",
                }
            )

    def test_public_request_accepts_only_transport_fields(self) -> None:
        value = ConversationResponseRequestV1.model_validate_json(
            '{"user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",'
            '"message":"Hello.","thread_id":null,"no_store":true}'
        )
        self.assertIsNone(value.thread_id)
        self.assertEqual(
            value.model_dump(mode="json"),
            {
                "user_id": str(ACTOR),
                "message": "Hello.",
                "thread_id": None,
                "message_id": None,
                "no_store": True,
                "include_inspection": False,
                "attachment_ids": [],
                "attachment_message_id": None,
            },
        )

    def test_attachment_transport_fields_are_uuid_bound_and_unique(self) -> None:
        attachment_id = "1a8beae3-58e5-4fb7-8f64-4fbb2bddcb73"
        message_id = "05e79a7d-1e58-4cf4-95d6-b06b46f8898d"
        value = ConversationResponseRequestV1.model_validate(
            {
                "user_id": str(ACTOR),
                "message": "Review the attachment.",
                "thread_id": "735e1cf0-5a02-456c-a035-5597b010c7aa",
                "attachment_ids": [attachment_id],
                "attachment_message_id": message_id,
            }
        )
        self.assertEqual([str(item) for item in value.attachment_ids], [attachment_id])
        self.assertEqual(str(value.attachment_message_id), message_id)
        with self.assertRaises(ValidationError):
            ConversationResponseRequestV1.model_validate(
                {
                    "user_id": str(ACTOR),
                    "message": "Review it.",
                    "thread_id": "735e1cf0-5a02-456c-a035-5597b010c7aa",
                    "attachment_ids": [attachment_id, attachment_id],
                    "attachment_message_id": message_id,
                }
            )

    def test_public_request_keeps_non_uuid_transport_fields_strict(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationResponseRequestV1.model_validate_json(
                '{"user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",'
                '"message":"Hello.","no_store":"true"}'
            )

    def test_no_store_response_headers_are_explicit(self) -> None:
        response = Response()
        apply_no_store_headers(response)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(response.headers["expires"], "0")

    def test_router_selects_only_memory_provenance_contract(self) -> None:
        exposed = build_memory_exposed_provenance_v1(
            {
                "dispatch_state": "dispatched",
                "outcome": "exposed",
                "response_id": "90000000-0000-4000-8000-000000000001",
                "prompt_sha256": "a" * 64,
                "outbound_request_sha256": "b" * 64,
                "binding_sha256": "c" * 64,
                "selection_manifest_sha256": "d" * 64,
                "injection_manifest_sha256": "c" * 64,
                "selected_count": 1,
                "injected_count": 1,
                "model_exposed_count": 1,
                "injected_claim_ids": (
                    "ffffffff-ffff-4fff-8fff-ffffffffffff",
                ),
                "injected_revision_ids": (
                    "12345678-1234-4234-8234-123456789abc",
                ),
            }
        )
        memory = response_memory_provenance_for_mode(
            mode=MEMORY_MODE_ZEP,
            memory_provenance=exposed,
        )
        self.assertEqual(
            memory["contract_version"],
            "governed_memory_successor_answer_provenance_v1",
        )
        self.assertEqual(memory["binding_outcome"], "exposed")
        self.assertEqual(memory["binding_manifest_sha256"], "c" * 64)
        self.assertEqual(len(memory["provenance_sha256"]), 64)
        self.assertEqual(
            memory["references"][0]["claim_id"],
            "ffffffff-ffff-4fff-8fff-ffffffffffff",
        )

        for mode, memory_value in (
            ("legacy", exposed),
            (MEMORY_MODE_ZEP, None),
        ):
            with self.subTest(mode=mode):
                with self.assertRaises(MemoryResponseConfigurationError):
                    response_memory_provenance_for_mode(
                        mode=mode,
                        memory_provenance=memory_value,
                    )

    def test_router_no_selection_is_not_legacy_no_binding(self) -> None:
        no_selection = build_memory_no_selection_provenance_v1(
            answer_id=UUID("90000000-0000-4000-8000-000000000001"),
            prompt_sha256="a" * 64,
            outbound_request_bytes=b'{"messages":[]}',
        )
        result = response_memory_provenance_for_mode(
            mode=MEMORY_MODE_ZEP,
            memory_provenance=no_selection,
        )
        self.assertEqual(result["binding_outcome"], "no_memory_selected")
        self.assertNotEqual(result["binding_outcome"], "no_memory_binding")

    def test_excluded_successor_reasons_are_typed_and_mutually_distinct(self) -> None:
        cases = (
            (
                dict(
                    no_store=True,
                    has_attachments=False,
                    is_voice=False,
                    has_web_search=False,
                ),
                MemoryNotApplicableReason.NO_STORE,
            ),
            (
                dict(
                    no_store=False,
                    has_attachments=True,
                    is_voice=False,
                    has_web_search=False,
                ),
                MemoryNotApplicableReason.ATTACHMENT,
            ),
            (
                dict(
                    no_store=False,
                    has_attachments=False,
                    is_voice=True,
                    has_web_search=False,
                ),
                MemoryNotApplicableReason.VOICE,
            ),
            (
                dict(
                    no_store=False,
                    has_attachments=False,
                    is_voice=False,
                    has_web_search=True,
                ),
                MemoryNotApplicableReason.WEB_SEARCH,
            ),
        )
        observed = set()
        for flags, expected in cases:
            with self.subTest(reason=expected.value):
                reason = memory_not_applicable_reason(**flags)
                self.assertIs(reason, expected)
                observed.add(reason)
                provenance = build_memory_not_applicable_provenance_v1(
                    reason=expected,
                    answer_id=UUID(
                        "90000000-0000-4000-8000-000000000001"
                    ),
                    prompt_sha256="a" * 64,
                    outbound_request_bytes=b'{"messages":[]}',
                )
                result = response_memory_provenance_for_mode(
                    mode=MEMORY_MODE_ZEP,
                    memory_provenance=provenance,
                )
                self.assertEqual(result["binding_outcome"], "not_applicable")
                self.assertEqual(
                    result["not_applicable_reason"],
                    expected.value,
                )
                self.assertEqual(result["references"], [])
        self.assertEqual(
            observed,
            set(MemoryNotApplicableReason)
            - {MemoryNotApplicableReason.RUNTIME_UNAVAILABLE},
        )
        degraded = build_memory_not_applicable_provenance_v1(
            reason=MemoryNotApplicableReason.RUNTIME_UNAVAILABLE,
            answer_id=UUID("90000000-0000-4000-8000-000000000001"),
            prompt_sha256="a" * 64,
            outbound_request_bytes=b'{"messages":[]}',
        )
        degraded_result = response_memory_provenance_for_mode(
            mode=MEMORY_MODE_ZEP,
            memory_provenance=degraded,
        )
        self.assertEqual(degraded_result["binding_outcome"], "not_applicable")
        self.assertEqual(
            degraded_result["not_applicable_reason"],
            "runtime_unavailable",
        )
        self.assertIs(
            memory_not_applicable_reason(
                no_store=True,
                has_attachments=True,
                is_voice=True,
                has_web_search=True,
            ),
            MemoryNotApplicableReason.NO_STORE,
        )
        self.assertIsNone(
            memory_not_applicable_reason(
                no_store=False,
                has_attachments=False,
                is_voice=False,
                has_web_search=False,
            )
        )

    def test_zep_is_the_only_eligible_chat_memory_provider(self) -> None:
        source = (ROOT / "seebx/capabilities/conversation/router.py").read_text()
        self.assertIn("memory_provider = zep_memory_provider", source)
        self.assertIn("zep_prompt_memory_unavailable", source)
        for retired_symbol in (
            "SuccessorResponseRuntime",
            "SUCCESSOR_RESPONSE_PROVIDER_FACTORY",
            "CHAT_MEMORY_LIFECYCLE_RUNTIME",
            "coordinate_chat_memory_ingest_v1",
            "choose_response_memory_provider",
        ):
            self.assertNotIn(retired_symbol, source)

    def test_non_zep_routes_retain_not_applicable_provenance_lifecycle(self) -> None:
        source = (ROOT / "seebx/capabilities/conversation/router.py").read_text()
        self.assertIn(
            """            else:
                if exclusion_reason is None:
                    raise MemoryResponseConfigurationError(
                        "memory_response_exclusion_reason_missing"
                    )
                memory_provider = InactiveMemoryContextProviderV1(exclusion_reason)
                memory_lifecycle = memory_provider
""",
            source,
        )


def successor_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/query",
            "headers": [
                (b"authorization", b"Bearer a.b.c"),
                (b"x-vs-actor-user-id", str(ACTOR).encode("ascii")),
            ],
        }
    )


class ZepResponseRouterAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def payload(self, *, message_id: UUID | None = None) -> ConversationResponseRequestV1:
        return ConversationResponseRequestV1(
            user_id=ACTOR,
            message="Which interface theme do I prefer?",
            thread_id=THREAD,
            message_id=message_id,
        )

    def context(self) -> ActorContext:
        return ActorContext(
            owner_user_id=ACTOR,
            session_id=SESSION,
            authentication_manifest_sha256="a" * 64,
            authority=TEXT_AUTHORITY,
        )

    async def assert_supabase_owner_auth_reaches_response_runtime(
        self, payload: ConversationResponseRequestV1
    ) -> None:
        authenticate = AsyncMock(return_value=self.context())
        connect = AsyncMock(side_effect=RuntimeError("stop_after_auth"))
        request = successor_request()
        postgres = PostgresConnectionProvider(
            "synthetic-configured-dsn",
            connect_factory=connect,
            connect_kwargs={"command_timeout": 90},
        )
        with (
            patch.object(response_router, "DSN", "synthetic-configured-dsn"),
            patch.object(
                response_router,
                "require_actor_context",
                new=authenticate,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop_after_auth"):
                await conversation_response_query(
                    payload,
                    request,
                    Response(),
                    postgres=postgres,
                    assistant_preferences=AsyncMock(),
                )
        authenticate.assert_awaited_once_with(request, str(ACTOR))
        connect.assert_awaited_once_with(
            "synthetic-configured-dsn",
            command_timeout=90,
        )

    async def test_normal_text_uses_supabase_owner_auth_without_retired_authority(
        self,
    ) -> None:
        await self.assert_supabase_owner_auth_reaches_response_runtime(
            self.payload()
        )

    async def test_regeneration_uses_same_supabase_owner_auth_path(self) -> None:
        await self.assert_supabase_owner_auth_reaches_response_runtime(
            self.payload(
                message_id=UUID("6240822d-ac9a-4096-95aa-e2b24d36ef50")
            )
        )

    async def test_non_successor_runtime_mode_refuses_before_resources(self) -> None:
        authenticate = AsyncMock()
        connect = AsyncMock()
        postgres = PostgresConnectionProvider(
            "synthetic-configured-dsn",
            connect_factory=connect,
            connect_kwargs={"command_timeout": 90},
        )
        with (
            patch.object(response_router, "RESPONSE_MEMORY_MODE", "legacy"),
            patch.object(response_router, "DSN", "synthetic-configured-dsn"),
            patch.object(
                response_router,
                "require_actor_context",
                new=authenticate,
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await conversation_response_query(
                    self.payload(),
                    successor_request(),
                    Response(),
                    postgres=postgres,
                    assistant_preferences=AsyncMock(),
                )
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "response_memory_mode_invalid")
        authenticate.assert_not_awaited()
        connect.assert_not_awaited()

    def test_retired_live_authority_is_absent_from_zep_text_router(self) -> None:
        source = (ROOT / "seebx/capabilities/conversation/router.py").read_text()
        for retired in (
            "SUCCESSOR_LIVE_AUTHORITY_FACTORY",
            "successor_live_authority_from_environment",
            "SuccessorLiveAuthorityConfigurationError",
            "require_live_authority=",
            "live_authority_verifier=",
        ):
            self.assertNotIn(retired, source)
        self.assertIn("require_actor_context(", source)
        self.assertNotIn("memory_actor_auth_v1", source)

    def test_successor_import_graph_blocks_retired_response_modules(self) -> None:
        environment = dict(os.environ)
        environment["POSTGRES_DSN"] = (
            "postgresql://brains_app:synthetic@127.0.0.1:5432/memory"
        )
        blocked = (
            "rag_engine.governed_memory_provider_v1",
            "rag_engine.memory_prompt_renderer_v1",
            "rag_engine.memory_v1_selection_envelope",
            "rag_engine.assistant_response_preferences_v1",
            "rag_engine.assistant_response_preferences_store_v1",
            "rag_engine.memory_v1_answer_provenance_v1",
        )
        script = (
            "import builtins,sys\n"
            f"blocked={blocked!r}\n"
            "original=builtins.__import__\n"
            "def guarded(name,*args,**kwargs):\n"
            "    if name in blocked:\n"
            "        raise AssertionError('blocked import:'+name)\n"
            "    return original(name,*args,**kwargs)\n"
            "builtins.__import__=guarded\n"
            "import seebx.capabilities.conversation.router\n"
            "assert all(name not in sys.modules for name in blocked)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
