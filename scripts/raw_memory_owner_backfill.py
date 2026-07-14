#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from qdrant_client.http import models as qmodels

from rag_engine.qdrant_compat import make_qdrant_client
from rag_engine.raw_memory_ownership import (
    RawMemoryOwnershipError,
    assert_raw_payload_owner,
    canonical_owner_user_id,
)


VERSION = "raw_memory_owner_backfill_v1"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--collection", default="memory_raw")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest-sha256")
    return parser.parse_args()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def payload_hash(payload: dict[str, Any]) -> str:
    without_owner = dict(payload)
    without_owner.pop("owner_user_id", None)
    return sha256_json(without_owner)


def scroll_all(client: Any, collection: str) -> list[Any]:
    points: list[Any] = []
    offset = None
    while True:
        page, offset = client.scroll(
            collection_name=collection,
            scroll_filter=None,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(page or [])
        if offset is None:
            break
    return points


def inventory(points: list[Any], collection: str) -> tuple[dict[str, Any], dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    invalid_aliases: Counter[str] = Counter()
    hardened = 0

    for point in points:
        payload = dict(getattr(point, "payload", None) or {})
        legacy = str(payload.get("user_id") or "").strip()
        owner = payload.get("owner_user_id")

        try:
            canonical_legacy = canonical_owner_user_id(legacy)
        except RawMemoryOwnershipError:
            if owner not in (None, ""):
                raise RuntimeError(
                    f"point {point.id} has canonical owner but invalid legacy user_id"
                )
            invalid_aliases[legacy or "<missing>"] += 1
            continue

        if owner not in (None, ""):
            assert_raw_payload_owner(payload, canonical_legacy)
            hardened += 1
            continue

        entries.append(
            {
                "point_id": str(point.id),
                "legacy_user_id": legacy,
                "owner_user_id": canonical_legacy,
                "payload_sha256_without_owner": payload_hash(payload),
            }
        )

    entries.sort(key=lambda item: item["point_id"])
    body = {
        "version": VERSION,
        "collection": collection,
        "entries": entries,
    }
    manifest = {**body, "manifest_sha256": sha256_json(body)}
    report = {
        "version": VERSION,
        "collection": collection,
        "total_points": len(points),
        "already_hardened": hardened,
        "eligible_missing_owner": len(entries),
        "quarantined_non_uuid": sum(invalid_aliases.values()),
        "quarantined_by_legacy_user_id": dict(sorted(invalid_aliases.items())),
        "manifest_sha256": manifest["manifest_sha256"],
    }
    return manifest, report


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    supplied_hash = value.pop("manifest_sha256", None)
    actual_hash = sha256_json(value)
    if supplied_hash != actual_hash:
        raise RuntimeError("manifest content hash mismatch")
    return {**value, "manifest_sha256": supplied_hash}


def apply_manifest(client: Any, manifest: dict[str, Any], supplied_hash: str) -> dict[str, Any]:
    if supplied_hash != manifest["manifest_sha256"]:
        raise RuntimeError("--manifest-sha256 does not match manifest")

    entries = list(manifest.get("entries") or [])
    by_id = {entry["point_id"]: entry for entry in entries}
    if len(by_id) != len(entries):
        raise RuntimeError("manifest contains duplicate point ids")

    current_points = scroll_all(client, manifest["collection"])
    current_manifest, _ = inventory(current_points, manifest["collection"])
    unexpected = sorted(
        entry["point_id"]
        for entry in current_manifest["entries"]
        if entry["point_id"] not in by_id
    )
    if unexpected:
        raise RuntimeError(
            f"{len(unexpected)} eligible points are absent from authorized manifest"
        )

    point_map = {str(point.id): point for point in current_points}
    writes: dict[str, list[str]] = defaultdict(list)
    replay_skips = 0
    for point_id, entry in by_id.items():
        point = point_map.get(point_id)
        if point is None:
            raise RuntimeError(f"manifest point missing: {point_id}")
        payload = dict(getattr(point, "payload", None) or {})
        if payload_hash(payload) != entry["payload_sha256_without_owner"]:
            raise RuntimeError(f"manifest payload changed: {point_id}")
        if str(payload.get("user_id") or "") != entry["legacy_user_id"]:
            raise RuntimeError(f"legacy owner changed: {point_id}")

        current_owner = payload.get("owner_user_id")
        if current_owner not in (None, ""):
            assert_raw_payload_owner(payload, entry["owner_user_id"])
            replay_skips += 1
            continue
        writes[entry["owner_user_id"]].append(point_id)

    client.create_payload_index(
        collection_name=manifest["collection"],
        field_name="owner_user_id",
        field_schema=qmodels.PayloadSchemaType.KEYWORD,
        wait=True,
    )

    written = 0
    for owner_user_id, point_ids in sorted(writes.items()):
        for start in range(0, len(point_ids), 128):
            batch = point_ids[start : start + 128]
            client.set_payload(
                collection_name=manifest["collection"],
                payload={"owner_user_id": owner_user_id},
                points=batch,
                wait=True,
            )
            written += len(batch)

    verified = 0
    after = {str(point.id): point for point in scroll_all(client, manifest["collection"])}
    for point_id, entry in by_id.items():
        point = after.get(point_id)
        if point is None:
            raise RuntimeError(f"point disappeared after apply: {point_id}")
        assert_raw_payload_owner(point.payload or {}, entry["owner_user_id"])
        verified += 1

    return {
        "manifest_sha256": manifest["manifest_sha256"],
        "authorized": len(entries),
        "written": written,
        "replay_zero_write": written == 0,
        "replay_skips": replay_skips,
        "verified": verified,
    }


def main() -> int:
    args = arguments()
    client = make_qdrant_client(
        url=args.qdrant_url,
        timeout=60,
        prefer_grpc=False,
        https=False,
    )

    if args.apply:
        if args.manifest is None or not args.manifest_sha256:
            raise RuntimeError("--apply requires --manifest and --manifest-sha256")
        manifest = load_manifest(args.manifest)
        result = apply_manifest(client, manifest, args.manifest_sha256)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    manifest, report = inventory(scroll_all(client, args.collection), args.collection)
    if args.output:
        args.output.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
