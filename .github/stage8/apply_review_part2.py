from __future__ import annotations

from pathlib import Path
from textwrap import dedent

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(content).lstrip(), encoding="utf-8")


write(
    "database/migrations/versions/20260814_0009_review_console.py",
    r'''
    """Add transactional review queue and append-only audit history.

    Revision ID: 20260814_0009
    Revises: 20260813_0008
    Create Date: 2026-08-14
    """

    from __future__ import annotations

    from collections.abc import Sequence

    import sqlalchemy as sa
    from alembic import op
    from sqlalchemy.dialects import postgresql

    revision: str = "20260814_0009"
    down_revision: str | None = "20260813_0008"
    branch_labels: str | Sequence[str] | None = None
    depends_on: str | Sequence[str] | None = None


    def upgrade() -> None:
        op.execute("CREATE SCHEMA IF NOT EXISTS review")
        op.create_table(
            "review_items",
            sa.Column("review_item_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("campaign_key", sa.Text(), nullable=False),
            sa.Column("item_kind", sa.Text(), nullable=False),
            sa.Column("subject_id", sa.Text(), nullable=False),
            sa.Column("source_snapshot_contract", sa.Text(), nullable=False),
            sa.Column("source_snapshot_digest", sa.Text(), nullable=False),
            sa.Column("payload_digest", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("current_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("active_suppression_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("correlation_id", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "campaign_key ~ '^[a-z][a-z0-9_]{0,199}$'",
                name="ck_review_items_campaign_key",
            ),
            sa.CheckConstraint(
                "item_kind IN ('resolution_pair', 'cluster_quality', "
                "'observation_conflict')",
                name="ck_review_items_kind",
            ),
            sa.CheckConstraint(
                "state IN ('pending', 'decided', 'suppressed')",
                name="ck_review_items_state",
            ),
            sa.CheckConstraint(
                "source_snapshot_digest ~ '^sha256:[0-9a-f]{64}$' AND "
                "payload_digest ~ '^sha256:[0-9a-f]{64}$'",
                name="ck_review_items_digests",
            ),
            sa.CheckConstraint("revision >= 0", name="ck_review_items_revision"),
            sa.PrimaryKeyConstraint("review_item_id", name="pk_review_items"),
            sa.UniqueConstraint(
                "source_snapshot_digest",
                "item_kind",
                "subject_id",
                name="uq_review_items_source_subject",
            ),
            schema="review",
        )
        op.create_index(
            "ix_review_items_queue",
            "review_items",
            ["state", "campaign_key", "item_kind", "created_at_utc", "review_item_id"],
            schema="review",
        )

        op.create_table(
            "review_item_payloads",
            sa.Column("review_item_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("payload_digest", sa.Text(), nullable=False),
            sa.Column("evidence_digest", sa.Text(), nullable=False),
            sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("correlation_id", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "payload_digest ~ '^sha256:[0-9a-f]{64}$' AND "
                "evidence_digest ~ '^sha256:[0-9a-f]{64}$'",
                name="ck_review_item_payloads_digests",
            ),
            sa.ForeignKeyConstraint(
                ["review_item_id"],
                ["review.review_items.review_item_id"],
                name="fk_review_item_payloads_review_item_id",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("review_item_id", name="pk_review_item_payloads"),
            schema="review",
        )

        op.create_table(
            "evidence_bindings",
            sa.Column("review_item_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("role", sa.Text(), nullable=False),
            sa.Column("evidence_kind", sa.Text(), nullable=False),
            sa.Column("locator", sa.Text(), nullable=False),
            sa.Column("scalar_digest", sa.Text(), nullable=True),
            sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("correlation_id", sa.Text(), nullable=False),
            sa.CheckConstraint("position >= 0", name="ck_evidence_bindings_position"),
            sa.CheckConstraint(
                "role ~ '^[a-z][a-z0-9_]{0,99}$' AND "
                "evidence_kind ~ '^[a-z][a-z0-9_]{0,99}$'",
                name="ck_evidence_bindings_identity",
            ),
            sa.CheckConstraint(
                "scalar_digest IS NULL OR "
                "scalar_digest ~ '^sha256:[0-9a-f]{64}$'",
                name="ck_evidence_bindings_scalar_digest",
            ),
            sa.ForeignKeyConstraint(
                ["review_item_id"],
                ["review.review_items.review_item_id"],
                name="fk_evidence_bindings_review_item_id",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["artifact_id"],
                ["sources.artifact_records.artifact_id"],
                name="fk_evidence_bindings_artifact_id",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint(
                "review_item_id",
                "position",
                name="pk_evidence_bindings",
            ),
            schema="review",
        )

        op.create_table(
            "review_decisions",
            sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("review_item_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("expected_revision", sa.Integer(), nullable=False),
            sa.Column("resulting_revision", sa.Integer(), nullable=False),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("reason_code", sa.Text(), nullable=False),
            sa.Column("selected_reference", sa.Text(), nullable=True),
            sa.Column("actor_reference", sa.Text(), nullable=False),
            sa.Column("occurred_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("correlation_id", sa.Text(), nullable=False),
            sa.Column("command_digest", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "resulting_revision = expected_revision + 1",
                name="ck_review_decisions_revision",
            ),
            sa.CheckConstraint(
                "action IN ('match', 'separate', 'approve', 'reject', "
                "'accept_candidate', 'reject_candidate', 'defer')",
                name="ck_review_decisions_action",
            ),
            sa.CheckConstraint(
                "reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$' AND "
                "command_digest ~ '^sha256:[0-9a-f]{64}$'",
                name="ck_review_decisions_identity",
            ),
            sa.ForeignKeyConstraint(
                ["review_item_id"],
                ["review.review_items.review_item_id"],
                name="fk_review_decisions_review_item_id",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("decision_id", name="pk_review_decisions"),
            sa.UniqueConstraint(
                "review_item_id",
                "resulting_revision",
                name="uq_review_decisions_item_revision",
            ),
            schema="review",
        )

        op.create_table(
            "manual_observations",
            sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("review_item_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("expected_revision", sa.Integer(), nullable=False),
            sa.Column("resulting_revision", sa.Integer(), nullable=False),
            sa.Column("field_key", sa.Text(), nullable=False),
            sa.Column("value_kind", sa.Text(), nullable=False),
            sa.Column("normalized_value", sa.Text(), nullable=False),
            sa.Column("reason_code", sa.Text(), nullable=False),
            sa.Column("evidence_note", sa.Text(), nullable=True),
            sa.Column("actor_reference", sa.Text(), nullable=False),
            sa.Column("occurred_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("correlation_id", sa.Text(), nullable=False),
            sa.Column("command_digest", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "resulting_revision = expected_revision + 1",
                name="ck_manual_observations_revision",
            ),
            sa.CheckConstraint(
                "field_key ~ '^[a-z][a-z0-9_]{0,99}$'",
                name="ck_manual_observations_field_key",
            ),
            sa.CheckConstraint(
                "value_kind IN ('text', 'phone', 'email', 'url', 'address', 'money')",
                name="ck_manual_observations_value_kind",
            ),
            sa.CheckConstraint(
                "reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$' AND "
                "command_digest ~ '^sha256:[0-9a-f]{64}$'",
                name="ck_manual_observations_identity",
            ),
            sa.ForeignKeyConstraint(
                ["review_item_id"],
                ["review.review_items.review_item_id"],
                name="fk_manual_observations_review_item_id",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("observation_id", name="pk_manual_observations"),
            sa.UniqueConstraint(
                "review_item_id",
                "resulting_revision",
                name="uq_manual_observations_item_revision",
            ),
            schema="review",
        )

        op.create_table(
            "suppression_events",
            sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("suppression_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("review_item_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("expected_revision", sa.Integer(), nullable=False),
            sa.Column("resulting_revision", sa.Integer(), nullable=False),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("reason_code", sa.Text(), nullable=False),
            sa.Column("actor_reference", sa.Text(), nullable=False),
            sa.Column("occurred_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("correlation_id", sa.Text(), nullable=False),
            sa.Column("command_digest", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "resulting_revision = expected_revision + 1",
                name="ck_suppression_events_revision",
            ),
            sa.CheckConstraint(
                "action IN ('activate', 'resolve')",
                name="ck_suppression_events_action",
            ),
            sa.CheckConstraint(
                "reason_code ~ '^[A-Z][A-Z0-9_]{0,99}$' AND "
                "command_digest ~ '^sha256:[0-9a-f]{64}$'",
                name="ck_suppression_events_identity",
            ),
            sa.ForeignKeyConstraint(
                ["review_item_id"],
                ["review.review_items.review_item_id"],
                name="fk_suppression_events_review_item_id",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("event_id", name="pk_suppression_events"),
            sa.UniqueConstraint(
                "review_item_id",
                "resulting_revision",
                name="uq_suppression_events_item_revision",
            ),
            schema="review",
        )

        op.create_table(
            "audit_events",
            sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("review_item_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("command_kind", sa.Text(), nullable=False),
            sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("expected_revision", sa.Integer(), nullable=True),
            sa.Column("resulting_revision", sa.Integer(), nullable=False),
            sa.Column("actor_reference", sa.Text(), nullable=False),
            sa.Column("payload_digest", sa.Text(), nullable=False),
            sa.Column("occurred_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("correlation_id", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "command_kind IN ('admit', 'decision', 'manual_observation', "
                "'suppression_activate', 'suppression_resolve')",
                name="ck_audit_events_kind",
            ),
            sa.CheckConstraint(
                "resulting_revision >= 0 AND "
                "(expected_revision IS NULL OR expected_revision >= 0)",
                name="ck_audit_events_revision",
            ),
            sa.CheckConstraint(
                "payload_digest ~ '^sha256:[0-9a-f]{64}$'",
                name="ck_audit_events_payload_digest",
            ),
            sa.ForeignKeyConstraint(
                ["review_item_id"],
                ["review.review_items.review_item_id"],
                name="fk_audit_events_review_item_id",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("audit_event_id", name="pk_audit_events"),
            sa.UniqueConstraint(
                "review_item_id",
                "resulting_revision",
                name="uq_audit_events_item_revision",
            ),
            sa.UniqueConstraint(
                "command_kind",
                "command_id",
                name="uq_audit_events_command",
            ),
            schema="review",
        )

        op.create_foreign_key(
            "fk_review_items_current_decision_id",
            "review_items",
            "review_decisions",
            ["current_decision_id"],
            ["decision_id"],
            source_schema="review",
            referent_schema="review",
            ondelete="RESTRICT",
        )
        op.create_foreign_key(
            "fk_review_items_active_suppression_id",
            "review_items",
            "suppression_events",
            ["active_suppression_id"],
            ["event_id"],
            source_schema="review",
            referent_schema="review",
            ondelete="RESTRICT",
        )

        op.execute(
            """
            CREATE FUNCTION review.reject_append_only_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'review history is append-only'
                    USING ERRCODE = 'integrity_constraint_violation',
                          DETAIL = TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME ||
                                   ' rejects update and delete';
            END;
            $$
            """
        )
        for table_name in (
            "review_item_payloads",
            "evidence_bindings",
            "review_decisions",
            "manual_observations",
            "suppression_events",
            "audit_events",
        ):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table_name}_append_only
                BEFORE UPDATE OR DELETE ON review.{table_name}
                FOR EACH ROW EXECUTE FUNCTION review.reject_append_only_mutation()
                """
            )

        op.execute(
            """
            CREATE FUNCTION review.validate_review_item_update()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.review_item_id <> OLD.review_item_id
                   OR NEW.campaign_key <> OLD.campaign_key
                   OR NEW.item_kind <> OLD.item_kind
                   OR NEW.subject_id <> OLD.subject_id
                   OR NEW.source_snapshot_contract <> OLD.source_snapshot_contract
                   OR NEW.source_snapshot_digest <> OLD.source_snapshot_digest
                   OR NEW.payload_digest <> OLD.payload_digest
                   OR NEW.created_at_utc <> OLD.created_at_utc THEN
                    RAISE EXCEPTION 'review item immutable identity cannot change'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.revision <> OLD.revision + 1 THEN
                    RAISE EXCEPTION 'review item revision must increment exactly once'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.updated_at_utc < OLD.updated_at_utc THEN
                    RAISE EXCEPTION 'review item update timestamp cannot move backward'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_review_items_validate_update
            BEFORE UPDATE ON review.review_items
            FOR EACH ROW EXECUTE FUNCTION review.validate_review_item_update()
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_review_items_reject_delete
            BEFORE DELETE ON review.review_items
            FOR EACH ROW EXECUTE FUNCTION review.reject_append_only_mutation()
            """
        )


    def downgrade() -> None:
        op.execute("DROP TRIGGER trg_review_items_reject_delete ON review.review_items")
        op.execute("DROP TRIGGER trg_review_items_validate_update ON review.review_items")
        op.execute("DROP FUNCTION review.validate_review_item_update()")
        for table_name in (
            "audit_events",
            "suppression_events",
            "manual_observations",
            "review_decisions",
            "evidence_bindings",
            "review_item_payloads",
        ):
            op.execute(
                f"DROP TRIGGER trg_{table_name}_append_only ON review.{table_name}"
            )
        op.execute("DROP FUNCTION review.reject_append_only_mutation()")
        op.drop_constraint(
            "fk_review_items_active_suppression_id",
            "review_items",
            schema="review",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_review_items_current_decision_id",
            "review_items",
            schema="review",
            type_="foreignkey",
        )
        op.drop_table("audit_events", schema="review")
        op.drop_table("suppression_events", schema="review")
        op.drop_table("manual_observations", schema="review")
        op.drop_table("review_decisions", schema="review")
        op.drop_table("evidence_bindings", schema="review")
        op.drop_table("review_item_payloads", schema="review")
        op.drop_index(
            "ix_review_items_queue",
            table_name="review_items",
            schema="review",
        )
        op.drop_table("review_items", schema="review")
        op.execute("DROP SCHEMA IF EXISTS review")
    ''',
)

