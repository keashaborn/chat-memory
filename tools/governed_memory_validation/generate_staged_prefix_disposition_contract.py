#!/usr/bin/env python3
from __future__ import annotations

"""Emit the exact canonical 000002-failure to 000003-successor contract."""

import sys
from typing import Final

from tools.governed_memory_validation import staged_prefix_disposition as staged


DISPOSITION_ID: Final = "phase9-v6-staged-to-v7-000003"
CORRECTED_PACKAGE_MANIFEST_SHA256: Final = (
    "5addc8e4ab40b6bc700fde57b67579f8caa3d21077715bcdaf61d5714b52743e"
)
CORRECTED_CONTROLLER_RUNTIME_RECEIPT_SHA256: Final = "0" * 64

_EVIDENCE_FILES: Final = (
    {
        "role": "old_contract",
        "path": str(staged.PRODUCTION_OLD_CONTRACT_PATH),
        "sha256": staged.PRODUCTION_OLD_CONTRACT_SHA256,
        "size": 12495,
        "mode": 0o400,
        "uid": 0,
        "gid": 0,
        "nlink": 1,
        "device": 66305,
        "inode": 4010938,
    },
    {
        "role": "old_permit",
        "path": str(staged.PRODUCTION_OLD_PERMIT_PATH),
        "sha256": staged.PRODUCTION_OLD_PERMIT_SHA256,
        "size": 2253,
        "mode": 0o400,
        "uid": 0,
        "gid": 0,
        "nlink": 1,
        "device": 66305,
        "inode": 1652215,
    },
    {
        "role": "old_pre_effect_receipt",
        "path": str(staged.PRODUCTION_OLD_RECEIPT_PATH),
        "sha256": staged.PRODUCTION_OLD_RECEIPT_SHA256,
        "size": 1820,
        "mode": 0o400,
        "uid": 0,
        "gid": 0,
        "nlink": 1,
        "device": 66305,
        "inode": 4781692,
    },
    {
        "role": "staged_capsule",
        "path": str(staged.PRODUCTION_STAGED_CAPSULE_PATH),
        "sha256": staged.PRODUCTION_STAGED_CAPSULE_SHA256,
        "size": 6719,
        "mode": 0o400,
        "uid": 0,
        "gid": 0,
        "nlink": 1,
        "device": 66305,
        "inode": 1652216,
    },
)

_EMPTY_DIRECTORIES: Final = (
    {
        "role": "executions_v2",
        "path": str(staged.PRODUCTION_EXECUTIONS_ROOT),
        "mode": 0o700,
        "uid": 0,
        "gid": 0,
        "nlink": 2,
        "device": 66305,
        "inode": 4010939,
    },
    {
        "role": "store_secret",
        "path": str(staged.PRODUCTION_SECRET_ROOT),
        "mode": 0o700,
        "uid": 0,
        "gid": 0,
        "nlink": 2,
        "device": 66305,
        "inode": 5244923,
    },
)


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
    reviewed = expectation()
    return {
        "schema_version": staged.CONTRACT_SCHEMA,
        "disposition_id": DISPOSITION_ID,
        "repository_contract_source": (
            staged.PRODUCTION_REPOSITORY_CONTRACT_SOURCE
        ),
        "durable_contract_path": str(
            staged.PRODUCTION_DURABLE_CONTRACT_PATH
        ),
        "tombstone_path": str(staged.PRODUCTION_TOMBSTONE_PATH),
        "failed_attempt": {
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
            "evidence_files": [dict(value) for value in _EVIDENCE_FILES],
        },
        "corrected_successor": staged._corrected_successor_document(
            reviewed
        ),
        "empty_directories": [dict(value) for value in _EMPTY_DIRECTORIES],
        "absent_resources": staged._expected_absent_resources(
            staged.production_disposition_paths()
        ),
    }


def main() -> int:
    sys.stdout.buffer.write(staged.canonical_json_bytes(generate()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
