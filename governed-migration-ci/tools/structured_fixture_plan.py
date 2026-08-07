#!/usr/bin/env python3
"""Deterministic, content-free PostgreSQL fixture-plan execution."""

from __future__ import annotations

import dataclasses
import hashlib
import re
import uuid
from collections.abc import Callable, Sequence
from typing import Protocol


SCHEMA_VERSION = "governed-structured-fixture-plan-v1"
MARKER_PREFIX = "__LS_FIXTURE_STAGE__"
STAGE_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
ROLE_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SQLSTATE_RE = re.compile(rb"ERROR:\s+([0-9A-Z]{5}):")
MARKER_RE = re.compile(rb"^__LS_FIXTURE_STAGE__([a-z][a-z0-9_]{0,62})$")
CONTROL_RE = re.compile(
    rb"(?im)^[ \t]*(?:BEGIN|COMMIT|ROLLBACK|SET[ \t]+(?:SESSION[ \t]+AUTHORIZATION|(?:LOCAL[ \t]+)?ROLE)|RESET[ \t]+(?:SESSION[ \t]+AUTHORIZATION|ROLE))\b"
)
DOLLAR_TAG_RE = re.compile(rb"\$(?:[a-zA-Z_][a-zA-Z0-9_]*)?\$")
SQL_COMMENT_RE = re.compile(rb"--[^\r\n]*(?:\r?\n|$)|/\*.*?\*/", re.DOTALL)
STAGE_RELATION_TOKEN = (
    rb'(?:(?:"memory"|memory)\s*\.\s*)?'
    rb'(?:"relational_stage_batch"|relational_stage_batch)'
)
STAGE_RELATION_RE = re.compile(
    rb'(?is)(?<![a-z0-9_"])' + STAGE_RELATION_TOKEN + rb'(?![a-z0-9_"])'
)
STAGE_WRITE_OPERATION_RE = re.compile(
    rb'(?is)\b(?:INSERT\s+INTO(?:\s+ONLY)?|UPDATE|DELETE\s+FROM)\s+(?:ONLY\s+)?'
    + STAGE_RELATION_TOKEN
    + rb'(?![a-z0-9_"])'
)
IDENTITY_CONTROL_RE = re.compile(
    rb'''(?is)\b(?:
        SET\s+(?:(?:SESSION\s+)?AUTHORIZATION|(?:LOCAL\s+)?ROLE|(?:LOCAL\s+|SESSION\s+)?(?:"app\.user_id"|app\s*\.\s*user_id|"role"|role|"session_authorization"|session_authorization))
        |RESET\s+(?:ALL|SESSION\s+AUTHORIZATION|ROLE|"app\.user_id"|app\s*\.\s*user_id|"role"|role|"session_authorization"|session_authorization)
        |set_config\s*\(\s*'(?:app\.user_id|role|session_authorization)'
    )''',
    re.VERBOSE,
)
SET_CONFIG_TOKEN_RE = re.compile(rb"(?is)\bset_config\s*\(")
SET_CONFIG_LITERAL_RE = re.compile(
    rb"(?is)\bset_config\s*\(\s*'([a-z][a-z0-9_.]{0,127})'\s*,"
)
UNICODE_IDENTIFIER_RE = re.compile(rb'(?is)\bU&\s*"')
DYNAMIC_SQL_RE = re.compile(rb"(?is)\bEXECUTE\b")
FORBIDDEN_DDL_RE = re.compile(
    rb"(?im)^[ \t]*(?:GRANT|REVOKE|ALTER[ \t]+POLICY|CREATE[ \t]+POLICY|DROP[ \t]+POLICY)\b"
)


class FixturePlanError(RuntimeError):
    """The plan is structurally unsafe or non-deterministic."""


class FixtureExecutionFailure(RuntimeError):
    """A content-free PostgreSQL fixture failure."""

    def __init__(self, stage: str, sqlstate: str, family: str) -> None:
        super().__init__("structured fixture execution failed")
        self.stage = stage
        self.sqlstate = sqlstate
        self.family = family

    def record(self) -> dict[str, str]:
        return {
            "exception_family": self.family,
            "schema_version": SCHEMA_VERSION,
            "sqlstate": self.sqlstate,
            "stage": self.stage,
        }


class CommandResult(Protocol):
    returncode: int
    stdout: bytes
    stderr: bytes


class FixtureDatabase(Protocol):
    def psql(
        self,
        sql: bytes,
        *,
        accept: Sequence[int] = (0,),
        timeout: int = 120,
    ) -> CommandResult: ...


@dataclasses.dataclass(frozen=True)
class FixtureStage:
    stage_id: str
    sql: bytes
    execution_role: str
    owner_user_id: str
    capability: str = "owner_api"
    expected_sql_sha256: str = ""


