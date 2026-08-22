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
    "thread_selection.py",
    "transcript_integrity.py",
    "zep_sync_postgres.py",
)
LEGACY_NAMESPACES = (
    "public.active_thread_selection",
    "public.chat_attachments",
    "public.chat_log",
    "public.threads",
    "chat_history_private.",
    "chat_integrity.",
)
CANONICAL_NAMESPACES = (
    "conversation.active_thread_selection",
    "conversation.chat_attachments",
    "conversation.chat_log",
    "conversation.threads",
    "conversation_private.",
    "conversation_integrity.",
)


class PlatformConversationNamespaceV1Tests(unittest.TestCase):
    def test_runtime_adapters_have_no_legacy_conversation_namespace(self) -> None:
        for name in ADAPTERS:
            source = (ROOT / "seebx" / "adapters" / name).read_text(encoding="utf-8")
            for legacy in LEGACY_NAMESPACES:
                self.assertNotIn(legacy, source, f"{name} retains {legacy}")

    def test_runtime_adapter_set_consumes_every_canonical_namespace(self) -> None:
        source = "\n".join(
            (ROOT / "seebx" / "adapters" / name).read_text(encoding="utf-8")
            for name in ADAPTERS
        )
        for canonical in CANONICAL_NAMESPACES:
            self.assertIn(canonical, source)


if __name__ == "__main__":
    unittest.main()
