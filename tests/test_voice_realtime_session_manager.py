from __future__ import annotations

import unittest
import uuid

from rag_engine.voice_realtime_session_manager import (
    RealtimePreviewSessionRegistry,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
VOICE_SESSION = uuid.UUID("a872d3f2-2d5c-4ae3-9f02-d9f43a38899e")
THREAD_ID = uuid.UUID("a401fdc5-92ee-4eeb-ad64-a98603c7dc69")


class RealtimePreviewSessionRegistryTests(unittest.TestCase):
    def test_expired_sessions_fail_closed(self) -> None:
        now = [100.0]
        registry = RealtimePreviewSessionRegistry(
            ttl_seconds=10,
            clock=lambda: now[0],
        )
        session = registry.register(
            owner_user_id=OWNER,
            voice_session_id=VOICE_SESSION,
            thread_id=THREAD_ID,
            openai_call_id="rtc_expiring",
        )
        self.assertIsNotNone(
            registry.get_owned(
                preview_session_id=session.preview_session_id,
                owner_user_id=OWNER,
                voice_session_id=VOICE_SESSION,
            )
        )

        now[0] = 111.0
        self.assertIsNone(
            registry.get_owned(
                preview_session_id=session.preview_session_id,
                owner_user_id=OWNER,
                voice_session_id=VOICE_SESSION,
            )
        )
        self.assertEqual(registry.size(), 0)

    def test_owner_and_voice_lease_are_part_of_the_key(self) -> None:
        registry = RealtimePreviewSessionRegistry()
        session = registry.register(
            owner_user_id=OWNER,
            voice_session_id=VOICE_SESSION,
            thread_id=THREAD_ID,
            openai_call_id="rtc_owned",
        )
        self.assertIsNone(
            registry.get_owned(
                preview_session_id=session.preview_session_id,
                owner_user_id="557ea042-cb82-48f8-9429-472e96c957ef",
                voice_session_id=VOICE_SESSION,
            )
        )
        self.assertIsNone(
            registry.get_owned(
                preview_session_id=session.preview_session_id,
                owner_user_id=OWNER,
                voice_session_id=uuid.uuid4(),
            )
        )

    def test_events_are_cursor_ordered_and_bounded(self) -> None:
        registry = RealtimePreviewSessionRegistry()
        session = registry.register(
            owner_user_id=OWNER,
            voice_session_id=VOICE_SESSION,
            thread_id=THREAD_ID,
            openai_call_id="rtc_events",
        )
        first = session.append_event("session.connected")
        second = session.append_event(
            "transcript.completed",
            {"transcript": "hello"},
        )

        events, cursor = session.events_after(0)
        self.assertEqual(first.cursor, 1)
        self.assertEqual(second.cursor, 2)
        self.assertEqual(
            [event["type"] for event in events],
            ["session.connected", "transcript.completed"],
        )
        self.assertEqual(cursor, 2)

        events, cursor = session.events_after(1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["transcript"], "hello")
        self.assertEqual(cursor, 2)


if __name__ == "__main__":
    unittest.main()
