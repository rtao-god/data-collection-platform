# Owner map

| Meaning | Canonical owner in the current slice |
|---|---|
| Authored campaign content | Versioned files under `campaigns/<campaign-key>/` |
| Campaign schema | `collection_contracts.campaign_config` |
| Campaign filesystem boundary | `collection_infrastructure.FilesystemCampaignBundleSource` |
| Campaign validation/canonicalization | `collection_application.CampaignSnapshotService` |
| Snapshot digest contract | `collection_contracts.snapshot` |
| Generated JSON Schema | Python contract types + `tools/contract_generation/generate.py` |
| Typed error envelope | `collection_contracts.errors` |
| Config database metadata | `collection_infrastructure.postgres.metadata` |
| Database migration history | `database/migrations/` |
| Migration execution | `collection_infrastructure.postgres.migrations` |
| Migration composition | `apps/migration` |
| Work-unit transitions | `collection_domain.work_units` |
| Import dependency policy | `tools/architecture_checks/check_dependencies.py` |

The migrated database scope contains only meanings already represented by the campaign snapshot.
Planned owners without production contracts remain absent; empty packages and placeholder services
are not created.