write(
    "packages/review_infrastructure/src/review_infrastructure/postgres.py",
    r'''
    from __future__ import annotations

    import base64
    import json
    from datetime import datetime
    from typing import cast
    from uuid import UUID

    import sqlalchemy as sa
    from sqlalchemy.engine import Engine, RowMapping

    from review_application import (
        EvidenceNotFound,
        ReviewConflict,
        ReviewItemNotFound,
        StaleReviewRevision,
    )
    from review_contracts import (
        AuditEvent,
        DecisionCommand,
        DecisionRecord,
        EvidenceBindingInput,
        EvidenceBindingRecord,
        EvidenceObject,
        JsonValue,
        ManualObservationCommand,
        ManualObservationRecord,
        ReviewAdmissionRequest,
        ReviewItemDetail,
        ReviewItemKind,
        ReviewItemState,
        ReviewItemSummary,
        ReviewMutationResult,
        ReviewQueuePage,
        SuppressionCommand,
        SuppressionRecord,
        allowed_decision_actions,
        canonical_digest,
        decision_command_digest,
        deterministic_audit_event_id,
        manual_observation_command_digest,
        suppression_command_digest,
    )


    class PostgresReviewRepository:
        def __init__(self, engine: Engine) -> None:
            self._engine = engine

        def admit(
            self,
            request: ReviewAdmissionRequest,
            *,
            actor_reference: str,
            correlation_id: str,
            now_utc: datetime,
        ) -> ReviewItemDetail:
            item_id = request.item_id()
            payload_digest = request.payload_digest()
            evidence_digest = request.evidence_digest()
            with self._engine.begin() as connection:
                existing = connection.execute(
                    sa.text(
                        """
                        SELECT review_item_id, payload_digest
                        FROM review.review_items
                        WHERE review_item_id = :review_item_id
                        """
                    ),
                    {"review_item_id": item_id},
                ).mappings().one_or_none()
                if existing is not None:
                    payload_row = connection.execute(
                        sa.text(
                            """
                            SELECT payload_digest, evidence_digest
                            FROM review.review_item_payloads
                            WHERE review_item_id = :review_item_id
                            """
                        ),
                        {"review_item_id": item_id},
                    ).mappings().one()
                    if (
                        payload_row["payload_digest"] != payload_digest
                        or payload_row["evidence_digest"] != evidence_digest
                    ):
                        raise ReviewConflict(
                            "REVIEW_ADMISSION_CONFLICT",
                            "review admission identity already owns different immutable content",
                        )
                    return self._get_detail(connection, item_id)

                connection.execute(
                    sa.text(
                        """
                        INSERT INTO review.review_items (
                            review_item_id,
                            campaign_key,
                            item_kind,
                            subject_id,
                            source_snapshot_contract,
                            source_snapshot_digest,
                            payload_digest,
                            state,
                            revision,
                            current_decision_id,
                            active_suppression_id,
                            created_at_utc,
                            updated_at_utc,
                            correlation_id
                        ) VALUES (
                            :review_item_id,
                            :campaign_key,
                            :item_kind,
                            :subject_id,
                            :source_snapshot_contract,
                            :source_snapshot_digest,
                            :payload_digest,
                            'pending',
                            0,
                            NULL,
                            NULL,
                            :now_utc,
                            :now_utc,
                            :correlation_id
                        )
                        """
                    ),
                    {
                        "review_item_id": item_id,
                        "campaign_key": request.campaign_key,
                        "item_kind": request.item_kind,
                        "subject_id": request.subject_id,
                        "source_snapshot_contract": request.source_snapshot_contract,
                        "source_snapshot_digest": request.source_snapshot_digest,
                        "payload_digest": payload_digest,
                        "now_utc": now_utc,
                        "correlation_id": correlation_id,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO review.review_item_payloads (
                            review_item_id,
                            payload,
                            payload_digest,
                            evidence_digest,
                            recorded_at_utc,
                            correlation_id
                        ) VALUES (
                            :review_item_id,
                            CAST(:payload AS jsonb),
                            :payload_digest,
                            :evidence_digest,
                            :now_utc,
                            :correlation_id
                        )
                        """
                    ),
                    {
                        "review_item_id": item_id,
                        "payload": json.dumps(
                            request.payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                            allow_nan=False,
                        ),
                        "payload_digest": payload_digest,
                        "evidence_digest": evidence_digest,
                        "now_utc": now_utc,
                        "correlation_id": correlation_id,
                    },
                )
                for binding in request.evidence_bindings:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO review.evidence_bindings (
                                review_item_id,
                                position,
                                artifact_id,
                                role,
                                evidence_kind,
                                locator,
                                scalar_digest,
                                recorded_at_utc,
                                correlation_id
                            ) VALUES (
                                :review_item_id,
                                :position,
                                :artifact_id,
                                :role,
                                :evidence_kind,
                                :locator,
                                :scalar_digest,
                                :now_utc,
                                :correlation_id
                            )
                            """
                        ),
                        {
                            "review_item_id": item_id,
                            "position": binding.position,
                            "artifact_id": binding.artifact_id,
                            "role": binding.role,
                            "evidence_kind": binding.evidence_kind,
                            "locator": binding.locator,
                            "scalar_digest": binding.scalar_digest,
                            "now_utc": now_utc,
                            "correlation_id": correlation_id,
                        },
                    )
                admission_digest = canonical_digest(
                    {
                        "evidenceDigest": evidence_digest,
                        "payloadDigest": payload_digest,
                    }
                )
                self._insert_audit(
                    connection,
                    review_item_id=item_id,
                    command_kind="admit",
                    command_id=item_id,
                    expected_revision=None,
                    resulting_revision=0,
                    actor_reference=actor_reference,
                    payload_digest=admission_digest,
                    occurred_at_utc=now_utc,
                    correlation_id=correlation_id,
                )
                return self._get_detail(connection, item_id)

        def list_queue(
            self,
            *,
            state: ReviewItemState | None,
            item_kind: ReviewItemKind | None,
            campaign_key: str | None,
            cursor: str | None,
            limit: int,
        ) -> ReviewQueuePage:
            cursor_created_at, cursor_id = _decode_cursor(cursor)
            with self._engine.connect() as connection:
                rows = connection.execute(
                    sa.text(
                        """
                        SELECT *
                        FROM review.review_items
                        WHERE (:state IS NULL OR state = :state)
                          AND (:item_kind IS NULL OR item_kind = :item_kind)
                          AND (:campaign_key IS NULL OR campaign_key = :campaign_key)
                          AND (
                              :cursor_created_at IS NULL
                              OR (created_at_utc, review_item_id)
                                 > (:cursor_created_at, :cursor_id)
                          )
                        ORDER BY created_at_utc, review_item_id
                        LIMIT :fetch_limit
                        """
                    ),
                    {
                        "state": state,
                        "item_kind": item_kind,
                        "campaign_key": campaign_key,
                        "cursor_created_at": cursor_created_at,
                        "cursor_id": cursor_id,
                        "fetch_limit": limit + 1,
                    },
                ).mappings().all()
            page_rows = rows[:limit]
            next_cursor = None
            if len(rows) > limit and page_rows:
                last = page_rows[-1]
                next_cursor = _encode_cursor(
                    cast(datetime, last["created_at_utc"]),
                    cast(UUID, last["review_item_id"]),
                )
            return ReviewQueuePage(
                items=tuple(_summary(row) for row in page_rows),
                nextCursor=next_cursor,
            )

        def get_detail(self, review_item_id: UUID) -> ReviewItemDetail:
            with self._engine.connect() as connection:
                return self._get_detail(connection, review_item_id)

        def decide(
            self,
            review_item_id: UUID,
            command: DecisionCommand,
            *,
            actor_reference: str,
            correlation_id: str,
            now_utc: datetime,
        ) -> ReviewMutationResult:
            digest = decision_command_digest(review_item_id, command, actor_reference)
            with self._engine.begin() as connection:
                replay = self._decision_replay(
                    connection,
                    review_item_id,
                    command.command_id,
                    digest,
                )
                if replay is not None:
                    return replay
                item = self._locked_item(connection, review_item_id)
                self._require_revision(item, command.expected_revision)
                item_kind = cast(ReviewItemKind, item["item_kind"])
                if command.action not in allowed_decision_actions(item_kind):
                    raise ReviewConflict(
                        "REVIEW_DECISION_ACTION_INVALID",
                        "decision action is invalid for the review item kind",
                    )
                selected_required = command.action in {
                    "accept_candidate",
                    "reject_candidate",
                }
                if selected_required != (command.selected_reference is not None):
                    raise ReviewConflict(
                        "REVIEW_SELECTED_REFERENCE_INVALID",
                        "decision selectedReference shape is invalid",
                    )
                resulting_revision = command.expected_revision + 1
                state = (
                    "suppressed"
                    if item["active_suppression_id"] is not None
                    else "decided"
                )
                self._update_item(
                    connection,
                    review_item_id=review_item_id,
                    expected_revision=command.expected_revision,
                    state=state,
                    current_decision_id=command.command_id,
                    active_suppression_id=cast(UUID | None, item["active_suppression_id"]),
                    now_utc=now_utc,
                    correlation_id=correlation_id,
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO review.review_decisions (
                            decision_id,
                            review_item_id,
                            expected_revision,
                            resulting_revision,
                            action,
                            reason_code,
                            selected_reference,
                            actor_reference,
                            occurred_at_utc,
                            correlation_id,
                            command_digest
                        ) VALUES (
                            :decision_id,
                            :review_item_id,
                            :expected_revision,
                            :resulting_revision,
                            :action,
                            :reason_code,
                            :selected_reference,
                            :actor_reference,
                            :occurred_at_utc,
                            :correlation_id,
                            :command_digest
                        )
                        """
                    ),
                    {
                        "decision_id": command.command_id,
                        "review_item_id": review_item_id,
                        "expected_revision": command.expected_revision,
                        "resulting_revision": resulting_revision,
                        "action": command.action,
                        "reason_code": command.reason_code,
                        "selected_reference": command.selected_reference,
                        "actor_reference": actor_reference,
                        "occurred_at_utc": now_utc,
                        "correlation_id": correlation_id,
                        "command_digest": digest,
                    },
                )
                self._insert_audit(
                    connection,
                    review_item_id=review_item_id,
                    command_kind="decision",
                    command_id=command.command_id,
                    expected_revision=command.expected_revision,
                    resulting_revision=resulting_revision,
                    actor_reference=actor_reference,
                    payload_digest=digest,
                    occurred_at_utc=now_utc,
                    correlation_id=correlation_id,
                )
                return ReviewMutationResult(
                    commandId=command.command_id,
                    resultingRevision=resulting_revision,
                    item=self._get_summary(connection, review_item_id),
                )

        def add_manual_observation(
            self,
            review_item_id: UUID,
            command: ManualObservationCommand,
            *,
            actor_reference: str,
            correlation_id: str,
            now_utc: datetime,
        ) -> ReviewMutationResult:
            digest = manual_observation_command_digest(
                review_item_id,
                command,
                actor_reference,
            )
            with self._engine.begin() as connection:
                replay = self._manual_replay(
                    connection,
                    review_item_id,
                    command.command_id,
                    digest,
                )
                if replay is not None:
                    return replay
                item = self._locked_item(connection, review_item_id)
                self._require_revision(item, command.expected_revision)
                resulting_revision = command.expected_revision + 1
                self._update_item(
                    connection,
                    review_item_id=review_item_id,
                    expected_revision=command.expected_revision,
                    state=cast(ReviewItemState, item["state"]),
                    current_decision_id=cast(UUID | None, item["current_decision_id"]),
                    active_suppression_id=cast(UUID | None, item["active_suppression_id"]),
                    now_utc=now_utc,
                    correlation_id=correlation_id,
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO review.manual_observations (
                            observation_id,
                            review_item_id,
                            expected_revision,
                            resulting_revision,
                            field_key,
                            value_kind,
                            normalized_value,
                            reason_code,
                            evidence_note,
                            actor_reference,
                            occurred_at_utc,
                            correlation_id,
                            command_digest
                        ) VALUES (
                            :observation_id,
                            :review_item_id,
                            :expected_revision,
                            :resulting_revision,
                            :field_key,
                            :value_kind,
                            :normalized_value,
                            :reason_code,
                            :evidence_note,
                            :actor_reference,
                            :occurred_at_utc,
                            :correlation_id,
                            :command_digest
                        )
                        """
                    ),
                    {
                        "observation_id": command.command_id,
                        "review_item_id": review_item_id,
                        "expected_revision": command.expected_revision,
                        "resulting_revision": resulting_revision,
                        "field_key": command.field_key,
                        "value_kind": command.value_kind,
                        "normalized_value": command.normalized_value,
                        "reason_code": command.reason_code,
                        "evidence_note": command.evidence_note,
                        "actor_reference": actor_reference,
                        "occurred_at_utc": now_utc,
                        "correlation_id": correlation_id,
                        "command_digest": digest,
                    },
                )
                self._insert_audit(
                    connection,
                    review_item_id=review_item_id,
                    command_kind="manual_observation",
                    command_id=command.command_id,
                    expected_revision=command.expected_revision,
                    resulting_revision=resulting_revision,
                    actor_reference=actor_reference,
                    payload_digest=digest,
                    occurred_at_utc=now_utc,
                    correlation_id=correlation_id,
                )
                return ReviewMutationResult(
                    commandId=command.command_id,
                    resultingRevision=resulting_revision,
                    item=self._get_summary(connection, review_item_id),
                )

        def change_suppression(
            self,
            review_item_id: UUID,
            command: SuppressionCommand,
            *,
            actor_reference: str,
            correlation_id: str,
            now_utc: datetime,
        ) -> ReviewMutationResult:
            digest = suppression_command_digest(
                review_item_id,
                command,
                actor_reference,
            )
            with self._engine.begin() as connection:
                replay = self._suppression_replay(
                    connection,
                    review_item_id,
                    command.command_id,
                    digest,
                )
                if replay is not None:
                    return replay
                item = self._locked_item(connection, review_item_id)
                self._require_revision(item, command.expected_revision)
                current_suppression = cast(UUID | None, item["active_suppression_id"])
                if command.action == "activate":
                    if current_suppression is not None:
                        raise ReviewConflict(
                            "REVIEW_SUPPRESSION_ALREADY_ACTIVE",
                            "review item already has an active suppression",
                        )
                    suppression_id = command.command_id
                    new_active_suppression = command.command_id
                    new_state: ReviewItemState = "suppressed"
                    audit_kind = "suppression_activate"
                else:
                    if current_suppression != command.target_suppression_id:
                        raise ReviewConflict(
                            "REVIEW_SUPPRESSION_TARGET_MISMATCH",
                            "suppression target is not the active suppression",
                        )
                    suppression_id = cast(UUID, command.target_suppression_id)
                    new_active_suppression = None
                    new_state = (
                        "decided"
                        if item["current_decision_id"] is not None
                        else "pending"
                    )
                    audit_kind = "suppression_resolve"
                resulting_revision = command.expected_revision + 1
                self._update_item(
                    connection,
                    review_item_id=review_item_id,
                    expected_revision=command.expected_revision,
                    state=new_state,
                    current_decision_id=cast(UUID | None, item["current_decision_id"]),
                    active_suppression_id=new_active_suppression,
                    now_utc=now_utc,
                    correlation_id=correlation_id,
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO review.suppression_events (
                            event_id,
                            suppression_id,
                            review_item_id,
                            expected_revision,
                            resulting_revision,
                            action,
                            reason_code,
                            actor_reference,
                            occurred_at_utc,
                            correlation_id,
                            command_digest
                        ) VALUES (
                            :event_id,
                            :suppression_id,
                            :review_item_id,
                            :expected_revision,
                            :resulting_revision,
                            :action,
                            :reason_code,
                            :actor_reference,
                            :occurred_at_utc,
                            :correlation_id,
                            :command_digest
                        )
                        """
                    ),
                    {
                        "event_id": command.command_id,
                        "suppression_id": suppression_id,
                        "review_item_id": review_item_id,
                        "expected_revision": command.expected_revision,
                        "resulting_revision": resulting_revision,
                        "action": command.action,
                        "reason_code": command.reason_code,
                        "actor_reference": actor_reference,
                        "occurred_at_utc": now_utc,
                        "correlation_id": correlation_id,
                        "command_digest": digest,
                    },
                )
                self._insert_audit(
                    connection,
                    review_item_id=review_item_id,
                    command_kind=audit_kind,
                    command_id=command.command_id,
                    expected_revision=command.expected_revision,
                    resulting_revision=resulting_revision,
                    actor_reference=actor_reference,
                    payload_digest=digest,
                    occurred_at_utc=now_utc,
                    correlation_id=correlation_id,
                )
                return ReviewMutationResult(
                    commandId=command.command_id,
                    resultingRevision=resulting_revision,
                    item=self._get_summary(connection, review_item_id),
                )

        def get_evidence_object(self, artifact_id: UUID) -> EvidenceObject:
            with self._engine.connect() as connection:
                row = connection.execute(
                    sa.text(
                        """
                        SELECT
                            records.artifact_id,
                            records.content_type,
                            objects.content_digest,
                            objects.size_bytes,
                            objects.storage_reference
                        FROM sources.artifact_records AS records
                        JOIN sources.artifact_objects AS objects
                          ON objects.object_id = records.object_id
                        WHERE records.artifact_id = :artifact_id
                        """
                    ),
                    {"artifact_id": artifact_id},
                ).mappings().one_or_none()
            if row is None:
                raise EvidenceNotFound(artifact_id)
            return EvidenceObject(
                artifactId=row["artifact_id"],
                contentDigest=row["content_digest"],
                contentType=row["content_type"],
                sizeBytes=row["size_bytes"],
                storageReference=row["storage_reference"],
            )

        def _get_detail(
            self,
            connection: sa.Connection,
            review_item_id: UUID,
        ) -> ReviewItemDetail:
            item = self._get_summary(connection, review_item_id)
            payload_row = connection.execute(
                sa.text(
                    """
                    SELECT payload
                    FROM review.review_item_payloads
                    WHERE review_item_id = :review_item_id
                    """
                ),
                {"review_item_id": review_item_id},
            ).mappings().one()
            evidence_rows = connection.execute(
                sa.text(
                    """
                    SELECT review_item_id, position, artifact_id, role,
                           evidence_kind, locator, scalar_digest
                    FROM review.evidence_bindings
                    WHERE review_item_id = :review_item_id
                    ORDER BY position
                    """
                ),
                {"review_item_id": review_item_id},
            ).mappings().all()
            decision_rows = connection.execute(
                sa.text(
                    """
                    SELECT * FROM review.review_decisions
                    WHERE review_item_id = :review_item_id
                    ORDER BY resulting_revision, decision_id
                    """
                ),
                {"review_item_id": review_item_id},
            ).mappings().all()
            manual_rows = connection.execute(
                sa.text(
                    """
                    SELECT * FROM review.manual_observations
                    WHERE review_item_id = :review_item_id
                    ORDER BY resulting_revision, observation_id
                    """
                ),
                {"review_item_id": review_item_id},
            ).mappings().all()
            suppression_rows = connection.execute(
                sa.text(
                    """
                    SELECT * FROM review.suppression_events
                    WHERE review_item_id = :review_item_id
                    ORDER BY resulting_revision, event_id
                    """
                ),
                {"review_item_id": review_item_id},
            ).mappings().all()
            audit_rows = connection.execute(
                sa.text(
                    """
                    SELECT * FROM review.audit_events
                    WHERE review_item_id = :review_item_id
                    ORDER BY resulting_revision, audit_event_id
                    """
                ),
                {"review_item_id": review_item_id},
            ).mappings().all()
            payload = cast(dict[str, JsonValue], payload_row["payload"])
            return ReviewItemDetail(
                item=item,
                payload=payload,
                evidenceBindings=tuple(_evidence(row) for row in evidence_rows),
                decisions=tuple(_decision(row) for row in decision_rows),
                manualObservations=tuple(_manual(row) for row in manual_rows),
                suppressions=tuple(_suppression(row) for row in suppression_rows),
                auditEvents=tuple(_audit(row) for row in audit_rows),
            )

        def _get_summary(
            self,
            connection: sa.Connection,
            review_item_id: UUID,
        ) -> ReviewItemSummary:
            row = connection.execute(
                sa.text(
                    """
                    SELECT * FROM review.review_items
                    WHERE review_item_id = :review_item_id
                    """
                ),
                {"review_item_id": review_item_id},
            ).mappings().one_or_none()
            if row is None:
                raise ReviewItemNotFound(review_item_id)
            return _summary(row)

        def _locked_item(
            self,
            connection: sa.Connection,
            review_item_id: UUID,
        ) -> RowMapping:
            row = connection.execute(
                sa.text(
                    """
                    SELECT * FROM review.review_items
                    WHERE review_item_id = :review_item_id
                    FOR UPDATE
                    """
                ),
                {"review_item_id": review_item_id},
            ).mappings().one_or_none()
            if row is None:
                raise ReviewItemNotFound(review_item_id)
            return row

        def _require_revision(self, item: RowMapping, expected_revision: int) -> None:
            current = cast(int, item["revision"])
            if current != expected_revision:
                raise StaleReviewRevision(
                    cast(UUID, item["review_item_id"]),
                    current,
                )

        def _update_item(
            self,
            connection: sa.Connection,
            *,
            review_item_id: UUID,
            expected_revision: int,
            state: ReviewItemState,
            current_decision_id: UUID | None,
            active_suppression_id: UUID | None,
            now_utc: datetime,
            correlation_id: str,
        ) -> None:
            result = connection.execute(
                sa.text(
                    """
                    UPDATE review.review_items
                    SET state = :state,
                        revision = revision + 1,
                        current_decision_id = :current_decision_id,
                        active_suppression_id = :active_suppression_id,
                        updated_at_utc = :now_utc,
                        correlation_id = :correlation_id
                    WHERE review_item_id = :review_item_id
                      AND revision = :expected_revision
                    """
                ),
                {
                    "state": state,
                    "current_decision_id": current_decision_id,
                    "active_suppression_id": active_suppression_id,
                    "now_utc": now_utc,
                    "correlation_id": correlation_id,
                    "review_item_id": review_item_id,
                    "expected_revision": expected_revision,
                },
            )
            if result.rowcount != 1:
                current = self._get_summary(connection, review_item_id)
                raise StaleReviewRevision(review_item_id, current.revision)

        def _insert_audit(
            self,
            connection: sa.Connection,
            *,
            review_item_id: UUID,
            command_kind: str,
            command_id: UUID,
            expected_revision: int | None,
            resulting_revision: int,
            actor_reference: str,
            payload_digest: str,
            occurred_at_utc: datetime,
            correlation_id: str,
        ) -> None:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO review.audit_events (
                        audit_event_id,
                        review_item_id,
                        command_kind,
                        command_id,
                        expected_revision,
                        resulting_revision,
                        actor_reference,
                        payload_digest,
                        occurred_at_utc,
                        correlation_id
                    ) VALUES (
                        :audit_event_id,
                        :review_item_id,
                        :command_kind,
                        :command_id,
                        :expected_revision,
                        :resulting_revision,
                        :actor_reference,
                        :payload_digest,
                        :occurred_at_utc,
                        :correlation_id
                    )
                    """
                ),
                {
                    "audit_event_id": deterministic_audit_event_id(
                        review_item_id,
                        resulting_revision,
                    ),
                    "review_item_id": review_item_id,
                    "command_kind": command_kind,
                    "command_id": command_id,
                    "expected_revision": expected_revision,
                    "resulting_revision": resulting_revision,
                    "actor_reference": actor_reference,
                    "payload_digest": payload_digest,
                    "occurred_at_utc": occurred_at_utc,
                    "correlation_id": correlation_id,
                },
            )

        def _decision_replay(
            self,
            connection: sa.Connection,
            review_item_id: UUID,
            command_id: UUID,
            digest: str,
        ) -> ReviewMutationResult | None:
            row = connection.execute(
                sa.text(
                    """
                    SELECT review_item_id, resulting_revision, command_digest
                    FROM review.review_decisions
                    WHERE decision_id = :command_id
                    """
                ),
                {"command_id": command_id},
            ).mappings().one_or_none()
            return self._replay_result(
                connection,
                review_item_id,
                command_id,
                digest,
                row,
            )

        def _manual_replay(
            self,
            connection: sa.Connection,
            review_item_id: UUID,
            command_id: UUID,
            digest: str,
        ) -> ReviewMutationResult | None:
            row = connection.execute(
                sa.text(
                    """
                    SELECT review_item_id, resulting_revision, command_digest
                    FROM review.manual_observations
                    WHERE observation_id = :command_id
                    """
                ),
                {"command_id": command_id},
            ).mappings().one_or_none()
            return self._replay_result(
                connection,
                review_item_id,
                command_id,
                digest,
                row,
            )

        def _suppression_replay(
            self,
            connection: sa.Connection,
            review_item_id: UUID,
            command_id: UUID,
            digest: str,
        ) -> ReviewMutationResult | None:
            row = connection.execute(
                sa.text(
                    """
                    SELECT review_item_id, resulting_revision, command_digest
                    FROM review.suppression_events
                    WHERE event_id = :command_id
                    """
                ),
                {"command_id": command_id},
            ).mappings().one_or_none()
            return self._replay_result(
                connection,
                review_item_id,
                command_id,
                digest,
                row,
            )

        def _replay_result(
            self,
            connection: sa.Connection,
            review_item_id: UUID,
            command_id: UUID,
            digest: str,
            row: RowMapping | None,
        ) -> ReviewMutationResult | None:
            if row is None:
                return None
            if row["review_item_id"] != review_item_id or row["command_digest"] != digest:
                raise ReviewConflict(
                    "REVIEW_COMMAND_ID_CONFLICT",
                    "command ID already owns a different immutable command",
                )
            return ReviewMutationResult(
                commandId=command_id,
                resultingRevision=row["resulting_revision"],
                item=self._get_summary(connection, review_item_id),
            )


    def _summary(row: RowMapping) -> ReviewItemSummary:
        return ReviewItemSummary(
            reviewItemId=row["review_item_id"],
            campaignKey=row["campaign_key"],
            itemKind=row["item_kind"],
            subjectId=row["subject_id"],
            sourceSnapshotContract=row["source_snapshot_contract"],
            sourceSnapshotDigest=row["source_snapshot_digest"],
            payloadDigest=row["payload_digest"],
            state=row["state"],
            revision=row["revision"],
            currentDecisionId=row["current_decision_id"],
            activeSuppressionId=row["active_suppression_id"],
            createdAtUtc=row["created_at_utc"],
            updatedAtUtc=row["updated_at_utc"],
        )


    def _evidence(row: RowMapping) -> EvidenceBindingRecord:
        return EvidenceBindingRecord(
            reviewItemId=row["review_item_id"],
            position=row["position"],
            artifactId=row["artifact_id"],
            role=row["role"],
            evidenceKind=row["evidence_kind"],
            locator=row["locator"],
            scalarDigest=row["scalar_digest"],
        )


    def _decision(row: RowMapping) -> DecisionRecord:
        return DecisionRecord(
            decisionId=row["decision_id"],
            reviewItemId=row["review_item_id"],
            expectedRevision=row["expected_revision"],
            resultingRevision=row["resulting_revision"],
            action=row["action"],
            reasonCode=row["reason_code"],
            selectedReference=row["selected_reference"],
            actorReference=row["actor_reference"],
            occurredAtUtc=row["occurred_at_utc"],
            correlationId=row["correlation_id"],
            commandDigest=row["command_digest"],
        )


    def _manual(row: RowMapping) -> ManualObservationRecord:
        return ManualObservationRecord(
            observationId=row["observation_id"],
            reviewItemId=row["review_item_id"],
            expectedRevision=row["expected_revision"],
            resultingRevision=row["resulting_revision"],
            fieldKey=row["field_key"],
            valueKind=row["value_kind"],
            normalizedValue=row["normalized_value"],
            reasonCode=row["reason_code"],
            evidenceNote=row["evidence_note"],
            actorReference=row["actor_reference"],
            occurredAtUtc=row["occurred_at_utc"],
            correlationId=row["correlation_id"],
            commandDigest=row["command_digest"],
        )


    def _suppression(row: RowMapping) -> SuppressionRecord:
        return SuppressionRecord(
            eventId=row["event_id"],
            suppressionId=row["suppression_id"],
            reviewItemId=row["review_item_id"],
            expectedRevision=row["expected_revision"],
            resultingRevision=row["resulting_revision"],
            action=row["action"],
            reasonCode=row["reason_code"],
            actorReference=row["actor_reference"],
            occurredAtUtc=row["occurred_at_utc"],
            correlationId=row["correlation_id"],
            commandDigest=row["command_digest"],
        )


    def _audit(row: RowMapping) -> AuditEvent:
        return AuditEvent(
            auditEventId=row["audit_event_id"],
            reviewItemId=row["review_item_id"],
            commandKind=row["command_kind"],
            commandId=row["command_id"],
            expectedRevision=row["expected_revision"],
            resultingRevision=row["resulting_revision"],
            actorReference=row["actor_reference"],
            payloadDigest=row["payload_digest"],
            occurredAtUtc=row["occurred_at_utc"],
            correlationId=row["correlation_id"],
        )


    def _encode_cursor(created_at_utc: datetime, review_item_id: UUID) -> str:
        payload = f"{created_at_utc.isoformat()}|{review_item_id}".encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


    def _decode_cursor(cursor: str | None) -> tuple[datetime | None, UUID | None]:
        if cursor is None:
            return None, None
        try:
            padding = "=" * (-len(cursor) % 4)
            decoded = base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
            timestamp, item_id = decoded.rsplit("|", 1)
            return datetime.fromisoformat(timestamp), UUID(item_id)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("review queue cursor is invalid") from exc
    ''',
)

