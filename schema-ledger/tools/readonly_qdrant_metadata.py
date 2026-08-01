#!/usr/bin/env python3
"""Emit Qdrant collection configuration without reading point payloads."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request


SCHEMA_VERSION = "qdrant-derived-index-metadata-v1"
CANONICALIZATION = "json-sort-keys-utf8-ensure-ascii-no-floats-lf-v1"
BASE_URL = "http://127.0.0.1:6333"
SAFE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
MAX_RESPONSE = 8 * 1024 * 1024
SENSITIVE_TOKEN_SHA256 = {"3a5a2512949399115565867a73a413ec6ba215c8f2df385f78b33238a6639b7c"}


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def sanitize_text(value: str) -> str:
    context = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        token_hash = hashlib.sha256(token.lower().encode("utf-8")).hexdigest()
        return "subject_" + token_hash[:16] + "_" + context if token_hash in SENSITIVE_TOKEN_SHA256 else token
    return re.sub(r"[A-Za-z0-9]+", replace, value)


def get_json(path: str) -> dict[str, object]:
    request = urllib.request.Request(BASE_URL + path, method="GET", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("Qdrant metadata request failed")
        payload = response.read(MAX_RESPONSE + 1)
    if not payload or len(payload) > MAX_RESPONSE or b"\0" in payload:
        raise RuntimeError("Qdrant metadata response is outside the bound")
    value = json.loads(payload.decode("utf-8", "strict"))
    if not isinstance(value, dict):
        raise RuntimeError("Qdrant metadata response is malformed")
    return value


def bounded_mapping(value: object, allowed: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, object] = {}
    for key in sorted(set(value) & allowed):
        child = value[key]
        if child is None or isinstance(child, (str, int, bool)):
            output[key] = child
        elif isinstance(child, dict):
            output[key] = bounded_mapping(child, {"size", "distance", "hnsw_config", "quantization_config", "on_disk", "datatype", "multivector_config"})
    return output


def main() -> int:
    root = get_json("/")
    version = root.get("version")
    if not isinstance(version, str) or len(version) > 64 or "\0" in version:
        raise RuntimeError("Qdrant version is malformed")
    collection_list = get_json("/collections")
    result = collection_list.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("collections"), list):
        raise RuntimeError("Qdrant collection inventory is malformed")
    names: list[tuple[str, str]] = []
    for item in result["collections"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise RuntimeError("Qdrant collection name is malformed")
        name = item["name"]
        if not SAFE_NAME.fullmatch(name):
            raise RuntimeError("Qdrant collection name is outside the metadata boundary")
        names.append((name, sanitize_text(name)))
    if len(names) != len({safe_name for _, safe_name in names}) or len(names) > 1024:
        raise RuntimeError("Qdrant collection inventory is duplicated or excessive")
    collections: list[dict[str, object]] = []
    for raw_name, name in sorted(names, key=lambda item: item[1]):
        detail = get_json("/collections/" + urllib.parse.quote(raw_name, safe=""))
        value = detail.get("result")
        if not isinstance(value, dict):
            raise RuntimeError("Qdrant collection detail is malformed")
        configuration = value.get("config")
        if not isinstance(configuration, dict):
            raise RuntimeError("Qdrant collection configuration is missing")
        params = bounded_mapping(configuration.get("params"), {"vectors", "shard_number", "replication_factor", "write_consistency_factor", "on_disk_payload", "sparse_vectors"})
        payload_schema = value.get("payload_schema", {})
        if not isinstance(payload_schema, dict):
            raise RuntimeError("Qdrant payload schema is malformed")
        fields: list[dict[str, str]] = []
        for field_name, field_value in sorted(payload_schema.items()):
            if not isinstance(field_name, str) or not SAFE_NAME.fullmatch(field_name) or not isinstance(field_value, dict):
                raise RuntimeError("Qdrant payload-schema field is malformed")
            data_type = field_value.get("data_type")
            if not isinstance(data_type, str) or len(data_type) > 64:
                raise RuntimeError("Qdrant payload-schema type is malformed")
            fields.append({"name": sanitize_text(field_name), "data_type": data_type})
        fields.sort(key=lambda item: item["name"])
        collections.append(
            {
                "name": name,
                "status": value.get("status") if isinstance(value.get("status"), str) else "unknown",
                "parameters": params,
                "hnsw_config": bounded_mapping(configuration.get("hnsw_config"), {"m", "ef_construct", "full_scan_threshold", "max_indexing_threads", "on_disk", "payload_m"}),
                "optimizer_config": bounded_mapping(configuration.get("optimizer_config"), {"deleted_threshold", "vacuum_min_vector_number", "default_segment_number", "max_segment_size", "memmap_threshold", "indexing_threshold", "flush_interval_sec", "max_optimization_threads"}),
                "wal_config": bounded_mapping(configuration.get("wal_config"), {"wal_capacity_mb", "wal_segments_ahead", "wal_retain_closed"}),
                "payload_schema": fields,
            }
        )
    body = {
        "schema_version": SCHEMA_VERSION,
        "canonicalization": CANONICALIZATION,
        "authority": "derived_rebuildable",
        "endpoint": "loopback",
        "version": version,
        "point_payload_read": False,
        "point_search_or_scroll": False,
        "collections": collections,
    }
    body["metadata_sha256"] = hashlib.sha256(canonical_bytes(body)).hexdigest()
    sys.stdout.buffer.write(canonical_bytes(body))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("Qdrant metadata inventory failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(2)
