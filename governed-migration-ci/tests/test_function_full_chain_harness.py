from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

from full_chain_harness import CommandResult, HarnessError  # noqa: E402
from function_full_chain_harness import verify_function_state, verify_rls_dependencies  # noqa: E402
from governed_function_migration import FunctionSpec  # noqa: E402
from governed_migration import sha256  # noqa: E402


class FakeDatabase:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = list(outputs)
        self.calls: list[bytes] = []

    def psql(self, sql: bytes, **_kwargs: object) -> CommandResult:
        self.calls.append(sql)
        if not self.outputs:
            raise AssertionError("unexpected PostgreSQL request")
        return CommandResult(0, self.outputs.pop(0), b"")


def spec(definition_hash: str) -> FunctionSpec:
    return FunctionSpec(
        "memory.fixture_api_v1",
        "memory.fixture_api_v1(p_owner uuid)",
        "memory.fixture_api_v1(uuid)",
        "sage",
        "plpgsql",
        "volatile",
        "unsafe",
        False,
        False,
        ("pg_catalog", "memory"),
        ("memory_reader",),
        ("memory_reader",),
        ({"relation": "memory.fixture", "owner": "sage", "forced": True, "policies": ["owner_policy"]},),
        definition_hash,
        definition_hash,
        "fixture-forward.pgsql",
        "a" * 64,
        "fixture-rollback.pgsql",
        "b" * 64,
    )


class FunctionStateTest(unittest.TestCase):
    def test_binds_definition_owner_security_search_path_and_acl(self) -> None:
        definition = b"CREATE OR REPLACE FUNCTION memory.fixture_api_v1(p_owner uuid) RETURNS uuid ...\n"
        database = FakeDatabase(
            [
                b"sage\tplpgsql\t1\t0\tv\tu\t0\tsearch_path=pg_catalog, memory\n",
                b"memory_reader\tEXECUTE\t0\n",
                sha256(definition).encode("ascii") + b"\n",
            ]
        )
        observed = verify_function_state(database, spec(sha256(definition)), rollback=False)  # type: ignore[arg-type]
        self.assertEqual(observed["definition_sha256"], sha256(definition))
        self.assertEqual(len(database.calls), 3)
        self.assertIn(b"public.digest", database.calls[2])
        self.assertIn(b"'sha256'", database.calls[2])

    def test_rejects_public_execute(self) -> None:
        definition = b"definition"
        database = FakeDatabase(
            [
                b"sage\tplpgsql\t1\t0\tv\tu\t0\tsearch_path=pg_catalog, memory\n",
                b"PUBLIC\tEXECUTE\t0\nmemory_reader\tEXECUTE\t0\n",
                sha256(definition).encode("ascii") + b"\n",
            ]
        )
        with self.assertRaisesRegex(HarnessError, "ACL differs"):
            verify_function_state(database, spec(sha256(definition)), rollback=False)  # type: ignore[arg-type]

    def test_rejects_search_path_drift(self) -> None:
        definition = b"definition"
        database = FakeDatabase(
            [
                b"sage\tplpgsql\t1\t0\tv\tu\t0\tsearch_path=memory, public\n",
                b"memory_reader\tEXECUTE\t0\n",
                sha256(definition).encode("ascii") + b"\n",
            ]
        )
        with self.assertRaisesRegex(HarnessError, "search path"):
            verify_function_state(database, spec(sha256(definition)), rollback=False)  # type: ignore[arg-type]


class RlsDependencyTest(unittest.TestCase):
    def test_accepts_exact_forced_rls_and_policy(self) -> None:
        database = FakeDatabase([b"sage\t1\t1\n", b"owner_policy\n"])
        result = verify_rls_dependencies(database, spec("a" * 64))  # type: ignore[arg-type]
        self.assertEqual(result[0]["rls_forced"], True)

    def test_rejects_nonforced_relation(self) -> None:
        database = FakeDatabase([b"sage\t1\t0\n", b"owner_policy\n"])
        with self.assertRaisesRegex(HarnessError, "RLS dependency"):
            verify_rls_dependencies(database, spec("a" * 64))  # type: ignore[arg-type]

    def test_rejects_extra_policy(self) -> None:
        database = FakeDatabase([b"sage\t1\t1\n", b"extra_policy\nowner_policy\n"])
        with self.assertRaisesRegex(HarnessError, "RLS dependency"):
            verify_rls_dependencies(database, spec("a" * 64))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