write(
    "packages/review_infrastructure/src/review_infrastructure/evidence.py",
    r'''
    from __future__ import annotations

    from typing import cast

    import boto3
    from mypy_boto3_s3 import S3Client


    class S3EvidenceReader:
        def __init__(
            self,
            *,
            bucket: str,
            endpoint_url: str,
            access_key_id: str,
            secret_access_key: str,
            region: str,
            client: S3Client | None = None,
        ) -> None:
            if not bucket:
                raise ValueError("evidence bucket is required")
            self._bucket = bucket
            self._client = client or cast(
                S3Client,
                boto3.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                    region_name=region,
                ),
            )

        def read_prefix(
            self,
            storage_reference: str,
            *,
            maximum_bytes: int,
        ) -> bytes:
            if not 1 <= maximum_bytes <= 256 * 1024:
                raise ValueError("evidence prefix limit is invalid")
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=storage_reference,
                Range=f"bytes=0-{maximum_bytes}",
            )
            body = response["Body"]
            try:
                return body.read(maximum_bytes + 1)
            finally:
                body.close()
    ''',
)

write(
    "packages/review_infrastructure/src/review_infrastructure/__init__.py",
    r'''
    from review_infrastructure.evidence import S3EvidenceReader
    from review_infrastructure.postgres import PostgresReviewRepository

    __all__ = ["PostgresReviewRepository", "S3EvidenceReader"]
    ''',
)

