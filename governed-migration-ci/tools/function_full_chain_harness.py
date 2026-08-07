#!/usr/bin/env python3
"""Authoritative PostgreSQL catalog checks for governed function replacements."""

from __future__ import annotations

from collections.abc import Mapping

from full_chain_harness import CommandResult, DockerPostgres, HarnessError
from governed_function_migration import FunctionSpec, LoadedFunctionPackage
from governed_migration import canonical_bytes, sha256


def _rows(result: CommandResult) -> list[list[str]]:
    try:
        text = result.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise HarnessError("function catalog output is not UTF-8") from error
    return [line.split("\t") for line in text.splitlines() if line]


def _quote(value: str) -> str:
    if any(character in value for character in ("\0", "\r", "\n")):
        raise HarnessError("function identity contains control text")
    return "'" + value.replace("'", "''") + "'"


def _definition_hash(database: DockerPostgres, signature: str) -> str:
    sql = (
        "SELECT encode(public.digest(convert_to(pg_get_functiondef(to_regprocedure("
        + _quote(signature)
        + ")), 'UTF8'),'sha256'),'hex');\n"
    ).encode("utf-8")
    rows = _rows(database.psql(sql))
    if (
        len(rows) != 1
        or len(rows[0]) != 1
        or len(rows[0][0]) != 64
        or any(character not in "0123456789abcdef" for character in rows[0][0])
    ):
        raise HarnessError("function definition is missing")
    return rows[0][0]


def _catalog(database: DockerPostgres, spec: FunctionSpec) -> dict[str, object]:
    signature = _quote(spec.signature_sql)
    rows = _rows(
        database.psql(
            (
                "SELECT pg_get_userbyid(p.proowner),l.lanname,p.prosecdef::int,p.proleakproof::int,"
                "p.provolatile,p.proparallel,p.proisstrict::int,COALESCE(array_to_string(p.proconfig,E'\\x1f'),'') "
                "FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang WHERE p.oid=to_regprocedure("
                + signature
                + ");\n"
            ).encode("utf-8")
        )
    )
    if len(rows) != 1 or len(rows[0]) != 8:
        raise HarnessError("preexisting function identity is absent or ambiguous")
    owner, language, security_definer, leakproof, volatility, parallel, strict, settings = rows[0]
    acl = _rows(
        database.psql(
            (
                "SELECT CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,"
                "x.privilege_type,x.is_grantable::int FROM pg_proc p,"
                "LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) x "
                "WHERE p.oid=to_regprocedure("
                + signature
                + ") ORDER BY 1,2,3;\n"
            ).encode("utf-8")
        )
    )
    return {
        "identity": spec.identity,
        "signature_sql": spec.signature_sql,
        "definition_sha256": _definition_hash(database, spec.signature_sql),
        "owner": owner,
        "language": language,
        "security_definer": security_definer == "1",
        "leakproof": leakproof == "1",
        "volatility": volatility,
        "parallel": parallel,
        "strict": strict == "1",
        "settings": settings.split("\x1f") if settings else [],
        "acl": acl,
    }


def _expected_acl(spec: FunctionSpec, roles: tuple[str, ...]) -> list[list[str]]:
    return [[role, "EXECUTE", "0"] for role in roles]


def verify_function_state(database: DockerPostgres, spec: FunctionSpec, *, rollback: bool) -> dict[str, object]:
    observed = _catalog(database, spec)
    expected_hash = spec.prior_definition_sha256 if rollback else spec.expected_definition_sha256
    expected_roles = spec.rollback_execute_roles if rollback else spec.forward_execute_roles
    if observed != {
        "identity": spec.identity,
        "signature_sql": spec.signature_sql,
        "definition_sha256": expected_hash,
        "owner": spec.owner,
        "language": spec.language,
        "security_definer": True,
        "leakproof": spec.leakproof,
        "volatility": {"volatile": "v", "stable": "s", "immutable": "i"}[spec.volatility],
        "parallel": {"unsafe": "u", "restricted": "r", "safe": "s"}[spec.parallel],
        "strict": spec.strict,
        "settings": ["search_path=" + (", ".join(spec.search_path) if spec.search_path else '""')],
        "acl": _expected_acl(spec, expected_roles),
    }:
        raise HarnessError("function definition, owner, security, search path, or ACL differs")
    return observed


