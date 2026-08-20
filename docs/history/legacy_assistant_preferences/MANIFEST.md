# Legacy assistant-preference evaluation archive

## Artifact

- former active path:
  `evals/assistant_preference_compiler_v3_cases.jsonl`
- historical path:
  `docs/history/legacy_assistant_preferences/assistant_preference_compiler_v3_cases.jsonl`
- last content commit: `5a0f265b475b4d5caa60798f96806fbb78981d26`
- Git blob: `82011ea25715d753995bba649fe62612bdc1da9f`
- SHA-256:
  `905e1bf442bf281f78a5d2144b365b1b4a96d41e6bbb812a417cb7fc91c14c3c`
- rows: 8
- keys: `case_id`, `expected_settings`, `forbidden_rule_ids`, `narrative`,
  `required_rejection_codes`, `required_rule_ids`

## Retirement evidence

A repository-wide search on 2026-08-20 found zero imports, scripts, tests,
services, timers, workflows, or reports that read this filename. The compiler
source is absent. The only similarly located retained evaluation is
`response_policy_regression_v1_cases.jsonl`, which is actively read by
`scripts/response_policy_regression_v1.py` and is not part of this retirement.

## Disposition

This file is historical design evidence only. It must not be added to runtime
paths, CI discovery, prompt assembly, user preference handling, or memory. Git
history is the rollback path. Restoring an active preference compiler requires
a new product decision, owner/security contract, current tests, and a new
version; it must not silently reactivate this v3 dataset.
