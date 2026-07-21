#!/usr/bin/env python3
from __future__ import annotations

"""Compile the pinned Fractal Monism v0.2 semantic package for runtime use.

This is an offline build tool. PyYAML is intentionally imported only here; the
generated JSON and ``rag_engine.fm_runtime_bundle_v0_2`` need only the Python
standard library and Pydantic.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any, Iterable, Mapping


BUNDLE_CONTRACT_VERSION = "fm_runtime_bundle_v0_2"
RUNTIME_REFERENCE = "fm_v0_2"
SEMANTIC_VERSION = "0.2"
CANONICAL_MANIFEST_ID = "fm-canonical-v0.2-manifest"
EXPECTED_MANIFEST_SHA256 = (
    "1d2912854b368f2a802752ad0c2d1a37a09f700c925e7beb842724e47acf627d"
)
EXPECTED_SOURCE_SHA256: dict[str, str] = {
    "semantic_model/FM_AUTHOR_QUESTIONS_v0_2.md": (
        "bd286edb3e042be18d574bed82d64120893c75b08edfcd96850b98d43d33cf2f"
    ),
    "semantic_model/FM_CANONICAL_V0_2_HANDOFF.md": (
        "6daa0e73e38b596c580f8be2480ad3e061a898ce60fbf8c48db972035f3743dc"
    ),
    "semantic_model/FM_CONCEPT_SCHEMA_v0_2.yaml": (
        "5d8674127a3902d91fd2bb5d3ef43e2b475d476fe1a1bd6e97c3c84a304808b0"
    ),
    "semantic_model/FM_CORE_MODEL_v0_2.yaml": (
        "4efa55403f474a837edc0d9d477c825cdf176293fe198f376326166416c75f0f"
    ),
    "semantic_model/FM_INFERENCE_RULES_v0_2.yaml": (
        "9b02dd60f366230c72f27948c5ca633c5294d00e10f235fe586646a6a71e4de7"
    ),
    "semantic_model/FM_KERNEL_v0_2.md": (
        "c0db8af61d7ed531a35bdc09ebaa4704c7a161ce6fbed2686f60c1387ce383e2"
    ),
    "semantic_model/FM_REASONING_EVALS_v0_2.jsonl": (
        "1db912c8cd42d3fe37e0971ce302577a8c2369103a264f9ca07614ba53237979"
    ),
    "semantic_model/FM_TENSIONS_v0_2.md": (
        "b7037d0992b07b5d5646cca9aa47fb0f2ac64396c4b6d4b38d1b2dfe8de0e5f7"
    ),
    "semantic_model/FM_VALIDATION_v0_2.json": (
        "83d73d7702f8bf8c59d7368b2306fd85bc57560f6b834f85caa6c9003baf9f50"
    ),
}
EXPECTED_COUNTS = {
    "concepts": 46,
    "inference_applications": 27,
    "inference_rules": 27,
    "noncurrent_formulations": 22,
    "primary_core_records": 200,
    "reasoning_evaluations": 48,
    "relationships": 48,
    "tensions": 28,
}

ASSERTION_FIELDS = (
    "id",
    "statement",
    "authorship_status",
    "representation",
    "claim_mode",
    "epistemic_status",
    "framework_status",
    "external_status",
    "confidence",
    "author_review_required",
    "provenance",
)
RULE_FIELDS = (
    "id",
    "name",
    "source_premises",
    "inference_rule",
    "derived_conclusion",
    "epistemic_status",
    "confidence",
    "possible_competing_interpretation",
    "author_review_required",
    "provenance",
)
APPLICATION_FIELDS = tuple(field for field in RULE_FIELDS if field != "name")


class CompileError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CompileError(message)


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised by build hosts
        raise CompileError(
            "PyYAML is required only to run the offline FM bundle compiler"
        ) from exc
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _source_path(source_dir: Path, manifest_path: str) -> Path:
    prefix = "semantic_model/"
    _require(manifest_path.startswith(prefix), f"unexpected source path: {manifest_path}")
    root = source_dir.resolve()
    candidate = (root / manifest_path[len(prefix) :]).resolve()
    _require(candidate.is_relative_to(root), f"source path escapes source directory: {manifest_path}")
    return candidate


def _verify_sources(source_dir: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    manifest_path = source_dir / "FM_CANONICAL_V0_2_MANIFEST.yaml"
    _require(manifest_path.is_file(), f"missing canonical manifest: {manifest_path}")
    manifest_bytes = manifest_path.read_bytes()
    actual_manifest_hash = _sha256_bytes(manifest_bytes)
    _require(
        actual_manifest_hash == EXPECTED_MANIFEST_SHA256,
        "canonical manifest SHA-256 mismatch: "
        f"expected {EXPECTED_MANIFEST_SHA256}, got {actual_manifest_hash}",
    )
    document = _load_yaml(manifest_path)
    manifest = document.get("manifest") if isinstance(document, Mapping) else None
    _require(isinstance(manifest, Mapping), "manifest document has no manifest object")
    _require(manifest.get("id") == CANONICAL_MANIFEST_ID, "wrong canonical manifest id")
    _require(manifest.get("status") == "author_approved_canonical", "manifest is not canonical")
    compatibility = manifest.get("compatibility_policy")
    _require(isinstance(compatibility, Mapping), "missing compatibility policy")
    _require(
        compatibility.get("runtime_reference") == RUNTIME_REFERENCE,
        "wrong fixed runtime reference",
    )
    _require(
        compatibility.get("floating_latest_reference_permitted") is False,
        "floating latest reference must remain prohibited",
    )
    _require(dict(manifest.get("counts") or {}) == EXPECTED_COUNTS, "canonical counts changed")

    declared: dict[str, str] = {}
    for item in manifest.get("semantic_artifacts") or ():
        declared[str(item["path"])] = str(item["sha256"])
    for key in ("integration_specification", "validation"):
        item = manifest.get(key)
        _require(isinstance(item, Mapping), f"missing manifest {key}")
        declared[str(item["path"])] = str(item["sha256"])
    _require(declared == EXPECTED_SOURCE_SHA256, "manifest artifact set or hashes changed")

    paths: dict[str, Path] = {}
    for relative_path, expected_hash in EXPECTED_SOURCE_SHA256.items():
        path = _source_path(source_dir, relative_path)
        _require(path.is_file(), f"missing canonical artifact: {relative_path}")
        actual_hash = _sha256_bytes(path.read_bytes())
        _require(
            actual_hash == expected_hash,
            f"artifact SHA-256 mismatch for {relative_path}: "
            f"expected {expected_hash}, got {actual_hash}",
        )
        paths[relative_path] = path
    return dict(manifest), paths


def _expected_numbered_ids(prefix: str, count: int) -> set[str]:
    return {f"{prefix}{number:03d}" for number in range(1, count + 1)}


def _validate_assertion(assertion: Mapping[str, Any], owner_id: str) -> None:
    missing = set(ASSERTION_FIELDS) - set(assertion)
    _require(not missing, f"{owner_id} assertion missing fields: {sorted(missing)}")
    _require(assertion.get("author_review_required") is False, f"{owner_id} still needs review")
    _require(bool(str(assertion.get("statement") or "").strip()), f"{owner_id} has empty statement")
    _require(
        bool(str(assertion.get("competing_interpretation") or "").strip()),
        f"{owner_id} has no competing interpretation",
    )
    _require(isinstance(assertion.get("provenance"), list), f"{owner_id} provenance is not a list")
    _require(bool(assertion.get("provenance")), f"{owner_id} has no provenance")


def _validate_core(core: Mapping[str, Any]) -> None:
    model = core.get("model")
    _require(isinstance(model, Mapping), "core model metadata missing")
    _require(model.get("id") == "fm-core-model-v0.2", "wrong core model id")
    _require(model.get("version") == SEMANTIC_VERSION, "wrong core model version")
    _require(model.get("status") == "author_approved_canonical", "core model is not canonical")

    concepts = core.get("concepts")
    relationships = core.get("relationships")
    historical = core.get("noncurrent_formulations")
    _require(isinstance(concepts, list), "concepts must be a list")
    _require(isinstance(relationships, list), "relationships must be a list")
    _require(isinstance(historical, list), "noncurrent formulations must be a list")
    concept_ids = {str(item.get("id")) for item in concepts}
    relationship_ids = {str(item.get("id")) for item in relationships}
    historical_ids = {str(item.get("id")) for item in historical}
    _require(concept_ids == _expected_numbered_ids("FM-C-", 46), "concept ID set changed")
    _require(
        relationship_ids == _expected_numbered_ids("FM-R-", 48),
        "relationship ID set changed",
    )
    _require(
        historical_ids == _expected_numbered_ids("FM-HF-", 22),
        "historical formulation ID set changed",
    )

    for item in concepts:
        _validate_assertion(item["semantic_assertion"], str(item["id"]))
    for item in relationships:
        _validate_assertion(item["semantic_assertion"], str(item["id"]))
        _require(
            item.get("subject") in concept_ids | historical_ids,
            f"{item['id']} has unresolved subject",
        )
        _require(
            item.get("object") in concept_ids | historical_ids,
            f"{item['id']} has unresolved object",
        )
    for item in historical:
        _validate_assertion(item["semantic_assertion"], str(item["id"]))
        _require(
            item.get("status") in {"deprecated", "disputed", "excluded", "historical_only"},
            f"{item['id']} is not explicitly noncurrent",
        )
        replacements = item.get("superseded_by") or ()
        _require(bool(replacements), f"{item['id']} has no current replacement/boundary")
        _require(
            set(replacements) <= concept_ids | relationship_ids,
            f"{item['id']} has unresolved superseded_by references",
        )

    gate = core.get("application_gate")
    _require(isinstance(gate, Mapping), "application gate missing")
    _require(gate.get("id") == "FM-AG-001", "wrong application gate id")
    _require(gate.get("fm_influence") == "off_or_deferred", "application gate weakened")


def _validate_inference(inference: Mapping[str, Any], core: Mapping[str, Any]) -> None:
    metadata = inference.get("inference_model")
    _require(isinstance(metadata, Mapping), "inference model metadata missing")
    _require(metadata.get("id") == "fm-inference-rules-v0.2", "wrong inference model id")
    _require(metadata.get("version") == SEMANTIC_VERSION, "wrong inference version")
    _require(metadata.get("status") == "author_approved_canonical", "inference model not canonical")

    rules = inference.get("rules")
    applications = inference.get("applications")
    execution_order = inference.get("execution_order")
    _require(isinstance(rules, list), "rules must be a list")
    _require(isinstance(applications, list), "applications must be a list")
    _require(isinstance(execution_order, list), "execution order must be a list")
    rule_ids = {str(item.get("id")) for item in rules}
    application_ids = {str(item.get("id")) for item in applications}
    rule_numbers = {
        int(match.group(1))
        for value in rule_ids
        if (match := re.fullmatch(r"FM-IR-(\d{3})-[a-z0-9-]+", value))
    }
    _require(rule_numbers == set(range(1, 28)), "inference rule ID set changed")
    _require(application_ids == _expected_numbered_ids("FM-IA-", 27), "application ID set changed")
    _require(len(execution_order) == 27 and set(execution_order) == rule_ids, "invalid rule execution order")

    concept_ids = {str(item["id"]) for item in core["concepts"]}
    historical_ids = {str(item["id"]) for item in core["noncurrent_formulations"]}
    for item in rules:
        missing = set(RULE_FIELDS) - set(item)
        _require(not missing, f"{item['id']} missing rule fields: {sorted(missing)}")
        _require(item.get("author_review_required") is False, f"{item['id']} still needs review")
        _require(bool(item.get("source_premises")), f"{item['id']} has no premises")
        _require(bool(item.get("provenance")), f"{item['id']} has no provenance")
        _require(
            bool(str(item.get("possible_competing_interpretation") or "").strip()),
            f"{item['id']} has no competing interpretation",
        )
    for item in applications:
        missing = set(APPLICATION_FIELDS) - set(item)
        _require(not missing, f"{item['id']} missing application fields: {sorted(missing)}")
        _require(item.get("inference_rule") in rule_ids, f"{item['id']} has unresolved rule")
        _require(item.get("author_review_required") is False, f"{item['id']} still needs review")
        for premise in item.get("source_premises") or ():
            reference_keys = set(premise) & {"concept_id", "formulation_id"}
            _require(len(reference_keys) == 1, f"{item['id']} premise must have one record reference")
            reference_key = next(iter(reference_keys))
            reference_id = premise[reference_key]
            expected = concept_ids if reference_key == "concept_id" else historical_ids
            _require(reference_id in expected, f"{item['id']} has unresolved premise {reference_id}")


def _normalize_selection_text(parts: Iterable[Any]) -> str:
    text = " ".join(str(part) for part in parts if part is not None)
    text = unicodedata.normalize("NFKC", text).casefold().replace("_", " ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _provenance_ref(value: Mapping[str, Any]) -> str:
    return "sha256:" + _sha256_bytes(_canonical_json_bytes(dict(value)))


def _register_provenance(
    registry: dict[str, dict[str, Any]], values: Iterable[Mapping[str, Any]]
) -> list[str]:
    references: list[str] = []
    for value in values:
        normalized = dict(value)
        reference = _provenance_ref(normalized)
        previous = registry.setdefault(reference, normalized)
        _require(previous == normalized, f"provenance digest collision: {reference}")
        references.append(reference)
    return sorted(set(references))


def _runtime_assertion(
    assertion: Mapping[str, Any], registry: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    return {
        "assertion_id": assertion["id"],
        "statement": assertion["statement"],
        "authorship_status": assertion["authorship_status"],
        "representation": assertion["representation"],
        "claim_mode": assertion["claim_mode"],
        "epistemic_status": assertion["epistemic_status"],
        "framework_status": assertion["framework_status"],
        "external_status": assertion["external_status"],
        "confidence": assertion["confidence"],
        "author_review_required": assertion["author_review_required"],
        "competing_interpretation": assertion["competing_interpretation"],
        "provenance_refs": _register_provenance(registry, assertion["provenance"]),
    }


def _semantic_status_text(assertion: Mapping[str, Any]) -> str:
    return (
        f"claim_mode={assertion['claim_mode']}; "
        f"epistemic_status={assertion['epistemic_status']}; "
        f"framework_status={assertion['framework_status']}; "
        f"external_status={assertion['external_status']}"
    )


def _compile_core_records(
    core: Mapping[str, Any], registry: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    concept_labels = {str(item["id"]): str(item["label"]) for item in core["concepts"]}
    historical_labels = {
        str(item["id"]): str(item["formulation"])
        for item in core["noncurrent_formulations"]
    }
    endpoint_labels = concept_labels | historical_labels

    for item in core["concepts"]:
        assertion = item["semantic_assertion"]
        render_text = (
            f"[{item['id']}] {item['label']}. Definition: {item['definition']} "
            f"Canonical statement: {assertion['statement']} "
            f"Status: {_semantic_status_text(assertion)}. "
            f"Competing interpretation: {assertion['competing_interpretation']}"
        )
        records.append(
            {
                "kind": "concept",
                "id": item["id"],
                "inference_use": "premise_with_named_rule",
                "label": item["label"],
                "definition": item["definition"],
                "kernel_role": item.get("kernel_role"),
                "assertion": _runtime_assertion(assertion, registry),
                "selection_text": _normalize_selection_text(
                    (
                        item["label"],
                        item["definition"],
                        item.get("kernel_role"),
                        assertion["statement"],
                        assertion["competing_interpretation"],
                    )
                ),
                "render_text": render_text,
            }
        )

    for item in core["relationships"]:
        assertion = item["semantic_assertion"]
        subject_label = endpoint_labels[str(item["subject"])]
        object_label = endpoint_labels[str(item["object"])]
        render_text = (
            f"[{item['id']}] {item['subject']} ({subject_label}) "
            f"{item['predicate']} {item['object']} ({object_label}). "
            f"Canonical statement: {assertion['statement']} "
            f"Status: {_semantic_status_text(assertion)}. "
            f"Competing interpretation: {assertion['competing_interpretation']}"
        )
        records.append(
            {
                "kind": "relationship",
                "id": item["id"],
                "inference_use": "premise_with_named_rule",
                "subject_id": item["subject"],
                "predicate": item["predicate"],
                "object_id": item["object"],
                "assertion": _runtime_assertion(assertion, registry),
                "selection_text": _normalize_selection_text(
                    (
                        item["predicate"],
                        subject_label,
                        object_label,
                        assertion["statement"],
                        assertion["competing_interpretation"],
                    )
                ),
                "render_text": render_text,
            }
        )

    for item in core["noncurrent_formulations"]:
        assertion = item["semantic_assertion"]
        render_text = (
            f"[{item['id']}] NONCURRENT {item['status']}; this formulation is status-only "
            f"and cannot license current inference. Historical formulation: {item['formulation']} "
            f"Reason: {item['reason']} Current replacements or boundaries: "
            f"{', '.join(item.get('superseded_by') or ())}. "
            f"Competing interpretation: {assertion['competing_interpretation']}"
        )
        records.append(
            {
                "kind": "historical_formulation",
                "id": item["id"],
                "inference_use": "status_only",
                "formulation": item["formulation"],
                "status": item["status"],
                "reason": item["reason"],
                "superseded_by": list(item.get("superseded_by") or ()),
                "assertion": _runtime_assertion(assertion, registry),
                "selection_text": _normalize_selection_text(
                    (
                        item["formulation"],
                        item["status"],
                        item["reason"],
                        assertion["statement"],
                        assertion["competing_interpretation"],
                    )
                ),
                "render_text": render_text,
            }
        )
    return records


def _compile_inference_records(
    inference: Mapping[str, Any], registry: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in inference["rules"]:
        provenance_refs = _register_provenance(registry, item["provenance"])
        premises = [str(value) for value in item["source_premises"]]
        render_text = (
            f"[{item['id']}] {item['name']}. Source premises: {'; '.join(premises)}. "
            f"Inference rule: {item['inference_rule']} Derived conclusion: "
            f"{item['derived_conclusion']} Epistemic status: {item['epistemic_status']}. "
            f"Representation confidence: {item['confidence']}. Competing interpretation: "
            f"{item['possible_competing_interpretation']} Author review required: "
            f"{str(item['author_review_required']).lower()}."
        )
        records.append(
            {
                "kind": "inference_rule",
                "id": item["id"],
                "inference_use": "named_rule",
                "name": item["name"],
                "source_premises": premises,
                "inference_rule": item["inference_rule"],
                "derived_conclusion": item["derived_conclusion"],
                "epistemic_status": item["epistemic_status"],
                "confidence": item["confidence"],
                "possible_competing_interpretation": item[
                    "possible_competing_interpretation"
                ],
                "author_review_required": item["author_review_required"],
                "provenance_refs": provenance_refs,
                "selection_text": _normalize_selection_text(
                    (
                        item["name"],
                        *premises,
                        item["inference_rule"],
                        item["derived_conclusion"],
                        item["possible_competing_interpretation"],
                    )
                ),
                "render_text": render_text,
            }
        )

    for item in inference["applications"]:
        provenance_refs = _register_provenance(registry, item["provenance"])
        premises: list[dict[str, str]] = []
        for premise in item["source_premises"]:
            if "concept_id" in premise:
                record_id = str(premise["concept_id"])
                premise_use = "current_premise"
            else:
                record_id = str(premise["formulation_id"])
                premise_use = "noncurrent_status_only"
            premises.append(
                {
                    "record_id": record_id,
                    "premise": str(premise["premise"]),
                    "premise_use": premise_use,
                }
            )
        premise_text = "; ".join(
            f"{premise['record_id']} ({premise['premise_use']}): {premise['premise']}"
            for premise in premises
        )
        render_text = (
            f"[{item['id']}] Reviewed inference application. Source premises: {premise_text}. "
            f"Named rule: {item['inference_rule']}. Derived conclusion: "
            f"{item['derived_conclusion']} Epistemic status: {item['epistemic_status']}. "
            f"Representation confidence: {item['confidence']}. Competing interpretation: "
            f"{item['possible_competing_interpretation']} Author review required: "
            f"{str(item['author_review_required']).lower()}."
        )
        records.append(
            {
                "kind": "inference_application",
                "id": item["id"],
                "inference_use": "reviewed_application",
                "source_premises": premises,
                "inference_rule_id": item["inference_rule"],
                "derived_conclusion": item["derived_conclusion"],
                "epistemic_status": item["epistemic_status"],
                "confidence": item["confidence"],
                "possible_competing_interpretation": item[
                    "possible_competing_interpretation"
                ],
                "author_review_required": item["author_review_required"],
                "provenance_refs": provenance_refs,
                "selection_text": _normalize_selection_text(
                    (
                        *(premise["premise"] for premise in premises),
                        item["inference_rule"],
                        item["derived_conclusion"],
                        item["possible_competing_interpretation"],
                    )
                ),
                "render_text": render_text,
            }
        )
    return records


def _clean_tension_status(value: str) -> str:
    value = value.strip()
    if value.startswith("**"):
        value = value[2:]
    if value.endswith("**."):
        value = value[:-3]
    elif value.endswith("**"):
        value = value[:-2]
    return value.strip().rstrip(".")


def _parse_tensions(
    path: Path, registry: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    headings = list(re.finditer(r"^## (FM-T-\d{3}) — (.+)$", text, re.MULTILINE))
    _require(len(headings) == 28, f"expected 28 tensions, found {len(headings)}")
    records: list[dict[str, Any]] = []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[match.end() : end]
        fields: dict[str, list[str]] = {}
        current: str | None = None
        for line in block.splitlines():
            bullet = re.match(r"^- (Claim A|Claim B|Status|Boundary|Provenance):\s*(.*)$", line)
            if bullet:
                current = bullet.group(1)
                fields[current] = [bullet.group(2).strip()]
            elif current and line.startswith("  ") and line.strip():
                fields[current].append(line.strip())
            elif line.strip():
                current = None
        required = {"Claim A", "Claim B", "Status", "Provenance"}
        _require(required <= set(fields), f"{match.group(1)} has an invalid tension block")
        values = {key: " ".join(fields[key]).strip() for key in required}
        if "Boundary" in fields:
            values["Boundary"] = " ".join(fields["Boundary"]).strip()
        else:
            # FM-T-026 encodes its resolution directly in Status and has no
            # separate Boundary bullet. Preserve that exact text rather than
            # inventing a new semantic formulation.
            _require(match.group(1) == "FM-T-026", f"{match.group(1)} has no boundary")
            values["Boundary"] = _clean_tension_status(values["Status"])
        start_line = text.count("\n", 0, match.start()) + 1
        end_line = text.count("\n", 0, end) + 1
        provenance = {
            "source_kind": "tension_register",
            "path": "semantic_model/FM_TENSIONS_v0_2.md",
            "heading": f"{match.group(1)} — {match.group(2).strip()}",
            "lines": f"{start_line}-{end_line}",
            "support_role": "constrains",
            "citation": values["Provenance"],
        }
        provenance_refs = _register_provenance(registry, (provenance,))
        status = _clean_tension_status(values["Status"])
        render_text = (
            f"[{match.group(1)}] Tension: {match.group(2).strip()}. "
            f"Claim A: {values['Claim A']} Claim B: {values['Claim B']} "
            f"Status: {status}. Boundary: {values['Boundary']}"
        )
        records.append(
            {
                "kind": "tension",
                "id": match.group(1),
                "inference_use": "constraint_only",
                "title": match.group(2).strip(),
                "claim_a": values["Claim A"],
                "claim_b": values["Claim B"],
                "status": status,
                "boundary": values["Boundary"],
                "provenance_citation": values["Provenance"],
                "provenance_refs": provenance_refs,
                "selection_text": _normalize_selection_text(
                    (
                        match.group(2),
                        values["Claim A"],
                        values["Claim B"],
                        status,
                        values["Boundary"],
                    )
                ),
                "render_text": render_text,
            }
        )
    ids = {item["id"] for item in records}
    _require(ids == _expected_numbered_ids("FM-T-", 28), "tension ID set changed")
    return records


def _compile_application_gate(
    core: Mapping[str, Any], registry: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    gate = core["application_gate"]
    return {
        "id": gate["id"],
        "trigger_context": gate["trigger_context"],
        "fm_influence": gate["fm_influence"],
        "fallback": gate["fallback"],
        "later_reentry": gate["later_reentry"],
        "layer_boundary": gate["layer_boundary"],
        "provenance_refs": _register_provenance(registry, gate["provenance"]),
    }


def compile_bundle(source_dir: Path) -> dict[str, Any]:
    manifest, paths = _verify_sources(source_dir)
    core = _load_yaml(paths["semantic_model/FM_CORE_MODEL_v0_2.yaml"])
    inference = _load_yaml(paths["semantic_model/FM_INFERENCE_RULES_v0_2.yaml"])
    _require(isinstance(core, Mapping), "core model is not an object")
    _require(isinstance(inference, Mapping), "inference model is not an object")
    _validate_core(core)
    _validate_inference(inference, core)

    validation = json.loads(
        paths["semantic_model/FM_VALIDATION_v0_2.json"].read_text(encoding="utf-8")
    )
    _require(validation.get("status") == "PASS_CANONICAL_AUTHOR_APPROVED", "validation did not pass")
    _require(validation.get("deterministic_checks_failed") == 0, "canonical validation has failures")

    registry: dict[str, dict[str, Any]] = {}
    records = _compile_core_records(core, registry)
    records.extend(_compile_inference_records(inference, registry))
    records.extend(
        _parse_tensions(paths["semantic_model/FM_TENSIONS_v0_2.md"], registry)
    )
    kind_order = {
        "concept": 0,
        "relationship": 1,
        "historical_formulation": 2,
        "inference_rule": 3,
        "inference_application": 4,
        "tension": 5,
    }
    records.sort(key=lambda item: (kind_order[item["kind"]], item["id"]))
    ids = [str(item["id"]) for item in records]
    _require(len(ids) == len(set(ids)) == 198, "runtime record IDs are not unique and complete")

    payload: dict[str, Any] = {
        "bundle_contract_version": BUNDLE_CONTRACT_VERSION,
        "runtime_reference": RUNTIME_REFERENCE,
        "semantic_version": SEMANTIC_VERSION,
        "canonical_manifest_id": CANONICAL_MANIFEST_ID,
        "canonical_manifest_sha256": EXPECTED_MANIFEST_SHA256,
        "canonical_status": manifest["status"],
        "canonical_created": str(manifest["created"]),
        "validation_status": validation["status"],
        "deterministic_checks_passed": validation["deterministic_checks_passed"],
        "source_artifact_sha256": dict(sorted(EXPECTED_SOURCE_SHA256.items())),
        "counts": {
            **EXPECTED_COUNTS,
            "runtime_records": len(records),
        },
        "application_gate": _compile_application_gate(core, registry),
        "execution_order": list(inference["execution_order"]),
        "provenance": dict(sorted(registry.items())),
        "records": records,
    }
    payload["bundle_sha256"] = _sha256_bytes(_canonical_json_bytes(payload))
    return payload


def output_bytes(bundle: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(bundle, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compile the pinned canonical Fractal Monism v0.2 runtime bundle."
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="Path to the canonical semantic_model directory",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if --output is not byte-identical to a fresh compilation",
    )
    args = parser.parse_args()
    try:
        compiled = output_bytes(compile_bundle(args.source_dir))
        if args.check:
            if not args.output.is_file():
                raise CompileError(f"compiled bundle does not exist: {args.output}")
            if args.output.read_bytes() != compiled:
                raise CompileError("compiled bundle is stale or non-deterministic")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(compiled)
    except (CompileError, KeyError, TypeError, ValueError) as exc:
        print(f"compile_fm_runtime_bundle_v0_2: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "bundle_sha256": json.loads(compiled)["bundle_sha256"],
                "canonical_manifest_sha256": EXPECTED_MANIFEST_SHA256,
                "output": str(args.output),
                "status": "checked" if args.check else "written",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
