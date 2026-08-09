from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

from full_chain_harness import CommandResult, HarnessError  # noqa: E402
from function_creation_full_chain_harness import (  # noqa: E402
    verify_created_function_state,
    verify_function_absent,
)
from governed_function_creation import FunctionCreationSpec  # noqa: E402
from governed_migration import sha256  # noqa: E402


PENDING_RETIREMENT_FIXTURE_SHA256 = (
    "28c2836b995c4845e61c6577596ad001022e54041452fb4264f33ab942a5daae"
)
PENDING_RETIREMENT_FIXTURE_SQL = rb"""\set ON_ERROR_STOP on

BEGIN;
\echo FIXTURE_STAGE_owner_a_setup

CREATE FUNCTION pg_temp.assert_retirement_denied(p_sql text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=''
AS $function$
DECLARE
  denied boolean := false;
BEGIN
  BEGIN
    EXECUTE p_sql;
  EXCEPTION
    WHEN check_violation OR insufficient_privilege OR invalid_parameter_value
    THEN denied := true;
  END;
  IF NOT denied THEN
    RAISE EXCEPTION 'unsafe retirement call was accepted';
  END IF;
END
$function$;

SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','a1111111-1111-4111-8111-111111111111',true
);

SELECT evidence_id,content_sha256
FROM memory.record_owner_evidence_v1(
  'user_statement','retirement_test','owner-a-valid',
  'Synthetic pending retirement fixture.','2026-08-08T12:00:00Z',
  1,1,'owner-a-valid','low','{"test":true}'::jsonb
)
\gset valid_evidence_
SELECT job_id
FROM memory.enqueue_owner_evidence_extraction_v1(
  :'valid_evidence_evidence_id',
  '20260808_retirement_test',
  :'valid_evidence_content_sha256',
  'relational_extraction',
  'eligible_unprocessed'
)
\gset valid_job_

\echo FIXTURE_STAGE_apply_and_replay
SELECT
  retirement_sha256,
  1/((job_id=:'valid_job_job_id'::uuid)::integer) AS job_matches,
  1/((status='skipped')::integer) AS status_matches,
  1/((apply_outcome='applied')::integer) AS outcome_matches
FROM memory.retire_owner_evidence_extraction_job_v1(
  'a2000000-0000-4000-8000-000000000001',
  :'valid_job_job_id',
  :'valid_evidence_content_sha256',
  0,
  'invalid_synthetic_evidence'
)
\gset retirement_

SELECT
  1/((status='skipped')::integer),
  1/((apply_outcome='replayed')::integer),
  1/((retirement_sha256=:'retirement_retirement_sha256')::integer)
FROM memory.retire_owner_evidence_extraction_job_v1(
  'a2000000-0000-4000-8000-000000000001',
  :'valid_job_job_id',
  :'valid_evidence_content_sha256',
  0,
  'invalid_synthetic_evidence'
);
SELECT pg_temp.assert_retirement_denied(format(
  $sql$
    SELECT * FROM memory.retire_owner_evidence_extraction_job_v1(
      'a2000000-0000-4000-8000-000000000001',%L,%L,0,
      'scheduler_contract_obsolete'
    )
  $sql$,
  :'valid_job_job_id',
  :'valid_evidence_content_sha256'
));

\echo FIXTURE_STAGE_owner_and_input_negatives
SELECT set_config(
  'app.user_id','b2222222-2222-4222-8222-222222222222',true
);
SELECT pg_temp.assert_retirement_denied(format(
  $sql$
    SELECT * FROM memory.retire_owner_evidence_extraction_job_v1(
      'b2000000-0000-4000-8000-000000000001',%L,%L,0,
      'invalid_synthetic_evidence'
    )
  $sql$,
  :'valid_job_job_id',
  :'valid_evidence_content_sha256'
));
SELECT set_config(
  'app.user_id','a1111111-1111-4111-8111-111111111111',true
);
SELECT pg_temp.assert_retirement_denied(format(
  $sql$
    SELECT * FROM memory.retire_owner_evidence_extraction_job_v1(
      'a2000000-0000-4000-8000-000000000002',%L,%L,0,
      'invalid_synthetic_evidence'
    )
  $sql$,
  :'valid_job_job_id',
  repeat('f',64)
));
SELECT pg_temp.assert_retirement_denied(format(
  $sql$
    SELECT * FROM memory.retire_owner_evidence_extraction_job_v1(
      'a2000000-0000-4000-8000-000000000003',%L,%L,1,
      'invalid_synthetic_evidence'
    )
  $sql$,
  :'valid_job_job_id',
  :'valid_evidence_content_sha256'
));
SELECT pg_temp.assert_retirement_denied(format(
  $sql$
    SELECT * FROM memory.retire_owner_evidence_extraction_job_v1(
      'a2000000-0000-4000-8000-000000000004',%L,%L,0,'unreviewed_reason'
    )
  $sql$,
  :'valid_job_job_id',
  :'valid_evidence_content_sha256'
));

\echo FIXTURE_STAGE_provider_side_effect_setup
SELECT evidence_id,content_sha256
FROM memory.record_owner_evidence_v1(
  'user_statement','retirement_test','owner-a-provider-side-effect',
  'Synthetic provider side effect fixture.','2026-08-08T12:01:00Z',
  1,1,'owner-a-provider-side-effect','low','{"test":true}'::jsonb
)
\gset provider_evidence_
SELECT job_id
FROM memory.enqueue_owner_evidence_extraction_v1(
  :'provider_evidence_evidence_id',
  '20260808_retirement_test',
  :'provider_evidence_content_sha256',
  'relational_extraction',
  'eligible_unprocessed'
)
\gset provider_job_

RESET SESSION AUTHORIZATION;
INSERT INTO memory.openai_provider_request_receipt_v1(
  receipt_id,owner_user_id,operation_id,run_id,job_id,evidence_id,
  request_sha256,request_receipt_sha256,request_receipt,
  provider_id,provider_version,provider_model_sha256,worker_id_sha256,
  maximum_cost_microusd
) VALUES (
  'a3000000-0000-4000-8000-000000000001',
  'a1111111-1111-4111-8111-111111111111',
  'a3000000-0000-4000-8000-000000000002',
  'a3000000-0000-4000-8000-000000000003',
  :'provider_job_job_id',
  :'provider_evidence_evidence_id',
  repeat('1',64),repeat('2',64),'{"synthetic":true}'::jsonb,
  'openai_responses','v1',repeat('3',64),repeat('4',64),1
);
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','a1111111-1111-4111-8111-111111111111',true
);
\echo FIXTURE_STAGE_provider_side_effect_denial
SELECT pg_temp.assert_retirement_denied(format(
  $sql$
    SELECT * FROM memory.retire_owner_evidence_extraction_job_v1(
      'a3000000-0000-4000-8000-000000000004',%L,%L,0,
      'operator_quarantine_before_processing'
    )
  $sql$,
  :'provider_job_job_id',
  :'provider_evidence_content_sha256'
));

\echo FIXTURE_STAGE_retention_assertions
RESET SESSION AUTHORIZATION;
SELECT set_config(
  'app.user_id','a1111111-1111-4111-8111-111111111111',true
);
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence
WHERE owner_user_id='a1111111-1111-4111-8111-111111111111'
  AND evidence_id=:'valid_evidence_evidence_id';
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_job
WHERE owner_user_id='a1111111-1111-4111-8111-111111111111'
  AND job_id=:'valid_job_job_id'
  AND status='skipped'
  AND attempts=0
  AND result#>>'{final,status}'='skipped'
  AND result#>>'{final,retirement_sha256}'=:'retirement_retirement_sha256';
SELECT 1/((count(*)=2)::integer)
FROM memory.evidence_extraction_event
WHERE owner_user_id='a1111111-1111-4111-8111-111111111111'
  AND job_id=:'valid_job_job_id';
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_event
WHERE owner_user_id='a1111111-1111-4111-8111-111111111111'
  AND job_id=:'valid_job_job_id'
  AND operation_id='a2000000-0000-4000-8000-000000000001'
  AND event_type='skipped'
  AND from_status='pending'
  AND to_status='skipped'
  AND details->>'retirement_sha256'=:'retirement_retirement_sha256';
SELECT 1/((count(*)=1)::integer)
FROM memory.evidence_extraction_job
WHERE owner_user_id='a1111111-1111-4111-8111-111111111111'
  AND job_id=:'provider_job_job_id'
  AND status='pending'
  AND attempts=0
  AND worker_id IS NULL;

ROLLBACK;

SELECT 'memory_v1_pending_job_retirement_v1: PASS' AS result;
"""


