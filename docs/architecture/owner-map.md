# Owner map

| Meaning | Canonical production owner |
|---|---|
| Authored campaign content | Versioned files under `campaigns/<campaign-key>/` |
| Campaign schema | `collection_contracts.campaign_config` |
| Campaign filesystem boundary | `collection_infrastructure.FilesystemCampaignBundleSource` |
| Manual import parsing, locators, record identity, plan identity, and error ledger | `manual_import_core` |
| Campaign validation/canonicalization | `collection_application.CampaignSnapshotService` |
| Snapshot digest contract | `collection_contracts.snapshot` |
| Snapshot persistence | `collection_infrastructure.postgres.PostgresCampaignSnapshotStore` |
| Generated JSON Schema/OpenAPI | Python owner types and `tools/contract_generation/generate.py` |
| Typed error envelope | `collection_contracts.errors` |
| Run, stage, work, lease, retry semantics | `collection_domain.runs`, `work_units`, `work_leases`, and `work_retry` |
| Work commands and ports | `collection_application.work_engine` |
| Worker-facing transport | `apps/worker_gateway` |
| Source connector Worker Gateway client | `source_connector_sdk.SourceWorkerGateway` |
| Extracted-record and field-observation wire contracts | `collection_contracts.observations` |
| Embedded structured/HTML evidence extraction | `extraction_core` |
| Typed evidence-preserving normalization | `normalization_core` |
| Extraction/normalization runtime composition | `apps/processing_worker` |
| Review wire contracts and immutable review records | `review_contracts` |
| Review and suppression state transitions | `review_core` |
| Review permissions, commands, queries, and persistence port | `review_application` |
| Review PostgreSQL persistence | `review_infrastructure.PostgresReviewRepository` |
| Review runtime composition and authenticated transport | `apps/control_api` |
| Worker authentication scope | `worker_gateway.auth` and mounted worker token document |
| Work persistence and queue claim | `collection_infrastructure.postgres.PostgresWorkEngine` |
| Artifact transfer contract | `collection_application.artifacts` |
| Artifact object integrity and content keys | `collection_infrastructure.object_store.S3ArtifactObjectStore` |
| Artifact metadata and work bindings | `collection_infrastructure.postgres.artifact_metadata` |
| Lease-scoped artifact transfer | `collection_infrastructure.postgres.PostgresArtifactTransfer` |
| Database migration history | `database/migrations/` |
| Migration execution | `collection_infrastructure.postgres.migrations` |
| Migration composition | `apps/migration` |
| Import dependency policy | `tools/architecture_checks/check_dependencies.py` |

Workers consume only Worker Gateway contracts and pre-signed object URLs. They do not import the
PostgreSQL adapters and receive no Collection database credentials. The Worker Gateway, migration,
and collector CLI dependency closures exclude Review owners; only Control API composes
`review_infrastructure`. S3/SeaweedFS details remain infrastructure concerns and do not enter domain
or application contracts.

Planned owners without production contracts remain absent; empty packages and placeholder services
are not created.
