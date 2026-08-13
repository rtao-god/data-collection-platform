# Artifact cleanup operation

The cleanup command removes only abandoned staging objects. It does not delete referenced raw or derived artifacts.

```bash
uv run python -m collector_cli.artifact_cleanup
```

Required settings:

- `COLLECTOR_DATABASE_URL`;
- `ARTIFACT_S3_ENDPOINT_URL`;
- `ARTIFACT_S3_BUCKET`;
- `ARTIFACT_S3_ACCESS_KEY_ID`;
- `ARTIFACT_S3_SECRET_ACCESS_KEY`;
- `ARTIFACT_S3_REGION`.

Optional policy settings:

- `ARTIFACT_CLEANUP_GRACE_SECONDS`, default `86400`;
- `ARTIFACT_CLEANUP_CLAIM_SECONDS`, default `900`;
- `ARTIFACT_CLEANUP_RETRY_SECONDS`, default `300`;
- `ARTIFACT_CLEANUP_BATCH_SIZE`, default `100`;
- `ARTIFACT_CLEANUP_MAX_ATTEMPTS`, default `10`.

The operation is one-shot and scheduler-owned. It never runs as a side effect of a read endpoint or application startup.

For each eligible upload, PostgreSQL first commits a durable tombstone and claim. S3 deletion is then idempotent. A process crash after deletion but before acknowledgement causes the same object key to be deleted again safely. Referenced artifacts and tombstoned uploads are protected by database constraints and triggers.
