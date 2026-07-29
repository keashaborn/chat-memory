from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from rag_engine.prior_web_provenance_v1 import (
    MAX_CITED_SOURCES_PER_RESPONSE,
    MAX_PROVENANCE_RESPONSES,
    PriorWebProvenanceEnvelopeV1,
    PriorWebProvenanceError,
    load_prior_web_provenance_v1,
    prior_web_provenance_requested_v1,
)
from rag_engine.response_conversation_snapshot_v1 import (
    ConversationSnapshotOutcome,
    _snapshot,
)
from rag_engine.response_policy_v0_2 import (
    ConversationRole,
    ResponsePolicyConversationMessageV0_2,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER_ACTOR = UUID("2240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
CURRENT_LOG = UUID("90000000-0000-4000-8000-000000000099")
CUTOFF = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)
QUESTION = "What sources did you use for your last answer?"


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def snapshot(message: str = QUESTION):
    return _snapshot(
        actor=ACTOR,
        thread=THREAD,
        request_id="provenance-request-001",
        outcome=ConversationSnapshotOutcome.CURRENT_REQUEST_BOUND,
        current_log_id=CURRENT_LOG,
        cutoff=CUTOFF,
        messages=(
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content="What happened with OpenAI today?",
            ),
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.ASSISTANT,
                content="OpenAI published a current update.",
            ),
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content=message,
            ),
        ),
        candidate_count=2,
        dropped_count=0,
        message_limit_truncated=False,
    )


