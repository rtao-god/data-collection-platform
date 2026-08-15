from __future__ import annotations

import sqlalchemy as sa
from collection_infrastructure.postgres.pipeline_advancement import _secret_digest
from collection_infrastructure.postgres.pipeline_advancement_metadata import (
    PIPELINE_ADVANCEMENT_TABLES,
    pipeline_advancement_attempts,
    pipeline_advancements,
)
from sqlalchemy.dialects import postgresql


def test_pipeline_advancement_metadata_has_exact_owner_tables() -> None:
    assert (
        pipeline_advancements,
        pipeline_advancement_attempts,
    ) == PIPELINE_ADVANCEMENT_TABLES
    assert pipeline_advancements.schema == "work"
    assert pipeline_advancement_attempts.schema == "work"
    assert pipeline_advancements.primary_key.columns.keys() == ["advancement_id"]
    assert pipeline_advancement_attempts.primary_key.columns.keys() == ["event_id"]
    assert pipeline_advancements.c.source_work_unit_id.unique is True
    assert pipeline_advancements.c.source_input_artifacts.type.__class__.__name__ == "JSONB"
    assert pipeline_advancement_attempts.c.blocker_context.type.__class__.__name__ == "JSONB"


def test_claim_statement_uses_skip_locked_only_for_queue_claim() -> None:
    statement = (
        sa.select(pipeline_advancements)
        .where(pipeline_advancements.c.state == "pending")
        .order_by(
            pipeline_advancements.c.created_at_utc,
            pipeline_advancements.c.advancement_id,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    sql = str(statement.compile(dialect=postgresql.dialect())).upper()

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "LIMIT" in sql


def test_lease_token_digest_is_deterministic_and_non_reversible() -> None:
    first = _secret_digest("lease-token-1")
    second = _secret_digest("lease-token-1")

    assert first == second
    assert first.startswith("sha256:")
    assert "lease-token-1" not in first


def test_pipeline_advancement_metadata_targets_canonical_work_identity() -> None:
    from collection_infrastructure.postgres.pipeline_advancement_metadata import (
        pipeline_advancements,
    )

    foreign_key = next(iter(pipeline_advancements.c.source_work_unit_id.foreign_keys))
    assert foreign_key.target_fullname == "work.work_units.work_id"
    assert foreign_key.ondelete == "RESTRICT"
