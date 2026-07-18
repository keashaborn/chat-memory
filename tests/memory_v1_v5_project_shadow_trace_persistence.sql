\set ON_ERROR_STOP on

SELECT count(*)::integer AS project_trace_count_before
FROM memory.v5_project_shadow_trace_event
\gset

BEGIN;
SET SESSION AUTHORIZATION brains_app;

DO $missing_actor$
BEGIN
  BEGIN
    PERFORM * FROM memory.record_v5_project_shadow_trace_v1(
      'memory_v1_v5_project_shadow_trace_v1',
      '1f58b9145b24d108d7ac38887338b3ea3229833b9c1e418250343f907bfd1047',
      '39200d1e8a8dbbb6d7bcea51e02b99f062d32a5f83151e8c5a9fab79576245dd',
      '80f70afeef3caa57646fd20afb95be9c3f2c03d38e091906de31838813dcc22e',
      'a8b771920b8319e47251d1360f5e880bc18e8d329b0f0d003ea3c7e615558947',
      'ok','evaluated','project_status','memory_architecture',
      '5f161c9149882e0e10124bc5dd5c11f0fbe8ec452edd52bcec76b01e9252cb33',
      'ba159dbf9d2f740f56bc2ea7245187ee726da4263440b055ceb89485f4dbcf38',
      '65045134064dd4d905665eceaf4153fea4f9d69d2ae22255a0b7cef4701f4886',
      1,1,100,'{}'::jsonb,4,600
    );
    RAISE EXCEPTION 'missing-actor project trace write unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$missing_actor$;

SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

DO $direct_table_denial$
BEGIN
  BEGIN
    PERFORM 1 FROM memory.v5_project_shadow_trace_event LIMIT 1;
    RAISE EXCEPTION 'brains_app directly read the project trace table';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$direct_table_denial$;

SELECT 1 / ((outcome='applied' AND rows_written=1)::integer)
FROM memory.record_v5_project_shadow_trace_v1(
  'memory_v1_v5_project_shadow_trace_v1',
  '1f58b9145b24d108d7ac38887338b3ea3229833b9c1e418250343f907bfd1047',
  '39200d1e8a8dbbb6d7bcea51e02b99f062d32a5f83151e8c5a9fab79576245dd',
  '80f70afeef3caa57646fd20afb95be9c3f2c03d38e091906de31838813dcc22e',
  'a8b771920b8319e47251d1360f5e880bc18e8d329b0f0d003ea3c7e615558947',
  'ok','evaluated','project_status','memory_architecture',
  '5f161c9149882e0e10124bc5dd5c11f0fbe8ec452edd52bcec76b01e9252cb33',
  'ba159dbf9d2f740f56bc2ea7245187ee726da4263440b055ceb89485f4dbcf38',
  '65045134064dd4d905665eceaf4153fea4f9d69d2ae22255a0b7cef4701f4886',
  1,1,100,'{}'::jsonb,4,600
);

SELECT 1 / ((outcome='replayed' AND rows_written=0)::integer)
FROM memory.record_v5_project_shadow_trace_v1(
  'memory_v1_v5_project_shadow_trace_v1',
  '1f58b9145b24d108d7ac38887338b3ea3229833b9c1e418250343f907bfd1047',
  '39200d1e8a8dbbb6d7bcea51e02b99f062d32a5f83151e8c5a9fab79576245dd',
  '80f70afeef3caa57646fd20afb95be9c3f2c03d38e091906de31838813dcc22e',
  'a8b771920b8319e47251d1360f5e880bc18e8d329b0f0d003ea3c7e615558947',
  'ok','evaluated','project_status','memory_architecture',
  '5f161c9149882e0e10124bc5dd5c11f0fbe8ec452edd52bcec76b01e9252cb33',
  'ba159dbf9d2f740f56bc2ea7245187ee726da4263440b055ceb89485f4dbcf38',
  '65045134064dd4d905665eceaf4153fea4f9d69d2ae22255a0b7cef4701f4886',
  1,1,100,'{}'::jsonb,4,600
);

