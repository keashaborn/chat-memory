from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from scripts.memory_v1_deferred_reconciliation_scan_worker import (
    load_roster,
    run_owner,
)


OWNER_A = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self):
        self.calls = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return {
            "run_id": args[0],
            "outcome": "applied",
            "candidate_count": 0,
            "candidates_sha256": "a" * 64,
            "run_manifest_sha256": "b" * 64,
            "rows_written": 1,
        }


class DeferredScanWorkerTest(unittest.TestCase):
    def _write_roster(self, owners):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "roster.json"
        value = {
            "contract_version": "memory_v1_deferred_scanner_owner_roster_v1",
            "source": "user_approved_active_accounts_20260715",
            "owners": owners,
        }
        path.write_text(json.dumps(value, indent=2) + "\n")
        return directory, path

    def test_roster_requires_exact_hash_sorted_unique(self):
        directory, path = self._write_roster([str(OWNER_A)])
        self.addCleanup(directory.cleanup)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        owners, actual = load_roster(str(path), digest)
        self.assertEqual(owners, [OWNER_A])
        self.assertEqual(actual, digest)
        with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
            load_roster(str(path), "0" * 64)
        with self.assertRaisesRegex(RuntimeError, "SHA-256 is required"):
            load_roster(str(path), "G" * 64)

    def test_roster_rejects_duplicates(self):
        directory, path = self._write_roster([str(OWNER_A), str(OWNER_A)])
        self.addCleanup(directory.cleanup)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "sorted and unique"):
            load_roster(str(path), digest)

    def test_owner_scan_sets_actor_and_writes_one_audit(self):
        conn = _Connection()
        run_id = uuid.UUID("47000000-0000-4000-8000-000000000001")
        result = asyncio.run(
            run_owner(
                conn,
                owner=OWNER_A,
                roster_sha256="c" * 64,
                worker_ref="test-worker",
                limit=25,
                run_id=run_id,
            )
        )
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(conn.calls[0][0], "execute")
        self.assertEqual(conn.calls[0][2], (str(OWNER_A),))
        self.assertEqual(conn.calls[1][0], "fetchrow")
        self.assertEqual(conn.calls[1][2][0], run_id)


if __name__ == "__main__":
    unittest.main()