def row(
    ordinal: int = 0,
    *,
    owner: UUID = ACTOR,
    answer: str = "OpenAI published a current update.",
    answer_hash: str | None = None,
    cited_sources: list[dict[str, Any]] | None = None,
    admitted_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    response_id = UUID(f"90000000-0000-4000-8000-{ordinal + 1:012d}")
    sources = cited_sources or [
        {
            "url": f"https://openai.com/news/item-{ordinal}",
            "title": "UNTRUSTED TITLE MUST NOT ENTER PROMPT",
            "excerpt": "UNTRUSTED PAGE TEXT MUST NOT ENTER PROMPT",
        }
    ]
    admitted = admitted_sources if admitted_sources is not None else sources
    return {
        "log_id": response_id,
        "owner_user_id": owner,
        "thread_id": THREAD,
        "assistant_text": answer,
        "created_at": CUTOFF - timedelta(minutes=ordinal + 1),
        "response_id": response_id,
        "assistant_chat_log_id": response_id,
        "search_id": UUID(f"80000000-0000-4000-8000-{ordinal + 1:012d}"),
        "route": "current_news",
        "policy_version": "search_decision_v1_2",
        "decision": "live",
        "answer_sha256": answer_hash or text_sha256(answer),
        "cited_sources": json.dumps(sources),
        "admitted_sources": json.dumps(admitted),
    }


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class ProvenanceConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.transactions: list[dict[str, Any]] = []
        self.fetch_calls = 0

    def transaction(self, **kwargs: Any) -> FakeTransaction:
        self.transactions.append(kwargs)
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.set_config_args = args
        return "SELECT 1"

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "current_user" in query:
            return "brains_app"
        if "transaction_read_only" in query:
            return "on"
        if "SELECT EXISTS" in query:
            return True
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls += 1
        self.fetch_args = args
        return self.rows


class PriorWebProvenanceV1Tests(unittest.IsolatedAsyncioTestCase):
    def test_intent_is_narrow_and_deterministic(self) -> None:
        for message in (
            QUESTION,
            "Did you check those links?",
            "Did you browse the web for that?",
            "Where did that information come from?",
        ):
            self.assertTrue(prior_web_provenance_requested_v1(message))
        for message in (
            "What is the capital of France?",
            "Search for sources about OpenAI.",
            "What happened with OpenAI today?",
        ):
            self.assertFalse(prior_web_provenance_requested_v1(message))

    async def test_unrelated_question_performs_no_database_work(self) -> None:
        conn = ProvenanceConn([row()])
        result = await load_prior_web_provenance_v1(
            conn,
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot("What is the capital of France?"),
        )
        self.assertIsNone(result)
        self.assertEqual(conn.transactions, [])
        self.assertEqual(conn.fetch_calls, 0)

    async def test_valid_provenance_is_owner_thread_snapshot_and_hash_bound(self) -> None:
        conn = ProvenanceConn([row()])
        source_snapshot = snapshot()
        result = await load_prior_web_provenance_v1(
            conn,
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=source_snapshot,
        )
        self.assertIsInstance(result, PriorWebProvenanceEnvelopeV1)
        assert result is not None
        self.assertEqual(
            result.authenticated_actor_user_id_sha256,
            text_sha256(str(ACTOR)),
        )
        self.assertEqual(result.thread_id_sha256, text_sha256(str(THREAD)))
        self.assertEqual(
            result.conversation_snapshot_sha256,
            source_snapshot.snapshot_sha256,
        )
        self.assertEqual(result.current_query_sha256, text_sha256(QUESTION))
        self.assertEqual(
            conn.transactions,
            [{"isolation": "repeatable_read", "readonly": True}],
        )
        self.assertEqual(conn.set_config_args, (str(ACTOR),))

        wire = result.canonical_json_bytes().decode("utf-8")
        duplicate = wire.replace(
            '"contract_version":',
            '"contract_version":"prior_web_provenance_v1","contract_version":',
            1,
        )
        with self.assertRaises(PriorWebProvenanceError):
            PriorWebProvenanceEnvelopeV1.from_wire_json(duplicate)

    async def test_cross_owner_and_answer_hash_tampering_are_omitted(self) -> None:
        for invalid_row in (
            row(owner=OTHER_ACTOR),
            row(answer_hash="0" * 64),
        ):
            with self.subTest(invalid_row=invalid_row):
                result = await load_prior_web_provenance_v1(
                    ProvenanceConn([invalid_row]),
                    authenticated_actor_user_id=ACTOR,
                    conversation_snapshot=snapshot(),
                )
                self.assertIsNone(result)

    async def test_cited_source_must_be_admitted(self) -> None:
        result = await load_prior_web_provenance_v1(
            ProvenanceConn(
                [
                    row(
                        cited_sources=[{"url": "https://openai.com/news/cited"}],
                        admitted_sources=[
                            {"url": "https://openai.com/news/different"}
                        ],
                    )
                ]
            ),
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot(),
        )
        self.assertIsNone(result)

    async def test_caps_and_content_sanitization_are_enforced(self) -> None:
        many_sources = [
            {
                "url": f"https://openai.com/news/source-{index}",
                "title": f"UNTRUSTED TITLE {index}",
                "excerpt": f"UNTRUSTED PAGE TEXT {index}",
            }
            for index in range(MAX_CITED_SOURCES_PER_RESPONSE + 4)
        ]
        rows = [
            row(index, cited_sources=many_sources, admitted_sources=many_sources)
            for index in range(MAX_PROVENANCE_RESPONSES + 3)
        ]
        result = await load_prior_web_provenance_v1(
            ProvenanceConn(rows),
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot(),
        )
        assert result is not None
        self.assertEqual(len(result.responses), MAX_PROVENANCE_RESPONSES)
        self.assertTrue(
            all(
                len(item.cited_sources) == MAX_CITED_SOURCES_PER_RESPONSE
                for item in result.responses
            )
        )
        self.assertNotIn("UNTRUSTED TITLE", result.content)
        self.assertNotIn("UNTRUSTED PAGE TEXT", result.content)
        self.assertNotIn("fragment", result.content)
        self.assertLessEqual(result.content_bytes, 16_384)

    async def test_nonpublic_or_non_https_source_is_omitted(self) -> None:
        for url in (
            "http://openai.com/news",
            "https://127.0.0.1/admin",
            "https://localhost/admin",
            "https://user:password@openai.com/news",
            "https://openai.com:8443/news",
        ):
            with self.subTest(url=url):
                result = await load_prior_web_provenance_v1(
                    ProvenanceConn(
                        [
                            row(
                                cited_sources=[{"url": url}],
                                admitted_sources=[{"url": url}],
                            )
                        ]
                    ),
                    authenticated_actor_user_id=ACTOR,
                    conversation_snapshot=snapshot(),
                )
                self.assertIsNone(result)

    async def test_source_query_and_fragment_are_not_forwarded(self) -> None:
        source = {
            "url": (
                "https://openai.com/news/item?"
                "utm_source=openai&access_token=secret#section"
            )
        }
        result = await load_prior_web_provenance_v1(
            ProvenanceConn(
                [
                    row(
                        cited_sources=[source],
                        admitted_sources=[source],
                    )
                ]
            ),
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot(),
        )
        assert result is not None
        self.assertIn("https://openai.com/news/item", result.content)
        self.assertNotIn("utm_source", result.content)
        self.assertNotIn("access_token", result.content)
        self.assertNotIn("secret", result.content)
        self.assertNotIn("#section", result.content)


if __name__ == "__main__":
    unittest.main()