write(
    "packages/review_infrastructure/tests/test_evidence.py",
    r'''
    from __future__ import annotations

    from io import BytesIO
    from typing import cast

    from mypy_boto3_s3 import S3Client
    from review_infrastructure import S3EvidenceReader


    class _Body:
        def __init__(self, value: bytes) -> None:
            self._stream = BytesIO(value)
            self.closed = False

        def read(self, amount: int) -> bytes:
            return self._stream.read(amount)

        def close(self) -> None:
            self.closed = True


    class _Client:
        def __init__(self, value: bytes) -> None:
            self.body = _Body(value)
            self.calls: list[dict[str, str]] = []

        def get_object(self, **kwargs: str) -> dict[str, object]:
            self.calls.append(kwargs)
            return {"Body": self.body}


    def test_s3_evidence_reader_uses_bounded_range_and_closes_body() -> None:
        client = _Client(b"0123456789")
        reader = S3EvidenceReader(
            bucket="evidence",
            endpoint_url="http://object-store",
            access_key_id="key",
            secret_access_key="secret",
            region="us-east-1",
            client=cast(S3Client, client),
        )

        assert reader.read_prefix("objects/evidence", maximum_bytes=4) == b"01234"
        assert client.calls == [
            {
                "Bucket": "evidence",
                "Key": "objects/evidence",
                "Range": "bytes=0-4",
            }
        ]
        assert client.body.closed is True
    ''',
)

