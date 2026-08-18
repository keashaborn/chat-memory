from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from seebx.capabilities.conversation.composition import (
    AuthenticatedResponseCommandV0_2,
    GovernedMemoryAssemblyV1,
    ConversationResponseComposer,
    ResponseCompositionError,
)
from rag_engine.response_conversation_snapshot_v1 import USER_SOURCE
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2
from rag_engine.search_capability_manifest_v1 import (
    VOICE_SEARCH_AUTHORIZATION_BASIS,
    SearchCapabilityManifestV1,
)
from seebx.contracts.conversation import (
    WEB_ASSISTANT_SOURCE,
    WEB_USER_SOURCE,
)
from tests.test_openai_chat_provider_v1 import provider_response
from tests.test_openai_moderation_adapter_v0_2 import categories, response as moderation_response


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
ANSWER = UUID("90000000-0000-4000-8000-000000000001")
CORRELATION = UUID("90000000-0000-4000-8000-000000000002")
CURRENT_LOG = UUID("90000000-0000-4000-8000-000000000099")
WEB_USER_LOG = UUID("90000000-0000-4000-8000-000000000010")
WEB_RESPONSE = UUID("90000000-0000-4000-8000-000000000011")
WEB_SEARCH = UUID("80000000-0000-4000-8000-000000000011")
NOW = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)
PRIOR_QUERY = "What happened with OpenAI today?"
PRIOR_ANSWER = "OpenAI published a current update."


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class SnapshotConn:
    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []

    def transaction(self, **kwargs: Any) -> FakeTransaction:
        self.transactions.append(kwargs)
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        return "SELECT 1"

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "current_user" in query:
            return "brains_app"
        if "transaction_read_only" in query:
            return "on"
        if "SELECT EXISTS" in query:
            return True
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if "FROM public.chat_log" in query:
            return []
        raise AssertionError(f"unexpected fetch: {query}")


class BoundProvenanceConn(SnapshotConn):
    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if "LEFT JOIN trusted_web.response_transcript_v1" in query:
            return [
                {
                    "id": WEB_RESPONSE,
                    "owner_user_id": ACTOR,
                    "thread_id": THREAD,
                    "source": WEB_ASSISTANT_SOURCE,
                    "text": PRIOR_ANSWER,
                    "request_id": "prior-web-request",
                    "created_at": NOW - timedelta(minutes=1),
                    "assistant_text_sha256": None,
                    "attestation_sha256": None,
                    "web_response_id": WEB_RESPONSE,
                    "web_query_sha256": hashlib.sha256(
                        PRIOR_QUERY.encode("utf-8")
                    ).hexdigest(),
                    "web_answer_sha256": hashlib.sha256(
                        PRIOR_ANSWER.encode("utf-8")
                    ).hexdigest(),
                },
                {
                    "id": WEB_USER_LOG,
                    "owner_user_id": ACTOR,
                    "thread_id": THREAD,
                    "source": WEB_USER_SOURCE,
                    "text": PRIOR_QUERY,
                    "request_id": "prior-web-request",
                    "created_at": NOW - timedelta(minutes=2),
                    "assistant_text_sha256": None,
                    "attestation_sha256": None,
                    "web_response_id": WEB_RESPONSE,
                    "web_query_sha256": hashlib.sha256(
                        PRIOR_QUERY.encode("utf-8")
                    ).hexdigest(),
                    "web_answer_sha256": hashlib.sha256(
                        PRIOR_ANSWER.encode("utf-8")
                    ).hexdigest(),
                },
            ]
        if "JOIN trusted_web.response_transcript_v1 AS web" in query:
            return [
                {
                    "log_id": WEB_RESPONSE,
                    "owner_user_id": ACTOR,
                    "thread_id": THREAD,
                    "assistant_text": PRIOR_ANSWER,
                    "created_at": NOW - timedelta(minutes=1),
                    "response_id": WEB_RESPONSE,
                    "assistant_chat_log_id": WEB_RESPONSE,
                    "search_id": WEB_SEARCH,
                    "route": "current_news",
                    "policy_version": "search_decision_v1_2",
                    "decision": "live",
                    "answer_sha256": hashlib.sha256(
                        PRIOR_ANSWER.encode("utf-8")
                    ).hexdigest(),
                    "cited_sources": [
                        {"url": "https://openai.com/news/"}
                    ],
                    "admitted_sources": [
                        {"url": "https://openai.com/news/"}
                    ],
                }
            ]
        if "FROM public.chat_log" in query:
            return [
                {
                    "id": CURRENT_LOG,
                    "owner_user_id": ACTOR,
                    "thread_id": THREAD,
                    "source": USER_SOURCE,
                    "request_id": "composition-request",
                    "text": "What sources did you use for your last answer?",
                    "created_at": NOW,
                }
            ]
        raise AssertionError(f"unexpected fetch: {query}")


