# Assistant Preference Compiler v3 Candidate

## Scope

This candidate changes only the governed response-preference compiler,
fixed server-owned preference catalog, candidate schema migration, and tests.
It does not modify production, prompts outside the preference block, Memory V1,
FM/RAG retrieval, authentication, Qdrant, environment variables, or services.

## Design decisions

- Editable user prose remains untrusted and is never injected directly.
- The compiled plan remains a closed list of typed, server-owned instructions.
- The maximum compiled rules increase from 8 to 12.
- Candidate summaries increase from 12 to 16 entries so four presentation
  settings and twelve compiled rules remain representable.
- `calm_patient_tone` adds calm and patience without placating language.
- `contextual_poetic_language` permits restrained poetic phrasing only in
  casual or reflective prose and explicitly excludes technical, high-stakes,
  sensitive, and serious contexts.
- Role labels and worldview claims cannot establish philosophy, domain policy,
  factual authority, memory behavior, tool control, or retrieval behavior.
- Selection instructions prioritize coverage, faithfulness, specificity,
  explicit evidence, and null settings when contradictory preferences have no
  clear priority.

## Semantic evaluation

The no-database-write provider evaluation contains eight cases:

- concise and direct ordinary preferences;
- natural and contextually playful preferences;
- nuanced prose, calmness, and poetic-language boundaries;
- long technical response preferences;
- unsafe or epistemically invalid requests;
- worldview-only material;
- prompt injection inside untrusted preference prose;
- contradictory response-length instructions.

The first two-run evaluation passed 4/8. It exposed four concrete defects:
unstable inference of `information_dense`, unstable inference of plain
technical depth, unstable inference of direct conversation style, and a false
`cannot_force_agreement` classification for worldview material.

After tightening explicitness and rejection requirements, the same evaluation
passed 8/8. All eight plans were identical across both runs. The runner uses
the Responses API with `store=False` and performs no database writes.

## Current status

This is an isolated candidate. Production activation requires separate review,
schema authorization, deployment authorization, rollback preparation, service
restart authorization, and authenticated post-deployment verification.
