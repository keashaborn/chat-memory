#!/usr/bin/env python3
"""Single deterministic SQL source-record projection implementation.

This module accepts Git metadata plus blob bytes and returns sanitized metadata.
It never emits SQL bodies and performs no filesystem, network, or database I/O.
"""

from __future__ import annotations

import hashlib
import pathlib
import re


MAX_BLOB = 64 * 1024 * 1024
COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SENSITIVE_TOKEN_SHA256 = {"3a5a2512949399115565867a73a413ec6ba215c8f2df385f78b33238a6639b7c"}

UNSAFE_PATTERNS = {
    "data_delete": r"\bDELETE\s+FROM\b",
    "data_insert": r"\bINSERT\s+INTO\b",
    "data_update": r"\bUPDATE\s+[A-Za-z_]",
    "drop_object": r"\bDROP\s+(?:TABLE|SCHEMA|VIEW|MATERIALIZED\s+VIEW|FUNCTION|PROCEDURE|TYPE|INDEX|POLICY|TRIGGER|ROLE)\b",
    "truncate": r"\bTRUNCATE\b",
    "copy": r"\bCOPY\b",
    "nontransactional_index": r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\b",
    "maintenance_command": r"\b(?:VACUUM|REINDEX|CLUSTER)\b",
    "role_mutation": r"\b(?:CREATE|ALTER|DROP)\s+ROLE\b",
    "grant_change": r"\b(?:GRANT|REVOKE)\b",
}

DECLARATION_RE = re.compile(
    r"\b(?:CREATE(?:\s+OR\s+REPLACE)?|ALTER)\s+"
    r"(SCHEMA|TABLE|VIEW|MATERIALIZED\s+VIEW|FUNCTION|PROCEDURE|TYPE|INDEX|POLICY|TRIGGER|SEQUENCE)\s+"
    r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?)",
    re.I,
)


class SourceProjectionError(RuntimeError):
    pass


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sanitize_text(value: str) -> str:
    if not isinstance(value, str) or CONTROL_RE.search(value):
        raise SourceProjectionError("source metadata text is malformed")
    context = sha256(value.encode("utf-8"))[:12]

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        digest = sha256(token.lower().encode("utf-8"))
        return "subject_" + digest[:16] + "_" + context if digest in SENSITIVE_TOKEN_SHA256 else token

    return re.sub(r"[A-Za-z0-9]+", replace, value)


def require_source_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path.encode("utf-8")) > 16 * 1024 or CONTROL_RE.search(path):
        raise SourceProjectionError("source path is malformed")
    pure = pathlib.PurePosixPath(path)
    if pure.is_absolute() or pure.as_posix() != path or path in {".", ".."} or ".." in pure.parts:
        raise SourceProjectionError("source path is not canonical and relative")
    return path


def strip_sql(payload: bytes) -> str:
    if not isinstance(payload, bytes) or len(payload) > MAX_BLOB:
        raise SourceProjectionError("SQL blob is malformed or oversized")
    text = payload.decode("utf-8", "replace")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$.*?\$\1\$", " DOLLAR_BODY ", text, flags=re.S)
    text = re.sub(r"'(?:''|[^'])*'", " STRING_LITERAL ", text)
    return text


def analyze_sql(payload: bytes) -> tuple[list[str], list[str]]:
    stripped = strip_sql(payload)
    unsafe = sorted(name for name, pattern in UNSAFE_PATTERNS.items() if re.search(pattern, stripped, re.I))
    declarations = sorted(
        {
            sanitize_text(kind.lower().replace(" ", "_") + ":" + name.lower())
            for kind, name in DECLARATION_RE.findall(stripped)
        }
    )
    return unsafe, declarations


def source_lane(path: str) -> str:
    value = require_source_path(path).lower()
    if "fractal" in value or "monism" in value:
        return "fractal_monism_policy"
    if "lifeswitch" in value:
        return "lifeswitch_live_context"
    if "assistant_response_preference" in value or "response_preference" in value:
        return "response_preferences"
    if "project" in value:
        return "projects"
    if "preference" in value:
        return "life_preferences"
    if any(token in value for token in ("review", "audit", "trace", "reconciliation")):
        return "audit_review"
    if any(token in value for token in ("projection", "retrieval", "qdrant")):
        return "derived_index_coordination"
    if "memory" in value:
        return "personal_memory"
    return "other_application"


def source_kind(path: str) -> str:
    value = require_source_path(path)
    if value.startswith("tests/"):
        return "test_fixture"
    if value.endswith("_rollback.sql"):
        return "rollback"
    if value == "tools/ci_bootstrap.sql" or value.startswith("sql/"):
        return "bootstrap"
    if value.startswith("ops/sql/"):
        return "forward"
    return "schema_source"


def project_source_record(path: str, mode: str, oid: str, payload: bytes) -> dict[str, object]:
    source_path = require_source_path(path)
    if mode not in {"100644", "100755"} or not isinstance(oid, str) or not COMMIT_RE.fullmatch(oid):
        raise SourceProjectionError("Git source identity is malformed")
    unsafe, declarations = analyze_sql(payload)
    kind = source_kind(source_path)
    stripped = strip_sql(payload)
    rollback_target = source_path.removesuffix("_rollback.sql") + ".sql" if kind == "rollback" else None
    transaction_mode = (
        "nontransactional"
        if any(value in unsafe for value in ("nontransactional_index", "maintenance_command"))
        else (
            "explicit"
            if re.search(r"\bBEGIN\b", stripped, re.I) and re.search(r"\bCOMMIT\b", stripped, re.I)
            else "unspecified"
        )
    )
    return {
        "path": sanitize_text(source_path),
        "mode": mode,
        "git_blob": oid,
        "sha256": sha256(payload),
        "size": len(payload),
        "kind": kind,
        "lane": source_lane(source_path),
        "unsafe_categories": unsafe,
        "declared_objects": declarations,
        "rollback_target": sanitize_text(rollback_target) if rollback_target is not None else None,
        "transaction_mode": transaction_mode,
    }
