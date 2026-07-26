from __future__ import annotations

import asyncio
import hashlib
import unittest
import uuid

from rag_engine.memory_v1_evidence_context_loader_v1 import (
    EvidenceContextContractError,
    load_memory_evidence_context_v1,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "557ea042-cb82-48f8-9429-472e96c957ef"
SOURCE_ID = "ed91f3b9-a4b8-4e63-aac7-53e370412483"
THREAD_ID = "2f6e6a6c-f43e-45e2-8137-8b1b01e67976"
REQUEST_ID = "bfca3e63-e670-4601-a06d-6345c18554f4"
TARGET_ID = "049205b4-9a6c-5e1a-bb8f-2ab9f05f8964"
SOURCE_TEXT = (
    "Much of the app is turning into life switch, so I’m thinking about "
    "making the verbal sage fractal monistic data that just be the kind of "
    "the engine that runs it through this platform. My idea is, I want to "
    "philosophy to be conveyed subtly cause I think the philosophy will "
    "help people in life. But no one‘s ever gonna ask about fractal monism "
    "so I need to figure out a way that the concepts and philosophy can be "
    "slipped into diet, exercise training, and just general questions of "
    "someone asks."
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def records():
    source = {
        "id": SOURCE_ID,
        "owner_user_id": OWNER,
        "thread_id": THREAD_ID,
        "request_id": REQUEST_ID,
        "created_at": "2026-07-02T02:05:41.345358+00:00",
        "text": SOURCE_TEXT,
    }
    definitions = (
        (
            "3bd07b8c-c047-531f-a3a7-05c8d901d5b6",
            0,
            183,
            "technical_project",
            "atomic",
        ),
        (
            "0c5c5763-ee69-532a-a0a6-e9be1e3433c1",
            184,
            238,
            "contextual_project",
            "compound_child",
        ),
        (
            TARGET_ID,
            239,
            293,
            "user_viewpoint",
            "compound_child",
        ),
    )
    evidence = []
    for evidence_id, start, end, lane, origin in definitions:
        content = SOURCE_TEXT[start:end]
        evidence.append(
            {
                "evidence_id": evidence_id,
                "owner_user_id": OWNER,
                "source_system": "public.chat_log",
                "external_id": f"chat_log:{SOURCE_ID}:span:{evidence_id}",
                "content": content,
                "content_sha256": sha(content),
                "recorded_at": "2026-07-02T02:05:41.345358+00:00",
                "metadata": {
                    "source_id": SOURCE_ID,
                    "thread_id": THREAD_ID,
                    "request_id": REQUEST_ID,
                    "source_content_sha256": sha(SOURCE_TEXT),
                    "source_char_start": start,
                    "source_char_end": end,
                    "primary_lane": lane,
                    "epistemic_role": "user_belief_or_opinion",
                    "span_origin": origin,
                },
            }
        )
    target = next(row for row in evidence if row["evidence_id"] == TARGET_ID)
    return source, evidence, target


class Transaction:
    def __init__(self, connection, readonly: bool) -> None:
        self.connection = connection
        self.readonly = readonly

    async def __aenter__(self):
        self.connection.entered_readonly = self.readonly
        return self

    async def __aexit__(self, *_args):
        self.connection.exited = True


class Connection:
    def __init__(self, *, actor: str = OWNER) -> None:
        self.actor = actor
        self.source, self.evidence, self.target = records()
        self.entered_readonly = False
        self.exited = False
        self.queries: list[str] = []

    def transaction(self, *, readonly: bool):
        return Transaction(self, readonly)

    async def execute(self, query: str, *_args):
        self.queries.append(query)
        return "SELECT 1"

    async def fetchval(self, query: str, *_args):
        self.queries.append(query)
        return self.actor

    async def fetchrow(self, query: str, *_args):
        self.queries.append(query)
        if "FROM memory.evidence" in query:
            return self.target
        if "FROM public.chat_log" in query:
            return self.source
        raise AssertionError(query)

    async def fetch(self, query: str, *_args):
        self.queries.append(query)
        return list(reversed(self.evidence))


class EvidenceContextLoaderV1Test(unittest.TestCase):
    def test_readonly_owner_scoped_loader(self) -> None:
        connection = Connection()
        envelope = asyncio.run(
            load_memory_evidence_context_v1(
                connection,
                expected_owner_user_id=OWNER,
                target_evidence_id=TARGET_ID,
                expected_target_content_sha256=connection.target[
                    "content_sha256"
                ],
            )
        )
        self.assertTrue(connection.entered_readonly)
        self.assertTrue(connection.exited)
        self.assertEqual(envelope.owner_user_id, OWNER)
        self.assertEqual(envelope.target_evidence_id, TARGET_ID)
        self.assertEqual(len(envelope.spans), 3)
        queries = "\n".join(connection.queries)
        self.assertIn("owner_user_id=$1", queries)
        self.assertIn("status='active'", queries)
        self.assertIn("source_system='public.chat_log'", queries)
        self.assertNotIn("INSERT", queries)
        self.assertNotIn("UPDATE", queries)
        self.assertNotIn("DELETE", queries)

    def test_actor_mismatch_fails_before_data_load(self) -> None:
        connection = Connection(actor=OTHER)
        with self.assertRaisesRegex(
            EvidenceContextContractError,
            "actor differs",
        ):
            asyncio.run(
                load_memory_evidence_context_v1(
                    connection,
                    expected_owner_user_id=OWNER,
                    target_evidence_id=TARGET_ID,
                    expected_target_content_sha256=connection.target[
                        "content_sha256"
                    ],
                )
            )
        self.assertEqual(
            sum("FROM memory.evidence" in query for query in connection.queries),
            0,
        )

    def test_large_source_uses_deterministic_target_centered_window(self) -> None:
        connection = Connection()
        source_text = " ".join(f"span-{index:02d}" for index in range(28))
        connection.source["text"] = source_text
        connection.evidence = []
        cursor = 0
        for index in range(28):
            content = f"span-{index:02d}"
            start = source_text.index(content, cursor)
            end = start + len(content)
            cursor = end
            evidence_id = (
                TARGET_ID
                if index == 20
                else str(uuid.uuid5(uuid.NAMESPACE_URL, f"span:{index}"))
            )
            connection.evidence.append(
                {
                    "evidence_id": evidence_id,
                    "owner_user_id": OWNER,
                    "source_system": "public.chat_log",
                    "external_id": f"chat_log:{SOURCE_ID}:span:{evidence_id}",
                    "content": content,
                    "content_sha256": sha(content),
                    "recorded_at": "2026-07-02T02:05:41.345358+00:00",
                    "metadata": {
                        "source_id": SOURCE_ID,
                        "thread_id": THREAD_ID,
                        "request_id": REQUEST_ID,
                        "source_content_sha256": sha(source_text),
                        "source_char_start": start,
                        "source_char_end": end,
                        "primary_lane": "user_viewpoint",
                        "epistemic_role": "user_belief_or_opinion",
                        "span_origin": "compound_child",
                    },
                }
            )
        connection.target = next(
            row
            for row in connection.evidence
            if row["evidence_id"] == TARGET_ID
        )

        envelope = asyncio.run(
            load_memory_evidence_context_v1(
                connection,
                expected_owner_user_id=OWNER,
                target_evidence_id=TARGET_ID,
                expected_target_content_sha256=connection.target[
                    "content_sha256"
                ],
                max_spans=12,
            )
        )

        self.assertEqual(len(envelope.spans), 12)
        self.assertEqual(
            [span.context_role for span in envelope.spans],
            ["before"] * 7 + ["target"] + ["after"] * 4,
        )
        self.assertEqual(
            [span.content for span in envelope.spans],
            [f"span-{index:02d}" for index in range(13, 25)],
        )
        queries = "\n".join(connection.queries)
        self.assertIn("row_number() OVER", queries)
        self.assertIn("target_position", queries)


if __name__ == "__main__":
    unittest.main()