@dataclasses.dataclass(frozen=True)
class FixturePlan:
    stages: tuple[FixtureStage, ...]
    required_order: tuple[str, ...]
    application_role: str = "brains_app"
    stage_writer_role: str = "memory_v5_writer"
    allowed_stage_settings: tuple[str, ...] = ()
    max_stage_count: int = 64
    max_stage_bytes: int = 2 * 1024 * 1024
    max_rendered_bytes: int = 16 * 1024 * 1024

    def validate(self) -> None:
        if not self.stages or len(self.stages) > self.max_stage_count:
            raise FixturePlanError("fixture stage count is invalid")
        identifiers = tuple(stage.stage_id for stage in self.stages)
        if identifiers != self.required_order:
            raise FixturePlanError("fixture stage order differs")
        if len(set(identifiers)) != len(identifiers):
            raise FixturePlanError("fixture stage identifiers are duplicated")
        if any(STAGE_ID_RE.fullmatch(value) is None for value in identifiers):
            raise FixturePlanError("fixture stage identifier is invalid")
        if any(
            ROLE_RE.fullmatch(value) is None
            for value in (self.application_role, self.stage_writer_role)
        ):
            raise FixturePlanError("fixture execution role is invalid")
        if self.application_role == self.stage_writer_role:
            raise FixturePlanError("fixture roles are not separated")
        if (
            tuple(sorted(set(self.allowed_stage_settings)))
            != self.allowed_stage_settings
            or any(
                re.fullmatch(r"[a-z][a-z0-9_.]{0,127}", value) is None
                for value in self.allowed_stage_settings
            )
        ):
            raise FixturePlanError("fixture stage settings are invalid")
        for stage in self.stages:
            self._validate_stage(stage)

    def _validate_stage(self, stage: FixtureStage) -> None:
        if not isinstance(stage.sql, bytes) or not stage.sql:
            raise FixturePlanError("fixture stage SQL is empty")
        if len(stage.sql) > self.max_stage_bytes:
            raise FixturePlanError("fixture stage SQL is oversized")
        try:
            stage.sql.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise FixturePlanError("fixture stage SQL is not UTF-8") from exc
        if b"\0" in stage.sql or MARKER_PREFIX.encode("ascii") in stage.sql:
            raise FixturePlanError("fixture stage SQL contains reserved bytes")
        if (
            SHA256_RE.fullmatch(stage.expected_sql_sha256) is None
            or hashlib.sha256(stage.sql).hexdigest() != stage.expected_sql_sha256
        ):
            raise FixturePlanError("fixture stage SQL hash differs")
        uncommented = SQL_COMMENT_RE.sub(b" ", stage.sql)
        if UNICODE_IDENTIFIER_RE.search(uncommented) or DYNAMIC_SQL_RE.search(uncommented):
            raise FixturePlanError("fixture stage uses unsupported SQL indirection")
        set_config_tokens = SET_CONFIG_TOKEN_RE.findall(uncommented)
        set_config_names = SET_CONFIG_LITERAL_RE.findall(uncommented)
        if len(set_config_tokens) != len(set_config_names) or any(
            value.decode("ascii") not in self.allowed_stage_settings
            for value in set_config_names
        ):
            raise FixturePlanError("fixture stage setting is not allowlisted")
        if _contains_outer_control(uncommented) or IDENTITY_CONTROL_RE.search(uncommented):
            raise FixturePlanError("fixture stage owns transaction or identity control")
        if FORBIDDEN_DDL_RE.search(uncommented):
            raise FixturePlanError("fixture stage changes grants or policies")
        if ROLE_RE.fullmatch(stage.execution_role) is None:
            raise FixturePlanError("fixture stage role is invalid")
        try:
            owner = uuid.UUID(stage.owner_user_id)
        except (ValueError, AttributeError) as exc:
            raise FixturePlanError("fixture owner identity is invalid") from exc
        if str(owner) != stage.owner_user_id or owner.version != 4:
            raise FixturePlanError("fixture owner identity is not canonical UUIDv4")
        touches_stage = STAGE_RELATION_RE.search(uncommented) is not None
        writes_stage = STAGE_WRITE_OPERATION_RE.search(uncommented) is not None
        if stage.capability == "stage_write":
            if (
                stage.execution_role != self.stage_writer_role
                or not touches_stage
                or not writes_stage
            ):
                raise FixturePlanError("stage write lacks exact writer authority")
        elif stage.capability == "owner_api":
            if stage.execution_role != self.application_role or touches_stage:
                raise FixturePlanError("owner API stage crosses stage-table authority")
        else:
            raise FixturePlanError("fixture stage capability is invalid")

    def stage_sequence(self) -> tuple[str, ...]:
        return (
            "transaction_begin",
            *self.required_order,
            "transaction_rollback",
            "transaction_complete",
        )

    def render(self) -> bytes:
        self.validate()
        pieces = [
            b"\\set ON_ERROR_STOP on\n",
            b"\\set VERBOSITY verbose\n",
            _marker("transaction_begin"),
            b"BEGIN;\n",
        ]
        for stage in self.stages:
            pieces.append(_marker(stage.stage_id))
            pieces.append(_identity_sql(stage.execution_role, stage.owner_user_id, self))
            pieces.append(stage.sql)
            if not stage.sql.endswith(b"\n"):
                pieces.append(b"\n")
        pieces.extend(
            [
                _marker("transaction_rollback"),
                b"ROLLBACK;\n",
                _marker("transaction_complete"),
            ]
        )
        rendered = b"".join(pieces)
        if len(rendered) > self.max_rendered_bytes:
            raise FixturePlanError("rendered fixture plan is oversized")
        return rendered

    def sha256(self) -> str:
        return hashlib.sha256(self.render()).hexdigest()


