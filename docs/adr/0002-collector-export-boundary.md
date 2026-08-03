# ADR 0002: collector-owned export boundary

## Status

Accepted.

## Decision

The final product of this repository is a deterministic, sealed collector export package. The
future Aggregator Platform will define its own ingestion API and generated client when that
repository exists.

## Rejected alternative

A temporary Aggregator DTO in this repository would create an unowned public-catalog contract and
would likely become legacy before its consumer exists.
