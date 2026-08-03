# Owner map

| Meaning | Canonical owner in the current slice |
|---|---|
| Authored campaign content | Versioned files under `campaigns/<campaign-key>/` |
| Campaign schema | `collection_contracts.campaign_config` |
| Campaign filesystem boundary | `collection_infrastructure.FilesystemCampaignBundleSource` |
| Campaign validation/canonicalization | `collection_application.CampaignSnapshotService` |
| Snapshot digest contract | `collection_contracts.snapshot` |
| Typed error envelope | `collection_contracts.errors` |
| Work-unit transitions | `collection_domain.work_units` |
| Import dependency policy | `tools/architecture_checks/check_dependencies.py` |

Planned owners that do not yet have production files are intentionally absent. Empty packages and
placeholder services are not created.
