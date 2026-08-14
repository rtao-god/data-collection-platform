# Extraction and normalization contracts

## Owner boundary

`extraction_core` owns deterministic conversion of one exact immutable HTML artifact into one
source-specific `ExtractedRecord`. `normalization_core` owns deterministic conversion of one exact
`ExtractedRecord` plus one exact `NormalizationProfile` into typed field observations.

Neither package owns candidates, public listings, source acquisition, database persistence, review,
quality, or export eligibility.

## Extraction input and output

The extraction input consists of exactly:

- `raw_source_document` — the immutable raw artifact bytes;
- `extraction_request` — source record identity, exact artifact digest, source URL, policy digest,
  extractor revision, allowed/prohibited fields, locale, and byte/evidence limits.

The output contract is `extracted-record@1`. It contains:

- exact raw artifact and source-policy identities;
- source-specific entity-kind/category candidates;
- typed extracted fields;
- bounded evidence references;
- prohibited-field evidence without retained source content;
- bounded text blocks for evidence-backed pattern rules;
- typed extraction issues;
- deterministic content digest.

Supported embedded metadata sources are JSON-LD, microdata, and RDFa through the `extruct` adapter.
HTML contact, canonical URL, address, and bounded semantic text extraction remain inside the same
owner. Full page text is never copied into the output contract.

## Normalization input and output

The normalization input consists of exactly:

- `extracted_record` — a digest-verified `ExtractedRecord`;
- `normalization_profile` — versioned field mappings, phone region context, pattern rules, and
  prohibited fields.

The output contract is `field-observation-batch@1`. Each observation has one explicit state:

- `observed`;
- `not_observed`;
- `absent_in_source`;
- `unsupported`;
- `prohibited_by_policy`;
- `invalid`;
- `expired`;
- `disputed`.

Observed and invalid values require evidence. `not_observed` and `unsupported` cannot contain a
fabricated value or evidence. Prohibited evidence retains locator and digest, but not the prohibited
source text.

The first normalizers cover text, URL/domain, email, phone, structured address, money, string sets,
and evidence-backed boolean pattern rules. Negative patterns take precedence over positive patterns.
A price is valid only when amount, currency, and basis are all explicit. A free-form-only address is
`invalid` until an approved parser or review path exists.

## Runtime isolation

`processing-worker` runs one configured capability per process: `extraction` or `normalization`.
It receives all inputs through Worker Gateway scoped reads and publishes exactly one
`derived_artifact` through the verified upload/completion protocol. It has no PostgreSQL, migration,
S3 SDK, browser, or crawler dependency.
