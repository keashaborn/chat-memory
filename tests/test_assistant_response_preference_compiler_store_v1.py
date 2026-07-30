from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from uuid import UUID

from rag_engine.assistant_response_preference_compiler_store_v1 import (
    PreferenceCompilationCandidateUnavailableV1,
    approve_compilation_candidate_v1,
    store_compilation_candidate_v1,
)
from rag_engine.assistant_response_preference_compiler_v1 import (
    PreferenceCompilationStatus,
    build_compilation_candidate_v1,
    _CompilerModelOutput,
)
from rag_engine.assistant_response_preferences_store_v1 import (
    AssistantResponsePreferenceConflictV1,
)
from rag_engine.assistant_response_preferences_v1 import (
    CompiledPreferenceRuleId,
    PreferenceSource,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
NOW = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)


def candidate():
    return build_compilation_candidate_v1(
        owner_user_id=OWNER,
        source_revision=2,
        source_narrative="Be direct and stop ending with offers.",
        output=_CompilerModelOutput(
            response_length=None,
            technical_depth=None,
            response_format=None,
            conversation_style=None,
            rule_ids=[
                CompiledPreferenceRuleId.NO_UNSOLICITED_CLOSING_OFFERS
            ],
            rejected_reason_codes=[],
        ),
        provider_model="gpt-5.6-test",
        provider_response_id="resp_test",
        now=NOW,
    )


def preference_row(revision: int = 2) -> dict[str, object]:
    return {
        "owner_user_id": OWNER,
        "revision": revision,
        "assistant_name": "Sage",
        "nickname": "Eric",
        "occupation": None,
        "more_about_you": None,
        "custom_instructions": None,
        "response_length": "balanced",
        "technical_depth": "balanced",
        "response_format": "auto",
        "conversation_style": "natural",
        "updated_at": NOW,
    }


def candidate_row(value) -> dict[str, object]:
    return {
        "candidate_id": value.candidate_id,
        "owner_user_id": OWNER,
        "source_revision": 2,
        "source_narrative": value.source_narrative,
        "proposed_response_length": None,
        "proposed_technical_depth": None,
        "proposed_response_format": None,
        "proposed_conversation_style": None,
        "rule_ids": [item.value for item in value.rule_ids],
        "rejected_reason_codes": [],
        "compilation_status": PreferenceCompilationStatus.ACCEPTED.value,
        "summary": list(value.summary),
        "not_applied": [],
        "plan_sha256": value.plan_sha256,
        "compiler_version": value.compiler_version,
        "status": "candidate",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }


class FakeConnection:
    def __init__(self, *, revision: int | None = 2) -> None:
        self.revision = revision
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.current: dict[str, object] | None = preference_row(
            revision or 2
        ) if revision is not None else None
        self.candidate: dict[str, object] | None = None

    async def fetchval(self, query: str, *args: object) -> object:
        self.calls.append((query, args))
        return self.revision

    async def execute(self, query: str, *args: object) -> str:
        self.calls.append((query, args))
        return "UPDATE 1"

    async def fetchrow(self, query: str, *args: object) -> object:
        self.calls.append((query, args))
        if "compilation_candidate_v1" in query and "SELECT *" in query:
            return self.candidate
        if "FOR UPDATE" in query:
            return self.current
        if (
            "UPDATE user_settings.assistant_response_preference_v1" in query
            or "INSERT INTO user_settings.assistant_response_preference_v1" in query
        ):
            result = dict(self.current or preference_row(0))
            result["revision"] = int(result["revision"]) + 1
            result["custom_instructions"] = (
                "assistant-preference-plan-v1:"
                "no_unsolicited_closing_offers"
            )
            return result
        return None


class AssistantResponsePreferenceCompilerStoreV1Tests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_candidate_store_rechecks_revision(self) -> None:
        conn = FakeConnection(revision=3)
        with self.assertRaises(AssistantResponsePreferenceConflictV1):
            await store_compilation_candidate_v1(  # type: ignore[arg-type]
                conn,
                candidate(),
            )
        self.assertEqual(len(conn.calls), 1)

    async def test_candidate_store_supersedes_only_same_owner(self) -> None:
        conn = FakeConnection(revision=2)
        value = candidate()
        await store_compilation_candidate_v1(conn, value)  # type: ignore[arg-type]
        queries = [item[0] for item in conn.calls]
        self.assertIn("owner_user_id=$1", queries[1])
        self.assertIn("status='candidate'", queries[1])
        self.assertIn("INSERT INTO", queries[2])
        self.assertEqual(conn.calls[2][1][1], OWNER)

    async def test_approval_is_owner_candidate_and_revision_bound(self) -> None:
        conn = FakeConnection(revision=2)
        value = candidate()
        conn.candidate = candidate_row(value)
        saved = await approve_compilation_candidate_v1(  # type: ignore[arg-type]
            conn,
            owner_user_id=OWNER,
            candidate_id=value.candidate_id,
            expected_revision=2,
        )
        self.assertEqual(saved.owner_user_id, OWNER)
        self.assertEqual(saved.revision, 3)
        self.assertIs(saved.source, PreferenceSource.POSTGRES)
        candidate_select = next(
            query
            for query, _ in conn.calls
            if "SELECT *" in query and "compilation_candidate_v1" in query
        )
        self.assertIn("owner_user_id=$1", candidate_select)
        self.assertIn("candidate_id=$2", candidate_select)
        preference_update = next(
            query
            for query, _ in conn.calls
            if "UPDATE user_settings.assistant_response_preference_v1" in query
        )
        self.assertIn("AND revision=$2", preference_update)

    async def test_rejected_candidate_cannot_be_approved(self) -> None:
        conn = FakeConnection(revision=2)
        value = candidate()
        row = candidate_row(value)
        row["compilation_status"] = PreferenceCompilationStatus.REJECTED.value
        conn.candidate = row
        with self.assertRaises(PreferenceCompilationCandidateUnavailableV1):
            await approve_compilation_candidate_v1(  # type: ignore[arg-type]
                conn,
                owner_user_id=OWNER,
                candidate_id=value.candidate_id,
                expected_revision=2,
            )
        preference_writes = [
            query
            for query, _ in conn.calls
            if (
                "UPDATE user_settings.assistant_response_preference_v1" in query
                or "INSERT INTO user_settings.assistant_response_preference_v1"
                in query
            )
        ]
        self.assertEqual(preference_writes, [])


if __name__ == "__main__":
    unittest.main()
