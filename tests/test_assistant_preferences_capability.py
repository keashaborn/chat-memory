from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from seebx.adapters.assistant_preferences_postgres import (
    PostgresAssistantPreferencesRepository,
    PreferenceCandidateUnavailable,
    PreferencesBundle,
    PreferencesConflict,
)
from seebx.capabilities.preferences.assistant_compiler import (
    OpenAIPreferenceCompiler,
    _CompilerOutput,
    build_candidate,
)
from seebx.capabilities.preferences.assistant_contracts import (
    AssistantPreferencesPublic,
    AssistantPreferencesUpdate,
    CompiledPreferenceRule,
    ConversationStyle,
    PreferenceCompilationPublic,
    PreferenceRejectionReason,
    ResponseFormat,
    ResponseLength,
    TechnicalDepth,
    default_preferences,
)
from seebx.capabilities.preferences.assistant_routes import (
    create_assistant_preferences_router,
)


OWNER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_OWNER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def public_preferences(revision: int = 0) -> AssistantPreferencesPublic:
    return AssistantPreferencesPublic(
        revision=revision,
        updated_at=NOW if revision else None,
        assistant_name="Sage",
        nickname=None,
        occupation=None,
        more_about_you=None,
        custom_instructions=None,
        response_length=ResponseLength.BALANCED,
        technical_depth=TechnicalDepth.BALANCED,
        response_format=ResponseFormat.AUTO,
        conversation_style=ConversationStyle.NATURAL,
        compilation=PreferenceCompilationPublic(status="none"),
    )


class FakeBundle:
    def __init__(self, value: AssistantPreferencesPublic) -> None:
        self.value = value

    def public_value(self) -> AssistantPreferencesPublic:
        return self.value


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.current_revision = 0
        self.put_error: Exception | None = None
        self.approve_error: Exception | None = None

    async def get(self, owner: UUID) -> FakeBundle:
        self.calls.append(("get", owner))
        return FakeBundle(public_preferences(self.current_revision))

    async def put(
        self,
        owner: UUID,
        value: AssistantPreferencesUpdate,
    ) -> FakeBundle:
        self.calls.append(("put", owner, value))
        if self.put_error:
            raise self.put_error
        self.current_revision += 1
        return FakeBundle(public_preferences(self.current_revision))

    async def revision(self, owner: UUID) -> int:
        self.calls.append(("revision", owner))
        return self.current_revision

    async def store_candidate(self, candidate: object) -> None:
        self.calls.append(("store_candidate", candidate))

    async def approve(
        self,
        owner: UUID,
        candidate_id: UUID,
        expected_revision: int,
    ) -> FakeBundle:
        self.calls.append(("approve", owner, candidate_id, expected_revision))
        if self.approve_error:
            raise self.approve_error
        self.current_revision += 1
        return FakeBundle(public_preferences(self.current_revision))


class FakeCompiler:
    def compile(
        self,
        *,
        owner_user_id: UUID,
        source_revision: int,
        narrative: str,
    ):
        return build_candidate(
            owner_user_id=owner_user_id,
            source_revision=source_revision,
            source_narrative="",
            output=None,
            provider_model="deterministic-test",
            provider_response_id=None,
            now=NOW,
        )


async def verified_owner(request: Request, owner_user_id: str) -> str:
    del request
    return owner_user_id


