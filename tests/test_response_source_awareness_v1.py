from __future__ import annotations

import unittest

from seebx.capabilities.conversation.source_awareness import (
    LifeSwitchSourceStatusV1,
    MemorySourceStatusV1,
    WebSourceStatusV1,
    append_lifeswitch_source_awareness_v1,
    lifeswitch_source_status_v1,
    render_base_source_awareness_v1,
    web_source_status_v1,
)


class ResponseSourceAwarenessV1Tests(unittest.TestCase):
    def test_all_memory_states_have_distinct_truthful_language(self) -> None:
        rendered = {
            status: render_base_source_awareness_v1(
                memory_status=status,
                web_status=WebSourceStatusV1.NOT_AUTHORIZED,
            )
            for status in MemorySourceStatusV1
        }

        self.assertEqual(len(set(rendered.values())), 4)
        self.assertIn("relevant saved claims are supplied", rendered[MemorySourceStatusV1.SELECTED])
        self.assertIn("no relevant saved claims were selected", rendered[MemorySourceStatusV1.CHECKED_EMPTY])
        self.assertIn("intentionally not consulted", rendered[MemorySourceStatusV1.NOT_APPLICABLE])
        self.assertIn("runtime was unavailable", rendered[MemorySourceStatusV1.UNAVAILABLE])

    def test_web_authorization_is_not_reported_as_completed_research(self) -> None:
        status = web_source_status_v1(authorized=True)
        rendered = render_base_source_awareness_v1(
            memory_status=MemorySourceStatusV1.CHECKED_EMPTY,
            web_status=status,
        )

        self.assertIs(status, WebSourceStatusV1.AUTHORIZED_BOUNDED)
        self.assertIn("authorization alone does not prove that a search ran", rendered)

    def test_lifeswitch_preparation_states_map_without_inference(self) -> None:
        expected = {
            "OFF": LifeSwitchSourceStatusV1.NOT_APPLICABLE,
            "TIMEZONE_UNAVAILABLE": LifeSwitchSourceStatusV1.UNAVAILABLE,
            "EMPTY": LifeSwitchSourceStatusV1.CHECKED_EMPTY,
            "SELECTED": LifeSwitchSourceStatusV1.SELECTED,
            "PARTIAL": LifeSwitchSourceStatusV1.PARTIAL,
        }

        self.assertEqual(
            {key: lifeswitch_source_status_v1(key) for key in expected},  # type: ignore[arg-type]
            expected,
        )

    def test_lifeswitch_status_is_added_as_system_instruction(self) -> None:
        rendered = append_lifeswitch_source_awareness_v1(
            "Base system prompt.",
            status=LifeSwitchSourceStatusV1.CHECKED_EMPTY,
        )

        self.assertIn("Additional source status", rendered)
        self.assertIn("LifeSwitch was checked", rendered)
        self.assertIn("no relevant structured records", rendered)


if __name__ == "__main__":
    unittest.main()
