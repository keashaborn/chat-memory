# Response Policy Regression V1

Status: isolated hardening candidate
Production activation: prohibited
Production runtime changed: no

## Purpose

This contract combines response-policy regression coverage with domain-risk
classifier calibration. It exercises the typed server path without calling a
Verbal Sage route, loading personal memory, retrieving FM material, or writing
to a database:

```text
synthetic conversation
  -> OpenAIServerResponseSignalClassifierV0_2
  -> ResponsePolicySignalsV0_2
  -> decide_response_policy_v0_2
  -> render_response_policy_prompt_v0_2
  -> content-free result
```

## Required behavior

- Ordinary questions, general educational questions, benign fitness language,
  and conventional closings must not become high stakes.
- Concrete computing, infrastructure, code, and device work may select
  `TECHNICAL`; simple arithmetic and general education must not.
- Guided reflection remains independent of domain response mode and cannot
  activate an intervention.
- Direct requests take precedence over reflection and cannot become material
  clarification solely from provider drift.
- Intervention design remains separate from consent to a specific experiment.
- `FM-IR-020` remains unavailable until every typed experiment prerequisite is
  explicitly satisfied.
- Question refusal applies across interactions.
- Transient classifier unavailability suppresses FM and intervention while
  preserving exact guided-reflection requests.
- Acute danger and consequential personal medical, legal, financial, abuse,
  child-safety, substance, and eating-disorder cases retain controlling
  safeguards.
- Educational, historical, quoted, research, or definitional use of a risk
  term is not by itself evidence of current personal danger.
- FM stays off unless an explicit or narrowly authorized FM signal survives
  the controlling gates.

## Coverage

The catalog contains 52 synthetic cases across:

- benign ordinary conversation and closings;
- benign fitness and nutrition;
- genuine fitness and nutrition risk;
- direct, reflective, and intervention interactions;
- technical and explicit FM modes;
- local and provider-derived high-stakes cases;
- transient provider failure;
- educational use of safety vocabulary;
- multi-turn continuation and interaction transitions.

Thirty-five cases are eligible for bounded live provider classification. Live
execution uses `store=false`, uses synthetic text only, does not call product
routes, and emits content-free result records.

## Exit behavior

The deterministic runner returns:

- `0` when all selected requirements pass;
- `1` for any unexpected regression;
- `2` only when a catalog explicitly retains reviewed known gaps.

The hardened catalog contains no accepted known gap.

## Commands

Run on **seebx**, from the isolated worktree:

```bash
cd /home/ubuntu/chat-memory-response-policy-hardening-v1
PYTHONDONTWRITEBYTECODE=1 /opt/chat-memory/venv/bin/python \
  scripts/response_policy_regression_v1.py
```

The live provider run requires the existing service credential to be supplied
to the process without printing or persisting it:

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/chat-memory/venv/bin/python \
  scripts/response_policy_regression_v1.py \
  --live-provider \
  --repeats 2
```

Do not commit live result JSON because provider identifiers and timing are
operational audit data.

## Non-goals

- No production deployment or restart.
- No Memory V1, FM corpus/RAG, authentication, database, Qdrant, web-search,
  voice, or frontend change.
- No user-account, personal-memory, or transcript use.
- No raw assembled prompt or generated answer capture.
