# Memory V1 V5 local inference

Status: isolated implementation and synthetic evaluation only. Runtime
inactive for production extraction, persistence, retrieval, and prompts.

## Boundary

The local provider is a separate capability. It does not reuse or relax the
`openai_responses` call ledger. `external_model_calls` remains zero. Local
inference calls are accounted separately before any scheduler activation.

The provider receives one trusted, owner-scoped evidence record after the
server has resolved ownership. It cannot receive or emit `owner_user_id`,
durable entity IDs, claim IDs, approval state, salience, or retrieval policy.
Generated output is untrusted until the existing V5 Pydantic, span, predicate,
temporal, project-scope, and normalized-schema validators accept it.

No generated packet directly creates a claim or influences retrieval. The
existing append-only review path remains mandatory.

## Pinned artifacts

- Runtime: `ggml-org/llama.cpp` release `b10066`, commit
  `86a9c79f866799eb0e7e89c03578ccfbcc5d808e`.
- Official Ubuntu CPU archive SHA-256:
  `8387ddb55db54ea0b12e38182b73284df6b0d1b847f3d53e6111c2102c5da331`.
- Production-candidate model repository: `Qwen/Qwen3-14B-GGUF`, revision
  `530227a7d994db8eca5ab5ced2fb692b614357fd`.
- Production-candidate file: `Qwen3-14B-Q4_K_M.gguf`, size `9001752960`
  bytes, SHA-256
  `500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0`.
- Rejected comparison model: `Qwen/Qwen3-8B-GGUF`, revision
  `7c41481f57cb95916b40956ab2f0b139b296d974`, file SHA-256
  `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`.
- License: Apache-2.0.

The runtime and model hashes must be verified before every installation. The
production-candidate alias exposed to the provider is
`qwen3-14b-local-extractor`.

## Runtime placement

RESSE is the intended always-on extraction host. The model server binds only
to `127.0.0.1`; it is never exposed on a public or VPC interface. CPU latency is
acceptable because extraction is asynchronous and initially limited to twelve
records per owner per rolling 24 hours.

Resse-Train is an evaluation host. Its A10G GPU is used for fast contract and
quality evaluation while thresholds and prompts are tuned. Production must not
depend on Resse-Train being powered on.

## Transport

The production transport is a dedicated SSH local-forward service from seebx
to RESSE. The RESSE authorized key must be restricted to port forwarding, with
`permitopen="127.0.0.1:18080"`, no shell, no PTY, no agent forwarding, no X11,
and no user RC. seebx connects only to its own loopback forwarded port.

The llama.cpp endpoint also requires a dedicated API key from a mode-0600
credential file. Plain HTTP is permitted only on loopback because the
inter-host segment is encrypted and authenticated by the restricted SSH
tunnel. Direct private-IP plaintext is rejected by the provider. Direct HTTPS
requires TLS 1.3, a pinned private CA, and a client certificate.

## Structured output

llama.cpp converts JSON Schema to GBNF. The canonical provider schema contains
a `maxLength=5000` source-quote bound that exceeds llama.cpp's safe grammar
repetition limit. The local adapter removes only grammar string bounds above
1024. It does not modify the canonical schema or post-generation validators.
The original Pydantic `maxLength=5000` rule remains authoritative.

The grammar also replaces the free-form predicate pattern with the exact
governed predicate registry allowlist. The local-only prompt includes
canonically validated structural examples for deferrals, preferences,
projects, entity-valued relations, pets, and corrections. The examples do not
change the canonical validator and do not contain production data.

Reasoning is disabled. Temperature is 0.2, seed is fixed, top-k is 20, top-p is
0.8, retries are zero,
responses are capped at four MiB, exactly one choice is accepted, and any
reasoning content, model alias mismatch, non-stop finish, malformed JSON, or
contract violation is rejected.

The 8B and initial 14B prompt each passed only 1/10 synthetic semantic cases
and were rejected. With the governed-predicate grammar and validated diverse
examples, the pinned 14B candidate passed the unchanged 10/10 suite. Unseen
and adversarial synthetic evaluation remains required before a real canary.

## Activation gates

1. Local adapter unit and existing provider regression tests pass.
2. Synthetic full-contract cases pass on the pinned GPU and CPU runtimes.
3. A separate append-only local-call ledger and owner-scoped atomic claim path
   pass the production-clone security suite.
4. Restricted SSH transport, host-key pinning, API-key enforcement, loopback
   binding, and fail-closed service behavior are verified.
5. One owner-scoped real canary creates only queue/audit state and one immutable
   extraction packet; account isolation and Qdrant invariance are proved.
6. The bounded owner allowlist scheduler may then be enabled. Retrieval and
   prompt influence remain separate later gates.
