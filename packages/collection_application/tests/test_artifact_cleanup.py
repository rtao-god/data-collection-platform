from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from collection_application.artifact_cleanup import (
    ArtifactCleanupClaim,
    ArtifactCleanupPolicy,
    ArtifactCleanupService,
)


class Store:
    def __init__(self, claims: tuple[ArtifactCleanupClaim, ...]) -> None:
        self.claims = claims
        self.deleted: list[UUID] = []
        self.failures: list[tuple[UUID, bool, str, str]] = []

    def claim(self, *, now_utc: datetime, policy: ArtifactCleanupPolicy):
        del now_utc, policy
        return self.claims

    def mark_deleted(
        self,
        claim: ArtifactCleanupClaim,
        *,
        deleted_at_utc: datetime,
    ) -> None:
        del deleted_at_utc
        self.deleted.append(claim.tombstone_id)

    def mark_failed(
        self,
        claim: ArtifactCleanupClaim,
        *,
        failed_at_utc: datetime,
        retry_not_before_utc: datetime | None,
        error_code: str,
        error_digest: str,
        terminal: bool,
    ) -> None:
        del failed_at_utc, retry_not_before_utc
        self.failures.append((claim.tombstone_id, terminal, error_code, error_digest))


class Objects:
    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.deleted: list[str] = []

    def delete(self, storage_reference: str) -> None:
        if storage_reference in self.failing:
            raise OSError(f"cannot delete {storage_reference}")
        self.deleted.append(storage_reference)


def claim(position: int, *, attempt_count: int = 1) -> ArtifactCleanupClaim:
    return ArtifactCleanupClaim(
        tombstone_id=UUID(int=position + 1),
        upload_id=UUID(int=position + 101),
        storage_reference=f"staging/{position}",
        attempt_count=attempt_count,
    )


def policy(*, max_attempts: int = 3) -> ArtifactCleanupPolicy:
    return ArtifactCleanupPolicy(
        grace_period=timedelta(days=1),
        claim_timeout=timedelta(minutes=15),
        retry_delay=timedelta(minutes=5),
        batch_size=10,
        max_attempts=max_attempts,
    )


def test_cleanup_deletes_claimed_objects_and_marks_tombstones() -> None:
    store = Store((claim(1), claim(2)))
    objects = Objects()
    result = ArtifactCleanupService(store, objects).run_once(
        now_utc=datetime(2026, 8, 13, tzinfo=UTC),
        policy=policy(),
    )
    assert result.deleted_count == 2
    assert store.deleted == [UUID(int=2), UUID(int=3)]
    assert objects.deleted == ["staging/1", "staging/2"]


def test_cleanup_schedules_retry_without_plain_error_text() -> None:
    store = Store((claim(1),))
    result = ArtifactCleanupService(store, Objects({"staging/1"})).run_once(
        now_utc=datetime(2026, 8, 13, tzinfo=UTC),
        policy=policy(),
    )
    assert result.retry_scheduled_count == 1
    _, terminal, code, digest = store.failures[0]
    assert terminal is False
    assert code == "O_S_ERROR"
    assert digest.startswith("sha256:")
    assert "staging/1" not in digest


def test_cleanup_marks_terminal_failure_at_attempt_budget() -> None:
    store = Store((claim(1, attempt_count=3),))
    result = ArtifactCleanupService(store, Objects({"staging/1"})).run_once(
        now_utc=datetime(2026, 8, 13, tzinfo=UTC),
        policy=policy(max_attempts=3),
    )
    assert result.failed_count == 1
    assert store.failures[0][1] is True
