#!/usr/bin/env python3

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
import sys
import unittest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from structured_fixture_plan import (  # noqa: E402
    FixtureExecutionFailure,
    FixturePlan,
    FixturePlanError,
    FixtureStage,
    execute_fixture_plan,
)


OWNER_A = "11111111-1111-4111-8111-111111111111"
OWNER_B = "22222222-2222-4222-8222-222222222222"


@dataclasses.dataclass
class Result:
    returncode: int
    stdout: bytes
    stderr: bytes


class Database:
    def __init__(self, result: Result) -> None:
        self.result = result
        self.sql = b""

    def psql(self, sql: bytes, *, accept=(0,), timeout: int = 120) -> Result:
        self.sql = sql
        return self.result


def stage(
    stage_id: str,
    sql: bytes = b"SELECT 1;\n",
    *,
    role: str = "brains_app",
    owner: str = OWNER_A,
    capability: str = "owner_api",
) -> FixtureStage:
    return FixtureStage(
        stage_id,
        sql,
        role,
        owner,
        capability,
        hashlib.sha256(sql).hexdigest(),
    )


def plan(*stages: FixtureStage) -> FixturePlan:
    return FixturePlan(tuple(stages), tuple(item.stage_id for item in stages))


class StructuredFixturePlanTests(unittest.TestCase):
    def test_repeated_identical_sql_is_valid_and_has_distinct_markers(self) -> None:
        fixture = plan(stage("first"), stage("second"))
        rendered = fixture.render()
        self.assertEqual(rendered.count(b"SELECT 1;"), 2)
        self.assertIn(b"__LS_FIXTURE_STAGE__first", rendered)
        self.assertIn(b"__LS_FIXTURE_STAGE__second", rendered)
        self.assertEqual(fixture.sha256(), fixture.sha256())

    def test_whitespace_and_comments_do_not_control_stage_identity(self) -> None:
        first = plan(stage("one", b"-- comment\nSELECT 1;\n"))
        second = plan(stage("one", b"SELECT    1;\n"))
        self.assertIn(b"__LS_FIXTURE_STAGE__one", first.render())
        self.assertIn(b"__LS_FIXTURE_STAGE__one", second.render())
        self.assertNotEqual(first.sha256(), second.sha256())

    def test_missing_reordered_and_duplicate_stages_fail(self) -> None:
        with self.assertRaisesRegex(FixturePlanError, "order"):
            FixturePlan((stage("a"),), ("a", "b")).validate()
        with self.assertRaisesRegex(FixturePlanError, "order"):
            FixturePlan((stage("b"), stage("a")), ("a", "b")).validate()
        with self.assertRaisesRegex(FixturePlanError, "duplicated"):
            FixturePlan((stage("a"), stage("a")), ("a", "a")).validate()

    def test_invalid_role_and_writer_escape_fail(self) -> None:
        with self.assertRaisesRegex(FixturePlanError, "role"):
            plan(stage("bad", role="not-allowed")).validate()
        with self.assertRaisesRegex(FixturePlanError, "writer authority"):
            plan(
                stage(
                    "bad",
                    b"INSERT INTO memory.relational_stage_batch(owner_user_id) VALUES(NULL);",
                    capability="stage_write",
                )
            ).validate()

    def test_all_stage_relation_spellings_are_denied_to_brains_app(self) -> None:
        forbidden = (
            b'INSERT INTO "memory"."relational_stage_batch"(owner_user_id) VALUES(NULL);',
            b"INSERT INTO ONLY memory.relational_stage_batch(owner_user_id) VALUES(NULL);",
            b"INSERT INTO memory.\nrelational_stage_batch(owner_user_id) VALUES(NULL);",
            b"UPDATE memory.relational_stage_batch SET result='{}';",
            b"DELETE FROM memory /* gap */ . relational_stage_batch;",
            b"INSERT INTO relational_stage_batch(owner_user_id) VALUES(NULL);",
        )
        for ordinal, sql in enumerate(forbidden):
            with self.subTest(ordinal=ordinal):
                with self.assertRaisesRegex(FixturePlanError, "crosses"):
                    plan(stage("bad", sql)).validate()
        with self.assertRaisesRegex(FixturePlanError, "crosses"):
            plan(
                stage(
                    "bad",
                    b"INSERT INTO memory.relational_stage_batch(owner_user_id) VALUES(NULL);",
                )
            ).validate()

    def test_writer_role_cannot_run_owner_api(self) -> None:
        with self.assertRaisesRegex(FixturePlanError, "crosses"):
            plan(stage("bad", role="memory_v5_writer")).validate()

    def test_stage_writer_requires_an_allowlisted_write_operation(self) -> None:
        with self.assertRaisesRegex(FixturePlanError, "writer authority"):
            plan(
                stage(
                    "bad",
                    b"SELECT * FROM memory.relational_stage_batch;",
                    role="memory_v5_writer",
                    capability="stage_write",
                )
            ).validate()

    def test_stage_sql_bytes_are_exactly_hash_bound(self) -> None:
        original = stage("bound")
        mutated = dataclasses.replace(original, sql=b"SELECT 2;\n")
        with self.assertRaisesRegex(FixturePlanError, "hash differs"):
            plan(mutated).validate()

    def test_transaction_identity_and_policy_control_are_plan_owned(self) -> None:
        rejected = (
            b"BEGIN;",
            b"ROLLBACK;",
            b"SET SESSION AUTHORIZATION brains_app;",
            b"RESET ROLE;",
            b"SELECT set_config('app.user_id','x',false);",
            b"SET app.user_id='x';",
            b"/* comment */ SET ROLE memory_v5_writer;",
            b'SET "app.user_id"=\'x\';',
            b"SELECT set_config('role','memory_v5_writer',false);",
            b"SELECT set_config('session_authorization','brains_app',false);",
            b"RESET ALL;",
            b"GRANT SELECT ON memory.evidence TO brains_app;",
            b"ALTER POLICY owner_isolation ON memory.evidence USING (true);",
        )
        for ordinal, sql in enumerate(rejected):
            with self.subTest(ordinal=ordinal):
                with self.assertRaises(FixturePlanError):
                    plan(stage("bad", sql)).validate()

    def test_set_config_is_exactly_allowlisted_and_indirection_is_rejected(self) -> None:
        allowed_sql = b"SELECT set_config('app.fixture_packet','x',false);"
        allowed = FixturePlan(
            (stage("allowed", allowed_sql),),
            ("allowed",),
            allowed_stage_settings=("app.fixture_packet",),
        )
        allowed.validate()
        rejected = (
            b"SELECT set_config(E'role','memory_v5_writer',false);",
            b"SELECT set_config('ro'||'le','memory_v5_writer',false);",
            b'INSERT INTO U&"relational_stage_b\\0061tch"(owner_user_id) VALUES(NULL);',
            b"EXECUTE 'SELECT 1';",
        )
        for ordinal, sql in enumerate(rejected):
            with self.subTest(ordinal=ordinal):
                with self.assertRaises(FixturePlanError):
                    plan(stage("bad", sql)).validate()

    def test_plpgsql_control_inside_a_dollar_quote_is_not_outer_control(self) -> None:
        fixture = plan(
            stage(
                "block",
                b"DO $fixture$\nBEGIN\n  PERFORM 1;\nEND\n$fixture$;\n",
            )
        )
        fixture.validate()
        self.assertIn(b"DO $fixture$", fixture.render())

    def test_unterminated_dollar_quote_fails_closed(self) -> None:
        with self.assertRaisesRegex(FixturePlanError, "unterminated"):
            plan(stage("bad", b"DO $fixture$\nBEGIN\n  PERFORM 1;\n")).validate()

    def test_rendered_roles_are_explicit_and_bounded(self) -> None:
        fixture = plan(
            stage("api"),
            stage(
                "write",
                b"INSERT INTO memory.relational_stage_batch(owner_user_id) VALUES(NULL);",
                role="memory_v5_writer",
                capability="stage_write",
            ),
            stage("other_owner", owner=OWNER_B),
        )
        rendered = fixture.render()
        self.assertEqual(rendered.count(b"SET SESSION AUTHORIZATION brains_app;"), 2)
        self.assertEqual(rendered.count(b"SET LOCAL ROLE memory_v5_writer;"), 1)
        self.assertEqual(rendered.count(OWNER_A.encode("ascii")), 2)
        self.assertEqual(rendered.count(OWNER_B.encode("ascii")), 1)

    def test_success_requires_exact_complete_marker_sequence(self) -> None:
        fixture = plan(stage("one"), stage("two"))
        stdout = b"\n".join(
            b"__LS_FIXTURE_STAGE__" + item.encode("ascii")
            for item in fixture.stage_sequence()
        )
        database = Database(Result(0, stdout + b"\n", b""))
        execute_fixture_plan(database, fixture)
        self.assertEqual(database.sql, fixture.render())
        self.assertTrue(database.sql.startswith(b"\\set ON_ERROR_STOP on\n"))

    def test_failure_reports_only_current_stage_and_sqlstate(self) -> None:
        database = Database(
            Result(
                3,
                b"__LS_FIXTURE_STAGE__transaction_begin\n__LS_FIXTURE_STAGE__one\n",
                b"ERROR:  42501: suppressed\n",
            )
        )
        with self.assertRaises(FixtureExecutionFailure) as raised:
            execute_fixture_plan(database, plan(stage("one")))
        self.assertEqual(
            raised.exception.record(),
            {
                "exception_family": "insufficient_privilege",
                "schema_version": "governed-structured-fixture-plan-v1",
                "sqlstate": "42501",
                "stage": "one",
            },
        )

    def test_noise_reordering_incomplete_and_oversized_output_fail_closed(self) -> None:
        fixture = plan(stage("one"))
        cases = (
            Result(0, b"__LS_FIXTURE_STAGE__one\n", b""),
            Result(0, b"__LS_FIXTURE_STAGE__transaction_begin\n", b""),
            Result(
                0,
                b"__LS_FIXTURE_STAGE__transaction_begin\n"
                b"__LS_FIXTURE_STAGE__unexpected\n",
                b"",
            ),
            Result(0, b"x" * (fixture.max_rendered_bytes + 1), b""),
            Result(
                0,
                b"noise-__LS_FIXTURE_STAGE__transaction_begin trailing\n"
                b"noise-__LS_FIXTURE_STAGE__one trailing\n"
                b"noise-__LS_FIXTURE_STAGE__transaction_rollback trailing\n"
                b"noise-__LS_FIXTURE_STAGE__transaction_complete trailing\n",
                b"",
            ),
        )
        for ordinal, result in enumerate(cases):
            with self.subTest(ordinal=ordinal):
                with self.assertRaises(FixtureExecutionFailure):
                    execute_fixture_plan(Database(result), fixture)

    def test_rollback_marker_without_completion_fails_closed(self) -> None:
        fixture = plan(stage("one"))
        stdout = b"\n".join(
            b"__LS_FIXTURE_STAGE__" + item.encode("ascii")
            for item in fixture.stage_sequence()[:-1]
        )
        with self.assertRaisesRegex(FixtureExecutionFailure, "failed"):
            execute_fixture_plan(Database(Result(0, stdout + b"\n", b"")), fixture)

    def test_invalid_owner_reserved_marker_and_oversized_sql_fail(self) -> None:
        bad = (
            stage("bad", owner="not-a-uuid"),
            stage("bad", b"SELECT '__LS_FIXTURE_STAGE__x';"),
            stage("bad", b"x" * (2 * 1024 * 1024 + 1)),
        )
        for value in bad:
            with self.assertRaises(FixturePlanError):
                plan(value).validate()


if __name__ == "__main__":
    unittest.main()
