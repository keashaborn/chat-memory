#!/usr/bin/env python3
from __future__ import annotations

"""Emit the reviewed 000003-failure to 000004-successor contract candidate.

The successor P/R pins are deliberately zero until the rebuilt package and
controller runtime are published.  The production executor rejects those
placeholders, so this generator is safe to use for structural review only.
"""

import sys
from typing import Final

from tools.governed_memory_validation import staged_prefix_disposition as staged


DISPOSITION_ID: Final = "phase9-v7-staged-to-v8-000004"
CORRECTED_PACKAGE_MANIFEST_SHA256: Final = "0" * 64
CORRECTED_CONTROLLER_RUNTIME_RECEIPT_SHA256: Final = "0" * 64
_DEVICE: Final = 66305


def _file(
    role: str,
    path: object,
    sha256: str,
    size: int,
    mode: int,
    inode: int,
) -> dict[str, object]:
    return {
        "role": role,
        "path": str(path),
        "sha256": sha256,
        "size": size,
        "mode": mode,
        "uid": 0,
        "gid": 0,
        "nlink": 1,
        "device": _DEVICE,
        "inode": inode,
    }


def _directory(
    role: str,
    path: object,
    inode: int,
    nlink: int,
    entries: list[str],
) -> dict[str, object]:
    return {
        "role": role,
        "path": str(path),
        "mode": 0o700,
        "uid": 0,
        "gid": 0,
        "nlink": nlink,
        "device": _DEVICE,
        "inode": inode,
        "entries": entries,
    }


def expectation() -> staged.ReviewedStagedPrefixExpectation:
    return staged.ReviewedStagedPrefixExpectation(
        contract_sha256="0" * 64,
        disposition_id=DISPOSITION_ID,
        old_tag_ref=staged.PRODUCTION_OLD_TAG_REF,
        old_tag_commit=staged.PRODUCTION_OLD_TAG_COMMIT,
        old_tag_tree=staged.PRODUCTION_OLD_TAG_TREE,
        failed_package_manifest_sha256=(
            staged.PRODUCTION_FAILED_PACKAGE_MANIFEST_SHA256
        ),
        failed_controller_runtime_receipt_sha256=(
            staged.PRODUCTION_FAILED_RUNTIME_RECEIPT_SHA256
        ),
        corrected_generation=staged.PRODUCTION_CORRECTED_GENERATION,
        corrected_package_manifest_sha256=(
            CORRECTED_PACKAGE_MANIFEST_SHA256
        ),
        corrected_controller_runtime_receipt_sha256=(
            CORRECTED_CONTROLLER_RUNTIME_RECEIPT_SHA256
        ),
    )


