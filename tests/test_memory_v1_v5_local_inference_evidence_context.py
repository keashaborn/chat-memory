from __future__ import annotations

import unittest

from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
)
from scripts.memory_v1_v5_local_inference_canary import (
    EvidenceContextBoundLocalProvider,
)


class Delegate:
    provider_id = "local_llama_cpp"
    provider_version = "v1"
    external_model_calls = 0
    external_call_capability = False

    def __init__(self) -> None:
        self.source = None
        self.evidence_context = None

    def extract(self, source, *, evidence_context=None):
        self.source = source
        self.evidence_context = evidence_context
        return ProviderPacket.model_validate(
            {
                "entity_mentions": [],
                "observations": [],
                "comparison_hints": [],
                "deferrals": [],
                "packet_findings": [],
            }
        )


class EvidenceContextBoundLocalProviderTest(unittest.TestCase):
    def test_context_is_bound_to_the_single_provider_call(self) -> None:
        delegate = Delegate()
        context = object()
        source = TrustedExtractionSource.create(
            job_id="00000000-0000-4000-8000-000000000001",
            source_system="public.chat_log",
            source_external_id="00000000-0000-4000-8000-000000000002",
            source_sha256=(
                "e3b0c44298fc1c149afbf4c8996fb924"
                "27ae41e4649b934ca495991b7852b855"
            ),
            source_recorded_at="2026-07-26T00:00:00+00:00",
            content="",
        )
        provider = EvidenceContextBoundLocalProvider(delegate, context)

        packet = provider.extract(source)

        self.assertIs(delegate.source, source)
        self.assertIs(delegate.evidence_context, context)
        self.assertEqual(packet.entity_mentions, [])
        self.assertEqual(provider.provider_id, delegate.provider_id)
        self.assertEqual(provider.provider_version, delegate.provider_version)
        self.assertEqual(
            provider.external_model_calls,
            delegate.external_model_calls,
        )
        self.assertEqual(
            provider.external_call_capability,
            delegate.external_call_capability,
        )


if __name__ == "__main__":
    unittest.main()
