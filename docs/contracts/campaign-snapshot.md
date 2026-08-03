# Campaign snapshot contract

A campaign snapshot is derived from a single allowlisted campaign directory.

Validation sequence:

1. reject path escape, symlink, unexpected file, invalid encoding, or excessive file size;
2. parse YAML while rejecting duplicate mapping keys;
3. validate every document against strict Pydantic models;
4. validate cross-document keys and source-policy references;
5. validate the exact manual-seed CSV header and every row;
6. serialize each document to canonical JSON;
7. hash each component with SHA-256;
8. hash the sorted canonical bundle representation.

The resulting digest is semantic: comments, YAML key order, and line endings do not change it;
modeled values, seed row order, and referenced identities do.

The current Berlin campaign is structurally valid but explicitly `blocked` for production runs
until the approved Berlin polygon artifact is added. Snapshot generation does not reinterpret this
blocker as readiness.
