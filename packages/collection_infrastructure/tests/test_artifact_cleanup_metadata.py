from collection_infrastructure.postgres.artifact_metadata import (
    artifact_cleanup_tombstones,
)


def test_artifact_cleanup_tombstone_metadata_is_complete() -> None:
    assert artifact_cleanup_tombstones.schema is not None
    assert {column.name for column in artifact_cleanup_tombstones.columns} == {
        "tombstone_id",
        "upload_id",
        "storage_reference",
        "reason",
        "state",
        "created_at_utc",
        "eligible_at_utc",
        "claimed_at_utc",
        "claim_expires_at_utc",
        "attempt_count",
        "retry_not_before_utc",
        "deleted_at_utc",
        "error_code",
        "error_digest",
        "revision",
    }
