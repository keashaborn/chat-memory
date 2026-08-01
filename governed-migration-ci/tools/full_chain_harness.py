#!/usr/bin/env python3
"""Network-isolated disposable PostgreSQL full-chain migration rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Mapping

sys.dont_write_bytecode = True

from governed_migration import LoadedPackage, MigrationError, canonical_bytes, load_package, parse_canonical, sha256


RUN_ID_RE = re.compile(r"[a-z][a-z0-9-]{3,48}")
IMAGE_RE = re.compile(r"postgres@sha256:[0-9a-f]{64}")
HEX_RE = re.compile(r"[0-9a-f]{64}")
ROLE_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")

BOOTSTRAP_SQL = b"""\
CREATE ROLE gm_ci_owner NOLOGIN;
CREATE ROLE gm_ci_reader NOLOGIN;
CREATE SCHEMA gm_ci AUTHORIZATION gm_ci_admin;
REVOKE ALL ON SCHEMA gm_ci FROM PUBLIC;
CREATE TABLE gm_ci.execution_events (
    event_sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id text NOT NULL,
    migration_id text NOT NULL,
    event_ordinal integer NOT NULL CHECK (event_ordinal > 0),
    event_type text NOT NULL,
    package_sha256 text NOT NULL CHECK (package_sha256 ~ '^[0-9a-f]{64}$'),
    previous_event_sha256 text NOT NULL CHECK (previous_event_sha256 ~ '^[0-9a-f]{64}$'),
    event_sha256 text NOT NULL UNIQUE CHECK (event_sha256 ~ '^[0-9a-f]{64}$'),
    state_sha256 text NOT NULL CHECK (state_sha256 ~ '^[0-9a-f]{64}$'),
    detail jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, event_ordinal)
);
REVOKE ALL ON gm_ci.execution_events FROM PUBLIC;
CREATE FUNCTION gm_ci.reject_event_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'execution events are append-only'; END $$;
CREATE TRIGGER execution_events_append_only
BEFORE UPDATE OR DELETE ON gm_ci.execution_events
FOR EACH ROW EXECUTE FUNCTION gm_ci.reject_event_mutation();
"""


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner:
    def run(self, argv: list[str], *, input_bytes: bytes | None = None, timeout: int = 120) -> CommandResult:
        result = subprocess.run(
            argv,
            input=input_bytes,
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=timeout,
            env={"LANG": "C", "LC_ALL": "C"},
        )
        return CommandResult(result.returncode, result.stdout, result.stderr)


class DockerPostgres:
    def __init__(self, docker: pathlib.Path, image: str, run_id: str, runner: CommandRunner | None = None) -> None:
        resolved = docker.resolve(strict=True)
        metadata = resolved.stat()
        if not resolved.is_file() or not os.access(resolved, os.X_OK) or not stat.S_ISREG(metadata.st_mode):
            raise HarnessError("Docker executable is invalid")
        if not IMAGE_RE.fullmatch(image) or not RUN_ID_RE.fullmatch(run_id):
            raise HarnessError("Docker image or run ID is invalid")
        self.docker = resolved.as_posix()
        self.image = image
        self.run_id = run_id
        self.container = "gmci-" + run_id
        self.label = "org.verbal-sage.governed-migration-run=" + run_id
        self.runner = runner or CommandRunner()
        self.started = False

    def _run(self, arguments: list[str], *, input_bytes: bytes | None = None, timeout: int = 120, accept: tuple[int, ...] = (0,)) -> CommandResult:
        result = self.runner.run([self.docker, *arguments], input_bytes=input_bytes, timeout=timeout)
        if result.returncode not in accept or len(result.stdout) > 16 * 1024 * 1024 or len(result.stderr) > 4 * 1024 * 1024:
            raise HarnessError("Docker operation failed: " + arguments[0])
        return result

    def verify_image(self) -> str:
        result = self._run(["image", "inspect", self.image, "--format", "{{index .RepoDigests 0}}"])
        identity = result.stdout.decode("utf-8", "strict").strip()
        if identity != self.image:
            raise HarnessError("Docker image digest differs")
        return identity

    def launch(self) -> None:
        absent = self._run(["container", "inspect", self.container], accept=(0, 1))
        if absent.returncode == 0:
            raise HarnessError("disposable container name already exists")
        result = self._run(
            [
                "run",
                "--detach",
                "--rm",
                "--name",
                self.container,
                "--label",
                self.label,
                "--network",
                "none",
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--pids-limit",
                "256",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "DAC_OVERRIDE",
                "--cap-add",
                "FOWNER",
                "--cap-add",
                "SETGID",
                "--cap-add",
                "SETUID",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,noexec,nosuid,size=256m",
                "--tmpfs",
                "/var/run/postgresql:rw,noexec,nosuid,size=16m",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=32m",
                "--env",
                "POSTGRES_USER=gm_ci_admin",
                "--env",
                "POSTGRES_PASSWORD=synthetic_ci_only",
                "--env",
                "POSTGRES_DB=gm_ci_test",
                self.image,
            ],
            timeout=120,
        )
        if not result.stdout.strip():
            raise HarnessError("Docker did not return a container identity")
        self.started = True
        for _ in range(60):
            ready = self._run(
                ["exec", self.container, "pg_isready", "-U", "gm_ci_admin", "-d", "gm_ci_test"],
                timeout=10,
                accept=(0, 1, 2),
            )
            if ready.returncode == 0:
                probe = self._run(
                    [
                        "exec",
                        self.container,
                        "psql",
                        "-X",
                        "--no-psqlrc",
                        "--set",
                        "ON_ERROR_STOP=1",
                        "--quiet",
                        "--tuples-only",
                        "--no-align",
                        "--username=gm_ci_admin",
                        "--dbname=gm_ci_test",
                        "--command=SELECT 1",
                    ],
                    timeout=10,
                    accept=(0, 1, 2),
                )
                if probe.returncode == 0 and probe.stdout.strip() == b"1":
                    return
            time.sleep(0.25)
        raise HarnessError("disposable PostgreSQL did not become ready")

    def psql(self, sql: bytes, *, accept: tuple[int, ...] = (0,), timeout: int = 120) -> CommandResult:
        if not self.started or not sql or len(sql) > 8 * 1024 * 1024 or b"\0" in sql:
            raise HarnessError("psql request is invalid")
        return self._run(
            [
                "exec",
                "-i",
                self.container,
                "psql",
                "-X",
                "--no-psqlrc",
                "--set",
                "ON_ERROR_STOP=1",
                "--quiet",
                "--tuples-only",
                "--no-align",
                "--field-separator=\t",
                "--username=gm_ci_admin",
                "--dbname=gm_ci_test",
            ],
            input_bytes=sql,
            timeout=timeout,
            accept=accept,
        )

    def cleanup(self) -> None:
        inspected = self._run(
            ["container", "inspect", self.container, "--format", "{{index .Config.Labels \"org.verbal-sage.governed-migration-run\"}}"],
            accept=(0, 1),
        )
        if inspected.returncode == 1:
            self.started = False
            return
        label_value = inspected.stdout.decode("utf-8", "strict").strip()
        if label_value != self.run_id:
            raise HarnessError("container identity changed before cleanup")
        self._run(["container", "rm", "--force", self.container], timeout=30)
        absent = self._run(["container", "inspect", self.container], accept=(0, 1))
        if absent.returncode != 1:
            raise HarnessError("container remained after cleanup")
        self.started = False


def _safe_text(value: str, pattern: re.Pattern[str], location: str) -> str:
    if not pattern.fullmatch(value):
        raise HarnessError(location + " is unsafe")
    return value


def _quote(value: str) -> str:
    if "\0" in value or "\r" in value or "\n" in value:
        raise HarnessError("SQL literal contains control text")
    return "'" + value.replace("'", "''") + "'"


def _rows(result: CommandResult) -> list[list[str]]:
    try:
        text = result.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise HarnessError("psql output is not UTF-8") from error
    return [line.split("\t") for line in text.splitlines() if line]


def target_state(database: DockerPostgres) -> tuple[list[dict[str, object]], str]:
    objects: list[dict[str, object]] = []
    schema_rows = _rows(database.psql(b"SELECT n.nspname,pg_get_userbyid(n.nspowner),COALESCE(n.nspacl::text,'') FROM pg_namespace n WHERE n.nspname='gm_fixture';\n"))
    if not schema_rows:
        return [], sha256(canonical_bytes([]))
    if len(schema_rows) != 1 or schema_rows[0][:2] != ["gm_fixture", "gm_ci_owner"] or len(schema_rows[0]) != 3:
        raise HarnessError("fixture schema state is unexpected")

    def add(identifier: str, kind: str, owner: str, detail: Mapping[str, object]) -> None:
        objects.append(
            {
                "id": identifier,
                "kind": kind,
                "owner": owner,
                "fingerprint_sha256": sha256(canonical_bytes(dict(detail))),
            }
        )

    add(
        "schema.gm_fixture",
        "schema",
        "gm_ci_owner",
        {"name": "gm_fixture", "owner": "gm_ci_owner", "acl_sha256": sha256(schema_rows[0][2].encode("utf-8"))},
    )
    relation = _rows(
        database.psql(
            b"SELECT pg_get_userbyid(c.relowner),c.relkind,c.relrowsecurity::int,c.relforcerowsecurity::int,COALESCE(c.relacl::text,'') "
            b"FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            b"WHERE n.nspname='gm_fixture' AND c.relname='governed_records';\n"
        )
    )
    if len(relation) != 1 or len(relation[0]) != 5:
        raise HarnessError("fixture relation state is missing")
    owner, relkind, rls, force, relation_acl = relation[0]
    constraints = _rows(
        database.psql(
            b"SELECT c.conname,c.contype,pg_get_constraintdef(c.oid,true) FROM pg_constraint c "
            b"JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace "
            b"WHERE n.nspname='gm_fixture' AND r.relname='governed_records' ORDER BY c.conname;\n"
        )
    )
    indexes = _rows(
        database.psql(
            b"SELECT i.relname,pg_get_indexdef(x.indexrelid) FROM pg_index x "
            b"JOIN pg_class r ON r.oid=x.indrelid JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_namespace n ON n.oid=r.relnamespace "
            b"WHERE n.nspname='gm_fixture' AND r.relname='governed_records' ORDER BY i.relname;\n"
        )
    )
    add(
        "relation.gm_fixture.governed_records",
        "relation",
        owner,
        {
            "owner": owner,
            "relkind": relkind,
            "rls_enabled": rls == "1",
            "rls_forced": force == "1",
            "acl_sha256": sha256(relation_acl.encode("utf-8")),
            "constraints_sha256": sha256(canonical_bytes(constraints)),
            "indexes_sha256": sha256(canonical_bytes(indexes)),
        },
    )
    columns = _rows(
        database.psql(
            b"SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull::int,"
            b"COALESCE(pg_get_expr(d.adbin,d.adrelid),'') "
            b"FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            b"LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
            b"WHERE n.nspname='gm_fixture' AND c.relname='governed_records' AND a.attnum>0 AND NOT a.attisdropped "
            b"ORDER BY a.attname;\n"
        )
    )
    for name, type_name, not_null, default_expression in columns:
        add(
            "column.gm_fixture.governed_records." + name,
            "column",
            owner,
            {
                "name": name,
                "type": type_name,
                "not_null": not_null == "1",
                "default_sha256": sha256(default_expression.encode("utf-8")),
            },
        )
    policies = _rows(
        database.psql(
            b"SELECT p.polname,p.polpermissive::int,"
            b"COALESCE((SELECT string_agg(r.rolname,',' ORDER BY r.rolname) FROM pg_roles r WHERE r.oid=ANY(p.polroles)),''),"
            b"p.polcmd,COALESCE(pg_get_expr(p.polqual,p.polrelid),''),COALESCE(pg_get_expr(p.polwithcheck,p.polrelid),'') "
            b"FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            b"WHERE n.nspname='gm_fixture' AND c.relname='governed_records' ORDER BY p.polname;\n"
        )
    )
    for name, permissive, roles, command, using_expression, check_expression in policies:
        add(
            "policy.gm_fixture.governed_records." + name,
            "policy",
            owner,
            {
                "name": name,
                "permissive": permissive == "1",
                "roles": roles.split(",") if roles else [],
                "command": command,
                "using_sha256": sha256(using_expression.encode("utf-8")),
                "check_sha256": sha256(check_expression.encode("utf-8")),
            },
        )
    schema_usage = _rows(database.psql(b"SELECT has_schema_privilege('gm_ci_reader','gm_fixture','USAGE')::int;\n"))
    table_select = _rows(database.psql(b"SELECT has_table_privilege('gm_ci_reader','gm_fixture.governed_records','SELECT')::int;\n"))
    add(
        "grant.gm_fixture.schema_usage.gm_ci_reader",
        "grant",
        owner,
        {"grantee": "gm_ci_reader", "object": "gm_fixture", "privilege": "USAGE", "present": schema_usage == [["1"]]},
    )
    add(
        "grant.gm_fixture.table_select.gm_ci_reader",
        "grant",
        owner,
        {"grantee": "gm_ci_reader", "object": "gm_fixture.governed_records", "privilege": "SELECT", "present": table_select == [["1"]]},
    )
    objects.sort(key=lambda item: str(item["id"]))
    return objects, sha256(canonical_bytes(objects))


class FullChain:
    def __init__(self, database: DockerPostgres, package: LoadedPackage, run_id: str) -> None:
        self.database = database
        self.package = package
        self.run_id = _safe_text(run_id, RUN_ID_RE, "run ID")
        self.ordinal = 0
        self.zero_hash = "0" * 64

    def append_event(self, event_type: str, state_hash: str, detail: Mapping[str, object]) -> str:
        _safe_text(event_type, re.compile(r"[a-z][a-z0-9_]{1,47}"), "event type")
        if not HEX_RE.fullmatch(state_hash):
            raise HarnessError("event state hash is invalid")
        prior_rows = _rows(self.database.psql(b"SELECT event_sha256 FROM gm_ci.execution_events ORDER BY event_sequence DESC LIMIT 1;\n"))
        previous = prior_rows[0][0] if prior_rows else self.zero_hash
        if not HEX_RE.fullmatch(previous):
            raise HarnessError("audit chain previous hash is invalid")
        self.ordinal += 1
        record = {
            "schema_version": "governed-migration-execution-event-v1",
            "run_id": self.run_id,
            "migration_id": self.package.migration_id,
            "event_ordinal": self.ordinal,
            "event_type": event_type,
            "package_sha256": self.package.package_sha256,
            "previous_event_sha256": previous,
            "state_sha256": state_hash,
            "detail": dict(detail),
        }
        event_hash = sha256(canonical_bytes(record))
        detail_json = json.dumps(detail, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        sql = (
            "INSERT INTO gm_ci.execution_events "
            "(run_id,migration_id,event_ordinal,event_type,package_sha256,previous_event_sha256,event_sha256,state_sha256,detail) VALUES ("
            + ",".join(
                [
                    _quote(self.run_id),
                    _quote(self.package.migration_id),
                    str(self.ordinal),
                    _quote(event_type),
                    _quote(self.package.package_sha256),
                    _quote(previous),
                    _quote(event_hash),
                    _quote(state_hash),
                    _quote(detail_json) + "::jsonb",
                ]
            )
            + ");\n"
        ).encode("utf-8")
        self.database.psql(sql)
        return event_hash

    def transaction(self, payload: bytes, *, expect_success: bool = True, failure_tail: bytes = b"") -> bool:
        transaction = self.package.manifest["transaction"]
        sql = (
            b"BEGIN;\n"
            + f"SET LOCAL statement_timeout='{transaction['statement_timeout_ms']}ms';\n".encode("ascii")
            + f"SET LOCAL lock_timeout='{transaction['lock_timeout_ms']}ms';\n".encode("ascii")
            + f"SELECT pg_advisory_xact_lock({transaction['advisory_lock_key']});\n".encode("ascii")
            + payload
            + failure_tail
            + b"COMMIT;\n"
        )
        result = self.database.psql(sql, accept=(0, 1, 2, 3), timeout=120)
        if expect_success and result.returncode != 0:
            raise HarnessError("migration transaction failed")
        if not expect_success and result.returncode == 0:
            raise HarnessError("injected migration failure unexpectedly succeeded")
        return result.returncode == 0

    def verify_expected(self) -> str:
        observed, state_hash = target_state(self.database)
        verification = self.package.manifest["verification"]
        if observed != verification["object_expectations"] or state_hash != verification["expected_state_sha256"]:
            raise HarnessError("post-migration state fingerprint differs: " + state_hash)
        relation = next(item for item in observed if item["kind"] == "relation")
        policy = next(item for item in observed if item["kind"] == "policy")
        grants = [item for item in observed if item["kind"] == "grant"]
        if relation["owner"] != "gm_ci_owner" or policy["owner"] != "gm_ci_owner" or len(grants) != 2:
            raise HarnessError("owner/RLS/policy/grant expectations weakened")
        return state_hash

    def verify_audit_chain(self) -> tuple[int, str]:
        rows = _rows(
            self.database.psql(
                b"SELECT run_id,migration_id,event_ordinal,event_type,package_sha256,previous_event_sha256,event_sha256,state_sha256,detail::text "
                b"FROM gm_ci.execution_events ORDER BY event_sequence;\n"
            )
        )
        previous = self.zero_hash
        for row in rows:
            if len(row) != 9:
                raise HarnessError("audit event row is malformed")
            run_id, migration_id, ordinal, event_type, package_hash, prior, event_hash, state_hash, detail_raw = row
            detail = json.loads(detail_raw)
            record = {
                "schema_version": "governed-migration-execution-event-v1",
                "run_id": run_id,
                "migration_id": migration_id,
                "event_ordinal": int(ordinal),
                "event_type": event_type,
                "package_sha256": package_hash,
                "previous_event_sha256": prior,
                "state_sha256": state_hash,
                "detail": detail,
            }
            if prior != previous or sha256(canonical_bytes(record)) != event_hash:
                raise HarnessError("append-only audit chain hash differs")
            previous = event_hash
        return len(rows), previous

    def prove_concurrency_and_stale_lock(self) -> None:
        key = int(self.package.manifest["transaction"]["advisory_lock_key"])
        argv = [
            self.database.docker,
            "exec",
            "-i",
            self.database.container,
            "psql",
            "-X",
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--quiet",
            "--username=gm_ci_admin",
            "--dbname=gm_ci_test",
        ]
        holder = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env={"LANG": "C", "LC_ALL": "C"},
        )
        assert holder.stdin is not None
        holder.stdin.write(f"SELECT pg_advisory_lock({key}); SELECT pg_sleep(1.0);\n".encode("ascii"))
        holder.stdin.close()
        blocked = False
        try:
            for _ in range(20):
                result = _rows(
                    self.database.psql(
                        f"SELECT CASE WHEN pg_try_advisory_lock({key}) THEN (pg_advisory_unlock({key})::text || ':acquired') ELSE 'blocked' END;\n".encode("ascii")
                    )
                )
                if result == [["blocked"]]:
                    blocked = True
                    break
                time.sleep(0.05)
            if not blocked:
                raise HarnessError("concurrent runner was not denied")
            returncode = holder.wait(timeout=10)
            if returncode != 0:
                raise HarnessError("lock-holder process failed")
        finally:
            if holder.poll() is None:
                holder.terminate()
                holder.wait(timeout=5)
            if holder.stdout is not None:
                holder.stdout.close()
            if holder.stderr is not None:
                holder.stderr.close()
        recovered = _rows(
            self.database.psql(
                f"SELECT pg_try_advisory_lock({key}); SELECT pg_advisory_unlock({key});\n".encode("ascii")
            )
        )
        if recovered != [["t"], ["t"]]:
            raise HarnessError("stale advisory lock did not recover")

    def prove_timeout(self, stable_hash: str) -> None:
        failed = self.database.psql(
            b"BEGIN; SET LOCAL statement_timeout='100ms'; SELECT pg_sleep(0.5); COMMIT;\n",
            accept=(0, 1, 2, 3),
            timeout=10,
        )
        if failed.returncode == 0:
            raise HarnessError("statement timeout injection succeeded")
        if target_state(self.database)[1] != stable_hash:
            raise HarnessError("timeout changed schema state")

    def prove_append_only(self) -> None:
        for payload in (
            b"UPDATE gm_ci.execution_events SET event_type='tampered';\n",
            b"DELETE FROM gm_ci.execution_events;\n",
        ):
            result = self.database.psql(payload, accept=(0, 1, 2, 3))
            if result.returncode == 0:
                raise HarnessError("append-only audit accepted mutation")

    def execute(self) -> dict[str, object]:
        self.database.psql(BOOTSTRAP_SQL)
        baseline_objects, baseline_hash = target_state(self.database)
        if baseline_objects or baseline_hash != sha256(canonical_bytes([])):
            raise HarnessError("synthetic baseline is not empty")
        self.prove_concurrency_and_stale_lock()
        self.append_event("planned", baseline_hash, {"phase": "initial"})
        self.append_event("applying", baseline_hash, {"attempt": 1})
        self.transaction(self.package.forward_bytes)
        applied_hash = self.verify_expected()
        self.append_event("applied", applied_hash, {"attempt": 1})

        if self.package.rollback_bytes is None:
            raise HarnessError("full-chain fixture requires rollback")
        self.append_event("rolling_back", applied_hash, {"attempt": 1})
        self.transaction(self.package.rollback_bytes)
        if target_state(self.database)[1] != baseline_hash:
            raise HarnessError("rollback did not exactly restore baseline")
        self.append_event("rolled_back", baseline_hash, {"attempt": 1})

        self.append_event("reapplying", baseline_hash, {"attempt": 2})
        self.transaction(self.package.forward_bytes)
        reapplied_hash = self.verify_expected()
        self.append_event("reapplied", reapplied_hash, {"attempt": 2})
        before_replay = target_state(self.database)[1]
        self.append_event("replay_noop", before_replay, {"decision": "already_applied"})
        if target_state(self.database)[1] != before_replay:
            raise HarnessError("replay changed schema state")

        self.append_event("rolling_back", before_replay, {"attempt": 2})
        self.transaction(self.package.rollback_bytes)
        if target_state(self.database)[1] != baseline_hash:
            raise HarnessError("second rollback did not restore baseline")
        self.append_event("rolled_back", baseline_hash, {"attempt": 2})

        self.append_event("applying", baseline_hash, {"attempt": 3, "injection": "transaction_failure"})
        self.transaction(self.package.forward_bytes, expect_success=False, failure_tail=b"SELECT 1/0;\n")
        if target_state(self.database)[1] != baseline_hash:
            raise HarnessError("failed transaction left a partial schema")
        self.append_event("apply_failed", baseline_hash, {"attempt": 3, "partial_state": False})

        self.append_event("applying", baseline_hash, {"attempt": 4, "injection": "post_commit_crash"})
        self.transaction(self.package.forward_bytes)
        recovered_hash = self.verify_expected()
        self.append_event("recovered_applied", recovered_hash, {"evidence": "exact_post_state"})
        self.prove_timeout(recovered_hash)
        self.append_event("timeout_rejected", recovered_hash, {"state_changed": False})
        self.prove_append_only()
        count, audit_hash = self.verify_audit_chain()
        return {
            "schema_version": "governed-migration-full-chain-report-v1",
            "migration_id": self.package.migration_id,
            "package_sha256": self.package.package_sha256,
            "baseline_state_sha256": baseline_hash,
            "post_state_sha256": recovered_hash,
            "audit_event_count": count,
            "audit_chain_sha256": audit_hash,
            "happy_path": True,
            "rollback_exact": True,
            "reapply_exact": reapplied_hash == applied_hash,
            "replay_noop": True,
            "transaction_failure_atomic": True,
            "crash_recovery_deterministic": True,
            "timeout_rejected": True,
            "concurrent_runner_denied": True,
            "stale_lock_recovered": True,
            "append_only_audit_proven": True,
            "network_mode": "none",
            "synthetic_records_written": 0,
            "production_credentials_used": False,
            "status": "passed",
        }


def ledger_identity(schema_ledger_root: pathlib.Path) -> dict[str, str]:
    root = schema_ledger_root.resolve(strict=True)
    ledger_path = root / "ledger/governed-memory-schema-ledger-v1.json"
    ledger_raw = ledger_path.read_bytes()
    ledger = parse_canonical(ledger_raw, maximum=64 * 1024 * 1024)
    if not isinstance(ledger, dict) or not isinstance(ledger.get("baseline"), dict):
        raise HarnessError("schema ledger is malformed")
    baseline = ledger["baseline"]
    return {
        "ledger_id": str(ledger["ledger_id"]),
        "ledger_sha256": sha256(ledger_raw),
        "catalog_evidence_sha256": str(baseline["catalog_evidence_sha256"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--schema-ledger-root", type=pathlib.Path, required=True)
    parser.add_argument("--package", type=pathlib.Path, required=True)
    parser.add_argument("--docker", type=pathlib.Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    package_root = args.package.resolve(strict=True)
    if package_root.parent != root / "fixtures":
        raise HarnessError("full-chain package must be the reviewed CI fixture")
    package = load_package(package_root, ledger_identity(args.schema_ledger_root))
    if package.manifest["ci_only"] is not True:
        raise HarnessError("full-chain fixture is not CI-only")
    database = DockerPostgres(args.docker, args.image, args.run_id)
    report: dict[str, object] | None = None
    primary: BaseException | None = None
    cleanup: BaseException | None = None
    try:
        database.verify_image()
        database.launch()
        report = FullChain(database, package, args.run_id).execute()
    except BaseException as error:
        primary = error
    finally:
        try:
            database.cleanup()
        except BaseException as error:
            cleanup = error
    if primary is not None or cleanup is not None:
        labels = []
        if primary is not None:
            labels.append("primary=" + type(primary).__name__ + ":" + str(primary))
        if cleanup is not None:
            labels.append("cleanup=" + type(cleanup).__name__ + ":" + str(cleanup))
        raise HarnessError("; ".join(labels))
    assert report is not None
    report["container_teardown_verified"] = True
    sys.stdout.buffer.write(canonical_bytes(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HarnessError, MigrationError) as error:
        print("full-chain harness failed: " + type(error).__name__ + ": " + str(error), file=sys.stderr)
        raise SystemExit(2)
