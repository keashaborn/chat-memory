#!/usr/bin/env python3
"""Run one fixed read-only production helper over SSH and validate its output."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import pathlib
import subprocess
import sys


SSH = "/usr/bin/ssh"
REMOTE_PYTHON = "/usr/bin/python3.12"
MAX_OUTPUT = 128 * 1024 * 1024
ALLOWED_HOSTS = {"seebx"}


def build_in_memory_source_helper(shared: bytes, helper: bytes) -> bytes:
    if not shared or not helper or len(shared) > 1024 * 1024 or len(helper) > 4 * 1024 * 1024 or b"\0" in shared or b"\0" in helper:
        raise RuntimeError("source helper inputs are malformed")
    shared_encoded = base64.b64encode(shared).decode("ascii")
    helper_encoded = base64.b64encode(helper).decode("ascii")
    shared_hash = hashlib.sha256(shared).hexdigest()
    helper_hash = hashlib.sha256(helper).hexdigest()
    program = f'''import base64,hashlib,sys,types
shared=base64.b64decode("{shared_encoded}",validate=True)
helper=base64.b64decode("{helper_encoded}",validate=True)
if hashlib.sha256(shared).hexdigest()!="{shared_hash}" or hashlib.sha256(helper).hexdigest()!="{helper_hash}":
    raise RuntimeError("embedded source helper hash mismatch")
module=types.ModuleType("source_record_projection")
module.__file__="<source_record_projection.py>"
exec(compile(shared,module.__file__,"exec"),module.__dict__)
sys.modules[module.__name__]=module
namespace={{"__name__":"__main__","__file__":"<readonly_repository_schema_sources.py>","__package__":None}}
exec(compile(helper,namespace["__file__"],"exec"),namespace)
'''.encode("ascii")
    if len(program) > 8 * 1024 * 1024:
        raise RuntimeError("embedded source helper is oversized")
    return program


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    catalog = subparsers.add_parser("catalog")
    catalog.add_argument("--host", required=True, choices=sorted(ALLOWED_HOSTS))
    catalog.add_argument("--container", required=True)
    catalog.add_argument("--database", required=True)
    catalog.add_argument("--user", required=True)
    sources = subparsers.add_parser("sources")
    sources.add_argument("--host", required=True, choices=sorted(ALLOWED_HOSTS))
    sources.add_argument("--source", required=True)
    sources.add_argument("--expected-commit", required=True)
    qdrant = subparsers.add_parser("qdrant")
    qdrant.add_argument("--host", required=True, choices=sorted(ALLOWED_HOSTS))
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parent
    if args.command == "catalog":
        helper = root / "readonly_pg_catalog_snapshot.py"
        remote_args = ["--container", args.container, "--database", args.database, "--user", args.user]
        expected_schema = "postgres-catalog-snapshot-v1"
    elif args.command == "sources":
        helper = root / "readonly_repository_schema_sources.py"
        remote_args = ["--source", args.source, "--expected-commit", args.expected_commit]
        expected_schema = "repository-schema-source-inventory-v1"
    else:
        helper = root / "readonly_qdrant_metadata.py"
        remote_args = []
        expected_schema = "qdrant-derived-index-metadata-v1"
    helper_payload = (
        build_in_memory_source_helper((root / "source_record_projection.py").read_bytes(), helper.read_bytes())
        if args.command == "sources"
        else helper.read_bytes()
    )
    result = subprocess.run(
        [SSH, args.host, REMOTE_PYTHON, "-", *remote_args],
        input=helper_payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=600,
        env={"LANG": "C", "LC_ALL": "C"},
    )
    if result.returncode != 0 or not result.stdout or len(result.stdout) > MAX_OUTPUT or len(result.stderr) > 1024 * 1024:
        print("read-only production helper failed", file=sys.stderr)
        return 2
    try:
        value = json.loads(result.stdout.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        print("read-only production helper returned malformed output", file=sys.stderr)
        return 2
    if not isinstance(value, dict) or value.get("schema_version") != expected_schema or result.stdout != canonical_bytes(value):
        print("read-only production helper schema or canonicalization rejected", file=sys.stderr)
        return 2
    if args.command == "catalog":
        proof = value.get("read_only_proof")
        if not isinstance(proof, dict) or proof.get("transaction_read_only") != "on" or proof.get("row_data_read") is not False or proof.get("vector_payload_read") is not False:
            print("read-only catalog proof rejected", file=sys.stderr)
            return 2
    if args.command == "qdrant" and (value.get("point_payload_read") is not False or value.get("point_search_or_scroll") is not False):
        print("Qdrant metadata-only proof rejected", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