class AssistantPreferenceContractTests(unittest.TestCase):
    def test_wire_values_are_normalized_and_unknown_fields_fail_closed(self) -> None:
        value = AssistantPreferencesUpdate.model_validate(
            {
                "expected_revision": 0,
                "assistant_name": "  Sage  ",
                "nickname": "  Eric   Lund ",
                "custom_instructions": " direct\r\n\r\n\r\n no filler ",
                "response_length": "concise",
                "technical_depth": "expert",
                "response_format": "steps",
                "conversation_style": "direct",
            }
        )
        self.assertEqual(value.assistant_name, "Sage")
        self.assertEqual(value.nickname, "Eric Lund")
        self.assertEqual(value.custom_instructions, "direct\n\nno filler")
        self.assertIs(value.response_length, ResponseLength.CONCISE)
        with self.assertRaises(ValidationError):
            AssistantPreferencesUpdate.model_validate(
                {"expected_revision": 0, "untrusted": "ignored"}
            )

    def test_clear_candidate_is_deterministic_and_reviewable(self) -> None:
        candidate = build_candidate(
            owner_user_id=OWNER,
            source_revision=4,
            source_narrative="   ",
            output=None,
            provider_model="deterministic",
            provider_response_id=None,
            now=NOW,
        )
        self.assertEqual(candidate.compilation_status.value, "clear")
        self.assertEqual(candidate.source_narrative_sha256, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        self.assertEqual(candidate.public_payload()["source_revision"], 4)
        self.assertNotIn("source_narrative", candidate.public_payload())

    def test_provider_call_is_structured_nonstored_and_pseudonymous(self) -> None:
        output = _CompilerOutput(
            response_length=ResponseLength.CONCISE,
            technical_depth=None,
            response_format=None,
            conversation_style=ConversationStyle.DIRECT,
            rule_ids=[CompiledPreferenceRule.NO_GENERIC_PRAISE],
            rejected_reason_codes=[
                PreferenceRejectionReason.CANNOT_CONTROL_TOOLS_OR_MEMORY
            ],
        )
        response = SimpleNamespace(
            output_parsed=output,
            model="gpt-test",
            id="response-test",
        )

        class Client:
            def __init__(self) -> None:
                self.options = None
                self.parameters = None
                self.responses = self

            def with_options(self, **options: object):
                self.options = options
                return self

            def parse(self, **parameters: object):
                self.parameters = parameters
                return response

        client = Client()
        candidate = OpenAIPreferenceCompiler(
            model="gpt-test",
            client=client,
        ).compile(
            owner_user_id=OWNER,
            source_revision=2,
            narrative="Be direct and avoid generic praise. Do not use memory.",
        )
        self.assertEqual(client.options, {"max_retries": 0, "timeout": 20.0})
        self.assertEqual(client.parameters["store"], False)
        self.assertIs(client.parameters["text_format"], _CompilerOutput)
        self.assertNotIn(str(OWNER), client.parameters["safety_identifier"])
        self.assertEqual(len(client.parameters["safety_identifier"]), 64)
        self.assertEqual(candidate.provider_response_id, "response-test")
        self.assertEqual(candidate.compilation_status.value, "partial")


class AssistantPreferenceRouteTests(unittest.TestCase):
    def client(
        self,
        repository: FakeRepository,
        *,
        identity=verified_owner,
    ) -> TestClient:
        app = FastAPI()
        app.include_router(
            create_assistant_preferences_router(
                repository=repository,
                compiler_factory=FakeCompiler,
                identity_verifier=identity,
            )
        )
        return TestClient(app)

    def test_exact_owner_scoped_surface_and_no_store_success(self) -> None:
        repository = FakeRepository()
        client = self.client(repository)
        actual = {
            (route.path, tuple(sorted(route.methods or ())))
            for route in client.app.routes
            if route.path.startswith("/assistant-preferences/")
        }
        self.assertEqual(
            actual,
            {
                ("/assistant-preferences/{owner_user_id}", ("GET",)),
                ("/assistant-preferences/{owner_user_id}", ("PUT",)),
                ("/assistant-preferences/{owner_user_id}/compile", ("POST",)),
                ("/assistant-preferences/{owner_user_id}/approve", ("POST",)),
            },
        )
        response = client.get(f"/assistant-preferences/{OWNER}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contract_version"], "assistant_response_preferences_v1")
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(repository.calls, [("get", OWNER)])

    def test_verified_actor_mismatch_fails_before_repository(self) -> None:
        repository = FakeRepository()

        async def wrong_owner(request: Request, owner_user_id: str) -> str:
            del request, owner_user_id
            return str(OTHER_OWNER)

        response = self.client(repository, identity=wrong_owner).get(
            f"/assistant-preferences/{OWNER}"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "supabase_actor_owner_mismatch")
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(repository.calls, [])

    def test_auth_failure_is_no_store_and_preserves_error_code(self) -> None:
        repository = FakeRepository()

        async def denied(request: Request, owner_user_id: str) -> str:
            del request, owner_user_id
            raise HTTPException(401, "invalid_supabase_access_token")

        response = self.client(repository, identity=denied).get(
            f"/assistant-preferences/{OWNER}"
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "invalid_supabase_access_token")
        self.assertIn("no-store", response.headers["cache-control"])

    def test_invalid_body_is_generic_and_does_not_echo_private_text(self) -> None:
        private = "private narrative that must not be reflected"
        response = self.client(FakeRepository()).put(
            f"/assistant-preferences/{OWNER}",
            json={"expected_revision": -1, "custom_instructions": private},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "invalid_preferences_request")
        self.assertNotIn(private, response.text)
        self.assertIn("no-store", response.headers["cache-control"])

    def test_revision_conflicts_are_explicit(self) -> None:
        repository = FakeRepository()
        repository.put_error = PreferencesConflict("synthetic")
        response = self.client(repository).put(
            f"/assistant-preferences/{OWNER}",
            json={"expected_revision": 0},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "preferences_revision_conflict")

        repository.current_revision = 3
        response = self.client(repository).post(
            f"/assistant-preferences/{OWNER}/compile",
            json={"expected_revision": 2, "narrative": "Be direct"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(any(call[0] == "store_candidate" for call in repository.calls))

    def test_compile_and_approve_are_separate_explicit_actions(self) -> None:
        repository = FakeRepository()
        client = self.client(repository)
        compiled = client.post(
            f"/assistant-preferences/{OWNER}/compile",
            json={"expected_revision": 0, "narrative": ""},
        )
        self.assertEqual(compiled.status_code, 200)
        self.assertEqual(compiled.json()["status"], "clear")
        candidate_id = compiled.json()["candidate_id"]
        self.assertTrue(any(call[0] == "store_candidate" for call in repository.calls))
        self.assertFalse(any(call[0] == "approve" for call in repository.calls))

        approved = client.post(
            f"/assistant-preferences/{OWNER}/approve",
            json={"expected_revision": 0, "candidate_id": candidate_id},
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["revision"], 1)

    def test_unavailable_candidate_is_a_bounded_conflict(self) -> None:
        repository = FakeRepository()
        repository.approve_error = PreferenceCandidateUnavailable("synthetic")
        response = self.client(repository).post(
            f"/assistant-preferences/{OWNER}/approve",
            json={
                "expected_revision": 0,
                "candidate_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"],
            "preference_compilation_candidate_unavailable",
        )


class Transaction:
    def __init__(self, options: dict[str, object]) -> None:
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object):
        return False


class ReadConnection:
    def __init__(self) -> None:
        self.transaction_options: dict[str, object] | None = None

    def transaction(self, **options: object) -> Transaction:
        self.transaction_options = options
        return Transaction(options)

    async def fetchrow(self, query: str, *args: object):
        self.last_query = query
        self.last_args = args
        return None


class ReadProvider:
    def __init__(self) -> None:
        self.connection = ReadConnection()
        self.owner: UUID | None = None

    @asynccontextmanager
    async def owner_connection(self, owner: UUID):
        self.owner = owner
        yield self.connection


class AssistantPreferenceRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_read_is_owner_scoped_and_read_only(self) -> None:
        provider = ReadProvider()
        bundle = await PostgresAssistantPreferencesRepository(  # type: ignore[arg-type]
            provider
        ).get(OWNER)
        self.assertEqual(provider.owner, OWNER)
        self.assertEqual(provider.connection.transaction_options, {"readonly": True})
        self.assertEqual(bundle.preferences, default_preferences(OWNER))
        self.assertEqual(bundle.public_value().compilation.status, "none")

    def test_source_and_sql_have_no_retired_memory_or_vector_dependency(self) -> None:
        paths = (
            ROOT / "seebx/capabilities/preferences/assistant_routes.py",
            ROOT / "seebx/adapters/assistant_preferences_postgres.py",
            ROOT / "ops/sql/20260820_assistant_preferences_capability_v1.sql",
        )
        source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        lowered = source.lower()
        self.assertNotIn("rag_engine", source)
        self.assertNotIn("qdrant", lowered)
        self.assertNotIn("memory_raw", source)
        self.assertIn("owner_connection", source)
        self.assertIn("FORCE ROW LEVEL SECURITY", source)
        self.assertIn("current_setting('app.user_id', true)", source)


if __name__ == "__main__":
    unittest.main()
