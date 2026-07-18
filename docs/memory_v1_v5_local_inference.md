# Memory V1 V5 local inference

Status: local provider, exact-target canary, append-only persistence, and
rollback-only security contracts implemented on an isolated branch. The RESSE
runtime is installed and active on authenticated loopback only. Production
extraction remains inactive until the seebx-to-RESSE security-group path is
opened and the exact canary passes. Retrieval and prompts remain unchanged.

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
  `86a9c79f866799eb0e7e89c03578cc5d808e`.
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
- Rejected larger comparison model: `Qwen/Qwen3-30B-A3B-GGUF`, revision
  `e4d4bafdfb96a411a163846265362aceb0b9c63a`, file size `18556685824`
  bytes, SHA-256
  `0d003f6662faee786ed5da3e31b29c978de5ae5d275c8794c606a7f3c01aa8f5`.
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

The llama.cpp endpoint also requires a dedicated API key. RESSE stores it in a
root/service-group credential and seebx stores its copy as root-owned mode 0600.
Plain HTTP is permitted only on loopback because the
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

The 8B candidate was rejected. On the frozen held-out packet, the raw pinned
14B candidate passed 14/22 while the 30B-A3B candidate passed 11/22. The 14B
provider plus deterministic policy/compiler layer then passed 119/119 frozen
synthetic cases across core, held-out, generalization, adversarial, and
project-current safety packets. The provider made zero external calls and the
suite made zero database, Qdrant, Redis, claim, staging, retrieval, or prompt
writes. The final provider also passed the 15/15 representative suite on the
RESSE CPU runtime. The exact provider file SHA-256 for both final runs is
`5d0bd5b146986bf85a3dbf51e48234d5402c9fd4f917c6fff24dab500d48c484`.

The GPU report SHA-256 is
`729957da7dc7b436525ca273203b027c719b60efa53150baa086e8fa30a6ad05`.
The CPU parity report SHA-256 is
`e74fee926b38f7a73a7306ff237a304a4ca3167c7fda03b766977243068d02a1`.
Neither report contains source prose.

The disposable production clone passed idempotent installation, accepted and
rejected local outcomes, exact target/hash binding, quota and circuit stops,
post-completion replay, cross-owner rejection, forced RLS, append-only ACLs,
restricted function ownership, full non-target table hashes, and complete
rollback. The production database was not modified by this test.

The installed RESSE service runs as `resse-inference`, checks the pinned model
and binary hashes before every start, binds only `127.0.0.1:18080`, requires an
API key, and has a systemd exposure score of 2.7 (`OK`). The restricted tunnel
user, source-IP-bound forwarding key, and pinned RESSE host key are installed.
The seebx tunnel unit is installed but intentionally disabled and inactive
because TCP 22 from the seebx security group to the RESSE security group is
still blocked.

## Persistence boundary

Local inference uses separate tables and functions. It does not reuse or
weaken the external-call ledger. `memory.v5_local_inference_event` stores
owner-scoped reservation/completion events, target hashes, pinned runtime and
model hashes, call counts, and rejection codes. The local packet table stores
the canonically validated immutable V5 packet. Both tables have forced RLS and
append-only triggers; `brains_app` has no direct table privileges.

The claim function is exact-job and exact-content-hash bound. An accepted
packet changes only the extraction job to `review_required`; it creates no
claim, projection, Qdrant point, retrieval candidate, or prompt influence.
Replays use the original operation IDs and perform zero writes.

## Activation gates

1. Local adapter unit and existing provider regression tests pass.
2. Synthetic full-contract cases pass on the pinned GPU and CPU runtimes.
3. A separate append-only local-call ledger and owner-scoped atomic claim path
   pass the production-clone security suite.
4. Restricted SSH transport, host-key pinning, API-key enforcement, loopback
   binding, and fail-closed service behavior are verified.
5. One owner-scoped real canary creates only queue/audit state and one immutable
   extraction packet; account isolation and Qdrant invariance are proved.
6. Promotion, a bounded owner allowlist scheduler, retrieval, and prompt
   influence remain separate later gates and are not authorized by this phase.
