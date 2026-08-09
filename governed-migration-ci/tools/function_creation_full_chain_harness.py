#!/usr/bin/env python3
"""Authoritative PostgreSQL checks for governed creation of new functions."""

from __future__ import annotations

from collections.abc import Mapping

from full_chain_harness import CommandResult, DockerPostgres, HarnessError
from function_full_chain_harness import verify_rls_dependencies
from governed_function_creation import (
    FunctionCreationSpec,
    LoadedFunctionCreationPackage,
)
from governed_migration import canonical_bytes, sha256


def _rows(result: CommandResult) -> list[list[str]]:
    try:
        text = result.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise HarnessError("function-creation catalog output is not UTF-8") from error
    return [line.split("\t") for line in text.splitlines() if line]


def _quote(value: str) -> str:
    if any(character in value for character in ("\0", "\r", "\n")):
        raise HarnessError("function-creation identity contains control text")
    return "'" + value.replace("'", "''") + "'"


def verify_function_absent(database: DockerPostgres, signature: str) -> None:
    rows = _rows(
        database.psql(
            ("SELECT (to_regprocedure(" + _quote(signature) + ") IS NULL)::int;\n").encode("utf-8")
        )
    )
    if rows != [["1"]]:
        raise HarnessError("new function signature already exists or is ambiguous")


def _definition_hash(database: DockerPostgres, signature: str) -> str:
    rows = _rows(
        database.psql(
            (
                "SELECT encode(public.digest(convert_to(pg_get_functiondef(to_regprocedure("
                + _quote(signature)
                + ")), 'UTF8'),'sha256'),'hex');\n"
            ).encode("utf-8")
        )
    )
    if (
        len(rows) != 1
        or len(rows[0]) != 1
        or len(rows[0][0]) != 64
        or any(character not in "0123456789abcdef" for character in rows[0][0])
    ):
        raise HarnessError("created function definition is missing")
    return rows[0][0]


def verify_created_function_state(
    database: DockerPostgres,
    spec: FunctionCreationSpec,
) -> dict[str, object]:
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
        raise HarnessError("created function identity is absent or ambiguous")
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
    observed = {
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
    expected = {
        "identity": spec.identity,
        "signature_sql": spec.signature_sql,
        "definition_sha256": spec.expected_definition_sha256,
        "owner": spec.owner,
        "language": spec.language,
        "security_definer": True,
        "leakproof": spec.leakproof,
        "volatility": {"volatile": "v", "stable": "s", "immutable": "i"}[spec.volatility],
        "parallel": {"unsafe": "u", "restricted": "r", "safe": "s"}[spec.parallel],
        "strict": spec.strict,
        "settings": ["search_path=" + (", ".join(spec.search_path) if spec.search_path else '""')],
        "acl": [[role, "EXECUTE", "0"] for role in spec.forward_execute_roles],
    }
    if observed != expected:
        raise HarnessError("created function definition, owner, security, search path, or ACL differs")
    return observed


class FunctionCreationFullChain:
    def __init__(
        self,
        database: DockerPostgres,
        package: LoadedFunctionCreationPackage,
    ) -> None:
        self.database = database
        self.package = package

    def _function_bytes(self, spec: FunctionCreationSpec, rollback: bool) -> bytes:
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
            raise HarnessError("governed function-creation transaction failed")

    def _applied_state(self) -> tuple[list[dict[str, object]], str]:
        output: list[dict[str, object]] = []
        for spec in self.package.functions:
            output.append(verify_created_function_state(self.database, spec))
            output.extend(verify_rls_dependencies(self.database, spec))
        return output, sha256(canonical_bytes(output))

    def _verify_absent(self) -> None:
        for spec in self.package.functions:
            verify_function_absent(self.database, spec.signature_sql)

    def execute(self) -> Mapping[str, object]:
        self._verify_absent()
        self._transaction([self._function_bytes(spec, False) for spec in self.package.functions])
        first, first_hash = self._applied_state()
        self._transaction(
            [self._function_bytes(spec, True) for spec in reversed(self.package.functions)]
        )
        self._verify_absent()
        self._transaction([self._function_bytes(spec, False) for spec in self.package.functions])
        second, second_hash = self._applied_state()
        if second != first or second_hash != first_hash:
            raise HarnessError("function creation reapplication did not reproduce exact state")
        self._transaction(
            [self._function_bytes(spec, True) for spec in reversed(self.package.functions)]
        )
        self._verify_absent()
        return {
            "schema_version": "governed-function-creation-full-chain-result-v1",
            "migration_id": self.package.migration_id,
            "package_sha256": self.package.package_sha256,
            "function_count": len(self.package.functions),
            "applied_sha256": first_hash,
            "prior_absence_proved": True,
            "rollback_restored_absence": True,
            "reapply_deterministic": True,
            "final_rollback_restored_absence": True,
            "status": "passed",
        }
