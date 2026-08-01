from __future__ import annotations

import datetime as dt
import unittest
import uuid

from pydantic import ValidationError

from rag_engine.lifeswitch_data_plan_v1 import create_lifeswitch_data_plan_v1
from rag_engine.lifeswitch_domain_context_v1 import (
    TrustedLifeSwitchContextRequestV1,
    render_lifeswitch_context_v1,
)
from rag_engine.lifeswitch_domain_provider_v1 import (
    LifeSwitchDomainContextProviderV1,
    LifeSwitchReadResultV1,
)


TODAY = dt.date(2026, 8, 1)
ACTOR = uuid.UUID("11111111-1111-4111-8111-111111111111")
REQUEST = "req_lifeswitch_20260731"
THREAD = uuid.UUID("22222222-2222-4222-8222-222222222222")
SNAPSHOT_SHA256 = "a" * 64


def trusted_request(query: str) -> TrustedLifeSwitchContextRequestV1:
    return TrustedLifeSwitchContextRequestV1.create(
        request_id=REQUEST,
        authenticated_actor_user_id=ACTOR,
        owner_user_id=ACTOR,
        thread_id=THREAD,
        conversation_snapshot_sha256=SNAPSHOT_SHA256,
        owner_timezone="America/Chicago",
        query=query,
        data_plan=create_lifeswitch_data_plan_v1(query, today=TODAY),
    )