def generate() -> dict[str, object]:
    paths = staged.production_disposition_paths()
    reviewed = expectation()
    preserved = {
        "generation": staged.PRODUCTION_PRESERVED_GENERATION,
        "tag": {
            "ref": staged.PRODUCTION_PRESERVED_TAG_REF,
            "object_type": "commit",
            "commit": staged.PRODUCTION_PRESERVED_TAG_COMMIT,
            "tree": staged.PRODUCTION_PRESERVED_TAG_TREE,
        },
        "evidence_files": [
            _file(
                "preserved_contract",
                paths.preserved_contract_path,
                staged.PRODUCTION_PRESERVED_CONTRACT_SHA256,
                12495,
                0o400,
                4010938,
            ),
            _file(
                "preserved_permit",
                paths.preserved_permit_path,
                staged.PRODUCTION_PRESERVED_PERMIT_SHA256,
                2253,
                0o400,
                1652215,
            ),
            _file(
                "preserved_pre_effect_receipt",
                paths.preserved_pre_effect_receipt_path,
                staged.PRODUCTION_PRESERVED_RECEIPT_SHA256,
                1820,
                0o400,
                4781692,
            ),
            _file(
                "preserved_staged_capsule",
                paths.preserved_staged_capsule_path,
                staged.PRODUCTION_PRESERVED_STAGED_CAPSULE_SHA256,
                6719,
                0o400,
                1652216,
            ),
        ],
        "evidence_directories": [
            _directory(
                "preserved_executions_v2",
                paths.preserved_executions_root,
                4010939,
                2,
                [],
            ),
            _directory(
                "preserved_store_secret",
                paths.preserved_secret_root,
                5244923,
                2,
                [],
            ),
        ],
    }
    failed = {
        "generation": staged.PRODUCTION_FAILED_GENERATION,
        "tag": {
            "ref": staged.PRODUCTION_OLD_TAG_REF,
            "object_type": "commit",
            "commit": staged.PRODUCTION_OLD_TAG_COMMIT,
            "tree": staged.PRODUCTION_OLD_TAG_TREE,
        },
        "package_manifest_sha256": (
            staged.PRODUCTION_FAILED_PACKAGE_MANIFEST_SHA256
        ),
        "controller_runtime_receipt_sha256": (
            staged.PRODUCTION_FAILED_RUNTIME_RECEIPT_SHA256
        ),
        "evidence_files": [
            _file(
                "failed_contract",
                paths.old_contract_path,
                staged.PRODUCTION_OLD_CONTRACT_SHA256,
                6572,
                0o400,
                4010940,
            ),
            _file(
                "failed_permit",
                paths.old_permit_path,
                staged.PRODUCTION_OLD_PERMIT_SHA256,
                2398,
                0o400,
                1652272,
            ),
            _file(
                "failed_store_spec_v2_tombstone",
                paths.old_pre_effect_receipt_path,
                staged.PRODUCTION_OLD_RECEIPT_SHA256,
                1734,
                0o400,
                4787754,
            ),
            _file(
                "failed_capsule_v4",
                paths.staged_capsule_path,
                staged.PRODUCTION_STAGED_CAPSULE_SHA256,
                6719,
                0o400,
                1652273,
            ),
            _file(
                "failed_authority_state_v3",
                paths.failed_authority_state_path,
                staged.PRODUCTION_FAILED_AUTHORITY_STATE_SHA256,
                28672,
                0o600,
                1652274,
            ),
            _file(
                "failed_execution_journal",
                paths.failed_execution_journal_path,
                staged.PRODUCTION_EMPTY_FILE_SHA256,
                0,
                0o600,
                5257381,
            ),
            _file(
                "failed_execution_resources",
                paths.failed_execution_resources_path,
                staged.PRODUCTION_EMPTY_FILE_SHA256,
                0,
                0o600,
                5257382,
            ),
        ],
        "evidence_directories": [
            _directory(
                "failed_executions_v3",
                paths.executions_root,
                5257379,
                3,
                [staged.PRODUCTION_EXECUTION_ID],
            ),
            _directory(
                "failed_execution",
                paths.failed_execution_root,
                5257380,
                2,
                ["journal.jsonl", "resources.jsonl"],
            ),
            _directory(
                "failed_store_secret",
                paths.store_secret_root,
                5257378,
                2,
                [],
            ),
        ],
    }
    return {
        "schema_version": staged.CONTRACT_SCHEMA,
        "disposition_id": DISPOSITION_ID,
        "repository_contract_source": (
            staged.PRODUCTION_REPOSITORY_CONTRACT_SOURCE
        ),
        "durable_contract_path": str(staged.PRODUCTION_DURABLE_CONTRACT_PATH),
        "tombstone_path": str(staged.PRODUCTION_TOMBSTONE_PATH),
        "preserved_predecessor": preserved,
        "failed_attempt": failed,
        "corrected_successor": staged._corrected_successor_document(reviewed),
        "absent_resources": staged._expected_absent_resources(paths),
    }


def main() -> int:
    sys.stdout.buffer.write(staged.canonical_json_bytes(generate()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
