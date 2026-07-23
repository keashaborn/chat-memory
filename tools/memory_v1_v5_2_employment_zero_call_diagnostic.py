#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

from scripts.memory_v1_predicate_runtime_profile_v2 import (
    load_runtime_profile_v2,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    LocalLlamaCppProvider,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    TrustedExtractionSource,
    validate_and_normalize,
)


EVIDENCE_ID = "3163b69a-f8a8-5263-93d7-300915c2342c"
JOB_ID = "c48c5d31-49e0-439d-a031-465ede14d402"
EXPECTED_SHA256 = (
    "48c3ea92b1c624634f20a54eb462c6953a11d700eeaa1a2b2cffa3c31f67929d"
)


class ZeroCallTransport:
    external_model_calls = 0
    local_model_calls = 0


def evidence_envelope() -> dict[str, str]:
    sql = f"""
    SELECT json_build_object(
      'source_system',source_system,
      'source_external_id',external_id,
      'content_sha256',content_sha256,
      'recorded_at',recorded_at,
      'content_b64',encode(convert_to(content,'UTF8'),'base64')
    )::text
    FROM memory.evidence
    WHERE evidence_id='{EVIDENCE_ID}'::uuid
    """
    process = subprocess.run(
        [
            "docker",
            "exec",
            "brains-postgres-1",
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "sage",
            "-d",
            "memory",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(process.stdout.strip())
    content = base64.b64decode(value.pop("content_b64")).decode("utf-8")
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != EXPECTED_SHA256:
        raise RuntimeError("target evidence content hash changed")
    value["content"] = content
    return value


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    profile = load_runtime_profile_v2(root, "v5_2")
    registry = json.loads(profile.registry_path.read_text(encoding="utf-8"))
    schema = json.loads(profile.schema_path.read_text(encoding="utf-8"))
    envelope = evidence_envelope()
    source = TrustedExtractionSource.create(
        job_id=JOB_ID,
        source_system=envelope["source_system"],
        source_external_id=EVIDENCE_ID,
        source_sha256=envelope["content_sha256"],
        source_recorded_at=envelope["recorded_at"],
        content=envelope["content"],
    )
    provider = LocalLlamaCppProvider(
        model="qwen3-14b-local-extractor",
        model_file_sha256="5" * 64,
        runtime_revision="zero-call-diagnostic",
        registry=registry,
        transport=ZeroCallTransport(),
    )
    validated = validate_and_normalize(
        provider,
        source=source,
        registry=registry,
        schema=schema,
        allowed_provider_versions={"local_llama_cpp": "v1"},
        max_external_model_calls=0,
    )
    packet = validated.normalized_packet
    entities = packet["entity_mentions"]
    observations = packet["observations"]
    employer = [
        item
        for item in entities
        if item["entity_ref"] == "e01"
    ]
    employment = [
        item
        for item in observations
        if item["predicate"] == "employment.worked_for"
    ]
    result = {
        "contract_version": "memory_v1_v5_2_employment_zero_call_diagnostic_v1",
        "local_model_calls": provider.local_model_calls,
        "external_model_calls": provider.external_model_calls,
        "policy_guard_code": provider.last_audit["policy_guard_code"],
        "entity_count": len(entities),
        "observation_count": len(observations),
        "entity_types": [item["entity_type"] for item in entities],
        "employer_entity_count": len(employer),
        "employer_type": employer[0]["entity_type"] if employer else None,
        "employer_name_exact": bool(
            employer
            and employer[0]["name_text"]
            == "Wisconsin Early Autism Project"
        ),
        "employer_name_casefold_exact": bool(
            employer
            and employer[0]["name_text"].casefold()
            == "Wisconsin Early Autism Project".casefold()
        ),
        "employer_name_without_article_casefold_exact": bool(
            employer
            and employer[0]["name_text"].casefold().removeprefix("the ")
            == "Wisconsin Early Autism Project".casefold()
        ),
        "employment_count": len(employment),
        "employment_object_kind": (
            employment[0]["object"]["kind"] if employment else None
        ),
        "employment_object_ref": (
            employment[0]["object"].get("entity_ref")
            if employment
            else None
        ),
        "temporal": (
            {
                key: employment[0]["temporal"][key]
                for key in (
                    "semantic",
                    "shape",
                    "basis",
                    "source_form",
                    "certainty",
                    "precision",
                    "anchored_to_source_time",
                )
            }
            if employment
            else None
        ),
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