class FakeDatabase:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = list(outputs)
        self.calls: list[bytes] = []

    def psql(self, sql: bytes, **_kwargs: object) -> CommandResult:
        self.calls.append(sql)
        if not self.outputs:
            raise AssertionError("unexpected PostgreSQL request")
        return CommandResult(0, self.outputs.pop(0), b"")


def spec(definition_hash: str) -> FunctionCreationSpec:
    return FunctionCreationSpec(
        "memory.fixture_created_v1",
        "memory.fixture_created_v1(p_owner uuid)",
        "memory.fixture_created_v1(uuid)",
        "sage",
        "plpgsql",
        "volatile",
        "unsafe",
        False,
        False,
        ("pg_catalog", "memory"),
        ("memory_reader",),
        (
            {
                "relation": "memory.fixture",
                "owner": "sage",
                "forced": True,
                "policies": ["owner_policy"],
            },
        ),
        definition_hash,
        "fixture-forward.pgsql",
        "a" * 64,
        "fixture-rollback.pgsql",
        "b" * 64,
    )


class FunctionCreationStateTest(unittest.TestCase):
    def test_pending_retirement_fixture_is_byte_exact(self) -> None:
        self.assertEqual(
            sha256(PENDING_RETIREMENT_FIXTURE_SQL),
            PENDING_RETIREMENT_FIXTURE_SHA256,
        )
        self.assertIn(
            b"memory_v1_pending_job_retirement_v1: PASS",
            PENDING_RETIREMENT_FIXTURE_SQL,
        )

    def test_proves_prior_absence(self) -> None:
        database = FakeDatabase([b"1\n"])
        verify_function_absent(database, "memory.fixture_created_v1(uuid)")  # type: ignore[arg-type]

    def test_rejects_preexisting_signature(self) -> None:
        database = FakeDatabase([b"0\n"])
        with self.assertRaisesRegex(HarnessError, "already exists"):
            verify_function_absent(database, "memory.fixture_created_v1(uuid)")  # type: ignore[arg-type]

    def test_binds_created_definition_and_acl(self) -> None:
        definition = b"CREATE FUNCTION memory.fixture_created_v1(p_owner uuid) RETURNS uuid ...\n"
        database = FakeDatabase(
            [
                b"sage\tplpgsql\t1\t0\tv\tu\t0\tsearch_path=pg_catalog, memory\n",
                b"memory_reader\tEXECUTE\t0\n",
                sha256(definition).encode("ascii") + b"\n",
            ]
        )
        observed = verify_created_function_state(database, spec(sha256(definition)))  # type: ignore[arg-type]
        self.assertEqual(observed["definition_sha256"], sha256(definition))

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
            verify_created_function_state(database, spec(sha256(definition)))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
