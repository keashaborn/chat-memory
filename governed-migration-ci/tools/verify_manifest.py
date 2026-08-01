#!/usr/bin/env python3
"""Verify the exact Step 4 candidate file set."""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import stat


LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    raw = (root / "MANIFEST.sha256").read_bytes()
    if not raw or b"\0" in raw or b"\r" in raw or not raw.endswith(b"\n"):
        raise RuntimeError("manifest encoding is invalid")
    expected: dict[str, str] = {}
    for line in raw.decode("ascii", "strict").splitlines():
        match = LINE.fullmatch(line)
        if match is None or match.group(2) in expected or match.group(2) == "MANIFEST.sha256":
            raise RuntimeError("manifest record is invalid")
        expected[match.group(2)] = match.group(1)
    if list(expected) != sorted(expected):
        raise RuntimeError("manifest is not sorted")
    observed: dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: os.fsencode(item.relative_to(root).as_posix())):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            if path.is_symlink():
                raise RuntimeError("manifest directory is a symlink")
            continue
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("candidate contains a non-regular file")
        if relative == "MANIFEST.sha256":
            continue
        observed[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed != expected:
        raise RuntimeError("candidate file set or hash differs")
    print("manifest valid: " + str(len(observed)) + " files")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("manifest verification failed: " + type(error).__name__ + ": " + str(error))
        raise SystemExit(2)
