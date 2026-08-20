# SeeBx refactor batch 05: domain-risk provider boundary

Date: 2026-08-20

## Scope

This candidate-only batch removes the final direct provider SDK dependency
from the canonical conversation capability. Domain-risk classification remains
application-owned, while the OpenAI Structured Outputs call, request options,
and provider-exception interpretation now belong to
`seebx.adapters.openai_signal_classifier`.

The internal classifier is renamed from
`OpenAIServerResponseSignalClassifierV0_2` to the provider-neutral
`ServerResponseSignalClassifierV0_2`. It receives a
`DomainRiskClassificationProvider` protocol. No public request, response,
stored record, prompt instruction, model setting, safety identifier, or
classification version changes.

## Changed files

- `seebx/capabilities/conversation/signal_classifier.py`
- `seebx/adapters/openai_signal_classifier.py`
- `seebx/capabilities/conversation/composition.py`
- `scripts/response_policy_regression_v1.py`
- `tests/test_server_response_signal_classifier_v0_2.py`
- `tests/test_provider_boundaries.py`

## Verified invariants

- No module under `seebx/capabilities` directly imports the OpenAI or Zep SDK.
- OpenAI transient failures still select the bounded unavailable path.
- Other provider failures still fail closed without provider details.
- The exact Structured Outputs prompt, payload, model, timeout, retry policy,
  output schema, output-token limit, storage policy, and safety identifier are
  unchanged.
- Local immediate-risk and input-limit gates still bypass the provider.
- Focused signal/provider tests pass 47/47.
- Affected composition, inspection, regression, and signal tests pass 72/72.
- The complete sealed-runtime suite passes 1,177/1,177.
- Parent and candidate both expose 143 routes and 129 OpenAPI paths.
- Parent and candidate route SHA-256 is
  `d1bf5ba978064948875b6341fd6f4e3f58281190fe66e3a963962cedb3897e2b`.
- Parent and candidate OpenAPI SHA-256 is
  `737c8b7f61cae70c975d513609800bfcc72339c6858fe56b4221cd5c489cde58`.
- Application import succeeds without an OpenAI key when supplied synthetic,
  non-connectable database configuration; the optional client remains absent.

## Deployment state

Candidate only. Production source, services, databases, containers, systemd,
AWS controls, credentials, network listeners, and frontend are unchanged.
