\set ON_ERROR_STOP on

BEGIN;
SET LOCAL statement_timeout='30s';
SET SESSION AUTHORIZATION brains_app;
SELECT set_config(
  'app.user_id','1240822d-ac9a-4096-95aa-e2b24d36ef50',true
);

SELECT * FROM memory.recover_owner_v5_local_orphan_v1(
  '6eb44d73-582e-5cd4-baa7-c8d3962307fd',
  '4771f9a9-dbae-5680-9a3e-98cf68c9e100',
  '4febf61b-5139-4eff-939e-45f4d0dff90d',
  'ae386c15-c6bd-4945-8da3-83b13b745ad2',
  'bf129eb2d5f7684502daa89c948da39ca4f7351675187a1bee7ebc43cdbbe663'
);

SELECT * FROM memory.recover_owner_v5_local_orphan_v1(
  '98a4bd91-cfc6-5bf1-aeb5-cc8c0aa652ef',
  'ad6a962e-7345-50e1-8f2d-d5a97e948d34',
  '8e00f329-d359-4c3c-9ce5-6a6ac5155277',
  'ac1a4065-f3c1-4871-9cb2-76b243acc5b7',
  '1d9ebcc6f804ad3f07c02b55ed60e90c7e08097d6ff13bd1d0483354e0288692'
);

SELECT * FROM memory.recover_owner_v5_local_orphan_v1(
  '3f6a78de-c1eb-5aae-8eec-ac259d79f9cf',
  '72191075-18f6-5497-b062-d2e58ea0b79e',
  '395e8ffc-a4fc-41a6-a33c-de5c6be0f36c',
  '9663ff97-f0c6-4047-ae98-00d7468fa6cb',
  '3e5b02245536b5c4b0cf6ba809730a53515aea994c5b4ccc9d8d201879b4f492'
);

RESET SESSION AUTHORIZATION;
COMMIT;
