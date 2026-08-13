from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from tools.governed_memory_release.inspect_standalone_cpython import (
    StandaloneCPythonInspectionError,
    _inspect_archive,
    inspect_selected_standalone_archive,
)


def _archive(path: Path, *, escaping: bool = False) -> str:
    with tarfile.open(path, mode="w:gz") as archive:
        for name in ("python", "python/bin", "python/lib"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            archive.addfile(member)
        raw = b"synthetic-python"
        member = tarfile.TarInfo("python/bin/python3.12")
        member.size = len(raw)
        member.mode = 0o755
        archive.addfile(member, io.BytesIO(raw))
        link = tarfile.TarInfo("python/bin/python")
        link.type = tarfile.SYMTYPE
        link.mode = 0o777
        link.linkname = "../../../escape" if escaping else "python3.12"
        archive.addfile(link)
        data = b"stdlib"
        member = tarfile.TarInfo("python/lib/os.py")
        member.size = len(data)
        member.mode = 0o644
        archive.addfile(member, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StandaloneCPythonInspectorTests(unittest.TestCase):
    def test_direct_script_imports_under_isolated_python(self):
        script = (
            Path(__file__).resolve().parents[2]
            / "tools/governed_memory_release/inspect_standalone_cpython.py"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-B", str(script), "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertIn(b"--archive", completed.stdout)

    def test_public_entrypoint_rejects_caller_selected_path(self):
        with self.assertRaises(StandaloneCPythonInspectionError):
            inspect_selected_standalone_archive("/tmp/cpython.tar.gz")

    def test_expands_relative_link_and_emits_canonical_v2_spec(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "python.tar.gz"
            digest = _archive(path)
            raw = _inspect_archive(
                path,
                expected_archive_sha256=digest,
                expected_symlink_count=1,
            )
            document = json.loads(raw.decode("ascii"))
            self.assertEqual(
                raw,
                json.dumps(
                    document,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii"),
            )
            self.assertEqual(
                document["schema_version"],
                "governed-memory-controller-standalone-cpython-substrate-v2",
            )
            self.assertEqual(document["archive_symlink_count"], 1)
            self.assertRegex(
                document["symlink_expansion_mapping_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertRegex(
                document["expanded_payload_tree_sha256"],
                r"^[0-9a-f]{64}$",
            )

    def test_escape_hash_and_count_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            safe = Path(temporary) / "safe.tar.gz"
            digest = _archive(safe)
            with self.assertRaises(StandaloneCPythonInspectionError):
                _inspect_archive(
                    safe,
                    expected_archive_sha256="0" * 64,
                    expected_symlink_count=1,
                )
            with self.assertRaises(StandaloneCPythonInspectionError):
                _inspect_archive(
                    safe,
                    expected_archive_sha256=digest,
                    expected_symlink_count=2,
                )
            escaping = Path(temporary) / "escaping.tar.gz"
            escaping_digest = _archive(escaping, escaping=True)
            with self.assertRaises(StandaloneCPythonInspectionError):
                _inspect_archive(
                    escaping,
                    expected_archive_sha256=escaping_digest,
                    expected_symlink_count=1,
                )


if __name__ == "__main__":
    unittest.main()
