#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from memory_v1_projection_v5_contract_test import sha256


CONTRACT = "memory_v1_claim_projection_review_batch_manifest_v1"
REVIEW_ROOT = Path("/home/ubuntu/memory-v1-reviews")
DECISION = "authorized"
REVIEWER_TYPE = "system"
REVIEWER_REF = "memory_v1_deterministic_claim_projection_v5_1_review_20260718"
REASON = (
    "direct owner-authored evidence with deterministic entity binding and "
    "predicate-specific claim rendering; reviewed for initial governed claim projection"
)
REASON_CODES = [
    "active_owner_evidence",
    "deterministic_entity_binding",
    "predicate_specific_projection",
    "phase_authorized_review",
]


class ManifestError(RuntimeError):
    pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--required-head", required=True)
    parser.add_argument("--bundle", action="append", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def private_path(value: str, *, output: bool = False) -> Path:
    path = Path(value).resolve()
    if REVIEW_ROOT not in path.parents:
        raise ManifestError("artifact is outside the private review root")
    if output:
        if path.exists():
            raise ManifestError("output already exists")
    elif not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ManifestError("bundle must be a private regular file")
    return path


def load_bundle(path: Path, owner: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if (
        value.get("contract_version")
        != "memory_v1_claim_projection_stage_bundle_v5_1"
        or value.get("owner_user_id") != owner
        or value.get("database_writes") != 0
        or value.get("external_model_calls") != 0
        or value.get("qdrant_writes") != 0
        or value.get("bundle_sha256")
        != sha256({key: item for key, item in value.items() if key != "bundle_sha256"})
    ):
        raise ManifestError("bundle boundary or hash mismatch")
    projections = value.get("packet", {}).get("projections")
    if not isinstance(projections, list) or len(projections) != 1:
        raise ManifestError("bundle must contain exactly one projection")
    projection = projections[0]
    if (
        projection.get("projection_ref") != "p01"
        or projection.get("lane") != "claim"
        or projection.get("target", {}).get("action") != "create"
        or projection.get("review", {}).get("state") != "manual_review_required"
        or projection.get("review", {}).get("authorization_required") is not True
    ):
        raise ManifestError("bundle is not an initial manual-review claim projection")
    uuid.UUID(value["plan_id"])
    return value


async def run() -> int:
    import asyncpg

    args = arguments()
    owner = str(uuid.UUID(args.owner))
    if not 1 <= len(args.bundle) <= 32:
        raise ManifestError("between one and 32 bundles are required")
    if len(args.required_head) != 40 or any(c not in "0123456789abcdef" for c in args.required_head):
        raise ManifestError("required head must be a full lowercase commit hash")
    output = private_path(args.output, output=True)
    loaded: list[tuple[Path, dict[str, Any]]] = []
    seen: set[str] = set()
    for raw in args.bundle:
        path = private_path(raw)
        bundle = load_bundle(path, owner)
        if bundle["plan_id"] in seen:
            raise ManifestError("duplicate plan id")
        seen.add(bundle["plan_id"])
        loaded.append((path, bundle))

    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise ManifestError("POSTGRES_DSN is required")
    conn = await asyncpg.connect(dsn, command_timeout=30)
    items: list[dict[str, Any]] = []
    try:
        if await conn.fetchval("SELECT session_user") != "brains_app":
            raise ManifestError("POSTGRES_DSN must authenticate as brains_app")
        tx = conn.transaction(readonly=True, isolation="serializable")
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id',$1,true)", owner)
        for path, bundle in sorted(loaded, key=lambda item: item[1]["plan_id"]):
            projection = bundle["packet"]["projections"][0]
            preflight = await conn.fetchrow(
                """SELECT * FROM memory.preflight_projection_review_v5(
                    $1,$2,'authorized'::memory.projection_review_decision_v5,
                    $3,$4,$5,$6::jsonb
                )""",
                uuid.UUID(bundle["plan_id"]),
                projection["projection_ref"],
                REVIEWER_TYPE,
                REVIEWER_REF,
                REASON,
                json.dumps(REASON_CODES, separators=(",", ":")),
            )
            if (
                preflight["review_number"] != 1
                or preflight["projection_sha256"] != sha256(projection)
                or preflight["semantic_key_sha256"]
                != projection["identity"]["semantic_key_sha256"]
            ):
                raise ManifestError("projection review preflight drifted")
            items.append(
                {
                    "bundle_path": str(path),
                    "bundle_file_sha256": file_sha256(path),
                    "bundle_sha256": bundle["bundle_sha256"],
                    "plan_id": bundle["plan_id"],
                    "projection_ref": projection["projection_ref"],
                    "predicate": projection["identity"]["predicate"],
                    "canonical_text_sha256": hashlib.sha256(
                        projection["payload"]["canonical_text"].encode()
                    ).hexdigest(),
                    "projection_sha256": sha256(projection),
                    "semantic_key_sha256": preflight["semantic_key_sha256"],
                    "review_number": preflight["review_number"],
                    "authorization_manifest_sha256": preflight[
                        "authorization_manifest_sha256"
                    ],
                }
            )
        await tx.rollback()
    finally:
        await conn.close()

    manifest = {
        "contract_version": CONTRACT,
        "owner_user_id": owner,
        "required_head_commit": args.required_head,
        "decision": DECISION,
        "reviewer_type": REVIEWER_TYPE,
        "reviewer_ref": REVIEWER_REF,
        "reason": REASON,
        "reason_codes": REASON_CODES,
        "expected_new_rows": len(items),
        "items": items,
    }
    manifest["manifest_sha256"] = sha256(manifest)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    print(f"manifest={output}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
