from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from rag_engine.response_composition_root_v0_2 import (
    AuthenticatedResponseCommandV0_2,
    InactiveResponseCompositionRootV0_2,
)
from tests.test_openai_chat_provider_v1 import provider_response
from tests.test_openai_moderation_adapter_v0_2 import categories, response as moderation_response


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
ANSWER = UUID("90000000-0000-4000-8000-000000000001")
CORRELATION = UUID("90000000-0000-4000-8000-000000000002")


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


def command(message: str) -> AuthenticatedResponseCommandV0_2:
    return AuthenticatedResponseCommandV0_2(
        authenticated_actor_user_id=ACTOR,
        thread_id=THREAD,
        request_id="composition-request",
        current_message=message,
        request_field_names=("mix", "vantage_id"),
    )


class ResponseCompositionRootV0_2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_authority_order_reaches_final_attestation(self) -> None:
        client = CombinedOpenAIClient()
        conn = SnapshotConn()
        root = InactiveResponseCompositionRootV0_2(
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

    async def test_detailed_execution_is_bound_to_public_finalization(self) -> None:
        client = CombinedOpenAIClient()
        root = InactiveResponseCompositionRootV0_2(
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

    async def test_local_domain_danger_skips_classifier_call_and_suppresses_fm(self) -> None:
        client = CombinedOpenAIClient(classifier_output(fm_explicit=True))
        root = InactiveResponseCompositionRootV0_2(
            openai_client=client,
            classifier_model="gpt-5.1",
            answer_id_factory=lambda: ANSWER,
            correlation_id_factory=lambda: CORRELATION,
        )

        await root.execute(
            SnapshotConn(),
            command(
                "Explain Fractal Monism, but I have crushing chest pain and cannot breathe."
            ),
        )

        self.assertEqual([name for name, _ in client.calls], ["moderation", "chat"])
        chat_kwargs = client.calls[-1][1]
        system_text = chat_kwargs["messages"][0]["content"]
        self.assertIn("Use conventional, concrete, domain-appropriate safeguards", system_text)
        self.assertIn("Effective Fractal Monism level: OFF", system_text)
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
