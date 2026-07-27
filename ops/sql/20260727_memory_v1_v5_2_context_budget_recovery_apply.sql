BEGIN;

SELECT set_config(
  'app.user_id',
  '1240822d-ac9a-4096-95aa-e2b24d36ef50',
  true
);

SELECT apply_outcome
FROM memory.requeue_owner_v5_2_context_budget_failure_v1(
  '52ffb345-3384-4b81-99a9-b271d27245a1'::uuid,
  'c9c5bc9a-0707-4a64-ae23-4755e886d8c7'::uuid,
  '5a8adf167d46b95dac70fcf0220e0aedfbc696b65a0cc0817ae51d9f1036737c',
  'ca88037d-2e65-595b-b7b9-d56f597a5e5c'::uuid,
  '601569ab-c680-4019-8316-57f3e2a0d75f'::uuid,
  1,
  'local_transport_http_rejected',
  'private_gpu_context_budget_recovery_v1'
);

SELECT apply_outcome
FROM memory.requeue_owner_v5_2_context_budget_failure_v1(
  '0d9eb08b-1130-408c-a8cb-1052fdf4d7ac'::uuid,
  '716e679e-00a8-444f-9f67-7082f9f65719'::uuid,
  '1458bbf1860c62fae9998d9e161f7830ed5b15c23a98c7b12e12100bdefce807',
  'fd9ebb9f-7424-5a43-9592-3294df5ad960'::uuid,
  '8be05c76-6faf-40c1-a687-df7aa4920eb3'::uuid,
  1,
  'local_transport_http_rejected',
  'private_gpu_context_budget_recovery_v1'
);

SELECT apply_outcome
FROM memory.requeue_owner_v5_2_context_budget_failure_v1(
  'c12fda14-6907-4f3f-a7e2-96b313ad7961'::uuid,
  '707128d8-aa3a-4634-a021-0973fbee3c1b'::uuid,
  'c01b32f1619ae3af5bbaa590a11e93801856c326c6e61c44821958a2aba848b0',
  'aabc8dce-ab7b-54a4-8974-fe08f7d0b3bb'::uuid,
  'edc23f59-aeec-44ad-9a8b-4be64eb5eda6'::uuid,
  1,
  'local_transport_http_rejected',
  'private_gpu_context_budget_recovery_v1'
);

COMMIT;
