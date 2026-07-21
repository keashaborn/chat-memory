#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence
from urllib.parse import urlparse

if TYPE_CHECKING:
    import asyncpg


VERSION = "memory_v1_atomic_evidence_split_worker_v1"
SPLITTER_VERSION = "memory_v1_sentence_splitter_v1"
APPLY_TOKEN = "memory_v1_atomic_evidence_split_apply_v1"
ABBREVIATIONS = {"dr", "e.g", "etc", "i.e", "jr", "mr", "mrs", "ms", "prof", "sr", "st", "vs"}
BOUNDARY_RE = re.compile(
    r"(?P<punct>[.!?](?:[\"'’”)]*))(?P<space>[ \t]+)"
    r"|(?P<newline>\r?\n+)"
    r"|(?P<tight>[.!?])(?=[A-Za-z])"
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split exact owner evidence into immutable sentence evidence")
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--parent-evidence-id", required=True)
    parser.add_argument("--expected-parent-content-sha256", required=True)
    parser.add_argument("--selector-version", required=True)
    parser.add_argument("--enqueue-ordinal", action="append", type=int, default=[])
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report-path", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def trimmed_range(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def is_abbreviation_boundary(text: str, punctuation_end: int) -> bool:
    if punctuation_end <= 0 or text[punctuation_end - 1] != ".":
        return False
    match = re.search(r"([A-Za-z](?:[A-Za-z.]*)?)$", text[: punctuation_end - 1])
    return bool(match and match.group(1).lower() in ABBREVIATIONS)


def sentence_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for match in BOUNDARY_RE.finditer(text):
        if match.group("newline") is not None:
            end, next_cursor = match.start(), match.end()
        elif match.group("punct") is not None:
            end = match.end("punct")
            if is_abbreviation_boundary(text, end):
                continue
            next_cursor = match.end()
        else:
            end = match.end("tight")
            next_cursor = end
        value = trimmed_range(text, cursor, end)
        if value:
            ranges.append(value)
        cursor = next_cursor
    value = trimmed_range(text, cursor, len(text))
    if value:
        ranges.append(value)
    return ranges


def spans(text: str) -> list[dict[str, Any]]:
    result = []
    ranges = sentence_ranges(text)
    if not 1 <= len(ranges) <= 20:
        raise RuntimeError("one to twenty sentence spans are required")
    previous_end = 0
    for ordinal, (start, end) in enumerate(ranges):
        content = text[start:end]
        if text[previous_end:start].strip() or len(content) > 600:
            raise RuntimeError("sentence split coverage or length is invalid")
        result.append({
            "ordinal": ordinal,
            "char_start": start,
            "char_end": end,
            "content": content,
            "content_sha256": sha256_text(content),
            "boundary_reason": "terminal_span" if ordinal == len(ranges) - 1 else "sentence_boundary",
        })
        previous_end = end
    if text[previous_end:].strip():
        raise RuntimeError("sentence split leaves trailing content")
    return result


def loopback_dsn(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("database DSN must be loopback-only")
    return value


def secure_write(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(payload).hexdigest()


async def set_actor(conn: Any, owner: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.user_id',$1,true)", str(owner))


async def run() -> int:
    try:
        import asyncpg
    except ModuleNotFoundError as exc:
        raise RuntimeError("asyncpg is required to run the database worker") from exc
    args = arguments()
    owner = uuid.UUID(args.owner_user_id)
    parent = uuid.UUID(args.parent_evidence_id)
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_parent_content_sha256):
        raise RuntimeError("expected parent SHA-256 is invalid")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", args.selector_version):
        raise RuntimeError("selector version is invalid")
    enqueue_ordinals = sorted(set(args.enqueue_ordinal))
    if any(value < 0 or value > 19 for value in enqueue_ordinals):
        raise RuntimeError("enqueue ordinal is invalid")
    if args.apply and os.getenv("MEMORY_V1_ATOMIC_EVIDENCE_SPLIT_APPLY") != APPLY_TOKEN:
        raise RuntimeError("atomic split apply capability is absent")
    if args.apply and not args.expected_plan_sha256:
        raise RuntimeError("apply requires the expected plan SHA-256")

    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(loopback_dsn(dsn), command_timeout=60, ssl=False)
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise RuntimeError("atomic split requires brains_app session")
        async with conn.transaction():
            await set_actor(conn, owner)
            row = await conn.fetchrow(
                "SELECT content,content_sha256 FROM memory.evidence WHERE owner_user_id=$1 AND evidence_id=$2 AND status='active'",
                owner,
                parent,
            )
            if row is None or row["content_sha256"] != args.expected_parent_content_sha256:
                raise RuntimeError("atomic split parent changed")
            plan_spans = spans(str(row["content"]))
            planned = await conn.fetchrow(
                "SELECT * FROM memory.preflight_owner_atomic_evidence_split_v1($1,$2,$3,$4::jsonb)",
                parent,
                args.expected_parent_content_sha256,
                SPLITTER_VERSION,
                json.dumps(plan_spans),
            )
            if planned is None:
                raise RuntimeError("atomic split preflight returned no row")
            plan_sha = str(planned["plan_sha256"])
            if args.expected_plan_sha256 and plan_sha != args.expected_plan_sha256:
                raise RuntimeError("atomic split plan SHA-256 changed")
            raw_plan = planned["span_plan"]
            normalized = (
                json.loads(raw_plan)
                if isinstance(raw_plan, str)
                else list(raw_plan)
            )
            children: list[dict[str, Any]] = []
            applied = replayed = queued = queue_replayed = 0
            if args.apply:
                first = await conn.fetch(
                    "SELECT * FROM memory.apply_owner_atomic_evidence_split_v1($1,$2,$3,$4::jsonb,$5)",
                    parent,
                    args.expected_parent_content_sha256,
                    SPLITTER_VERSION,
                    json.dumps(plan_spans),
                    plan_sha,
                )
                second = await conn.fetch(
                    "SELECT * FROM memory.apply_owner_atomic_evidence_split_v1($1,$2,$3,$4::jsonb,$5)",
                    parent,
                    args.expected_parent_content_sha256,
                    SPLITTER_VERSION,
                    json.dumps(plan_spans),
                    plan_sha,
                )
                applied = sum(item["apply_outcome"] == "applied" for item in first)
                replayed = sum(item["apply_outcome"] == "replayed" for item in second)
                by_ordinal = {int(item["ordinal"]): item for item in first}
                if set(enqueue_ordinals) - set(by_ordinal):
                    raise RuntimeError("enqueue ordinal is absent from split plan")
                for ordinal in enqueue_ordinals:
                    child_id = by_ordinal[ordinal]["child_evidence_id"]
                    content_sha = by_ordinal[ordinal]["child_content_sha256"]
                    plan_row = await conn.fetchrow(
                        "SELECT * FROM memory.plan_owner_evidence_intake_v1($1,1,$2)",
                        args.selector_version,
                        child_id,
                    )
                    if plan_row is None or plan_row["outcome"] != "eligible" or plan_row["route"] != "relational_extraction" or plan_row["reason_code"] != "eligible_unprocessed" or plan_row["evidence_content_sha256"] != content_sha:
                        raise RuntimeError("atomic child intake plan changed")
                    values = (child_id, args.selector_version, content_sha, "relational_extraction", "eligible_unprocessed")
                    first_queue = await conn.fetchrow("SELECT * FROM memory.enqueue_owner_evidence_extraction_v1($1,$2,$3,$4,$5)", *values)
                    second_queue = await conn.fetchrow("SELECT * FROM memory.enqueue_owner_evidence_extraction_v1($1,$2,$3,$4,$5)", *values)
                    queued += int(first_queue["apply_outcome"] == "applied")
                    queue_replayed += int(second_queue["apply_outcome"] == "replayed")
            for item in normalized:
                children.append({
                    "ordinal": int(item["ordinal"]),
                    "child_evidence_id_sha256": sha256_text(str(item["child_evidence_id"])),
                    "content_sha256": item["content_sha256"],
                    "char_start": int(item["char_start"]),
                    "char_end": int(item["char_end"]),
                    "boundary_reason": item["boundary_reason"],
                    "queued": int(item["ordinal"]) in enqueue_ordinals,
                })
    finally:
        await conn.close()

    report = {
        "contract_version": VERSION,
        "apply": args.apply,
        "owner_user_id_sha256": sha256_text(str(owner)),
        "parent_evidence_id_sha256": sha256_text(str(parent)),
        "parent_content_sha256": args.expected_parent_content_sha256,
        "splitter_version": SPLITTER_VERSION,
        "selector_version_sha256": sha256_text(args.selector_version),
        "plan_sha256": plan_sha,
        "span_count": len(children),
        "children": children,
        "applied": applied,
        "replayed": replayed,
        "queued": queued,
        "queue_replayed": queue_replayed,
        "local_model_calls": 0,
        "external_model_calls": 0,
        "claim_writes": 0,
        "qdrant_writes": 0,
        "prompt_influence": 0,
    }
    output = Path(args.report_path).resolve()
    report_sha = secure_write(output, report)
    print(json.dumps({
        "version": VERSION,
        "apply": args.apply,
        "plan_sha256": plan_sha,
        "span_count": len(children),
        "applied": applied,
        "replayed": replayed,
        "queued": queued,
        "queue_replayed": queue_replayed,
        "output": str(output),
        "sha256": report_sha,
        "local_model_calls": 0,
        "external_model_calls": 0,
        "claim_writes": 0,
        "qdrant_writes": 0,
        "prompt_influence": 0,
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