write(
    "database/tests/test_review_console_schema.py",
    r'''
    from __future__ import annotations

    import os
    from datetime import UTC, datetime, timedelta
    from hashlib import sha256
    from uuid import NAMESPACE_URL, UUID, uuid5

    import pytest
    import sqlalchemy as sa
    from review_application import ReviewConflict, StaleReviewRevision
    from review_contracts import (
        DecisionCommand,
        ManualObservationCommand,
        ReviewAdmissionRequest,
        SuppressionCommand,
    )
    from review_infrastructure import PostgresReviewRepository
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.pool import NullPool

    pytestmark = pytest.mark.integration

    _NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


    def _database_url() -> str:
        value = os.environ.get("COLLECTOR_DATABASE_URL", "").strip()
        if not value:
            pytest.fail("COLLECTOR_DATABASE_URL is required for review integration tests.")
        return value


    def _id(label: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"review-console:{label}")


    def _digest(label: str) -> str:
        return f"sha256:{sha256(label.encode('utf-8')).hexdigest()}"


    def _admission(label: str, *, payload_value: str = "value") -> ReviewAdmissionRequest:
        return ReviewAdmissionRequest.model_validate(
            {
                "contract": "review-admission",
                "contractRevision": "review-admission-v1",
                "sourceSnapshotContract": "entity-resolution-snapshot-v1",
                "sourceSnapshotDigest": _digest(f"{label}:snapshot"),
                "campaignKey": "berlin_recording_services",
                "itemKind": "resolution_pair",
                "subjectId": label,
                "payload": {"label": payload_value},
                "evidenceBindings": [],
            }
        )


    def _repository() -> PostgresReviewRepository:
        return PostgresReviewRepository(
            sa.create_engine(_database_url(), poolclass=NullPool)
        )


    def test_fresh_migration_creates_exact_review_schema_and_triggers() -> None:
        engine = sa.create_engine(_database_url(), poolclass=NullPool)
        inspector = sa.inspect(engine)

        assert set(inspector.get_table_names(schema="review")) == {
            "audit_events",
            "evidence_bindings",
            "manual_observations",
            "review_decisions",
            "review_item_payloads",
            "review_items",
            "suppression_events",
        }
        with engine.connect() as connection:
            triggers = {
                row[0]
                for row in connection.execute(
                    sa.text(
                        """
                        SELECT tgname
                        FROM pg_trigger
                        WHERE tgrelid IN (
                            SELECT c.oid
                            FROM pg_class AS c
                            JOIN pg_namespace AS n ON n.oid = c.relnamespace
                            WHERE n.nspname = 'review'
                        )
                          AND NOT tgisinternal
                        """
                    )
                )
            }
        assert triggers >= {
            "trg_audit_events_append_only",
            "trg_evidence_bindings_append_only",
            "trg_manual_observations_append_only",
            "trg_review_decisions_append_only",
            "trg_review_item_payloads_append_only",
            "trg_review_items_reject_delete",
            "trg_review_items_validate_update",
            "trg_suppression_events_append_only",
        }


    def test_admission_is_idempotent_and_conflicting_content_fails() -> None:
        repository = _repository()
        request = _admission("admission-idempotency")

        first = repository.admit(
            request,
            actor_reference="system-admission",
            correlation_id="correlation-admission-1",
            now_utc=_NOW,
        )
        second = repository.admit(
            request,
            actor_reference="system-admission",
            correlation_id="correlation-admission-2",
            now_utc=_NOW + timedelta(seconds=1),
        )

        assert first.item.review_item_id == second.item.review_item_id
        assert second.item.revision == 0
        assert len(second.audit_events) == 1

        with pytest.raises(ReviewConflict) as conflict:
            repository.admit(
                _admission("admission-idempotency", payload_value="different"),
                actor_reference="system-admission",
                correlation_id="correlation-admission-3",
                now_utc=_NOW + timedelta(seconds=2),
            )
        assert conflict.value.code == "REVIEW_ADMISSION_CONFLICT"


    def test_decision_is_atomic_idempotent_and_stale_command_writes_nothing() -> None:
        repository = _repository()
        admitted = repository.admit(
            _admission("decision-stale"),
            actor_reference="system-admission",
            correlation_id="correlation-decision-admit",
            now_utc=_NOW,
        )
        item_id = admitted.item.review_item_id
        decision = DecisionCommand(
            commandId=_id("decision-stale:decision"),
            expectedRevision=0,
            action="match",
            reasonCode="CONFIRMED_MATCH",
        )

        result = repository.decide(
            item_id,
            decision,
            actor_reference="reviewer-1",
            correlation_id="correlation-decision-1",
            now_utc=_NOW + timedelta(seconds=1),
        )
        replay = repository.decide(
            item_id,
            decision,
            actor_reference="reviewer-1",
            correlation_id="correlation-decision-replay",
            now_utc=_NOW + timedelta(seconds=2),
        )

        assert result.resulting_revision == 1
        assert replay.resulting_revision == 1
        assert replay.item.state == "decided"

        stale = ManualObservationCommand(
            commandId=_id("decision-stale:manual"),
            expectedRevision=0,
            fieldKey="name",
            valueKind="text",
            normalizedValue="Signal Room",
            reasonCode="MANUAL_EVIDENCE",
        )
        with pytest.raises(StaleReviewRevision) as error:
            repository.add_manual_observation(
                item_id,
                stale,
                actor_reference="reviewer-1",
                correlation_id="correlation-stale",
                now_utc=_NOW + timedelta(seconds=3),
            )
        assert error.value.current_revision == 1

        detail = repository.get_detail(item_id)
        assert len(detail.decisions) == 1
        assert detail.manual_observations == ()
        assert [event.command_kind for event in detail.audit_events] == [
            "admit",
            "decision",
        ]


    def test_suppression_activation_and_resolution_restore_projection_state() -> None:
        repository = _repository()
        item_id = repository.admit(
            _admission("suppression-lifecycle"),
            actor_reference="system-admission",
            correlation_id="correlation-suppression-admit",
            now_utc=_NOW,
        ).item.review_item_id
        activate = SuppressionCommand(
            commandId=_id("suppression-lifecycle:activate"),
            expectedRevision=0,
            action="activate",
            reasonCode="LEGAL_REVIEW",
        )
        activated = repository.change_suppression(
            item_id,
            activate,
            actor_reference="reviewer-2",
            correlation_id="correlation-suppression-1",
            now_utc=_NOW + timedelta(seconds=1),
        )
        assert activated.item.state == "suppressed"
        assert activated.item.active_suppression_id == activate.command_id

        resolve = SuppressionCommand(
            commandId=_id("suppression-lifecycle:resolve"),
            expectedRevision=1,
            action="resolve",
            reasonCode="LEGAL_REVIEW_RESOLVED",
            targetSuppressionId=activate.command_id,
        )
        resolved = repository.change_suppression(
            item_id,
            resolve,
            actor_reference="reviewer-2",
            correlation_id="correlation-suppression-2",
            now_utc=_NOW + timedelta(seconds=2),
        )
        assert resolved.item.state == "pending"
        assert resolved.item.active_suppression_id is None
        assert len(repository.get_detail(item_id).suppressions) == 2


    def test_append_only_history_and_projection_revision_guard_fail_closed() -> None:
        repository = _repository()
        item_id = repository.admit(
            _admission("append-only"),
            actor_reference="system-admission",
            correlation_id="correlation-append-admit",
            now_utc=_NOW,
        ).item.review_item_id
        decision_id = _id("append-only:decision")
        repository.decide(
            item_id,
            DecisionCommand(
                commandId=decision_id,
                expectedRevision=0,
                action="separate",
                reasonCode="CONFIRMED_DISTINCT",
            ),
            actor_reference="reviewer-3",
            correlation_id="correlation-append-decision",
            now_utc=_NOW + timedelta(seconds=1),
        )
        engine = sa.create_engine(_database_url(), poolclass=NullPool)

        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    UPDATE review.review_decisions
                    SET reason_code = 'MUTATED'
                    WHERE decision_id = :decision_id
                    """
                ),
                {"decision_id": decision_id},
            )

        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    UPDATE review.review_items
                    SET revision = revision + 2,
                        updated_at_utc = :updated_at_utc
                    WHERE review_item_id = :review_item_id
                    """
                ),
                {
                    "review_item_id": item_id,
                    "updated_at_utc": _NOW + timedelta(seconds=2),
                },
            )
    ''',
)
