from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = (
    "conversation_attachments.py",
    "conversation_erasure.py",
    "conversation_export.py",
    "conversation_history.py",
    "conversation_persistence.py",
    "conversation_snapshot.py",
    "prior_web_provenance_postgres.py",
    "telemetry_postgres.py",
    "thread_selection.py",
    "transcript_integrity.py",
    "usage_postgres.py",
    "voice_session.py",
    "zep_sync_postgres.py",
)
LEGACY_TOKENS = (
    "public.active_thread_selection",
    "public.chat_attachments",
    "public.chat_log",
    "public.telemetry_event",
    "public.threads",
    "public.voice_session_lease",
    "chat_history_private.",
    "chat_integrity.",
    "lifeswitch_usage.",
    "lifeswitch_usage_writer_v1",
    "INSERT INTO telemetry_event",
    "FROM telemetry_event",
)
CANONICAL_TOKENS = (
    "conversation.active_thread_selection",
    "conversation.chat_attachments",
    "conversation.chat_log",
    "conversation.threads",
    "conversation_private.",
    "conversation_integrity.",
    "telemetry.telemetry_event",
    "usage.ai_usage_event_v1",
    "seebx_usage_writer_v1",
    "voice.voice_session_lease",
)


class PlatformDatabaseRuntimeNamespacesV1Tests(unittest.TestCase):
    def test_runtime_adapters_have_no_legacy_platform_namespace(self) -> None:
        for name in ADAPTERS:
            source = (ROOT / "seebx" / "adapters" / name).read_text(encoding="utf-8")
            for legacy in LEGACY_TOKENS:
                self.assertNotIn(legacy, source, f"{name} retains {legacy}")

    def test_runtime_adapter_set_consumes_every_canonical_namespace(self) -> None:
        source = "\n".join(
            (ROOT / "seebx" / "adapters" / name).read_text(encoding="utf-8")
            for name in ADAPTERS
        )
        for canonical in CANONICAL_TOKENS:
            self.assertIn(canonical, source)


if __name__ == "__main__":
    unittest.main()
