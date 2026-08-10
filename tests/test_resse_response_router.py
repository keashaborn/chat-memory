from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, Response
from pydantic import ValidationError
from starlette.requests import Request

from rag_engine.governed_memory.response_provider import (
    EXCLUSIVE_MODE_ENV,
    EXCLUSIVE_MODE_LEGACY,
    EXCLUSIVE_MODE_SUCCESSOR,
    SuccessorResponseConfigurationError,
)
from rag_engine.governed_memory.response_provenance import (
    SuccessorMemoryNotApplicableReason,
    build_successor_exposed_provenance_v1,
    build_successor_no_memory_selected_provenance_v1,
    build_successor_not_applicable_provenance_v1,
)
from rag_engine.governed_memory.runtime.live_supabase import (
    LiveSupabaseAuthorityConfig,
    LiveSupabaseAuthorityVerifier,
    LiveSupabaseSessionVerifier,
    LiveSupabaseUserVerifier,
)
from rag_engine.supabase_actor_auth import VerifiedSupabaseIdentity
from rag_engine.memory_v1_answer_provenance_v1 import (
    build_governed_memory_answer_provenance_v1,
)
from rag_engine import resse_response_router as response_router
from rag_engine.resse_response_router import (
    NO_STORE_HEADERS,
    ResseResponseRequestV1,
    apply_no_store_headers,
    response_memory_provenance_for_mode,
    resse_response_query,
    successor_not_applicable_reason,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_ACTOR = UUID("2240822d-ac9a-4096-95aa-e2b24d36ef50")
SESSION = UUID("3240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_SESSION = UUID("4240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("5240822d-ac9a-4096-95aa-e2b24d36ef50")
ROOT = Path(__file__).resolve().parents[1]


class ResseResponseRouterTests(unittest.TestCase):
    def test_normal_chat_generation_is_backend_owned(self) -> None:
        source = (ROOT / "rag_engine/resse_response_router.py").read_text()
        self.assertIn("generation_config = OpenAIChatGenerationConfigV1()", source)
        self.assertIn("generation_config=generation_config", source)
        self.assertNotIn("OPENAI_CHAT_MODEL", source)
        self.assertNotIn("normalize_chat_model", source)

    def test_public_request_rejects_client_policy_controls(self) -> None:
        with self.assertRaises(ValidationError):
            ResseResponseRequestV1.model_validate(
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
        source = (ROOT / "rag_engine/resse_response_router.py").read_text()
        self.assertIn("require_web_search_actor_v1(", source)
        self.assertIn("SearchCapabilityManifestV1.create(", source)
        self.assertIn("req.headers.get(VOICE_SEARCH_AUTHORIZATION_HEADER)", source)
        self.assertNotIn("payload.search_capability", source)
        with self.assertRaises(ValidationError):
            ResseResponseRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "message": "Hello.",
                    "no_store": True,
                    "search_capability_manifest": {
                        "available_routes": ["unbounded_web"],
                    },
                }
            )

    def test_response_preferences_are_mode_owned_and_not_public_payload(self) -> None:
        source = (ROOT / "rag_engine/resse_response_router.py").read_text()
        self.assertIn("await _legacy_assistant_response_preferences(", source)
        self.assertIn(
            "SUCCESSOR_RESPONSE_DEFAULTS.response_policy_overlay",
            source,
        )
        self.assertIn(
            "assistant_response_preferences=assistant_response_preferences",
            source,
        )
        self.assertNotIn("payload.assistant_name", source)
        with self.assertRaises(ValidationError):
            ResseResponseRequestV1.model_validate(
                {
                    "user_id": ACTOR,
                    "message": "Hello.",
                    "no_store": True,
                    "assistant_name": "Sage",
                }
            )

    def test_public_request_accepts_only_transport_fields(self) -> None:
        value = ResseResponseRequestV1.model_validate_json(
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
                "no_store": True,
                "include_inspection": False,
                "attachment_ids": [],
                "attachment_message_id": None,
            },
        )

    def test_attachment_transport_fields_are_uuid_bound_and_unique(self) -> None:
        attachment_id = "1a8beae3-58e5-4fb7-8f64-4fbb2bddcb73"
        message_id = "05e79a7d-1e58-4cf4-95d6-b06b46f8898d"
        value = ResseResponseRequestV1.model_validate(
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
            ResseResponseRequestV1.model_validate(
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
            ResseResponseRequestV1.model_validate_json(
                '{"user_id":"1240822d-ac9a-4096-95aa-e2b24d36ef50",'
                '"message":"Hello.","no_store":"true"}'
            )

    def test_no_store_response_headers_are_explicit(self) -> None:
        response = Response()
        apply_no_store_headers(response)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(response.headers["expires"], "0")

    def test_router_selects_exactly_one_mode_owned_provenance_contract(self) -> None:
        expected_legacy = build_governed_memory_answer_provenance_v1(
            None
        ).model_dump(mode="json")
        legacy = response_memory_provenance_for_mode(
            mode=EXCLUSIVE_MODE_LEGACY,
            legacy_binding=None,
            successor_provenance=None,
        )
        self.assertEqual(legacy, expected_legacy)
        self.assertEqual(legacy["binding_outcome"], "no_memory_binding")

        exposed = build_successor_exposed_provenance_v1(
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
        successor = response_memory_provenance_for_mode(
            mode=EXCLUSIVE_MODE_SUCCESSOR,
            legacy_binding=None,
            successor_provenance=exposed,
        )
        self.assertEqual(
            successor["contract_version"],
            "governed_memory_successor_answer_provenance_v1",
        )
        self.assertEqual(successor["binding_outcome"], "exposed")
        self.assertEqual(successor["binding_manifest_sha256"], "c" * 64)
        self.assertEqual(len(successor["provenance_sha256"]), 64)
        self.assertEqual(
            successor["references"][0]["claim_id"],
            "ffffffff-ffff-4fff-8fff-ffffffffffff",
        )

        for mode, successor_value in (
            (EXCLUSIVE_MODE_LEGACY, exposed),
            (EXCLUSIVE_MODE_SUCCESSOR, None),
        ):
            with self.subTest(mode=mode):
                with self.assertRaises(SuccessorResponseConfigurationError):
                    response_memory_provenance_for_mode(
                        mode=mode,
                        legacy_binding=None,
                        successor_provenance=successor_value,
                    )
        with self.assertRaises(SuccessorResponseConfigurationError):
            response_memory_provenance_for_mode(
                mode=EXCLUSIVE_MODE_SUCCESSOR,
                legacy_binding=object(),
                successor_provenance=exposed,
            )

    def test_router_successor_no_selection_is_not_legacy_no_binding(self) -> None:
        no_selection = build_successor_no_memory_selected_provenance_v1(
            answer_id=UUID("90000000-0000-4000-8000-000000000001"),
            prompt_sha256="a" * 64,
            outbound_request_bytes=b'{"messages":[]}',
        )
        result = response_memory_provenance_for_mode(
            mode=EXCLUSIVE_MODE_SUCCESSOR,
            legacy_binding=None,
            successor_provenance=no_selection,
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
                SuccessorMemoryNotApplicableReason.NO_STORE,
            ),
            (
                dict(
                    no_store=False,
                    has_attachments=True,
                    is_voice=False,
                    has_web_search=False,
                ),
                SuccessorMemoryNotApplicableReason.ATTACHMENT,
            ),
            (
                dict(
                    no_store=False,
                    has_attachments=False,
                    is_voice=True,
                    has_web_search=False,
                ),
                SuccessorMemoryNotApplicableReason.VOICE,
            ),
            (
                dict(
                    no_store=False,
                    has_attachments=False,
                    is_voice=False,
                    has_web_search=True,
                ),
                SuccessorMemoryNotApplicableReason.WEB_SEARCH,
            ),
        )
        observed = set()
        for flags, expected in cases:
            with self.subTest(reason=expected.value):
                reason = successor_not_applicable_reason(**flags)
                self.assertIs(reason, expected)
                observed.add(reason)
                provenance = build_successor_not_applicable_provenance_v1(
                    reason=expected,
                    answer_id=UUID(
                        "90000000-0000-4000-8000-000000000001"
                    ),
                    prompt_sha256="a" * 64,
                    outbound_request_bytes=b'{"messages":[]}',
                )
                result = response_memory_provenance_for_mode(
                    mode=EXCLUSIVE_MODE_SUCCESSOR,
                    legacy_binding=None,
                    successor_provenance=provenance,
                )
                self.assertEqual(result["binding_outcome"], "not_applicable")
                self.assertEqual(
                    result["not_applicable_reason"],
                    expected.value,
                )
                self.assertEqual(result["references"], [])
        self.assertEqual(observed, set(SuccessorMemoryNotApplicableReason))
        self.assertIs(
            successor_not_applicable_reason(
                no_store=True,
                has_attachments=True,
                is_voice=True,
                has_web_search=True,
            ),
            SuccessorMemoryNotApplicableReason.NO_STORE,
        )
        self.assertIsNone(
            successor_not_applicable_reason(
                no_store=False,
                has_attachments=False,
                is_voice=False,
                has_web_search=False,
            )
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


def live_verifier(
    *,
    user_id: UUID = ACTOR,
    session_id: UUID = SESSION,
    session_present: bool = True,
    missing_session: bool = False,
    unavailable: bool = False,
) -> LiveSupabaseAuthorityVerifier:
    config = LiveSupabaseAuthorityConfig(
        issuer="https://synthetic.supabase.co/auth/v1",
        api_key="synthetic-publishable-key",
    )

    def fetch_user(*_args: object) -> bytes:
        if unavailable:
            raise OSError("synthetic authority unavailable")
        return json.dumps({"id": str(user_id)}).encode("utf-8")

    def fetch_session(*_args: object) -> bytes:
        if missing_session:
            return b"[]"
        return json.dumps(
            [
                {
                    "owner_user_id": str(ACTOR),
                    "session_id": str(session_id),
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


class SuccessorResponseRouterAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def payload(self) -> ResseResponseRequestV1:
        return ResseResponseRequestV1(
            user_id=ACTOR,
            message="Which interface theme do I prefer?",
            thread_id=THREAD,
        )

    async def assert_live_refusal(
        self,
        verifier: LiveSupabaseAuthorityVerifier,
        *,
        expected_status: int,
        expected_detail: str,
    ) -> None:
        identity = VerifiedSupabaseIdentity(
            actor_user_id=str(ACTOR),
            session_id=str(SESSION),
            authentication_manifest_sha256="a" * 64,
        )
        connect = AsyncMock()
        with (
            patch.dict(
                os.environ,
                {EXCLUSIVE_MODE_ENV: "successor_pilot"},
                clear=False,
            ),
            patch.object(
                response_router,
                "RESPONSE_MEMORY_MODE",
                EXCLUSIVE_MODE_SUCCESSOR,
            ),
            patch.object(response_router, "DSN", "synthetic-configured-dsn"),
            patch.object(
                response_router,
                "SUCCESSOR_LIVE_AUTHORITY_FACTORY",
                return_value=verifier,
            ),
            patch(
                "rag_engine.memory_actor_auth_v1.require_verified_supabase_identity",
                new=AsyncMock(return_value=identity),
            ),
            patch.object(response_router.asyncpg, "connect", connect),
        ):
            with self.assertRaises(HTTPException) as raised:
                await resse_response_query(
                    self.payload(),
                    successor_request(),
                    Response(),
                )
        self.assertEqual(raised.exception.status_code, expected_status)
        self.assertEqual(raised.exception.detail, expected_detail)
        self.assertEqual(raised.exception.headers, NO_STORE_HEADERS)
        connect.assert_not_awaited()

    async def test_missing_and_signed_out_sessions_fail_closed_before_database(
        self,
    ) -> None:
        for verifier in (
            live_verifier(missing_session=True),
            live_verifier(session_present=False),
        ):
            with self.subTest(verifier=verifier):
                await self.assert_live_refusal(
                    verifier,
                    expected_status=401,
                    expected_detail="successor_live_authority_denied",
                )

    async def test_live_user_and_session_mismatch_fail_closed(self) -> None:
        for verifier in (
            live_verifier(user_id=OTHER_ACTOR),
            live_verifier(session_id=OTHER_SESSION),
        ):
            with self.subTest(verifier=verifier):
                await self.assert_live_refusal(
                    verifier,
                    expected_status=401,
                    expected_detail="successor_live_authority_denied",
                )

    async def test_live_authority_unavailable_is_no_store_503(self) -> None:
        await self.assert_live_refusal(
            live_verifier(unavailable=True),
            expected_status=503,
            expected_detail="successor_live_authority_unavailable",
        )

    def test_invalid_startup_mode_fails_import_before_runtime_io(self) -> None:
        environment = dict(os.environ)
        environment[EXCLUSIVE_MODE_ENV] = " successor_pilot"
        script = (
            "import rag_engine.resse_response_router\n"
            "raise AssertionError('invalid mode import unexpectedly succeeded')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "invalid_governed_memory_exclusive_mode",
            result.stderr,
        )

    async def test_default_legacy_mode_never_constructs_successor_resources(
        self,
    ) -> None:
        legacy_stop = RuntimeError("stop after legacy authentication")
        environment = dict(os.environ)
        environment[EXCLUSIVE_MODE_ENV] = EXCLUSIVE_MODE_SUCCESSOR
        successor_factory = Mock()
        response_runtime_provider = Mock()
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                response_router,
                "RESPONSE_MEMORY_MODE",
                EXCLUSIVE_MODE_LEGACY,
            ),
            patch.object(response_router, "DSN", "synthetic-configured-dsn"),
            patch.object(
                response_router,
                "SUCCESSOR_LIVE_AUTHORITY_FACTORY",
                successor_factory,
            ),
            patch.object(
                response_router.SUCCESSOR_RESPONSE_RUNTIME,
                "provider",
                response_runtime_provider,
            ),
            patch.object(
                response_router,
                "require_memory_actor_v1",
                new=AsyncMock(side_effect=legacy_stop),
            ) as legacy_auth,
        ):
            with self.assertRaisesRegex(RuntimeError, "legacy authentication"):
                await resse_response_query(
                    self.payload(),
                    successor_request(),
                    Response(),
                )
        legacy_auth.assert_awaited_once()
        successor_factory.assert_not_called()
        response_runtime_provider.assert_not_called()

    def test_successor_import_graph_blocks_retired_response_modules(self) -> None:
        environment = dict(os.environ)
        environment[EXCLUSIVE_MODE_ENV] = EXCLUSIVE_MODE_SUCCESSOR
        environment["POSTGRES_DSN"] = (
            "postgresql://synthetic:synthetic@127.0.0.1/synthetic"
        )
        blocked = (
            "rag_engine.governed_memory_provider_v1",
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
            "import app\n"
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
