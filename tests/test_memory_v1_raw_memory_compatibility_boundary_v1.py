from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"


def _log_chat_node() -> ast.AsyncFunctionDef:
    module = ast.parse(APP.read_text(encoding="utf-8"), filename=str(APP))
    matches = [
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "log_chat"
    ]
    if len(matches) != 1:
        raise AssertionError("expected exactly one log_chat route")
    return matches[0]


class RawMemoryCompatibilityBoundaryV1Tests(unittest.TestCase):
    def test_ordinary_log_has_no_raw_qdrant_or_embedding_projection(self) -> None:
        node = _log_chat_node()
        memory_raw = [
            item
            for item in ast.walk(node)
            if isinstance(item, ast.Constant) and item.value == "memory_raw"
        ]
        self.assertEqual(len(memory_raw), 1)

        source = ast.get_source_segment(APP.read_text(encoding="utf-8"), node)
        assert source is not None
        identity_start = source.index(
            'if source == "frontend/identity" and text.startswith("FULL_NAME:"):'
        )
        transcript_start = source.index("# Stable transcript row id.")
        identity_branch = source[identity_start:transcript_start]
        ordinary_branch = source[transcript_start:]
        self.assertIn('collection_name="memory_raw"', identity_branch)
        self.assertIn("client.embeddings.create", identity_branch)
        self.assertNotIn("memory_raw", ordinary_branch)
        self.assertNotIn("client.embeddings.create", ordinary_branch)
        self.assertNotIn("get_qdrant()", ordinary_branch)

    def test_postgres_transcript_and_attachment_binding_remain_authoritative(self) -> None:
        source = ast.get_source_segment(
            APP.read_text(encoding="utf-8"), _log_chat_node()
        )
        assert source is not None
        registry_guard = source.index("if LEGACY_MEMORY_SURFACES_ENABLED:")
        registry_write = source.index("memory.register_authenticated_owner_v1")
        self.assertLess(registry_guard, registry_write)
        self.assertIn("INSERT INTO chat_log(", source)
        self.assertIn("fetch_attachment_bindings(", source)
        self.assertIn("bind_attachments_to_message(", source)
        self.assertIn('"transcript_write_failed"', source)
        self.assertIn(
            'return {"status": "ok", "id": rec_id, "request_id": request_id}',
            source,
        )

    def test_card_routes_are_explicitly_compatibility_only(self) -> None:
        source = APP.read_text(encoding="utf-8")
        self.assertIn("compatibility-only card artifacts", source)
        self.assertIn("compatibility-card upsert", source)
        self.assertIn("not a governed claim lifecycle operation", source)


if __name__ == "__main__":
    unittest.main()
