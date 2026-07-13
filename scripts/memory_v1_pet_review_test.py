#!/usr/bin/env python3
from __future__ import annotations

from rag_engine.memory_v1_pet_review import (
    NEKO_CORRECTION_CLAIM_ID,
    dahlia_loss_proposal,
    neko_loss_proposal,
)
from rag_engine.memory_v1_store import proposal_hash


def main() -> int:
    neko = neko_loss_proposal()
    dahlia = dahlia_loss_proposal()

    if neko["object_literal"]["subject"]["known_name"] != "Neko":
        raise AssertionError("Neko event was not normalized")
    normalization = neko["qualifiers"]["name_normalization"]
    if normalization["source_value"] != "Nemo":
        raise AssertionError("Neko source spelling provenance is missing")
    if normalization["correction_claim_id"] != str(NEKO_CORRECTION_CLAIM_ID):
        raise AssertionError("Neko correction dependency is missing")
    if dahlia["object_literal"]["subject"]["known_name"] != "Dahlia":
        raise AssertionError("Dahlia event subject is incorrect")
    if dahlia["qualifiers"]["age_at_event_years"] != 12:
        raise AssertionError("Dahlia age-at-event qualifier is missing")
    if proposal_hash(neko) == proposal_hash(dahlia):
        raise AssertionError("distinct pet events produced the same proposal hash")

    print("memory_v1_pet_review: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
