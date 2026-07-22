#!/usr/bin/env python3
"""Generate deterministic synthetic fixtures for the V5.2 stage-batch clone test."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import uuid


OWNER_A = "11111111-1111-4111-8111-111111111111"
OWNER_B = "22222222-2222-4222-8222-222222222222"
SELF_A = "a1111111-1111-4111-8111-111111111111"
CONFIRMATION = "STAGE_REVIEWED_OWNER_V5_2_PACKETS_ONLY"
ROOT = Path(__file__).resolve().parents[1]


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--review-root", required=True)
    authorize = subparsers.add_parser("authorize")
    authorize.add_argument("--plan", required=True)
    authorize.add_argument("--output", required=True)
    authorize.add_argument("--head", required=True)
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def secure_write(path: Path, value: dict[str, Any]) -> str:
    if value.get("contract_version") == "memory_v1_v5_2_stage_preflight_v1":
        value = dict(value)
        report_path = path.with_name(f"{path.stem}-source-review.json")
        report_sha = secure_write(
            report_path,
            {
                "contract_version": "memory_v1_v5_2_stage_fixture_review_v1",
                "case_id": value["case_id"],
            },
        )
        value["source_report"] = {
            "path": str(report_path),
            "sha256": report_sha,
        }
        value["schemas"] = {
            "extraction_sha256": digest(
                (ROOT / "specs/memory_v1_relational_extraction_v5_2.schema.json").read_bytes()
            ),
            "resolution_sha256": digest(
                (ROOT / "specs/memory_v1_entity_resolution_review_v5_2.schema.json").read_bytes()
            ),
        }
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
    return digest(raw)


def source(
    job_id: str, source_id: str, source_sha: str, recorded_at: str
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "source_system": "public.chat_log",
        "source_external_id": source_id,
        "source_sha256": source_sha,
        "source_recorded_at": recorded_at,
    }


def resolution_packet(
    source_value: dict[str, Any], resolutions: list[dict[str, Any]]
) -> dict[str, Any]:
    body = {
        "contract_version": "memory_v1_entity_resolution_review_v5_2",
        "source_envelope": source_value,
        "predicate_registry_version": "memory_predicate_registry_v5_2",
        "entity_normalization_version": "memory_entity_normalization_v5",
        "resolver": "memory_v1_stage_batch_clone_fixture",
        "resolver_version": "v5_2",
        "resolutions": resolutions,
    }
    return {**body, "packet_sha256": digest(stable_json(body))}


def stage_bundle(
    *,
    owner: str,
    case_id: str,
    evidence_id: str,
    request_id: str,
    extraction: dict[str, Any],
    resolution: dict[str, Any],
    resolution_summary: dict[str, int],
) -> dict[str, Any]:
    extraction_text = stable_json(extraction)
    resolution_text = stable_json(resolution)
    return {
        "contract_version": "memory_v1_v5_2_stage_preflight_v1",
        "generated_at": "2026-07-16T12:05:00Z",
        "mode": "preflight_only_zero_write",
        "server": "seebx",
        "owner_user_id": owner,
        "case_id": case_id,
        "evidence_id": evidence_id,
        "request_id": request_id,
        "extractor": "memory_v1_stage_batch_clone_fixture",
        "extractor_version": "v5_2",
        "source_report": {
            "path": "/synthetic/clone-fixture.json",
            "sha256": "1" * 64,
        },
        "schemas": {
            "extraction_sha256": "2" * 64,
            "resolution_sha256": "3" * 64,
        },
        "extraction_packet_text": extraction_text,
        "resolution_packet_text": resolution_text,
        "extraction_packet_sha256": digest(extraction_text),
        "resolution_packet_sha256": digest(resolution_text),
        "resolution_summary": resolution_summary,
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "authorized_stage": False,
    }


def empty_bundle(
    owner: str,
    *,
    case_id: str,
    evidence_id: str,
    request_id: str,
    source_id: str,
    source_sha: str,
    recorded_at: str,
) -> dict[str, Any]:
    source_value = source(case_id, source_id, source_sha, recorded_at)
    extraction = {
        "contract_version": "memory_v1_relational_extraction_v5_2",
        "source_envelope": source_value,
        "predicate_registry_version": "memory_predicate_registry_v5_2",
        "entity_mentions": [],
        "observations": [],
        "comparison_hints": [],
        "deferrals": [],
        "packet_findings": [],
    }
    return stage_bundle(
        owner=owner,
        case_id=case_id,
        evidence_id=evidence_id,
        request_id=request_id,
        extraction=extraction,
        resolution=resolution_packet(source_value, []),
        resolution_summary={
            "auto_link_eligible": 0,
            "manual_review_required": 0,
            "deferred": 0,
            "rejected": 0,
        },
    )


def fact_bundle() -> dict[str, Any]:
    source_value = source(
        "clone-owner-a-name",
        "aeeeeeee-1111-4111-8111-111111111112",
        "3f55b194db3ae03b7100b9e8f572ab254f43bed87b151c3a5e0015163374d2aa",
        "2026-07-16T12:01:00Z",
    )
    mention = {
        "entity_ref": "e01",
        "entity_type": "self",
        "mention_kind": "self_reference",
        "name_text": None,
        "relationship_role": None,
        "source_spans": [
            {"start": 0, "end": 2, "span_sha256": digest("My")}
        ],
        "extraction_confidence": 1.0,
        "reason_codes": [],
    }
    observation = {
        "observation_ref": "o01",
        "subject_entity_ref": "e01",
        "predicate": "identity.name",
        "predicate_registry_status": "governed",
        "object": {
            "kind": "literal",
            "datatype": "text",
            "value": "Avery",
            "unit": None,
            "approximate": False,
        },
        "polarity": "affirmed",
        "modality": "asserted",
        "projection_class": "direct_claim",
        "surface_policy": "direct_or_relevant",
        "temporal": {
            "semantic": "none",
            "shape": "none",
            "basis": "none",
            "source_form": "none",
            "certainty": "unknown",
            "precision": "unknown",
            "instant": None,
            "calendar_range": None,
            "instant_range": None,
            "relative_offset": None,
            "recurrence": None,
            "anchored_to_source_time": False,
            "normalization_policy_version": "memory_temporal_normalization_v5",
            "reason_codes": [],
        },
        "project_scope": {
            "state": "not_applicable",
            "project_key": None,
            "component_key": None,
            "binding_source": "not_applicable",
        },
        "sensitivity": "low",
        "extraction_confidence": 0.99,
        "source_spans": [
            {
                "start": 0,
                "end": 17,
                "span_sha256": digest("My name is Avery."),
            }
        ],
        "reason_codes": [],
    }
    extraction = {
        "contract_version": "memory_v1_relational_extraction_v5_2",
        "source_envelope": source_value,
        "predicate_registry_version": "memory_predicate_registry_v5_2",
        "entity_mentions": [mention],
        "observations": [observation],
        "comparison_hints": [],
        "deferrals": [],
        "packet_findings": [],
    }
    candidate_set = [
        {
            "entity_id": SELF_A,
            "entity_type": "self",
            "features": {
                "active_status": True,
                "entity_type_match": True,
                "exact_canonical_name": False,
                "exact_alias": False,
                "relationship_role_supported": False,
                "source_local_coreference": True,
                "graph_neighbor_supported": True,
                "conflicting_attribute_count": 0,
                "same_name_candidate_count": 1,
            },
            "exclusion_reasons": [],
        }
    ]
    decision = {
        "entity_ref": "e01",
        "mention_sha256": digest(stable_json(mention)),
        "action": "link_existing",
        "decision_state": "auto_link_eligible",
        "selected_entity_id": SELF_A,
        "proposed_entity": None,
        "candidate_set_sha256": digest(stable_json(candidate_set)),
        "candidate_set": candidate_set,
        "review_reason_codes": ["trusted_owner_self_binding"],
    }
    decision["decision_sha256"] = digest(stable_json(decision))
    return stage_bundle(
        owner=OWNER_A,
        case_id="clone-owner-a-name",
        evidence_id="aeeeeeee-1111-4111-8111-111111111112",
        request_id="10000000-0000-4000-8000-000000000002",
        extraction=extraction,
        resolution=resolution_packet(source_value, [decision]),
        resolution_summary={
            "auto_link_eligible": 1,
            "manual_review_required": 0,
            "deferred": 0,
            "rejected": 0,
        },
    )


def stance_bundle() -> dict[str, Any]:
    content = (
        "I think expert consensus should be treated as evidence, not absolute fact."
    )
    source_value = source(
        "clone-owner-a-stance",
        "aeeeeeee-1111-4111-8111-111111111115",
        digest(content),
        "2026-07-16T12:04:00Z",
    )
    mention = {
        "entity_ref": "e01",
        "entity_type": "self",
        "mention_kind": "self_reference",
        "name_text": None,
        "relationship_role": None,
        "source_spans": [
            {"start": 0, "end": 1, "span_sha256": digest("I")}
        ],
        "extraction_confidence": 1.0,
        "reason_codes": ["first_person_stance_holder"],
    }
    observation = {
        "observation_ref": "o01",
        "subject_entity_ref": "e01",
        "predicate": "stance.reported",
        "predicate_registry_status": "governed",
        "object": {
            "kind": "literal",
            "datatype": "text",
            "value": (
                "expert consensus should be treated as evidence, not absolute fact"
            ),
            "unit": None,
            "approximate": False,
        },
        "polarity": "affirmed",
        "modality": "reported_belief",
        "projection_class": "reported_stance",
        "surface_policy": "relevant_recall_or_explicit_recall",
        "temporal": {
            "semantic": "none",
            "shape": "none",
            "basis": "none",
            "source_form": "none",
            "certainty": "unknown",
            "precision": "unknown",
            "instant": None,
            "calendar_range": None,
            "instant_range": None,
            "relative_offset": None,
            "recurrence": None,
            "anchored_to_source_time": False,
            "normalization_policy_version": "memory_temporal_normalization_v5",
            "reason_codes": [],
        },
        "project_scope": {
            "state": "not_applicable",
            "project_key": None,
            "component_key": None,
            "binding_source": "not_applicable",
        },
        "sensitivity": "medium",
        "extraction_confidence": 0.99,
        "source_spans": [
            {"start": 0, "end": len(content), "span_sha256": digest(content)}
        ],
        "reason_codes": ["explicit_first_person_stance"],
    }
    extraction = {
        "contract_version": "memory_v1_relational_extraction_v5_2",
        "source_envelope": source_value,
        "predicate_registry_version": "memory_predicate_registry_v5_2",
        "entity_mentions": [mention],
        "observations": [observation],
        "comparison_hints": [],
        "deferrals": [],
        "packet_findings": [],
    }
    candidate_set = [
        {
            "entity_id": SELF_A,
            "entity_type": "self",
            "features": {
                "active_status": True,
                "entity_type_match": True,
                "exact_canonical_name": False,
                "exact_alias": False,
                "relationship_role_supported": False,
                "source_local_coreference": True,
                "graph_neighbor_supported": True,
                "conflicting_attribute_count": 0,
                "same_name_candidate_count": 1,
            },
            "exclusion_reasons": [],
        }
    ]
    decision = {
        "entity_ref": "e01",
        "mention_sha256": digest(stable_json(mention)),
        "action": "link_existing",
        "decision_state": "auto_link_eligible",
        "selected_entity_id": SELF_A,
        "proposed_entity": None,
        "candidate_set_sha256": digest(stable_json(candidate_set)),
        "candidate_set": candidate_set,
        "review_reason_codes": ["trusted_owner_self_binding"],
    }
    decision["decision_sha256"] = digest(stable_json(decision))
    return stage_bundle(
        owner=OWNER_A,
        case_id="clone-owner-a-stance",
        evidence_id="aeeeeeee-1111-4111-8111-111111111115",
        request_id="10000000-0000-4000-8000-000000000005",
        extraction=extraction,
        resolution=resolution_packet(source_value, [decision]),
        resolution_summary={
            "auto_link_eligible": 1,
            "manual_review_required": 0,
            "deferred": 0,
            "rejected": 0,
        },
    )


def project_component_bundle(
    *,
    case_id: str,
    evidence_id: str,
    request_id: str,
    source_id: str,
    recorded_at: str,
    component_name: str,
    component_key: str,
    content: str,
) -> dict[str, Any]:
    source_value = source(
        case_id,
        source_id,
        digest(content),
        recorded_at,
    )
    mention = {
        "entity_ref": "e01",
        "entity_type": "project",
        "mention_kind": "named",
        "name_text": component_name,
        "relationship_role": None,
        "source_spans": [
            {
                "start": 0,
                "end": len(component_name),
                "span_sha256": digest(component_name),
            }
        ],
        "extraction_confidence": 1.0,
        "reason_codes": ["explicit_registered_component"],
    }
    observation = {
        "observation_ref": "o01",
        "subject_entity_ref": "e01",
        "predicate": "project.requirement",
        "predicate_registry_status": "governed",
        "object": {
            "kind": "literal",
            "datatype": "text",
            "value": "preserve component scope",
            "unit": None,
            "approximate": False,
        },
        "polarity": "affirmed",
        "modality": "asserted",
        "projection_class": "project_knowledge",
        "surface_policy": "exact_project_scope_only",
        "temporal": {
            "semantic": "observation_time",
            "shape": "instant",
            "basis": "instant",
            "source_form": "implicit_source_time",
            "certainty": "exact",
            "precision": "minute",
            "instant": recorded_at,
            "calendar_range": None,
            "instant_range": None,
            "relative_offset": None,
            "recurrence": None,
            "anchored_to_source_time": True,
            "normalization_policy_version": "memory_temporal_normalization_v5",
            "reason_codes": ["observation_time_uses_source_timestamp"],
        },
        "project_scope": {
            "state": "resolved",
            "project_key": "verbal-sage",
            "component_key": component_key,
            "binding_source": "trusted_component_registry",
        },
        "sensitivity": "medium",
        "extraction_confidence": 0.99,
        "source_spans": [
            {"start": 0, "end": len(content), "span_sha256": digest(content)}
        ],
        "reason_codes": ["explicit_component_requirement"],
    }
    extraction = {
        "contract_version": "memory_v1_relational_extraction_v5_2",
        "source_envelope": source_value,
        "predicate_registry_version": "memory_predicate_registry_v5_2",
        "entity_mentions": [mention],
        "observations": [observation],
        "comparison_hints": [],
        "deferrals": [],
        "packet_findings": ["synthetic_component_stage_test"],
    }
    candidate_set: list[dict[str, Any]] = []
    decision = {
        "entity_ref": "e01",
        "mention_sha256": digest(stable_json(mention)),
        "action": "defer",
        "decision_state": "deferred",
        "selected_entity_id": None,
        "proposed_entity": None,
        "candidate_set_sha256": digest(stable_json(candidate_set)),
        "candidate_set": candidate_set,
        "review_reason_codes": ["project_entity_resolution_deferred"],
    }
    decision["decision_sha256"] = digest(stable_json(decision))
    return stage_bundle(
        owner=OWNER_A,
        case_id=case_id,
        evidence_id=evidence_id,
        request_id=request_id,
        extraction=extraction,
        resolution=resolution_packet(source_value, [decision]),
        resolution_summary={
            "auto_link_eligible": 0,
            "manual_review_required": 0,
            "deferred": 1,
            "rejected": 0,
        },
    )


def bundle_spec(path: Path, bundle_sha: str, counts: dict[str, int]) -> dict:
    return {
        "path": str(path),
        "sha256": bundle_sha,
        "expected_outcome": "applied",
        "expected_counts": counts,
    }


def prepare(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=False)
    os.chmod(root, 0o700)
    empty_path = root / "owner-a-empty.json"
    empty_sha = secure_write(
        empty_path,
        empty_bundle(
            OWNER_A,
            case_id="clone-owner-a-empty",
            evidence_id="aeeeeeee-1111-4111-8111-111111111111",
            request_id="10000000-0000-4000-8000-000000000001",
            source_id="aaaaaaaa-0001-4000-8000-000000000001",
            source_sha=(
                "cd61bf0cc67fc2233111c91167e2abb3c652200bfe22059a166475d37366b7a8"
            ),
            recorded_at="2026-07-16T12:00:00Z",
        ),
    )
    fact_path = root / "owner-a-name.json"
    fact_sha = secure_write(fact_path, fact_bundle())
    stance_path = root / "owner-a-stance.json"
    stance_sha = secure_write(stance_path, stance_bundle())
    component_path = root / "owner-a-memory-component.json"
    component_sha = secure_write(
        component_path,
        project_component_bundle(
            case_id="clone-owner-a-memory-component",
            evidence_id="aeeeeeee-1111-4111-8111-111111111113",
            request_id="10000000-0000-4000-8000-000000000003",
            source_id="aaaaaaaa-0003-4000-8000-000000000003",
            recorded_at="2026-07-16T12:02:00Z",
            component_name="Memory V1",
            component_key="memory-v1",
            content="Memory V1 must preserve component scope.",
        ),
    )
    forged_component_path = root / "owner-a-forged-component.json"
    forged_component_sha = secure_write(
        forged_component_path,
        project_component_bundle(
            case_id="clone-owner-a-forged-component",
            evidence_id="aeeeeeee-1111-4111-8111-111111111114",
            request_id="10000000-0000-4000-8000-000000000004",
            source_id="aaaaaaaa-0004-4000-8000-000000000004",
            recorded_at="2026-07-16T12:03:00Z",
            component_name="Owner B Only",
            component_key="owner-b-only",
            content="Owner B Only must preserve component scope.",
        ),
    )
    other_path = root / "owner-b-empty.json"
    other_sha = secure_write(
        other_path,
        empty_bundle(
            OWNER_B,
            case_id="clone-owner-b-empty",
            evidence_id="beeeeeee-2222-4222-8222-222222222222",
            request_id="20000000-0000-4000-8000-000000000001",
            source_id="bbbbbbbb-0001-4000-8000-000000000001",
            source_sha=(
                "e284bbab043edef01158fbe82bc74e6887109c27bd6dfbc3eed2fb645a40217a"
            ),
            recorded_at="2026-07-16T12:02:00Z",
        ),
    )
    manifest = {
        "contract_version": "memory_v1_v5_2_stage_batch_manifest_v1",
        "target_server": "seebx",
        "owner_user_id": OWNER_A,
        "bundles": [
            bundle_spec(
                empty_path,
                empty_sha,
                {
                    "mentions": 0,
                    "resolutions": 0,
                    "candidates": 0,
                    "observations": 0,
                    "temporals": 0,
                },
            ),
            bundle_spec(
                fact_path,
                fact_sha,
                {
                    "mentions": 1,
                    "resolutions": 1,
                    "candidates": 1,
                    "observations": 1,
                    "temporals": 1,
                },
            ),
            bundle_spec(
                component_path,
                component_sha,
                {
                    "mentions": 1,
                    "resolutions": 1,
                    "candidates": 0,
                    "observations": 1,
                    "temporals": 1,
                },
            ),
            bundle_spec(
                stance_path,
                stance_sha,
                {
                    "mentions": 1,
                    "resolutions": 1,
                    "candidates": 1,
                    "observations": 1,
                    "temporals": 1,
                },
            ),
        ],
    }
    secure_write(root / "manifest.json", manifest)
    forged_component_manifest = {
        "contract_version": "memory_v1_v5_2_stage_batch_manifest_v1",
        "target_server": "seebx",
        "owner_user_id": OWNER_A,
        "bundles": [
            bundle_spec(
                forged_component_path,
                forged_component_sha,
                {
                    "mentions": 1,
                    "resolutions": 1,
                    "candidates": 0,
                    "observations": 1,
                    "temporals": 1,
                },
            )
        ],
    }
    secure_write(
        root / "forged-component-manifest.json",
        forged_component_manifest,
    )
    cross_owner = {
        "contract_version": "memory_v1_v5_2_stage_batch_manifest_v1",
        "target_server": "seebx",
        "owner_user_id": OWNER_A,
        "bundles": [
            bundle_spec(
                other_path,
                other_sha,
                {
                    "mentions": 0,
                    "resolutions": 0,
                    "candidates": 0,
                    "observations": 0,
                    "temporals": 0,
                },
            )
        ],
    }
    secure_write(root / "cross-owner-manifest.json", cross_owner)
    print(
        stable_json(
            {
                "review_root": str(root),
                "manifest": str(root / "manifest.json"),
                "cross_owner_manifest": str(root / "cross-owner-manifest.json"),
                "forged_component_manifest": str(
                    root / "forged-component-manifest.json"
                ),
            }
        )
    )


def authorize(plan_path: Path, output: Path, head: str) -> None:
    plan_raw = plan_path.read_bytes()
    plan = json.loads(plan_raw)
    if plan["required_head_commit"] != head:
        raise RuntimeError("plan and requested authorization head differ")
    now = dt.datetime.now(dt.timezone.utc)
    value = {
        "contract_version": "memory_v1_v5_2_stage_batch_authorization_v1",
        "authorization_id": str(
            uuid.uuid5(
                uuid.UUID("a0000000-0000-5000-8000-000000000001"),
                digest(plan_raw),
            )
        ),
        "authorized": True,
        "authorized_by": "Eric Lund",
        "authorized_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(minutes=20)).isoformat(),
        "expected_head_commit": head,
        "target_server": "seebx",
        "scope": "stage_reviewed_owner_v5_2_packets_only",
        "owner_user_id": plan["owner_user_id"],
        "plan_sha256": digest(plan_raw),
        "expected_bundle_count": plan["bundle_count"],
        "expected_new_rows": plan["expected_new_rows"],
        "confirmation": CONFIRMATION,
    }
    secure_write(output, value)
    print(stable_json({"authorization": str(output), "plan_sha256": digest(plan_raw)}))


def main() -> int:
    args = arguments()
    if args.command == "prepare":
        prepare(Path(args.review_root).resolve())
    else:
        authorize(
            Path(args.plan).resolve(),
            Path(args.output).resolve(),
            args.head,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
