from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from rag_engine.memory_v1_contextual_span_splitter_v2 import (
    SPLITTER_VERSION,
    contextual_span_plan_v2,
)
from scripts.memory_v1_predicate_runtime_profile_v2 import (
    load_runtime_profile_v2,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    SEMANTIC_V5_2_REGISTRY_VERSION,
    _compile_entity_links,
    _deterministic_policy_packet,
    _example_entity,
    _example_observation,
    _literal,
    _packet,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
)


ROOT = Path(__file__).resolve().parents[1]
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
FOREIGN_OWNER = "557ea042-cb82-48f8-9429-472e96c957ef"
CLONE_DATABASE = "memory_pet_context_clone_20260729"
REVIEW_ROOT = Path(
    "/home/ubuntu/memory-v1-reviews/"
    "exact23-route-20260729T011706Z"
)
EVIDENCE_IDS = {
    "bella_beauty": "1f881f1c-1494-50da-8b87-edb547694f5e",
    "max": "3a480d24-fa2d-53f6-8e30-2be230e59ea8",
    "keasha": "740c3437-8df3-5628-8797-18dedcd36659",
    "neko": "b0473066-9d37-5d88-8bfd-437eea7ce264",
    "eric": "d6011472-0a2b-5997-8646-a3e9d42e2d7a",
    "ambiguous_breeding": "e0957d2a-23f3-5e95-8664-f7cccad65f10",
}
PARENT_IDS = (
    "78fc7204-d211-4206-8570-9d3efacd6654",
    "8f986090-19db-45a4-96ca-c0ec3ec5c6c0",
)
PROTECTED_TABLES = (
    "claim",
    "claim_evidence",
    "claim_observation",
    "claim_revision",
    "entity",
    "entity_alias",
    "entity_mention",
    "evidence",
    "evidence_contextual_span_v2",
    "evidence_extraction_job",
    "evidence_extraction_packet_v5",
    "evidence_extraction_packet_v5_local",
    "observation",
    "observation_entity_binding",
    "projection_claim_payload",
    "projection_outbox",
    "v5_2_local_packet_route_event",
    "v5_local_packet_disposition",
    "v5_local_packet_review_artifact",
    "v5_local_packet_stage_admission",
)


def clone_dsn(source_dsn: str) -> str:
    parsed = urlsplit(source_dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("POSTGRES_DSN is not PostgreSQL")
    if parsed.path.lstrip("/") == CLONE_DATABASE:
        return source_dsn
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{CLONE_DATABASE}",
            parsed.query,
            parsed.fragment,
        )
    )


def sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def owner_counts(conn: asyncpg.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in PROTECTED_TABLES:
        if not re.fullmatch(r"[a-z0-9_]+", table):
            raise RuntimeError("unsafe table identifier")
        visible = bool(
            await conn.fetchval(
                "SELECT has_table_privilege("
                "current_user, $1, 'SELECT')",
                f"memory.{table}",
            )
        )
        if not visible:
            continue
        counts[table] = int(
            await conn.fetchval(
                f"SELECT count(*) FROM memory.{table}"
            )
        )
    required = {
        "claim",
        "entity",
        "evidence",
        "evidence_extraction_job",
    }
    if not required.issubset(counts):
        missing = sorted(required - set(counts))
        raise RuntimeError(
            "restricted clone role lacks required protected reads: "
            f"{missing}"
        )
    return counts


def packet_by_evidence() -> dict[str, dict]:
    result: dict[str, dict] = {}
    for path in REVIEW_ROOT.glob("*-stage.json"):
        artifact = json.loads(path.read_text(encoding="utf-8"))
        evidence_id = artifact["evidence_id"]
        if evidence_id in EVIDENCE_IDS.values():
            result[evidence_id] = json.loads(
                artifact["extraction_packet_text"]
            )
    if set(result) != set(EVIDENCE_IDS.values()):
        missing = sorted(set(EVIDENCE_IDS.values()) - set(result))
        raise RuntimeError(f"missing exact packet artifacts: {missing}")
    return result


def compiled_summary(
    source: TrustedExtractionSource,
    raw_packet: dict,
    registry: dict,
) -> dict:
    packet = ProviderPacket.model_validate(raw_packet)
    compiled, repairs = _compile_entity_links(
        source,
        packet,
        registry,
    )
    value = compiled.model_dump(mode="json")
    return {
        "entities": sorted(
            (
                item["entity_ref"],
                item["entity_type"],
                item["name_text"],
            )
            for item in value["entity_mentions"]
        ),
        "observations": sorted(
            (
                item["predicate"],
                item["subject_entity_ref"],
                (
                    item["object"].get("value")
                    if item["object"]["kind"] == "literal"
                    else item["object"].get("entity_ref")
                ),
                item["temporal"]["shape"],
            )
            for item in value["observations"]
        ),
        "deferrals": sorted(
            item["reason_code"] for item in value["deferrals"]
        ),
        "repairs": sorted(repairs),
    }


def pet_provider_fixture(
    source: TrustedExtractionSource,
    *,
    wrong_breed: str | None,
    wrong_species: str,
) -> dict:
    content = source.content
    self_entity = _example_entity(
        content,
        entity_ref="e00",
        entity_type="self",
        mention_kind="self_reference",
        name_text=None,
        relationship_role="user:self",
        reason_code="explicit_self_reference",
    )
    animal_entity = _example_entity(
        content,
        entity_ref="e01",
        entity_type="animal",
        mention_kind="role_only",
        name_text=None,
        relationship_role="pet:reported",
        reason_code="reported_pet",
    )
    observations = [
        _example_observation(
            content,
            observation_ref="o00",
            subject_entity_ref="e00",
            predicate="relationship.has_pet",
            object_value={"kind": "entity", "entity_ref": "e01"},
            projection_class="direct_claim",
            surface_policy="direct_or_relevant",
            sensitivity="low",
            reason_code="reported_pet_relationship",
            temporal_semantic="state_validity",
        ),
    ]
    if wrong_breed is not None:
        observations.append(
            _example_observation(
                content,
                observation_ref="o01",
                subject_entity_ref="e01",
                predicate="pet.breed",
                object_value=_literal("text", wrong_breed),
                projection_class="direct_claim",
                surface_policy="direct_or_relevant",
                sensitivity="low",
                reason_code="reported_pet_breed",
            )
        )
    observations.append(
        _example_observation(
            content,
            observation_ref=f"o{len(observations):02d}",
            subject_entity_ref="e01",
            predicate="pet.species",
            object_value=_literal("text", wrong_species),
            projection_class="direct_claim",
            surface_policy="direct_or_relevant",
            sensitivity="low",
            reason_code="reported_pet_species",
        )
    )
    return _packet(
        entities=[self_entity, animal_entity],
        observations=observations,
    )


def self_name_provider_fixture(
    source: TrustedExtractionSource,
) -> dict:
    content = source.content
    return _packet(
        entities=[
            _example_entity(
                content,
                entity_ref="e00",
                entity_type="self",
                mention_kind="self_reference",
                name_text=None,
                relationship_role="user:self",
                reason_code="explicit_self_reference",
            )
        ],
        observations=[
            _example_observation(
                content,
                observation_ref="o00",
                subject_entity_ref="e00",
                predicate="identity.name",
                object_value=_literal("text", "Eric Lund"),
                projection_class="direct_claim",
                surface_policy="direct_or_relevant",
                sensitivity="medium",
                reason_code="explicit_name_statement",
            )
        ],
    )


def assert_pet(
    summary: dict,
    *,
    name: str,
    breed: str | None,
    species: str,
) -> None:
    entity_keys = {
        entity_key
        for entity_key, entity_type, canonical_name in summary["entities"]
        if entity_type == "animal" and canonical_name == name
    }
    if len(entity_keys) != 1:
        raise AssertionError(
            f"{name}: expected one canonical animal entity"
        )
    entity_key = next(iter(entity_keys))
    self_keys = {
        entity_key
        for entity_key, entity_type, _ in summary["entities"]
        if entity_type == "self"
    }
    if len(self_keys) != 1:
        raise AssertionError(f"{name}: expected one self entity")
    self_key = next(iter(self_keys))
    observations = {
        (predicate, subject_ref, object_value)
        for predicate, subject_ref, object_value, _ in summary[
            "observations"
        ]
    }
    required = {
        ("identity.name", entity_key, name),
        ("pet.species", entity_key, species),
        ("relationship.has_pet", self_key, entity_key),
    }
    if breed is not None:
        required.add(("pet.breed", entity_key, breed))
    missing = required - observations
    if missing:
        raise AssertionError(f"{name}: missing observations {missing}")
    invalid_values = {
        (predicate, object_value)
        for predicate, _, object_value in observations
        if predicate in {"pet.breed", "pet.species"}
        and object_value == name
    }
    if invalid_values:
        raise AssertionError(
            f"{name}: name remains in taxonomy {invalid_values}"
        )


async def run() -> None:
    source_dsn = os.environ.get("POSTGRES_DSN")
    if not source_dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    dsn = clone_dsn(source_dsn)
    conn = await asyncpg.connect(
        dsn,
        server_settings={
            "application_name": "memory_pet_context_clone_verify",
        },
    )
    transaction = conn.transaction(readonly=True)
    await transaction.start()
    try:
        database = await conn.fetchval("SELECT current_database()")
        if database != CLONE_DATABASE:
            raise RuntimeError("verification is not using disposable clone")
        await conn.execute(
            "SELECT set_config('app.user_id', $1, false)",
            OWNER,
        )
        before = await owner_counts(conn)
        rows = await conn.fetch(
            """
            SELECT evidence_id::text, source_system, content,
                   content_sha256, recorded_at
              FROM memory.evidence
             WHERE owner_user_id = $1::uuid
               AND evidence_id = ANY($2::uuid[])
            """,
            OWNER,
            list(EVIDENCE_IDS.values()),
        )
        if len(rows) != 6:
            raise AssertionError(
                f"expected six owner rows, found {len(rows)}"
            )
        sources: dict[str, TrustedExtractionSource] = {}
        for row in rows:
            sources[row["evidence_id"]] = TrustedExtractionSource.create(
                job_id="00000000-0000-4000-8000-000000000001",
                source_system=row["source_system"],
                source_external_id=row["evidence_id"],
                source_sha256=row["content_sha256"],
                source_recorded_at=row["recorded_at"].isoformat(),
                content=row["content"],
            )
        await conn.execute(
            "SELECT set_config('app.user_id', $1, false)",
            FOREIGN_OWNER,
        )
        foreign_visible = int(
            await conn.fetchval(
                """
                SELECT count(*)
                  FROM memory.evidence
                 WHERE evidence_id = ANY($1::uuid[])
                """,
                list(EVIDENCE_IDS.values()),
            )
        )
        if foreign_visible != 0:
            raise AssertionError("cross-owner evidence became visible")
        await conn.execute(
            "SELECT set_config('app.user_id', $1, false)",
            OWNER,
        )
        parent_rows = await conn.fetch(
            """
            SELECT evidence_id::text, content
              FROM memory.evidence
             WHERE owner_user_id = $1::uuid
               AND evidence_id = ANY($2::uuid[])
             ORDER BY evidence_id
            """,
            OWNER,
            list(PARENT_IDS),
        )
        if len(parent_rows) != 2:
            raise AssertionError("expected two exact parent records")
        plans = {
            row["evidence_id"]: contextual_span_plan_v2(row["content"])
            for row in parent_rows
        }
        plan_counts = {
            evidence_id: len(plan)
            for evidence_id, plan in plans.items()
        }
        if sorted(plan_counts.values()) != [2, 20]:
            raise AssertionError(
                f"unexpected contextual split counts: {plan_counts}"
            )
        coalesced = [
            span
            for plan in plans.values()
            for span in plan
            if span["content"].count("Bella") == 1
            and span["content"].count("Beauty") == 1
        ]
        if len(coalesced) != 1:
            raise AssertionError(
                "Bella and Beauty did not coalesce into one source span"
            )
        if coalesced[0]["context_needed"]:
            raise AssertionError(
                "coalesced Bella and Beauty span still needs context"
            )

        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(
            profile.registry_path.read_text(encoding="utf-8")
        )
        artifacts = packet_by_evidence()
        artifact_set_sha256 = sha256_json(artifacts)
        provider_fixtures = {
            "max": pet_provider_fixture(
                sources[EVIDENCE_IDS["max"]],
                wrong_breed="Max",
                wrong_species="German Shepherd",
            ),
            "keasha": pet_provider_fixture(
                sources[EVIDENCE_IDS["keasha"]],
                wrong_breed="Keasha von Steffen Haus",
                wrong_species="German Shepherd",
            ),
            "neko": pet_provider_fixture(
                sources[EVIDENCE_IDS["neko"]],
                wrong_breed=None,
                wrong_species="Neko",
            ),
            "eric": self_name_provider_fixture(
                sources[EVIDENCE_IDS["eric"]]
            ),
        }
        summaries = {
            name: compiled_summary(
                sources[EVIDENCE_IDS[name]],
                raw_packet,
                registry,
            )
            for name, raw_packet in provider_fixtures.items()
        }
        assert_pet(
            summaries["max"],
            name="Max",
            breed="German Shepherd",
            species="dog",
        )
        assert_pet(
            summaries["keasha"],
            name="Keasha von Steffen Haus",
            breed="German Shepherd",
            species="dog",
        )
        assert_pet(
            summaries["neko"],
            name="Neko",
            breed=None,
            species="cat",
        )
        eric_observations = {
            (predicate, subject_ref, object_value)
            for predicate, subject_ref, object_value, _ in summaries[
                "eric"
            ]["observations"]
        }
        eric_self_refs = {
            entity_ref
            for entity_ref, entity_type, _ in summaries["eric"][
                "entities"
            ]
            if entity_type == "self"
        }
        if len(eric_self_refs) != 1:
            raise AssertionError("self name packet lost its self entity")
        if (
            "identity.name",
            next(iter(eric_self_refs)),
            "Eric Lund",
        ) not in eric_observations:
            raise AssertionError("self name packet changed unexpectedly")

        ambiguous = _deterministic_policy_packet(
            sources[EVIDENCE_IDS["ambiguous_breeding"]],
            registry_version=SEMANTIC_V5_2_REGISTRY_VERSION,
        )
        if ambiguous is None:
            raise AssertionError("ambiguous transcript was not deferred")
        ambiguous_packet, guard_code = ambiguous
        ambiguous_value = ambiguous_packet.model_dump(mode="json")
        if guard_code != "ambiguous_breeding_transcription":
            raise AssertionError(f"unexpected guard {guard_code}")
        if ambiguous_value["observations"]:
            raise AssertionError(
                "ambiguous transcript produced observations"
            )
        if [
            item["reason_code"]
            for item in ambiguous_value["deferrals"]
        ] != ["ambiguous_transcription"]:
            raise AssertionError(
                "ambiguous transcript reason changed"
            )
        after = await owner_counts(conn)
        if before != after:
            raise AssertionError("protected clone rows changed")
        result = {
            "status": "PASS",
            "database": database,
            "splitter_version": SPLITTER_VERSION,
            "plan_counts": plan_counts,
            "coalesced_span_count": len(coalesced),
            "compiled_cases": {
                name: {
                    "entity_count": len(summary["entities"]),
                    "observation_count": len(
                        summary["observations"]
                    ),
                    "deferral_count": len(summary["deferrals"]),
                    "summary_sha256": sha256_json(summary),
                }
                for name, summary in summaries.items()
            },
            "ambiguous_guard": guard_code,
            "retained_artifact_set_sha256": artifact_set_sha256,
            "local_model_calls": 0,
            "external_model_calls": 0,
            "foreign_visible_rows": foreign_visible,
            "protected_counts_sha256_before": sha256_json(before),
            "protected_counts_sha256_after": sha256_json(after),
        }
        print(json.dumps(result, sort_keys=True))
    finally:
        await transaction.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())
