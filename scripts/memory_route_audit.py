#!/usr/bin/env python3
"""
Verbal Sage / Brains memory route audit.

Checks:
- technical/admin query suppresses biography/profile cards
- specific recall query retrieves narrow personal archive context
- broad background query includes profile cards
- retired/test artifacts stay suppressed
- Lucifer tenant isolation remains intact

Usage:
  VS_SERVICE_TOKEN=... python scripts/memory_route_audit.py
  python scripts/memory_route_audit.py   # loads /opt/chat-memory/.env if present
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path("/opt/chat-memory")
ENV_PATH = ROOT / ".env"
BASE_URL = os.getenv("BRAINS_AUDIT_URL", "http://127.0.0.1:8088")
OUT_DIR = Path(os.getenv("BRAINS_AUDIT_OUT_DIR", "/tmp"))


def load_env_token() -> str:
    token = os.getenv("VS_SERVICE_TOKEN", "").strip()
    if token:
        return token

    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(errors="replace").splitlines():
            if line.startswith("VS_SERVICE_TOKEN="):
                return line.split("=", 1)[1].strip()

    raise SystemExit("missing VS_SERVICE_TOKEN")


def request_vantage(token: str, body: dict[str, Any], name: str) -> tuple[int, dict[str, Any], str]:
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        json.dump(body, f)
        req_path = f.name

    out_path = OUT_DIR / f"memory_route_audit_{name}.out"

    cp = subprocess.run(
        [
            "curl",
            "-sS",
            f"{BASE_URL}/vantage/query",
            "-H",
            "Content-Type: application/json",
            "-H",
            f"X-VS-Service-Token: {token}",
            "--data-binary",
            f"@{req_path}",
            "-o",
            str(out_path),
            "-w",
            "%{http_code}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    status = int((cp.stdout or "0").strip() or "0")
    raw = out_path.read_text(errors="replace")

    try:
        data = json.loads(raw)
    except Exception:
        data = {"_raw": raw}

    return status, data, raw


def contains(data: dict[str, Any], marker: str) -> bool:
    return marker in json.dumps(data, ensure_ascii=False)



def run_psql_scalar(sql: str) -> str:
    cp = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "brains-postgres-1",
            "psql",
            "-U",
            "sage",
            "-d",
            "memory",
            "-At",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return (cp.stdout or "").strip()


def run_psql_table(sql: str) -> str:
    cp = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "brains-postgres-1",
            "psql",
            "-U",
            "sage",
            "-d",
            "memory",
            "-P",
            "pager=off",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return cp.stdout or ""


def audit_card_policy_metadata() -> bool:
    """
    Direct policy metadata audit.

    This checks the card store itself, not route output. The route audit below
    proves behavior; this proves the policy layer is populated and coherent.
    """
    checks = [
        {
            "name": "all_cards_have_policy_fields",
            "sql": """
                select count(*)
                from vantage_card.card_head
                where payload ? 'use_scope' = false
                   or payload ? 'surface_policy' = false
                   or payload ? 'domains' = false
                   or payload ? 'sensitivity' = false
            """,
            "expected": "0",
        },
        {
            "name": "retired_cards_are_never_surface",
            "sql": """
                select count(*)
                from vantage_card.card_head
                where status <> 'active'
                  and coalesce(payload->>'use_scope','') <> 'NEVER_SURFACE'
            """,
            "expected": "0",
        },
        {
            "name": "active_system_cards_are_never_surface",
            "sql": """
                select count(*)
                from vantage_card.card_head
                where status='active'
                  and kind in ('system','audit')
                  and coalesce(payload->>'use_scope','') <> 'NEVER_SURFACE'
            """,
            "expected": "0",
        },
        {
            "name": "active_pref_style_cards_are_style_only",
            "sql": """
                select count(*)
                from vantage_card.card_head
                where status='active'
                  and kind in ('pref','style')
                  and coalesce(payload->>'use_scope','') <> 'STYLE_ONLY'
            """,
            "expected": "0",
        },
        {
            "name": "active_profile_cards_are_content_ok",
            "sql": """
                select count(*)
                from vantage_card.card_head
                where status='active'
                  and kind in ('identity','background','project')
                  and coalesce(payload->>'use_scope','') <> 'CONTENT_OK'
            """,
            "expected": "0",
        },
        {
            "name": "no_active_user_global_test_or_design_artifacts",
            "sql": """
                select count(*)
                from vantage_card.card_head
                where status='active'
                  and vantage_id='user_global'
                  and kind='pref'
                  and (
                    topic_key like '%/pref/remember_this_test_detail_for_later'
                    or topic_key like '%/pref/personalization'
                    or topic_key like '%/pref/allowed_memory_scopes'
                    or topic_key like '%/pref/corpus_allowed'
                    or topic_key like '%/pref/domain'
                    or topic_key like '%/pref/personal_archive_allowed'
                    or topic_key like '%/pref/profile_cards_allowed'
                    or topic_key like '%/pref/surface_personal_details'
                    or topic_key like '%/pref/turn_intent'
                    or topic_key like '%/pref/it_sharpens_the_roadmap_in_one_important_way'
                    or topic_key like '%/pref/the_point_is_valid'
                  )
            """,
            "expected": "0",
        },
    ]

    print("\n" + "=" * 96)
    print("CARD POLICY METADATA AUDIT")

    ok = True
    for check in checks:
        got = run_psql_scalar(check["sql"])
        passed = got == check["expected"]
        ok = ok and passed
        print(f"{check['name']}: {'PASS' if passed else 'FAIL'} got={got!r} expected={check['expected']!r}")

    print("\nPolicy distribution:")
    print(run_psql_table("""
        select
          status,
          kind,
          payload->>'use_scope' as use_scope,
          payload->>'surface_policy' as surface_policy,
          count(*) as n
        from vantage_card.card_head
        group by 1,2,3,4
        order by 1,2,3,4
    """).strip())

    print(f"\nCARD POLICY RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    token = load_env_token()

    policy_ok = audit_card_policy_metadata()

    tests = [
        {
            "name": "tech_restart",
            "body": {
                "user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
                "vantage_id": "RILEY",
                "message": "How do I restart the frontend?",
                "inspect_only": True,
                "debug": True,
            },
            "expect_true": ["[VANTAGE PREFERENCE CARDS]"],
            "expect_false": [
                "[VANTAGE PROFILE CARDS]",
                "Caravel",
                "clinical psychologist",
                "Jerry",
                "DeeDee",
                "silver fox",
            ],
            "expect_recall_mode": False,
            "expect_turn_intent": "TECH",
            "expect_plan": {
                "turn_intent": "TECH",
                "recall_mode": False,
                "personal_archive_enabled": False,
                "corpus_enabled": False,
                "k_personal": 0,
                "k_corpus": 0,
            },
        },
        {
            "name": "tech_lens_clamp",
            "body": {
                "user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
                "vantage_id": "RILEY",
                "message": "How do I restart the frontend?",
                "inspect_only": True,
                "debug": True,
                "mix": {
                    "conversation": 0.7,
                    "memory_cards": 0.7,
                    "corpus": 0.8,
                    "lens_fm": 0.7,
                    "recency_bias": 0.7,
                    "similarity_threshold": 0.4,
                },
            },
            "expect_true": [
                "[VANTAGE PREFERENCE CARDS]",
                "lens_fm clamped to 0.0 because turn_intent=TECH",
                "conversation clamped to 0.0 because turn_intent=TECH",
                "memory_cards/personal archive clamped to 0.0 because turn_intent=TECH",
                "corpus clamped to 0.0 because turn_intent=TECH",
            ],
            "expect_false": [
                "[FM LENS]",
                "[VANTAGE PROFILE CARDS]",
                "Caravel",
                "clinical psychologist",
                "Jerry",
                "DeeDee",
                "silver fox",
            ],
            "expect_recall_mode": False,
            "expect_turn_intent": "TECH",
            "expect_plan": {
                "turn_intent": "TECH",
                "recall_mode": False,
                "personal_archive_enabled": False,
                "corpus_enabled": False,
                "k_personal": 0,
                "k_corpus": 0,
            },
            "expect_turn_plan": {
                "requested_controls.lens_fm": 0.7,
                "effective_controls.lens_fm": 0.0,
                "requested_controls.conversation": 0.7,
                "effective_controls.conversation": 0.0,
                "requested_controls.memory_cards": 0.7,
                "effective_controls.memory_cards": 0.0,
                "requested_controls.corpus": 0.8,
                "effective_controls.corpus": 0.0,
                "injection_budget.thread_messages_injected": 0,
                "injection_budget.thread_chars_injected": 0,
                "injection_budget.max_personal_hits_requested": 0,
                "injection_budget.max_corpus_hits_requested": 0,
                "allowed_stores.personal_archive": False,
                "allowed_stores.corpus": False,
            },
        },
        {
            "name": "memory_architecture_intent",
            "body": {
                "user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
                "vantage_id": "RILEY",
                "message": "What should our memory architecture do to avoid injection bloat while still retrieving useful memories?",
                "inspect_only": True,
                "debug": True,
                "mix": {
                    "conversation": 0.7,
                    "memory_cards": 0.7,
                    "corpus": 0.8,
                    "lens_fm": 0.7,
                    "recency_bias": 0.7,
                    "similarity_threshold": 0.4,
                },
            },
            "expect_true": [
                "[VANTAGE PREFERENCE CARDS]",
                "lens_fm clamped to 0.0 because turn_intent=MEMORY_ARCHITECTURE",
            ],
            "expect_false": [
                "[FM LENS]",
                "[VANTAGE PROFILE CARDS]",
                "silver fox",
            ],
            "expect_system_prompt_true": [
                "Relevant context from memory:",
                " …",
            ],
            "expect_system_prompt_false": [
                "[FM LENS]",
                "[VANTAGE PROFILE CARDS]",
                "Jerry during dad-name recall",
                "Caravel during background question",
                "direct concise style in technical work",
                "poetic/fractal explanation during command-patching",
            ],
            "expect_recall_mode": False,
            "expect_turn_intent": "MEMORY_ARCHITECTURE",
            "expect_plan": {
                "turn_intent": "MEMORY_ARCHITECTURE",
                "recall_mode": False,
                "base_k": 3,
                "k_personal": 2,
                "k_corpus": 1,
                "personal_archive_enabled": True,
                "corpus_enabled": True,
            },
            "expect_turn_plan": {
                "turn_intent": "MEMORY_ARCHITECTURE",
                "requested_controls.lens_fm": 0.7,
                "effective_controls.lens_fm": 0.0,
                "injection_budget.compression_required": True,
                "injection_budget.max_personal_hits_requested": 2,
                "injection_budget.max_corpus_hits_requested": 1,
                "allowed_stores.profile_cards": False,
            },
            "expect_compression_preview": {
                "version": "memory_compression_preview_v0",
                "mode": "extractive_preview",
                "turn_intent": "MEMORY_ARCHITECTURE",
                "source_count": 2,
            },
            "expect_compression_preview_min": {
                "raw_chars": 1,
                "compact_chars": 1,
            },
            "expect_semantic_preview": {
                "version": "semantic_extraction_preview_v0",
                "schema_version": "semantic_unit_schema_v1",
                "mode": "deterministic_preview",
                "turn_intent": "MEMORY_ARCHITECTURE",
                "source": "current_message",
                "unit_count": 3,
                "memory_source_count": 2,
            },
            "expect_semantic_preview_min": {
                "unit_count": 1,
            },
            "expect_semantic_unit_fields": [
                "semantic_type",
                "claim",
                "function",
                "domain",
                "source",
                "source_ref",
                "surface_policy",
                "confidence",
                "retrieval_conditions",
                "suppression_conditions",
                "durability",
                "promotion_candidate",
            ],
            "expect_semantic_unit_values": {
                "semantic_type": "design_constraint",
                "durability": "long_term_project_preference",
                "promotion_candidate": True,
            },
            "expect_promotion_preview": {
                "version": "semantic_promotion_preview_v0",
                "mode": "debug_only",
                "source": "semantic_extraction_preview",
                "candidate_count": 2,
            },
            "expect_promotion_candidate_fields": [
                "candidate_id",
                "kind",
                "use_scope",
                "surface_policy",
                "domains",
                "claim",
                "function",
                "retrieval_conditions",
                "suppression_conditions",
                "durability",
                "source",
                "source_ref",
                "confidence",
            ],
            "expect_promotion_candidate_values": {
                "kind": "project",
                "use_scope": "CONTENT_OK",
                "surface_policy": "mention_when_relevant",
                "durability": "long_term_project_preference",
            },
            "expect_decision_preview": {
                "version": "semantic_promotion_decision_preview_v0",
                "mode": "debug_only",
                "source": "semantic_promotion_preview",
                "decision_count": 2,
                "eligible_count": 2,
                "blocked_count": 0,
                "needs_review_count": 2,
                "write_intent": "none_debug_only",
            },
            "expect_decision_fields": [
                "candidate_id",
                "eligible",
                "needs_review",
                "duplicate_risk",
                "blocked_reason",
                "suggested_action",
                "rationale",
            ],
            "expect_decision_values": {
                "eligible": True,
                "needs_review": True,
                "duplicate_risk": "unknown",
                "blocked_reason": "",
                "suggested_action": "candidate_for_review",
            },
        },
        {
            "name": "fm_conceptual_origin_consciousness",
            "body": {
                "user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
                "vantage_id": "RILEY",
                "message": "Could you describe the origin of consciousness.",
                "inspect_only": True,
                "debug": True,
                "mix": {
                    "conversation": 0.7,
                    "memory_cards": 0.7,
                    "corpus": 0.8,
                    "lens_fm": 0.7,
                    "recency_bias": 0.7,
                    "similarity_threshold": 0.4,
                },
            },
            "expect_true": [
                "[FM LENS]",
                "Relevant context from memory:",
            ],
            "expect_false": [
                "Jerry",
                "DeeDee",
                "silver fox",
            ],
            "expect_system_prompt_true": [
                "[FM LENS]",
                "Relevant context from memory:",
                "consciousness",
            ],
            "expect_system_prompt_false": [
                "Jerry during dad-name recall",
                "direct concise style in technical work",
                "poetic/fractal explanation during command-patching",
            ],
            "expect_memory_used_count": 2,
            "expect_recall_mode": False,
            "expect_turn_intent": "FM_CONCEPTUAL",
            "expect_plan": {
                "turn_intent": "FM_CONCEPTUAL",
                "recall_mode": False,
                "personal_archive_enabled": False,
                "corpus_enabled": True,
                "base_k": 4,
                "k_personal": 0,
                "k_corpus": 4,
            },
            "expect_turn_plan": {
                "turn_intent": "FM_CONCEPTUAL",
                "requested_controls.lens_fm": 0.7,
                "effective_controls.lens_fm": 0.7,
                "allowed_stores.corpus": True,
                "allowed_stores.personal_archive": False,
                "allowed_stores.profile_cards": False,
                "injection_budget.compression_required": True,
                "injection_budget.max_personal_hits_requested": 0,
                "injection_budget.max_corpus_hits_requested": 4,
            },
            "expect_dedupe_preview": {
                "version": "semantic_dedupe_preview_v0",
                "mode": "debug_only",
                "turn_intent": "FM_CONCEPTUAL",
                "input_count": 4,
                "cluster_count": 1,
                "duplicate_risk_count": 2,
            },
            "expect_dedupe_cluster_fields": [
                "cluster_key",
                "canonical_ref",
                "duplicate_refs",
                "member_count",
                "duplicate_count",
                "reason",
                "canonical_question",
            ],
            "expect_dedupe_cluster_values": {
                "member_count": 3,
                "duplicate_count": 2,
                "reason": "normalized_question_match",
            },
            "expect_dedupe_apply": {
                "version": "semantic_dedupe_apply_v0",
                "mode": "deterministic_apply",
                "turn_intent": "FM_CONCEPTUAL",
                "applied": True,
                "input_count": 4,
                "output_count": 2,
                "removed_count": 2,
                "reason": "remove_duplicate_refs_from_semantic_dedupe_preview",
            },
            "expect_dedupe_apply_removed_ref_contains": [
                "nta_primary_core_v1:75",
                "nta_primary_core_v1:119",
            ],
        },
        {
            "name": "specific_recall_dad",
            "body": {
                "user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
                "vantage_id": "RILEY",
                "message": "What is my dad's name?",
                "inspect_only": True,
                "debug": True,
            },
            "expect_true": ["Jerry"],
            "expect_false": [
                "[VANTAGE PROFILE CARDS]",
                "Caravel",
                "clinical psychologist",
                "silver fox",
            ],
            "expect_recall_mode": True,
            "expect_turn_intent": "SPECIFIC_RECALL",
            "expect_plan": {
                "turn_intent": "SPECIFIC_RECALL",
                "recall_mode": True,
                "personal_archive_enabled": True,
                "corpus_enabled": False,
                "base_k": 3,
                "k_personal": 10,
                "k_corpus": 0,
            },
        },
        {
            "name": "broad_background",
            "body": {
                "user_id": "1240822d-ac9a-4096-95aa-e2b24d36ef50",
                "vantage_id": "RILEY",
                "message": "What do you know about my background?",
                "inspect_only": True,
                "debug": True,
            },
            "expect_true": [
                "[VANTAGE PROFILE CARDS]",
                "background/company_history: founded Caravel Autism Health",
                "background/profession: clinical psychologist",
            ],
            "expect_false": [
                "silver fox",
                "pref/remember_this_test_detail_for_later",
            ],
            "expect_recall_mode": False,
            "expect_turn_intent": "PROFILE_SUMMARY",
            "expect_plan": {
                "turn_intent": "PROFILE_SUMMARY",
                "recall_mode": False,
                "personal_archive_enabled": True,
                "corpus_enabled": True,
                "k_personal": 5,
                "k_corpus": 5,
            },
        },
        {
            "name": "lucifer_isolation_recall",
            "body": {
                "user_id": "e049fcde-655a-4377-af91-e85fd98b4d8c",
                "vantage_id": "RILEY",
                "message": "What is my dad's name?",
                "inspect_only": True,
                "debug": True,
            },
            "expect_true": [],
            "expect_false": [
                "Jerry",
                "DeeDee",
                "Caravel",
                "clinical psychologist",
                "1240822d-ac9a-4096-95aa-e2b24d36ef50",
                "silver fox",
            ],
            "expect_recall_mode": True,
            "expect_turn_intent": "SPECIFIC_RECALL",
            "expect_plan": {
                "turn_intent": "SPECIFIC_RECALL",
                "recall_mode": True,
                "personal_archive_enabled": True,
                "corpus_enabled": False,
                "base_k": 3,
                "k_personal": 10,
                "k_corpus": 0,
            },
        },
    ]

    overall = bool(policy_ok)

    for t in tests:
        status, data, raw = request_vantage(token, t["body"], t["name"])
        rd = (((data.get("meta_explanation") or {}).get("vantage") or {}).get("retrieval_debug") or {})
        memory = data.get("memory_used") or []
        system_prompt = data.get("system_prompt") or ""

        print("\n" + "=" * 96)
        print(f"TEST: {t['name']}")
        print(f"HTTP: {status}")
        print(f"MESSAGE: {t['body']['message']}")
        print(f"recall_mode: {rd.get('recall_mode')}")
        print(f"turn_intent: {rd.get('turn_intent')}")
        plan = rd.get("retrieval_plan") or {}
        vantage_meta = ((data.get("meta_explanation") or {}).get("vantage") or {})
        turn_plan = vantage_meta.get("turn_plan") or {}
        compression_preview = vantage_meta.get("memory_compression_preview") or {}
        dedupe_preview = vantage_meta.get("semantic_dedupe_preview") or {}
        dedupe_apply = vantage_meta.get("semantic_dedupe_apply") or {}
        semantic_preview = vantage_meta.get("semantic_extraction_preview") or {}
        promotion_preview = vantage_meta.get("semantic_promotion_preview") or {}
        decision_preview = vantage_meta.get("semantic_promotion_decision_preview") or {}
        print(f"k_personal_requested: {rd.get('k_personal_requested')}")
        print(f"k_corpus_requested: {rd.get('k_corpus_requested')}")
        print(f"combined_after_trim: {rd.get('combined_after_trim')}")
        print(f"memory_used_count: {len(memory)}")
        print(f"system_prompt_chars: {len(system_prompt)}")
        print(f"retrieval_plan: {json.dumps(plan, ensure_ascii=False, sort_keys=True)}")
        print(f"turn_plan: {json.dumps(turn_plan, ensure_ascii=False, sort_keys=True)}")
        if compression_preview:
            print(f"memory_compression_preview: {json.dumps(compression_preview, ensure_ascii=False, sort_keys=True)}")
        if dedupe_preview:
            print(f"semantic_dedupe_preview: {json.dumps(dedupe_preview, ensure_ascii=False, sort_keys=True)}")
        if dedupe_apply:
            print(f"semantic_dedupe_apply: {json.dumps(dedupe_apply, ensure_ascii=False, sort_keys=True)}")
        if semantic_preview:
            print(f"semantic_extraction_preview: {json.dumps(semantic_preview, ensure_ascii=False, sort_keys=True)}")
        if promotion_preview:
            print(f"semantic_promotion_preview: {json.dumps(promotion_preview, ensure_ascii=False, sort_keys=True)}")
        if decision_preview:
            print(f"semantic_promotion_decision_preview: {json.dumps(decision_preview, ensure_ascii=False, sort_keys=True)}")

        ok = status == 200

        expected_memory_used_count = t.get("expect_memory_used_count")
        if expected_memory_used_count is not None:
            passed = len(memory) == int(expected_memory_used_count)
            ok = ok and passed
            print(f"EXPECT memory_used_count={expected_memory_used_count}: {passed} (got={len(memory)})")

        expected_recall = t.get("expect_recall_mode")
        if expected_recall is not None:
            got = bool(rd.get("recall_mode"))
            passed = got == bool(expected_recall)
            ok = ok and passed
            print(f"EXPECT recall_mode={expected_recall}: {passed}")

        expected_intent = t.get("expect_turn_intent")
        if expected_intent is not None:
            got_intent = str(rd.get("turn_intent") or "")
            passed = got_intent == str(expected_intent)
            ok = ok and passed
            print(f"EXPECT turn_intent={expected_intent}: {passed} (got={got_intent})")

        expected_plan = t.get("expect_plan") or {}
        plan = rd.get("retrieval_plan") or {}
        for key, expected_value in expected_plan.items():
            got_value = plan.get(key)
            passed = got_value == expected_value
            ok = ok and passed
            print(f"EXPECT plan.{key}={expected_value!r}: {passed} (got={got_value!r})")


        # turn_plan is diagnostic visibility for control arbitration.
        tp_version = turn_plan.get("version")
        tp_intent = turn_plan.get("turn_intent")
        tp_req = turn_plan.get("requested_controls") or {}
        tp_eff = turn_plan.get("effective_controls") or {}
        tp_budget = turn_plan.get("injection_budget") or {}
        tp_stores = turn_plan.get("allowed_stores") or {}

        tp_checks = [
            ("version", tp_version == "turn_plan_v0_visibility", tp_version),
            ("turn_intent", tp_intent == t.get("expect_turn_intent"), tp_intent),
            ("requested_controls", isinstance(tp_req, dict) and "conversation" in tp_req and "memory_cards" in tp_req, tp_req),
            ("effective_controls", isinstance(tp_eff, dict) and "conversation" in tp_eff and "memory_cards" in tp_eff, tp_eff),
            ("injection_budget", isinstance(tp_budget, dict) and "base_k" in tp_budget, tp_budget),
            ("allowed_stores", isinstance(tp_stores, dict) and "profile_cards" in tp_stores, tp_stores),
        ]

        for label, check, got in tp_checks:
            print(f"EXPECT turn_plan.{label}: {check} (got={got!r})")
            ok = ok and check

        for path, expected_value in (t.get("expect_turn_plan") or {}).items():
            cur = turn_plan
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            passed = cur == expected_value
            print(f"EXPECT turn_plan.{path}={expected_value!r}: {passed} (got={cur!r})")
            ok = ok and passed

        for path, expected_value in (t.get("expect_compression_preview") or {}).items():
            cur = compression_preview
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            passed = cur == expected_value
            print(f"EXPECT compression_preview.{path}={expected_value!r}: {passed} (got={cur!r})")
            ok = ok and passed

        for path, expected_min in (t.get("expect_compression_preview_min") or {}).items():
            cur = compression_preview
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            try:
                passed = float(cur) >= float(expected_min)
            except Exception:
                passed = False
            print(f"EXPECT compression_preview.{path}>={expected_min!r}: {passed} (got={cur!r})")
            ok = ok and passed

        for path, expected_value in (t.get("expect_dedupe_preview") or {}).items():
            cur = dedupe_preview
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            passed = cur == expected_value
            print(f"EXPECT dedupe_preview.{path}={expected_value!r}: {passed} (got={cur!r})")
            ok = ok and passed

        for path, expected_min in (t.get("expect_dedupe_preview_min") or {}).items():
            cur = dedupe_preview
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            try:
                passed = float(cur) >= float(expected_min)
            except Exception:
                passed = False
            print(f"EXPECT dedupe_preview.{path}>={expected_min!r}: {passed} (got={cur!r})")
            ok = ok and passed

        for field in (t.get("expect_dedupe_cluster_fields") or []):
            clusters = dedupe_preview.get("clusters") or []
            passed = bool(clusters) and all(isinstance(c, dict) and field in c for c in clusters)
            print(f"EXPECT dedupe_preview.clusters[*].{field}: {passed}")
            ok = ok and passed

        for path, expected_value in (t.get("expect_dedupe_cluster_values") or {}).items():
            parts = str(path).split(".")
            clusters = dedupe_preview.get("clusters") or []
            got_values = []
            for cluster in clusters:
                cur = cluster
                for part in parts:
                    if isinstance(cur, dict):
                        cur = cur.get(part)
                    else:
                        cur = None
                        break
                got_values.append(cur)
            passed = expected_value in got_values
            print(f"EXPECT dedupe_preview.clusters contains {path}={expected_value!r}: {passed} (got={got_values!r})")
            ok = ok and passed

        for path, expected_value in (t.get("expect_dedupe_apply") or {}).items():
            cur = dedupe_apply
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            passed = cur == expected_value
            print(f"EXPECT dedupe_apply.{path}={expected_value!r}: {passed} (got={cur!r})")
            ok = ok and passed

        for path, expected_min in (t.get("expect_dedupe_apply_min") or {}).items():
            cur = dedupe_apply
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            try:
                passed = float(cur) >= float(expected_min)
            except Exception:
                passed = False
            print(f"EXPECT dedupe_apply.{path}>={expected_min!r}: {passed} (got={cur!r})")
            ok = ok and passed

        for marker in (t.get("expect_dedupe_apply_removed_ref_contains") or []):
            removed_refs = dedupe_apply.get("removed_refs") or []
            present = any(str(marker) in str(ref) for ref in removed_refs)
            ok = ok and present
            print(f"EXPECT dedupe_apply.removed_refs contains {marker!r}: {present} (got={removed_refs!r})")

        for path, expected_value in (t.get("expect_semantic_preview") or {}).items():
            cur = semantic_preview
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            passed = cur == expected_value
            print(f"EXPECT semantic_preview.{path}={expected_value!r}: {passed} (got={cur!r})")
            ok = ok and passed

        for path, expected_min in (t.get("expect_semantic_preview_min") or {}).items():
            cur = semantic_preview
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            try:
                passed = float(cur) >= float(expected_min)
            except Exception:
                passed = False
            print(f"EXPECT semantic_preview.{path}>={expected_min!r}: {passed} (got={cur!r})")
            ok = ok and passed

        for field in (t.get("expect_semantic_unit_fields") or []):
            units = semantic_preview.get("units") or []
            passed = bool(units) and all(isinstance(u, dict) and field in u for u in units)
            print(f"EXPECT semantic_preview.units[*].{field}: {passed}")
            ok = ok and passed

        for path, expected_value in (t.get("expect_semantic_unit_values") or {}).items():
            parts = str(path).split(".")
            units = semantic_preview.get("units") or []
            got_values = []
            for unit in units:
                cur = unit
                for part in parts:
                    if isinstance(cur, dict):
                        cur = cur.get(part)
                    else:
                        cur = None
                        break
                got_values.append(cur)
            passed = expected_value in got_values
            print(f"EXPECT semantic_preview.units contains {path}={expected_value!r}: {passed} (got={got_values!r})")
            ok = ok and passed

        for path, expected_value in (t.get("expect_promotion_preview") or {}).items():
            cur = promotion_preview
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            passed = cur == expected_value
            print(f"EXPECT promotion_preview.{path}={expected_value!r}: {passed} (got={cur!r})")
            ok = ok and passed

        for field in (t.get("expect_promotion_candidate_fields") or []):
            candidates = promotion_preview.get("candidates") or []
            passed = bool(candidates) and all(isinstance(c, dict) and field in c for c in candidates)
            print(f"EXPECT promotion_preview.candidates[*].{field}: {passed}")
            ok = ok and passed

        for path, expected_value in (t.get("expect_promotion_candidate_values") or {}).items():
            parts = str(path).split(".")
            candidates = promotion_preview.get("candidates") or []
            got_values = []
            for candidate in candidates:
                cur = candidate
                for part in parts:
                    if isinstance(cur, dict):
                        cur = cur.get(part)
                    else:
                        cur = None
                        break
                got_values.append(cur)
            passed = expected_value in got_values
            print(f"EXPECT promotion_preview.candidates contains {path}={expected_value!r}: {passed} (got={got_values!r})")
            ok = ok and passed

        for path, expected_value in (t.get("expect_decision_preview") or {}).items():
            cur = decision_preview
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            passed = cur == expected_value
            print(f"EXPECT decision_preview.{path}={expected_value!r}: {passed} (got={cur!r})")
            ok = ok and passed

        for field in (t.get("expect_decision_fields") or []):
            decisions = decision_preview.get("decisions") or []
            passed = bool(decisions) and all(isinstance(d, dict) and field in d for d in decisions)
            print(f"EXPECT decision_preview.decisions[*].{field}: {passed}")
            ok = ok and passed

        for path, expected_value in (t.get("expect_decision_values") or {}).items():
            parts = str(path).split(".")
            decisions = decision_preview.get("decisions") or []
            got_values = []
            for decision in decisions:
                cur = decision
                for part in parts:
                    if isinstance(cur, dict):
                        cur = cur.get(part)
                    else:
                        cur = None
                        break
                got_values.append(cur)
            passed = expected_value in got_values
            print(f"EXPECT decision_preview.decisions contains {path}={expected_value!r}: {passed} (got={got_values!r})")
            ok = ok and passed

        for marker in (t.get("expect_system_prompt_true") or []):
            present = marker in system_prompt
            ok = ok and present
            print(f"EXPECT SYSTEM_PROMPT TRUE  {marker!r}: {present}")

        for marker in (t.get("expect_system_prompt_false") or []):
            present = marker in system_prompt
            ok = ok and not present
            print(f"EXPECT SYSTEM_PROMPT FALSE {marker!r}: {present}")

        for marker in t["expect_true"]:
            present = contains(data, marker)
            ok = ok and present
            print(f"EXPECT TRUE  {marker!r}: {present}")

        for marker in t["expect_false"]:
            present = contains(data, marker)
            ok = ok and not present
            print(f"EXPECT FALSE {marker!r}: {present}")

        print(f"RESULT: {'PASS' if ok else 'FAIL'}")
        overall = overall and ok

    print("\n" + "=" * 96)
    print(f"OVERALL: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