def classifier_output(**updates: Any) -> dict[str, Any]:
    value = {
        "domain_risk_gate": "pass",
        "categories": [],
        "safety_action_required": False,
        "fm_application_gate": "pass",
        "technical": False,
        "fm_explicit": False,
        "coaching": False,
        "ordinary_fm_relevant": False,
        "user_fm_opt_out": False,
        "technical_procedure_requested": False,
        "coaching_consent": False,
        "material_clarification_required": False,
        "explicit_next_step_requested": False,
    }
    value.update(updates)
    return value


class FakeResponses:
    def __init__(self, client: "CombinedOpenAIClient", parsed: Any) -> None:
        self.client = client
        self.parsed = parsed

    def parse(self, **kwargs: Any) -> dict[str, Any]:
        self.client.calls.append(("classifier", kwargs))
        return {
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "id": "resp_classifier_001",
            "model": "gpt-5.1",
            "output_parsed": self.parsed,
        }


class FakeModerations:
    def __init__(self, client: "CombinedOpenAIClient") -> None:
        self.client = client

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.client.calls.append(("moderation", kwargs))
        return moderation_response(flagged=False, category_values=categories())


class FakeCompletions:
    def __init__(self, client: "CombinedOpenAIClient") -> None:
        self.client = client

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.client.calls.append(("chat", kwargs))
        return provider_response(content="A bounded test response.")


