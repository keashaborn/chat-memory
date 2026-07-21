#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import uuid

from scripts.memory_v1_v5_1_entity_resolution_batch import CONFIRMATION


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--head", required=True)
    return parser.parse_args()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> int:
    args = arguments()
    plan_path = Path(args.plan).resolve(strict=True)
    raw = plan_path.read_bytes()
    plan = json.loads(raw)
    if plan["required_head_commit"] != args.head:
        raise RuntimeError("plan and authorization head differ")
    now = dt.datetime.now(dt.timezone.utc)
    plan_sha = digest(raw)
    value = {
        "contract_version": "memory_v1_v5_1_entity_resolution_batch_authorization_v1",
        "authorization_id": str(
            uuid.uuid5(
                uuid.UUID("5f811e9c-87c0-55b8-93ba-638cde5cdd0c"), plan_sha
            )
        ),
        "authorized": True,
        "authorized_by": "Eric Lund",
        "authorized_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(minutes=20)).isoformat(),
        "expected_head_commit": args.head,
        "target_server": "seebx",
        "scope": "reconcile_review_and_apply_owner_v5_1_entity_resolutions_only",
        "owner_user_id": plan["owner_user_id"],
        "plan_sha256": plan_sha,
        "expected_item_count": plan["item_count"],
        "expected_total_bindings": plan["expected_total_bindings"],
        "expected_new_rows": plan["expected_new_rows"],
        "confirmation": CONFIRMATION,
    }
    output = Path(args.output).resolve()
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
    print(
        json.dumps(
            {"authorization": str(output), "plan_sha256": plan_sha},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