def _marker(stage_id: str) -> bytes:
    return f"\\echo {MARKER_PREFIX}{stage_id}\n".encode("ascii")


def _contains_outer_control(sql: bytes) -> bool:
    active_tag: bytes | None = None
    for line in sql.splitlines():
        remaining = line
        while True:
            if active_tag is not None:
                closing = remaining.find(active_tag)
                if closing < 0:
                    break
                remaining = remaining[closing + len(active_tag) :]
                active_tag = None
                continue
            if CONTROL_RE.search(remaining):
                return True
            opening = DOLLAR_TAG_RE.search(remaining)
            if opening is None:
                break
            tag = opening.group(0)
            suffix = remaining[opening.end() :]
            closing = suffix.find(tag)
            if closing >= 0:
                remaining = suffix[closing + len(tag) :]
                continue
            active_tag = tag
            break
    if active_tag is not None:
        raise FixturePlanError("fixture stage has an unterminated dollar quote")
    return False


def _identity_sql(role: str, owner_user_id: str, plan: FixturePlan) -> bytes:
    if role == plan.application_role:
        role_sql = (
            b"RESET ROLE;\nRESET SESSION AUTHORIZATION;\n"
            + f"SET SESSION AUTHORIZATION {role};\n".encode("ascii")
        )
    elif role == plan.stage_writer_role:
        role_sql = (
            b"RESET ROLE;\nRESET SESSION AUTHORIZATION;\n"
            + f"SET LOCAL ROLE {role};\n".encode("ascii")
        )
    else:
        raise FixturePlanError("fixture stage role is not allowlisted")
    return role_sql + (
        "SELECT set_config('app.user_id','" + owner_user_id + "',false);\n"
    ).encode("ascii")


def sqlstate_family(sqlstate: str) -> str:
    exact = {
        "22012": "division_by_zero",
        "22023": "invalid_parameter_value",
        "23503": "foreign_key_violation",
        "23505": "unique_violation",
        "23514": "check_violation",
        "42501": "insufficient_privilege",
        "42702": "ambiguous_column",
        "42703": "undefined_column",
        "42883": "undefined_function",
        "P0001": "raise_exception",
    }
    return exact.get(sqlstate, "postgresql_sqlstate_class_" + sqlstate[:2].lower())


def execute_fixture_plan(
    database: FixtureDatabase,
    plan: FixturePlan,
    *,
    timeout: int = 180,
    before_execute: Callable[[bytes], None] | None = None,
) -> CommandResult:
    rendered = plan.render()
    if before_execute is not None:
        before_execute(rendered)
    result = database.psql(rendered, accept=(0, 1, 2, 3), timeout=timeout)
    if len(result.stdout) > plan.max_rendered_bytes or len(result.stderr) > 4 * 1024 * 1024:
        raise FixtureExecutionFailure("bounded_output", "XX000", "bounded_output_failure")
    observed = tuple(
        match.group(1).decode("ascii")
        for line in result.stdout.splitlines()
        if (match := MARKER_RE.fullmatch(line)) is not None
    )
    expected = plan.stage_sequence()
    if observed != expected[: len(observed)] or len(observed) > len(expected):
        raise FixtureExecutionFailure("marker_sequence", "XX000", "stage_sequence_failure")
    current = observed[-1] if observed else "no_stage"
    if result.returncode != 0:
        states = SQLSTATE_RE.findall(result.stderr)
        sqlstate = states[-1].decode("ascii") if states else "XX000"
        raise FixtureExecutionFailure(current, sqlstate, sqlstate_family(sqlstate))
    if observed != expected:
        raise FixtureExecutionFailure(current, "XX000", "incomplete_stage_sequence")
    return result
