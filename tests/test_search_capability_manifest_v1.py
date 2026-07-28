from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from rag_engine.search_capability_manifest_v1 import (
    SEARCH_CAPABILITY_AUTHORITY,
    TEXT_SEARCH_AUTHORIZATION_BASIS,
    VOICE_SEARCH_AUTHORIZATION_BASIS,
    SearchCapabilityManifestV1,
)


class SearchCapabilityManifestV1Tests(unittest.TestCase):
    def test_text_and_voice_manifests_are_server_owned_and_hashed(self) -> None:
        for basis in (
            TEXT_SEARCH_AUTHORIZATION_BASIS,
            VOICE_SEARCH_AUTHORIZATION_BASIS,
        ):
            with self.subTest(basis=basis):
                manifest = SearchCapabilityManifestV1.create(
                    authorization_basis=basis,
                )
                self.assertEqual(manifest.authority, SEARCH_CAPABILITY_AUTHORITY)
                self.assertEqual(
                    manifest.available_routes,
                    ("current_news", "trusted_health"),
                )
                self.assertIn("server, not the browser or model", manifest.model_brief)
                self.assertIn("unless retrieved evidence", manifest.model_brief)
                self.assertIn(
                    "currently supported categories are only",
                    manifest.model_brief,
                )
                self.assertIn("general fact-checking", manifest.model_brief)
                self.assertIn("arbitrary page retrieval", manifest.model_brief)
                self.assertEqual(
                    SearchCapabilityManifestV1.from_wire_json(
                        manifest.model_dump_json()
                    ),
                    manifest,
                )

    def test_tampering_fails_closed(self) -> None:
        manifest = SearchCapabilityManifestV1.create(
            authorization_basis=TEXT_SEARCH_AUTHORIZATION_BASIS,
        )
        payload = json.loads(manifest.model_dump_json())
        payload["available_routes"].append("unbounded_web")
        with self.assertRaises(ValidationError):
            SearchCapabilityManifestV1.model_validate(payload)

        payload = json.loads(manifest.model_dump_json())
        payload["authorization_basis"] = VOICE_SEARCH_AUTHORIZATION_BASIS
        with self.assertRaises(ValidationError):
            SearchCapabilityManifestV1.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