class CombinedOpenAIClient:
    def __init__(self, parsed: Any | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.options: list[dict[str, Any]] = []
        self.responses = FakeResponses(self, parsed or classifier_output())
        self.moderations = FakeModerations(self)
        self.chat = SimpleNamespace(completions=FakeCompletions(self))

    def with_options(self, **kwargs: Any) -> "CombinedOpenAIClient":
        self.options.append(kwargs)
        return self


class CapturingMemoryProvider:
    def __init__(self) -> None:
        self.signals: list[ResponsePolicySignalsV0_2] = []

    def prepare(
        self,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: Any,
        trusted_policy_signals: ResponsePolicySignalsV0_2,
    ) -> GovernedMemoryAssemblyV1:
        self.signals.append(trusted_policy_signals)
        return GovernedMemoryAssemblyV1()


class FailingMemoryProvider:
    def prepare(self, **kwargs: Any) -> GovernedMemoryAssemblyV1:
        del kwargs
        raise RuntimeError("private provider detail")


def command(message: str) -> AuthenticatedResponseCommandV0_2:
    return AuthenticatedResponseCommandV0_2(
        authenticated_actor_user_id=ACTOR,
        thread_id=THREAD,
        request_id="composition-request",
        current_message=message,
        request_field_names=("mix", "vantage_id"),
    )


class ConversationCompositionTests(unittest.IsolatedAsyncioTestCase):
    def test_authenticated_command_rejects_retired_preferences_field(self) -> None:
        payload = command("Hello").model_dump(mode="json")
        payload["assistant_response_preferences"] = None
        with self.assertRaises(ValidationError):
            AuthenticatedResponseCommandV0_2.model_validate(payload)

    async def test_source_followup_uses_prior_provenance_without_new_search(self) -> None:
        client = CombinedOpenAIClient()
        conn = BoundProvenanceConn()
        root = ConversationResponseComposer(
            openai_client=client,
            classifier_model="gpt-5.1",
            answer_id_factory=lambda: ANSWER,
            correlation_id_factory=lambda: CORRELATION,
        )

        execution = await root.execute_detailed(
            conn,
            command("What sources did you use for your last answer?"),
        )

        self.assertEqual(
            [name for name, _ in client.calls],
            ["classifier", "moderation", "chat"],
        )
        messages = client.calls[-1][1]["messages"]
        provenance = [
            item
            for item in messages
            if item.get("name") == "prior_web_provenance_v1"
        ]
        self.assertEqual(len(provenance), 1)
        self.assertEqual(
            execution.trusted_plan.assembled_prompt.manifest.context_block_count,
            1,
        )
        self.assertEqual(
            conn.transactions,
            [
                {"isolation": "repeatable_read", "readonly": True},
                {"isolation": "repeatable_read", "readonly": True},
            ],
        )

    async def test_failure_reports_only_the_composition_stage(self) -> None:
        root = ConversationResponseComposer(
            openai_client=CombinedOpenAIClient(),
            classifier_model="gpt-5.1",
            memory_provider=FailingMemoryProvider(),
        )

        with self.assertRaises(ResponseCompositionError) as raised:
            await root.execute(SnapshotConn(), command("Who is my dad?"))

        self.assertEqual(raised.exception.stage, "memory_selection")
        self.assertNotIn("private provider detail", str(raised.exception))

    async def test_trusted_response_signals_reach_memory_provider(self) -> None:
        for signal_values in (
            {"technical": True},
            {"fm_explicit": True},
        ):
            with self.subTest(signal_values=signal_values):
                client = CombinedOpenAIClient(classifier_output(**signal_values))
                memory_provider = CapturingMemoryProvider()
                root = ConversationResponseComposer(
                    openai_client=client,
                    classifier_model="gpt-5.1",
                    memory_provider=memory_provider,
                    answer_id_factory=lambda: ANSWER,
                    correlation_id_factory=lambda: CORRELATION,
                )

                await root.execute(
                    SnapshotConn(),
                    command("Explain this request using the trusted server policy."),
                )

                self.assertEqual(len(memory_provider.signals), 1)
                for key, expected in signal_values.items():
                    self.assertIs(getattr(memory_provider.signals[0], key), expected)

    async def test_exact_authority_order_reaches_final_attestation(self) -> None:
        client = CombinedOpenAIClient()
        conn = SnapshotConn()
        root = ConversationResponseComposer(
            openai_client=client,
            classifier_model="gpt-5.1",
            answer_id_factory=lambda: ANSWER,
            correlation_id_factory=lambda: CORRELATION,
        )

        finalized = await root.execute(conn, command("What time is it?"))

        self.assertEqual(finalized.answer_id, ANSWER)
        self.assertEqual(finalized.assistant_text, "A bounded test response.")
        self.assertEqual(
            [name for name, _ in client.calls],
            ["classifier", "moderation", "chat"],
        )
        chat_kwargs = client.calls[-1][1]
        self.assertFalse(chat_kwargs["store"])
        self.assertTrue(chat_kwargs["safety_identifier"].startswith("vs1_"))
        self.assertNotIn(str(ACTOR), str(chat_kwargs))
        self.assertEqual(
            conn.transactions,
            [{"isolation": "repeatable_read", "readonly": True}],
        )

    async def test_capability_manifest_reaches_provider_system_prompt(self) -> None:
        client = CombinedOpenAIClient()
        root = ConversationResponseComposer(
            openai_client=client,
            classifier_model="gpt-5.1",
            answer_id_factory=lambda: ANSWER,
            correlation_id_factory=lambda: CORRELATION,
        )
        capability = SearchCapabilityManifestV1.create(
            authorization_basis=VOICE_SEARCH_AUTHORIZATION_BASIS,
        )
        governed_command = command("Can this system research current news?").model_copy(
            update={"search_capability_manifest": capability}
        )

        execution = await root.execute_detailed(SnapshotConn(), governed_command)

        chat_kwargs = client.calls[-1][1]
        system_text = chat_kwargs["messages"][0]["content"]
        self.assertIn("Server-mediated research is available", system_text)
        self.assertEqual(
            execution.trusted_plan.assembled_prompt.manifest.search_capability_manifest_sha256,
            capability.manifest_sha256,
        )

    async def test_detailed_execution_is_bound_to_public_finalization(self) -> None:
        client = CombinedOpenAIClient()
        root = ConversationResponseComposer(
            openai_client=client,
            classifier_model="gpt-5.1",
            answer_id_factory=lambda: ANSWER,
            correlation_id_factory=lambda: CORRELATION,
        )

        execution = await root.execute_detailed(
            SnapshotConn(), command("What time is it?")
        )

        self.assertEqual(execution.finalized.answer_id, ANSWER)
        self.assertEqual(
            execution.finalized.attestation.trusted_plan_sha256,
            execution.trusted_plan.plan_sha256,
        )
        self.assertEqual(
            execution.finalized.attestation.provider_response_sha256,
            execution.provider_response.response_sha256,
        )
        timings = execution.stage_timings.model_dump()
        self.assertEqual(
            set(timings),
            {
                "command_validation_ms",
                "conversation_snapshot_ms",
                "policy_input_ms",
                "signal_classification_ms",
                "signal_binding_ms",
                "memory_selection_ms",
                "trusted_request_ms",
                "orchestration_ms",
                "answer_generation_ms",
                "finalization_ms",
                "pipeline_total_ms",
            },
        )
        self.assertTrue(
            all(isinstance(value, int) and value >= 0 for value in timings.values())
        )
        self.assertGreaterEqual(
            timings["pipeline_total_ms"],
            max(value for key, value in timings.items() if key != "pipeline_total_ms"),
        )

    async def test_local_domain_danger_skips_classifier_call_and_suppresses_fm(self) -> None:
        client = CombinedOpenAIClient(classifier_output(fm_explicit=True))
        root = ConversationResponseComposer(
            openai_client=client,
            classifier_model="gpt-5.1",
            answer_id_factory=lambda: ANSWER,
            correlation_id_factory=lambda: CORRELATION,
        )

        await root.execute(
            SnapshotConn(),
            command(
                "Explain Relational Monism, but I have crushing chest pain and cannot breathe."
            ),
        )

        self.assertEqual([name for name, _ in client.calls], ["moderation", "chat"])
        chat_kwargs = client.calls[-1][1]
        system_text = chat_kwargs["messages"][0]["content"]
        self.assertIn("Use conventional, concrete, domain-appropriate safeguards", system_text)
        self.assertIn("Effective Relational Monism level: OFF", system_text)
        self.assertFalse(
            any(
                message.get("name") == "fractal_monism_v0_2"
                for message in chat_kwargs["messages"]
            )
        )

    def test_command_cannot_carry_hidden_policy_or_trusted_artifacts(self) -> None:
        with self.assertRaises(ValidationError):
            AuthenticatedResponseCommandV0_2.model_validate(
                {
                    **command("Hello").model_dump(),
                    "response_mode": "FM_EXPLICIT",
                    "trusted_plan": {},
                    "lens_fm": 1.0,
                }
            )

    def test_canonical_composition_paths_have_no_legacy_wrappers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        canonical = (
            root / "seebx/capabilities/conversation/composition.py",
            root / "seebx/capabilities/conversation/lifeswitch_composition.py",
        )
        legacy = (
            root / "rag_engine/response_composition_root_v0_2.py",
            root / "rag_engine/response_composition_root_v0_4.py",
        )
        for candidate in canonical:
            self.assertTrue(candidate.is_file(), candidate)
        for retired in legacy:
            self.assertFalse(retired.exists(), retired)

        router = (
            root / "seebx/capabilities/conversation/router.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "from seebx.capabilities.conversation.composition import (",
            router,
        )
        self.assertIn(
            "from seebx.capabilities.conversation.lifeswitch_composition import (",
            router,
        )
        self.assertNotIn("rag_engine.response_composition_root", router)

    def test_phase3_root_is_absent_from_live_shared_request_files(self) -> None:
        root = Path(__file__).resolve().parents[1]
        forbidden = (
            "response_composition_root_v0_2",
            "response_finalization_v1",
            "server_response_signal_classifier_v0_2",
        )
        for relative in (
            "app.py",
            "rag_engine/prompt_builder.py",
            "rag_engine/vantage_router.py",
            "rag_engine/persona_loader.py",
        ):
            text = (root / relative).read_text(encoding="utf-8")
            for module_name in forbidden:
                self.assertNotIn(module_name, text, msg=f"{relative}: {module_name}")


if __name__ == "__main__":
    unittest.main()
