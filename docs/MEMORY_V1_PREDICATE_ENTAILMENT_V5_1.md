# Memory V1 predicate entailment V5.1

Status: offline and production-clone testing only. Runtime inactive.

Server: seebx backend.

The V5 structural registry proves that a predicate, object shape, modality,
projection class, temporal shape, and surface policy are allowed. It does not
prove that the source entails the proposed observation. V5.1 adds that missing
source-level gate.

The gate receives the complete source clause plus the proposed observation. It
returns `accept` or `defer`, records a policy version and exact source span, and
never rewrites one predicate into another. Raw evidence remains canonical even
when structured extraction is deferred.

The first guarded semantic family is occupation/credential separation:

- `occupation.works_as` requires explicit employment, work, practice, or a
  current professional-role statement.
- Training, education, qualification, and credentialing do not establish
  employment.
- An employment disclaimer blocks an affirmed occupation observation.
- `credential.reported` requires an explicit credential report.
- Ambiguous language is deferred rather than mapped to the nearest predicate.

Historical V5 artifacts are not rewritten. The V5.1 override for `v5-04`
supersedes its incorrect evaluation expectation, while the original file and
production audit trail remain reproducible.
