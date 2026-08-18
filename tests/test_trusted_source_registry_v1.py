from __future__ import annotations

import unittest
from pathlib import Path

from seebx.capabilities.search.registry import (
    ACSM_DOMAIN,
    APNEWS_DOMAIN,
    BACB_DOMAIN,
    CDC_DOMAIN,
    CLINICAL_TRIALS_DOMAIN,
    COCHRANE_DOMAIN,
    FDC_DOMAIN,
    GITHUB_DOCS_DOMAIN,
    NHK_DOMAIN,
    NSCA_DOMAIN,
    OPENAI_DEVELOPERS_DOMAIN,
    PUBMED_DOMAIN,
    REUTERS_DOMAIN,
    SOURCE_REGISTRY_VERSION,
    SUPABASE_DOMAIN,
    TrustedSourcePackIdV1,
    trusted_source_pack_v1,
)


class TrustedSourceRegistryV1Tests(unittest.TestCase):
    def test_legacy_registry_module_is_retired(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        self.assertFalse(
            (repository / "rag_engine/trusted_source_registry_v1.py").exists()
        )

    def test_registry_exposes_bounded_intent_specific_packs(self) -> None:
        news = trusted_source_pack_v1(
            TrustedSourcePackIdV1.GENERAL_CURRENT_NEWS
        )
        medical = trusted_source_pack_v1(
            TrustedSourcePackIdV1.MEDICAL_HEALTH
        )
        exercise = trusted_source_pack_v1(
            TrustedSourcePackIdV1.EXERCISE_TRAINING
        )
        software = trusted_source_pack_v1(
            TrustedSourcePackIdV1.SOFTWARE_SECURITY_REFERENCE
        )

        self.assertEqual(news.registry_version, SOURCE_REGISTRY_VERSION)
        self.assertEqual(
            news.allowed_domains,
            (APNEWS_DOMAIN, REUTERS_DOMAIN, NHK_DOMAIN),
        )
        self.assertIn(PUBMED_DOMAIN, medical.allowed_domains)
        self.assertIn(CLINICAL_TRIALS_DOMAIN, medical.allowed_domains)
        self.assertIn(CDC_DOMAIN, medical.allowed_domains)
        self.assertIn(COCHRANE_DOMAIN, medical.allowed_domains)
        self.assertIn(ACSM_DOMAIN, exercise.allowed_domains)
        self.assertIn(NSCA_DOMAIN, exercise.allowed_domains)
        self.assertIn(OPENAI_DEVELOPERS_DOMAIN, software.allowed_domains)
        self.assertIn(SUPABASE_DOMAIN, software.allowed_domains)
        self.assertIn(GITHUB_DOCS_DOMAIN, software.allowed_domains)
        self.assertNotIn(FDC_DOMAIN, software.allowed_domains)

    def test_optional_bacb_domain_remains_disabled_by_default(self) -> None:
        default = trusted_source_pack_v1(
            TrustedSourcePackIdV1.BEHAVIOR_CHANGE
        )
        enabled = trusted_source_pack_v1(
            TrustedSourcePackIdV1.BEHAVIOR_CHANGE,
            allow_bacb=True,
        )
        self.assertNotIn(BACB_DOMAIN, default.allowed_domains)
        self.assertIn(BACB_DOMAIN, enabled.allowed_domains)

    def test_packs_have_no_duplicate_domains(self) -> None:
        for pack_id in TrustedSourcePackIdV1:
            with self.subTest(pack_id=pack_id):
                domains = trusted_source_pack_v1(pack_id).allowed_domains
                self.assertEqual(len(domains), len(set(domains)))
                self.assertLessEqual(len(domains), 100)


if __name__ == "__main__":
    unittest.main()
