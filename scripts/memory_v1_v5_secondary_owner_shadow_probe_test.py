#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
import uuid
from argparse import Namespace
from pathlib import Path

from memory_v1_v5_secondary_owner_shadow_probe import (
    sanitized_trace,
    secure_write,
    temporary_owner_allowlist,
    validate,
)


OWNER = uuid.UUID("557ea042-cb82-48f8-9429-472e96c957ef")


def trace() -> dict:
    return {
        "version": "memory_v1_v5_shadow_trace_v1",
        "status": "ok",
        "persistable": False,
        "selected_count": 0,
        "rejected_counts": {"predicate_not_allowed": 1},
        "database_writes": 0,
        "qdrant_writes": 0,
        "trace_writes": 0,
        "prompt_injection": False,
        "answer_model_exposure": False,
        "retrieval_activation": False,
    }


def main() -> None:
    parsed_owner, parsed_seed = validate(
        Namespace(
            owner_user_id=str(OWNER),
            expected_selected=1,
            query="Tell me about my pets.",
            request_classification="SPECIFIC_RECALL",
            seed_claim_id="fc4b1c5b-40f6-4e8e-8c78-8b3429930506",
        )
    )
    assert parsed_owner == OWNER
    assert parsed_seed == uuid.UUID("fc4b1c5b-40f6-4e8e-8c78-8b3429930506")

    original = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
    os.environ["MEMORY_V1_V5_SHADOW_USER_IDS"] = original
    with temporary_owner_allowlist(OWNER):
        values = set(os.environ["MEMORY_V1_V5_SHADOW_USER_IDS"].split(","))
        assert str(OWNER) in values
        assert original in values
    assert os.environ["MEMORY_V1_V5_SHADOW_USER_IDS"] == original

    value = sanitized_trace(trace())
    assert value["selected_count"] == 0
    unsafe = trace()
    unsafe["prompt_injection"] = True
    try:
        sanitized_trace(unsafe)
    except RuntimeError:
        pass
    else:
        raise AssertionError("prompt-influencing trace was accepted")

    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "probe.json"
        secure_write(output, {"test": True})
        try:
            secure_write(output, {"test": False})
        except RuntimeError:
            pass
        else:
            raise AssertionError("shadow probe output overwrite was accepted")

    print("memory_v1_v5_secondary_owner_shadow_probe: PASS")


if __name__ == "__main__":
    main()
