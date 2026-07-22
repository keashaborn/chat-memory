from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "tools" / "memory_v1_v5_local_inference_canary_apply.sh"


class MemoryV1V52CanaryWrapperTest(unittest.TestCase):
    def test_v5_2_profile_is_explicitly_gated_and_audited(self) -> None:
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertIn(
            "MEMORY_V1_V5_2_LOCAL_INFERENCE_CANARY=authorized is required",
            source,
        )
        self.assertIn('--contract-profile "$contract_profile"', source)
        self.assertIn(
            ".predicate_contract_profile==$profile", source
        )
        self.assertIn(
            "memory_v1_relational_extraction_v5_2", source
        )
        self.assertIn("memory_predicate_registry_v5_2", source)

    def test_canary_preserves_hard_stops(self) -> None:
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("external_model_calls:0", source)
        self.assertIn("claim_promotion:false", source)
        self.assertIn("retrieval_activation:false", source)
        self.assertIn("prompt_influence:false", source)
        self.assertIn("qdrant_unchanged:true", source)
        self.assertIn("cross_owner_claim_rejected:true", source)
        self.assertIn("fresh_postgres_backup:true", source)
        self.assertIn("database_role=sage", source)


if __name__ == "__main__":
    unittest.main()
