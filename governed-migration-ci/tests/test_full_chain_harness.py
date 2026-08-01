from __future__ import annotations

import json
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

from full_chain_harness import (  # noqa: E402
    CommandResult,
    DockerPostgres,
    FullChain,
    HarnessError,
    ledger_identity,
    target_state,
)
from governed_migration import canonical_bytes, sha256  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeRunner:
    def __init__(self, responses: list[CommandResult]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], bytes | None, int]] = []

    def run(self, argv: list[str], *, input_bytes: bytes | None = None, timeout: int = 120) -> CommandResult:
        self.calls.append((list(argv), input_bytes, timeout))
        if not self.responses:
            raise AssertionError("unexpected subprocess call")
        return self.responses.pop(0)


class DockerAdapterTest(unittest.TestCase):
    image = "postgres@sha256:" + "a" * 64

    def database(self, runner: FakeRunner, run_id: str = "fixture-run") -> DockerPostgres:
        return DockerPostgres(pathlib.Path("/usr/bin/true"), self.image, run_id, runner)

    def test_rejects_unpinned_image(self) -> None:
        with self.assertRaises(HarnessError):
            DockerPostgres(pathlib.Path("/usr/bin/true"), "postgres:16", "fixture-run", FakeRunner([]))

    def test_rejects_malformed_run_id(self) -> None:
        with self.assertRaises(HarnessError):
            DockerPostgres(pathlib.Path("/usr/bin/true"), self.image, "../escape", FakeRunner([]))

    def test_image_digest_must_match(self) -> None:
        runner = FakeRunner([CommandResult(0, b"postgres@sha256:" + b"b" * 64 + b"\n", b"")])
        with self.assertRaisesRegex(HarnessError, "digest differs"):
            self.database(runner).verify_image()

    def test_launch_is_networkless_resource_bounded_and_unpublished(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(1, b"", b"not found"),
                CommandResult(0, b"container-id\n", b""),
                CommandResult(0, b"accepting connections\n", b""),
                CommandResult(0, b"1\n", b""),
            ]
        )
        database = self.database(runner)
        database.launch()
        run_argv = runner.calls[1][0]
        self.assertIn("none", run_argv[run_argv.index("--network") + 1 :])
        self.assertIn("--memory", run_argv)
        self.assertIn("--cpus", run_argv)
        self.assertIn("--pids-limit", run_argv)
        self.assertIn("no-new-privileges", run_argv)
        self.assertNotIn("--publish", run_argv)
        self.assertNotIn("-p", run_argv)
        self.assertNotIn("--volume", run_argv)

    def test_psql_uses_stdin_not_process_argument(self) -> None:
        runner = FakeRunner([CommandResult(0, b"", b"")])
        database = self.database(runner)
        database.started = True
        payload = b"SELECT 1;\n"
        database.psql(payload)
        argv, input_bytes, _ = runner.calls[0]
        self.assertEqual(input_bytes, payload)
        self.assertNotIn("SELECT 1;", argv)
        self.assertIn("--no-psqlrc", argv)

    def test_cleanup_is_idempotent_when_container_absent(self) -> None:
        runner = FakeRunner([CommandResult(1, b"", b"not found")])
        database = self.database(runner)
        database.cleanup()
        self.assertFalse(database.started)
        self.assertEqual(len(runner.calls), 1)

    def test_cleanup_rejects_label_substitution(self) -> None:
        runner = FakeRunner([CommandResult(0, b"different-run\n", b"")])
        database = self.database(runner)
        with self.assertRaisesRegex(HarnessError, "identity changed"):
            database.cleanup()
        self.assertEqual(len(runner.calls), 1)

    def test_cleanup_removes_only_exact_labeled_container(self) -> None:
        runner = FakeRunner(
            [
                CommandResult(0, b"fixture-run\n", b""),
                CommandResult(0, b"fixture-run\n", b""),
                CommandResult(1, b"", b"not found"),
            ]
        )
        database = self.database(runner)
        database.started = True
        database.cleanup()
        remove = runner.calls[1][0]
        self.assertEqual(remove[-3:], ["rm", "--force", "gmci-fixture-run"])
        self.assertFalse(database.started)


class FakeDatabase:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = list(outputs)
        self.calls: list[bytes] = []

    def psql(self, sql: bytes, **_kwargs) -> CommandResult:
        self.calls.append(sql)
        if not self.outputs:
            raise AssertionError("unexpected psql request")
        return CommandResult(0, self.outputs.pop(0), b"")


class StateFingerprintTest(unittest.TestCase):
    def test_empty_target_state_is_deterministic(self) -> None:
        database = FakeDatabase([b""])
        objects, digest = target_state(database)  # type: ignore[arg-type]
        self.assertEqual(objects, [])
        self.assertEqual(digest, sha256(canonical_bytes([])))

    def test_target_state_binds_owner_rls_policy_and_grants(self) -> None:
        outputs = [
            b"gm_fixture\tgm_ci_owner\t{gm_ci_owner=UC/gm_ci_owner}\n",
            b"gm_ci_owner\tr\t1\t1\t{gm_ci_owner=arwdDxt/gm_ci_owner}\n",
            b"governed_records_pkey\tp\tPRIMARY KEY (record_key)\n",
            b"governed_records_pkey\tCREATE UNIQUE INDEX governed_records_pkey ON gm_fixture.governed_records USING btree (record_key)\n",
            b"owner_key\ttext\t1\t\nrecord_key\ttext\t1\t\n",
            b"owner_isolation\t1\tgm_ci_reader\t*\t(owner_key = current_setting('app.owner_key'::text, true))\t(owner_key = current_setting('app.owner_key'::text, true))\n",
            b"1\n",
            b"1\n",
        ]
        objects, digest = target_state(FakeDatabase(outputs))  # type: ignore[arg-type]
        self.assertEqual(len(objects), 7)
        self.assertEqual([item["id"] for item in objects], sorted(item["id"] for item in objects))
        self.assertEqual(len(digest), 64)
        self.assertTrue(all(item["owner"] == "gm_ci_owner" for item in objects))

    def test_missing_relation_fails_closed(self) -> None:
        database = FakeDatabase([b"gm_fixture\tgm_ci_owner\t{}\n", b""])
        with self.assertRaisesRegex(HarnessError, "relation"):
            target_state(database)  # type: ignore[arg-type]


class LedgerIdentityTest(unittest.TestCase):
    def test_ledger_identity_binds_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "ledger").mkdir()
            value = {
                "schema_version": "governed-memory-schema-ledger-v1",
                "ledger_id": "baseline_one",
                "baseline": {"catalog_evidence_sha256": "a" * 64},
            }
            payload = canonical_bytes(value)
            (root / "ledger/governed-memory-schema-ledger-v1.json").write_bytes(payload)
            identity = ledger_identity(root)
            self.assertEqual(identity["ledger_sha256"], sha256(payload))
            self.assertEqual(identity["catalog_evidence_sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
