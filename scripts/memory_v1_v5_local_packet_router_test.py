#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import uuid

from scripts.memory_v1_v5_local_packet_router import (
    artifact_ids,
    artifact_paths,
    secure_review_root,
)


def main() -> int:
    owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
    packet = uuid.UUID("2fa0db2a-0636-5353-9f3f-3f952bd4632b")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        root.chmod(0o700)
        if secure_review_root(str(root)) != root.resolve():
            raise AssertionError("secure review root changed")
        report, bundle = artifact_paths(root, packet)
        if report == bundle or str(packet) in report.name or str(packet) in bundle.name:
            raise AssertionError("artifact paths expose or collide on packet IDs")
        root.chmod(0o770)
        try:
            secure_review_root(str(root))
        except RuntimeError:
            pass
        else:
            raise AssertionError("group-writable review root was accepted")
        root.chmod(stat.S_IRWXU)
    first = artifact_ids(owner, packet, "a" * 64, "b" * 64)
    second = artifact_ids(owner, packet, "a" * 64, "b" * 64)
    changed = artifact_ids(owner, packet, "a" * 64, "c" * 64)
    if first != second or first == changed or first[0] == first[1]:
        raise AssertionError("artifact identities are not deterministic")
    print("memory_v1_v5_local_packet_router: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