class SpyReader:
    def __init__(self, *, source_override: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.source_override = source_override

    def _result(self, method: str, kwargs: dict[str, object]) -> LifeSwitchReadResultV1:
        self.calls.append((method, kwargs))
        relation = self.source_override or {
            "read_plan": "lifeswitch_agentic.plan_versions",
            "read_overall_status": "lifeswitch_nutrition.nutrition_day",
            "read_nutrition_day": "lifeswitch_nutrition.nutrition_day",
            "read_nutrition_range": "lifeswitch_nutrition.nutrition_day",
            "read_training_summary": "lifeswitch_training.training_session_current_v",
            "read_training_session": "lifeswitch_training.training_session_current_v",
            "read_training_range": "lifeswitch_training.training_set_effective_role_v1",
            "read_daily_status_range": "lifeswitch_nutrition.nutrition_day",
            "read_exercise_progression": "lifeswitch_training.training_set_log",
            "read_exercise_frequency": "lifeswitch_training.training_set_effective_role_v1",
            "read_lifting_progression_summary": "lifeswitch_training.training_set_effective_role_v1",
            "read_measurements_summary": "public.lifeswitch_measurement_entries",
        }[method]
        return LifeSwitchReadResultV1(
            status="AVAILABLE",
            plan_source=(
                "agentic_active"
                if method in {
                    "read_plan",
                    "read_overall_status",
                    "read_nutrition_day",
                    "read_nutrition_range",
                    "read_training_summary",
                    "read_daily_status_range",
                    "read_lifting_progression_summary",
                }
                else "not_requested"
            ),
            record_count=1,
            source_relations=(relation,),
            payload={"summary": "bounded", "value": 1},
        )

    async def read_plan(self, **kwargs):
        return self._result("read_plan", kwargs)

    async def read_overall_status(self, **kwargs):
        return self._result("read_overall_status", kwargs)

    async def read_nutrition_day(self, **kwargs):
        return self._result("read_nutrition_day", kwargs)

    async def read_nutrition_range(self, **kwargs):
        return self._result("read_nutrition_range", kwargs)

    async def read_training_summary(self, **kwargs):
        return self._result("read_training_summary", kwargs)

    async def read_training_session(self, **kwargs):
        return self._result("read_training_session", kwargs)

    async def read_training_range(self, **kwargs):
        return self._result("read_training_range", kwargs)

    async def read_daily_status_range(self, **kwargs):
        return self._result("read_daily_status_range", kwargs)

    async def read_exercise_progression(self, **kwargs):
        return self._result("read_exercise_progression", kwargs)

    async def read_exercise_frequency(self, **kwargs):
        return self._result("read_exercise_frequency", kwargs)

    async def read_lifting_progression_summary(self, **kwargs):
        return self._result("read_lifting_progression_summary", kwargs)

    async def read_measurements_summary(self, **kwargs):
        return self._result("read_measurements_summary", kwargs)


class LifeSwitchDomainContextV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_off_plan_performs_zero_reader_calls(self) -> None:
        reader = SpyReader()
        envelope = await LifeSwitchDomainContextProviderV1(reader).select(
            trusted_request("Who won America's Next Top Model in 2015?")
        )
        self.assertEqual(envelope.status, "OFF")
        self.assertEqual(envelope.sections, ())
        self.assertEqual(reader.calls, [])
        self.assertIsNone(render_lifeswitch_context_v1(envelope))

    async def test_nutrition_day_dispatches_exactly_one_bounded_read(self) -> None:
        reader = SpyReader()
        envelope = await LifeSwitchDomainContextProviderV1(reader).select(
            trusted_request("Was I low on protein Monday?")
        )
        self.assertEqual(envelope.status, "SELECTED")
        self.assertEqual(len(reader.calls), 1)
        method, arguments = reader.calls[0]
        self.assertEqual(method, "read_nutrition_day")
        self.assertEqual(arguments["owner_user_id"], ACTOR)
        self.assertEqual(arguments["day"], dt.date(2026, 7, 27))
        rendered = render_lifeswitch_context_v1(envelope)
        self.assertIsNotNone(rendered)
        self.assertLessEqual(rendered.estimated_tokens, 250)

    async def test_combined_request_dispatches_one_compact_projection(self) -> None:
        reader = SpyReader()
        envelope = await LifeSwitchDomainContextProviderV1(reader).select(
            trusted_request(
                "Look at my plan and make sure I am meeting all my macros and exercise goals."
            )
        )
        self.assertEqual([call[0] for call in reader.calls], ["read_overall_status"])
        self.assertEqual(envelope.sections[0].projection, "plan_adherence")
        self.assertLessEqual(envelope.estimated_prompt_tokens, 800)

    async def test_progression_subject_reaches_only_progression_reader(self) -> None:
        reader = SpyReader()
        await LifeSwitchDomainContextProviderV1(reader).select(
            trusted_request("How is my squat progressing?")
        )
        self.assertEqual(len(reader.calls), 1)
        method, arguments = reader.calls[0]
        self.assertEqual(method, "read_exercise_progression")
        self.assertEqual(arguments["subject"], "squat")

    async def test_exercise_frequency_reaches_only_frequency_reader(self) -> None:
        reader = SpyReader()
        envelope = await LifeSwitchDomainContextProviderV1(reader).select(
            trusted_request("What exercises do I do the most?")
        )
        self.assertEqual([call[0] for call in reader.calls], ["read_exercise_frequency"])
        self.assertEqual(envelope.sections[0].projection, "exercise_frequency")
        self.assertLessEqual(envelope.estimated_prompt_tokens, 550)

    async def test_training_range_reaches_exact_bounded_reader(self) -> None:
        reader = SpyReader()
        envelope = await LifeSwitchDomainContextProviderV1(reader).select(
            trusted_request(
                "For each day from July 18 through July 31, show whether I "
                "completed strength training or conditioning."
            )
        )
        self.assertEqual([call[0] for call in reader.calls], ["read_training_range"])
        _, arguments = reader.calls[0]
        self.assertEqual(arguments["start_date"], dt.date(2026, 7, 18))
        self.assertEqual(arguments["end_date"], dt.date(2026, 7, 31))
        self.assertEqual(envelope.sections[0].projection, "training_range")

    async def test_daily_status_range_dispatches_one_cross_domain_read(self) -> None:
        reader = SpyReader()
        envelope = await LifeSwitchDomainContextProviderV1(reader).select(
            trusted_request(
                "For each day from July 18 through July 31, show my protein "
                "and whether I completed strength training."
            )
        )
        self.assertEqual(
            [call[0] for call in reader.calls],
            ["read_daily_status_range"],
        )
        self.assertEqual(envelope.sections[0].projection, "daily_status_range")
        self.assertLessEqual(envelope.estimated_prompt_tokens, 800)

    async def test_broad_lifting_progress_reaches_summary_reader(self) -> None:
        reader = SpyReader()
        envelope = await LifeSwitchDomainContextProviderV1(reader).select(
            trusted_request(
                "Have I been progressing with my weights and if so, which ones?"
            )
        )
        self.assertEqual(
            [call[0] for call in reader.calls],
            ["read_lifting_progression_summary"],
        )
        self.assertEqual(
            envelope.sections[0].projection,
            "lifting_progression_summary",
        )
        self.assertLessEqual(envelope.estimated_prompt_tokens, 750)

    async def test_owner_mismatch_is_rejected_before_data_access(self) -> None:
        plan = create_lifeswitch_data_plan_v1("How am I doing?", today=TODAY)
        with self.assertRaises(ValidationError):
            TrustedLifeSwitchContextRequestV1.create(
                request_id=REQUEST,
                authenticated_actor_user_id=ACTOR,
                owner_user_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
                thread_id=THREAD,
                conversation_snapshot_sha256=SNAPSHOT_SHA256,
                owner_timezone="America/Chicago",
                query="How am I doing?",
                data_plan=plan,
            )

    async def test_reader_cannot_escape_projection_source_allowlist(self) -> None:
        reader = SpyReader(source_override="memory_v1.governed_claim")
        with self.assertRaisesRegex(ValueError, "unauthorized source"):
            await LifeSwitchDomainContextProviderV1(reader).select(
                trusted_request("Was I low on protein Monday?")
            )

    async def test_render_exposes_no_owner_uuid_or_raw_query(self) -> None:
        query = "Was I low on protein Monday?"
        envelope = await LifeSwitchDomainContextProviderV1(SpyReader()).select(
            trusted_request(query)
        )
        rendered = render_lifeswitch_context_v1(envelope)
        self.assertNotIn(str(ACTOR), rendered.content)
        self.assertNotIn(query, rendered.content)


if __name__ == "__main__":
    unittest.main()
