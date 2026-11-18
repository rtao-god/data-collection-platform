# ADR 0002: collector-owned export boundary

## Status

Accepted.

## Decision

The final product of this repository is a deterministic, sealed collector export package. The
future Aggregator Platform will define its own ingestion API and generated client when that
repository exists.

## Sealed package contract

- `collection_contracts.export_package` owns the `export-package@1` contract and its canonical
  digest. `package_digest` is computed over the canonical JSON of the payload and is verified
  on construction and on every decode; a package whose digest does not match its content is
  rejected rather than repaired.
- `collection_infrastructure.export.assembly` owns assembly. It reads candidate revisions and
  cluster quality through read-only ports and never recomputes quality or reinterprets match
  scores. It consumes the quality owner's `exportEligible` as the sole eligibility authority.
- Assembly fails the whole package closed when quality state is missing or not eligible,
  geography coverage is unproven, a candidate carries no name or no source provenance, more
  than one revision of the same candidate is exposed, or a run would produce an empty package.
  It never drops a bad candidate silently: an incomplete catalog release is not published.
- Entries are ordered by candidate id and each entry binds its exact candidate revision,
  snapshot digest, quality evaluation digest, quality policy digest, and source artifacts.

## Verification

`verify_export_package` and `decode_export_package` are the independent integrity check. A
consumer must verify the package before use; integrity is never assumed from transport.

## Rejected alternative

A temporary Aggregator DTO in this repository would create an unowned public-catalog contract and
would likely become legacy before its consumer exists.