def verify_rls_dependencies(database: DockerPostgres, spec: FunctionSpec) -> list[dict[str, object]]:
    observed: list[dict[str, object]] = []
    for expected in spec.rls_dependencies:
        relation = str(expected["relation"])
        schema, name = relation.split(".", 1)
        rows = _rows(
            database.psql(
                (
                    "SELECT pg_get_userbyid(c.relowner),c.relrowsecurity::int,c.relforcerowsecurity::int "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname="
                    + _quote(schema)
                    + " AND c.relname="
                    + _quote(name)
                    + " AND c.relkind IN ('r','p');\n"
                ).encode("utf-8")
            )
        )
        policies = _rows(
            database.psql(
                (
                    "SELECT p.polname FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname="
                    + _quote(schema)
                    + " AND c.relname="
                    + _quote(name)
                    + " ORDER BY p.polname;\n"
                ).encode("utf-8")
            )
        )
        item = {
            "relation": relation,
            "owner": rows[0][0] if len(rows) == 1 and len(rows[0]) == 3 else "",
            "rls_enabled": len(rows) == 1 and rows[0][1] == "1",
            "rls_forced": len(rows) == 1 and rows[0][2] == "1",
            "policies": [row[0] for row in policies if len(row) == 1],
        }
        required = {
            "relation": relation,
            "owner": expected["owner"],
            "rls_enabled": True,
            "rls_forced": True,
            "policies": expected["policies"],
        }
        if item != required:
            raise HarnessError("function RLS dependency differs")
        observed.append(item)
    return observed


class FunctionFullChain:
    def __init__(self, database: DockerPostgres, package: LoadedFunctionPackage) -> None:
        self.database = database
        self.package = package

    def _function_bytes(self, spec: FunctionSpec, rollback: bool) -> bytes:
        relative = spec.rollback_path if rollback else spec.forward_path
        return (self.package.directory / relative).read_bytes()

    def _transaction(self, payloads: list[bytes]) -> None:
        transaction = self.package.manifest["transaction"]
        sql = (
            b"BEGIN;\n"
            + f"SET LOCAL statement_timeout='{transaction['statement_timeout_ms']}ms';\n".encode("ascii")
            + f"SET LOCAL lock_timeout='{transaction['lock_timeout_ms']}ms';\n".encode("ascii")
            + f"SELECT pg_advisory_xact_lock({transaction['advisory_lock_key']});\n".encode("ascii")
            + b"\n".join(payloads)
            + b"COMMIT;\n"
        )
        result = self.database.psql(sql, accept=(0, 1, 2, 3), timeout=120)
        if result.returncode != 0:
            raise HarnessError("governed function transaction failed")

    def _state(self, rollback: bool, *, rls: bool) -> tuple[list[dict[str, object]], str]:
        output: list[dict[str, object]] = []
        for spec in self.package.functions:
            output.append(verify_function_state(self.database, spec, rollback=rollback))
            if rls:
                output.extend(verify_rls_dependencies(self.database, spec))
        return output, sha256(canonical_bytes(output))

    def execute(self) -> Mapping[str, object]:
        baseline, baseline_hash = self._state(True, rls=False)
        self._transaction(
            [self.package.relation_forward_bytes]
            + [self._function_bytes(spec, False) for spec in self.package.functions]
        )
        first, first_hash = self._state(False, rls=True)
        self._transaction(
            [self._function_bytes(spec, True) for spec in reversed(self.package.functions)]
            + [self.package.relation_rollback_bytes]
        )
        restored, restored_hash = self._state(True, rls=False)
        if restored != baseline or restored_hash != baseline_hash:
            raise HarnessError("function rollback did not restore the exact baseline")
        self._transaction(
            [self.package.relation_forward_bytes]
            + [self._function_bytes(spec, False) for spec in self.package.functions]
        )
        second, second_hash = self._state(False, rls=True)
        if second != first or second_hash != first_hash:
            raise HarnessError("function reapplication did not reproduce the exact state")
        self._transaction(
            [self._function_bytes(spec, True) for spec in reversed(self.package.functions)]
            + [self.package.relation_rollback_bytes]
        )
        final, final_hash = self._state(True, rls=False)
        if final != baseline or final_hash != baseline_hash:
            raise HarnessError("second function rollback did not restore the exact baseline")
        return {
            "schema_version": "governed-function-full-chain-result-v1",
            "migration_id": self.package.migration_id,
            "package_sha256": self.package.package_sha256,
            "function_count": len(self.package.functions),
            "baseline_sha256": baseline_hash,
            "applied_sha256": first_hash,
            "rollback_restored": True,
            "reapply_deterministic": True,
            "final_rollback_restored": True,
            "status": "passed",
        }