DO $conflicting_replay$
BEGIN
  BEGIN
    PERFORM * FROM memory.record_v5_project_shadow_trace_v1(
      'memory_v1_v5_project_shadow_trace_v1',
      '1f58b9145b24d108d7ac38887338b3ea3229833b9c1e418250343f907bfd1047',
      '39200d1e8a8dbbb6d7bcea51e02b99f062d32a5f83151e8c5a9fab79576245dd',
      '80f70afeef3caa57646fd20afb95be9c3f2c03d38e091906de31838813dcc22e',
      'a8b771920b8319e47251d1360f5e880bc18e8d329b0f0d003ea3c7e615558947',
      'ok','evaluated','project_status','memory_architecture',
      '5f161c9149882e0e10124bc5dd5c11f0fbe8ec452edd52bcec76b01e9252cb33',
      'ba159dbf9d2f740f56bc2ea7245187ee726da4263440b055ceb89485f4dbcf38',
      'ba159dbf9d2f740f56bc2ea7245187ee726da4263440b055ceb89485f4dbcf38',
      1,1,100,'{}'::jsonb,4,600
    );
    RAISE EXCEPTION 'conflicting project trace replay unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '23514' THEN
    NULL;
  END;
END
$conflicting_replay$;

SELECT set_config(
  'app.user_id','557ea042-cb82-48f8-9429-472e96c957ef',true
);
SELECT 1 / ((outcome='applied' AND rows_written=1)::integer)
FROM memory.record_v5_project_shadow_trace_v1(
  'memory_v1_v5_project_shadow_trace_v1',
  '1f58b9145b24d108d7ac38887338b3ea3229833b9c1e418250343f907bfd1047',
  '39200d1e8a8dbbb6d7bcea51e02b99f062d32a5f83151e8c5a9fab79576245dd',
  '80f70afeef3caa57646fd20afb95be9c3f2c03d38e091906de31838813dcc22e',
  'a8b771920b8319e47251d1360f5e880bc18e8d329b0f0d003ea3c7e615558947',
  'ok','evaluated','project_status','memory_architecture',
  '5f161c9149882e0e10124bc5dd5c11f0fbe8ec452edd52bcec76b01e9252cb33',
  'ba159dbf9d2f740f56bc2ea7245187ee726da4263440b055ceb89485f4dbcf38',
  '65045134064dd4d905665eceaf4153fea4f9d69d2ae22255a0b7cef4701f4886',
  1,1,100,'{}'::jsonb,4,600
);

RESET SESSION AUTHORIZATION;
ROLLBACK;

DO $maintenance_denial$
BEGIN
  BEGIN
    PERFORM * FROM memory.record_v5_project_shadow_trace_v1(
      'memory_v1_v5_project_shadow_trace_v1',
      '1f58b9145b24d108d7ac38887338b3ea3229833b9c1e418250343f907bfd1047',
      '39200d1e8a8dbbb6d7bcea51e02b99f062d32a5f83151e8c5a9fab79576245dd',
      '80f70afeef3caa57646fd20afb95be9c3f2c03d38e091906de31838813dcc22e',
      'a8b771920b8319e47251d1360f5e880bc18e8d329b0f0d003ea3c7e615558947',
      'ok','evaluated','project_status','memory_architecture',
      '5f161c9149882e0e10124bc5dd5c11f0fbe8ec452edd52bcec76b01e9252cb33',
      'ba159dbf9d2f740f56bc2ea7245187ee726da4263440b055ceb89485f4dbcf38',
      '65045134064dd4d905665eceaf4153fea4f9d69d2ae22255a0b7cef4701f4886',
      1,1,100,'{}'::jsonb,4,600
    );
    RAISE EXCEPTION 'maintenance project trace write unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '42501' THEN
    NULL;
  END;
END
$maintenance_denial$;

SELECT 1 / ((count(*)=:project_trace_count_before)::integer)
FROM memory.v5_project_shadow_trace_event;

SELECT 1 / ((count(*)=0)::integer)
FROM information_schema.role_table_grants
WHERE grantee='brains_app'
  AND table_schema='memory'
  AND table_name='v5_project_shadow_trace_event';

SELECT 1 / ((
  pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid=
    'memory.record_v5_project_shadow_trace_v1(text,text,text,text,text,text,text,text,text,text,text,text,integer,integer,integer,jsonb,integer,integer)'::regprocedure
  ))='memory_v5_trace_writer'
)::integer);

SELECT 'memory_v1_v5_project_shadow_trace_persistence: PASS' AS result;
