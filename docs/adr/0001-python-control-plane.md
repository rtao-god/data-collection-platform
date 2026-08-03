# ADR 0001: one Python backend runtime

## Status

Accepted.

## Decision

Use Python 3.13 for the control plane, orchestration integration, acquisition workers, extraction,
normalization, entity resolution, and quality pipeline. Use one `uv` workspace and one committed
`uv.lock`.

## Consequences

The repository does not introduce a .NET control plane alongside Python workers. This avoids a
second transport-contract owner, a second migration stack, and an additional runtime without a
separate bounded context that justifies it.
