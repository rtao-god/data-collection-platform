"""Create durable run, source-capacity, worker, and work-unit storage.

Revision ID: 20260811_0002
Revises: 20260804_0001
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260811_0002"
down_revision: str | None = "20260804_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DDL = (
    """
    CREATE TABLE runs.collection_runs (
        run_id UUID NOT NULL,
        campaign_key TEXT NOT NULL,
        config_bundle_digest TEXT NOT NULL,
        state TEXT NOT NULL,
        revision BIGINT NOT NULL,
        created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        correlation_id TEXT NOT NULL,
        PRIMARY KEY (run_id),
        CONSTRAINT ck_collection_runs_campaign_key_format
            CHECK (campaign_key ~ '^[a-z][a-z0-9_]{0,79}$'),
        CONSTRAINT ck_collection_runs_config_digest_format
            CHECK (config_bundle_digest ~ '^sha256:[0-9a-f]{64}$'),
        CONSTRAINT ck_collection_runs_state
            CHECK (state IN ('created', 'running', 'paused', 'cancelled', 'completed', 'blocked')),
        CONSTRAINT ck_collection_runs_revision CHECK (revision >= 0),
        CONSTRAINT ck_collection_runs_time_order CHECK (updated_at_utc >= created_at_utc),
        FOREIGN KEY(config_bundle_digest)
            REFERENCES config.config_bundles (bundle_digest)
    )
    """,
    """
    CREATE TABLE runs.stage_runs (
        stage_run_id UUID NOT NULL,
        run_id UUID NOT NULL,
        stage TEXT NOT NULL,
        state TEXT NOT NULL,
        revision BIGINT NOT NULL,
        created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        correlation_id TEXT NOT NULL,
        PRIMARY KEY (stage_run_id),
        CONSTRAINT ck_stage_runs_stage
            CHECK (stage IN (
                'discovery', 'acquisition', 'extraction', 'normalization',
                'geography', 'entity_resolution', 'quality', 'export'
            )),
        CONSTRAINT ck_stage_runs_state
            CHECK (state IN ('pending', 'running', 'succeeded', 'failed', 'blocked', 'cancelled')),
        CONSTRAINT ck_stage_runs_revision CHECK (revision >= 0),
        CONSTRAINT ck_stage_runs_time_order CHECK (updated_at_utc >= created_at_utc),
        CONSTRAINT uq_stage_runs_run_stage UNIQUE (run_id, stage),
        CONSTRAINT uq_stage_runs_owner_identity UNIQUE (stage_run_id, run_id, stage),
        FOREIGN KEY(run_id) REFERENCES runs.collection_runs (run_id)
    )
    """,
    """
    CREATE TABLE sources.source_capacity_states (
        source_key TEXT NOT NULL,
        policy_digest TEXT NOT NULL,
        operational_state TEXT NOT NULL,
        max_active_requests INTEGER NOT NULL,
        active_requests INTEGER NOT NULL,
        minimum_interval_milliseconds INTEGER NOT NULL,
        next_allowed_request_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        retry_after_utc TIMESTAMP WITH TIME ZONE,
        revision BIGINT NOT NULL,
        updated_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        correlation_id TEXT NOT NULL,
        PRIMARY KEY (source_key),
        CONSTRAINT ck_source_capacity_states_key_format
            CHECK (source_key ~ '^[a-z][a-z0-9_]{0,79}$'),
        CONSTRAINT ck_source_capacity_states_policy_digest_format
            CHECK (policy_digest ~ '^sha256:[0-9a-f]{64}$'),
        CONSTRAINT ck_source_capacity_states_operational_state
            CHECK (operational_state IN ('active', 'suspended', 'circuit_open')),
        CONSTRAINT ck_source_capacity_states_max_active
            CHECK (max_active_requests BETWEEN 1 AND 10000),
        CONSTRAINT ck_source_capacity_states_active
            CHECK (active_requests BETWEEN 0 AND max_active_requests),
        CONSTRAINT ck_source_capacity_states_minimum_interval
            CHECK (minimum_interval_milliseconds BETWEEN 0 AND 86400000),
        CONSTRAINT ck_source_capacity_states_revision CHECK (revision >= 0)
    )
    """,
    """
    CREATE TABLE work.worker_registrations (
        worker_id TEXT NOT NULL,
        registration_digest TEXT NOT NULL,
        build_identity TEXT NOT NULL,
        max_concurrency INTEGER NOT NULL,
        resource_profile TEXT NOT NULL,
        registered_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        correlation_id TEXT NOT NULL,
        PRIMARY KEY (worker_id),
        CONSTRAINT ck_worker_registrations_id_format
            CHECK (worker_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'),
        CONSTRAINT ck_worker_registrations_digest_format
            CHECK (registration_digest ~ '^sha256:[0-9a-f]{64}$'),
        CONSTRAINT ck_worker_registrations_max_concurrency
            CHECK (max_concurrency BETWEEN 1 AND 10000),
        CONSTRAINT uq_worker_registrations_digest UNIQUE (registration_digest)
    )
    """,
    """
    CREATE TABLE work.worker_capabilities (
        worker_id TEXT NOT NULL,
        capability TEXT NOT NULL,
        PRIMARY KEY (worker_id, capability),
        CONSTRAINT ck_worker_capabilities_capability
            CHECK (capability IN (
                'manual_import', 'osm_query', 'http_fetch', 'browser_fetch',
                'extraction', 'normalization', 'geography', 'entity_resolution',
                'quality', 'export'
            )),
        FOREIGN KEY(worker_id) REFERENCES work.worker_registrations (worker_id)
    )
    """,
    """
    CREATE TABLE work.worker_heartbeats (
        worker_id TEXT NOT NULL,
        last_seen_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        active_lease_count INTEGER NOT NULL,
        correlation_id TEXT NOT NULL,
        PRIMARY KEY (worker_id),
        CONSTRAINT ck_worker_heartbeats_active_lease_count
            CHECK (active_lease_count BETWEEN 0 AND 10000),
        FOREIGN KEY(worker_id) REFERENCES work.worker_registrations (worker_id)
    )
    """,
    """
    CREATE TABLE work.work_units (
        work_id UUID NOT NULL,
        run_id UUID NOT NULL,
        stage_run_id UUID NOT NULL,
        stage TEXT NOT NULL,
        capability TEXT NOT NULL,
        source_key TEXT,
        semantic_key TEXT NOT NULL,
        input_digest TEXT NOT NULL,
        expected_output_contract TEXT NOT NULL,
        priority INTEGER NOT NULL,
        state TEXT NOT NULL,
        attempt_count INTEGER NOT NULL,
        max_attempts INTEGER NOT NULL,
        retry_initial_delay_seconds INTEGER NOT NULL,
        retry_multiplier INTEGER NOT NULL,
        retry_max_delay_seconds INTEGER NOT NULL,
        available_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        active_lease_id UUID,
        active_lease_token UUID,
        active_worker_id TEXT,
        lease_issued_at_utc TIMESTAMP WITH TIME ZONE,
        lease_expires_at_utc TIMESTAMP WITH TIME ZONE,
        heartbeat_deadline_utc TIMESTAMP WITH TIME ZONE,
        source_policy_digest TEXT,
        source_permit_not_before_utc TIMESTAMP WITH TIME ZONE,
        output_contract TEXT,
        output_digest TEXT,
        completed_at_utc TIMESTAMP WITH TIME ZONE,
        revision BIGINT NOT NULL,
        created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        correlation_id TEXT NOT NULL,
        PRIMARY KEY (work_id),
        CONSTRAINT fk_work_units_stage_owner
            FOREIGN KEY(stage_run_id, run_id, stage)
            REFERENCES runs.stage_runs (stage_run_id, run_id, stage),
        CONSTRAINT uq_work_units_run_semantic_key UNIQUE (run_id, semantic_key),
        CONSTRAINT ck_work_units_state
            CHECK (state IN (
                'pending', 'leased', 'retry_wait', 'succeeded', 'dead_letter',
                'blocked_by_policy', 'cancelled', 'superseded'
            )),
        CONSTRAINT ck_work_units_stage
            CHECK (stage IN (
                'discovery', 'acquisition', 'extraction', 'normalization',
                'geography', 'entity_resolution', 'quality', 'export'
            )),
        CONSTRAINT ck_work_units_capability
            CHECK (capability IN (
                'manual_import', 'osm_query', 'http_fetch', 'browser_fetch',
                'extraction', 'normalization', 'geography', 'entity_resolution',
                'quality', 'export'
            )),
        CONSTRAINT ck_work_units_stage_capability CHECK (
            (stage = 'discovery' AND capability IN ('manual_import', 'osm_query'))
            OR (stage = 'acquisition' AND capability IN ('http_fetch', 'browser_fetch'))
            OR (stage = 'extraction' AND capability = 'extraction')
            OR (stage = 'normalization' AND capability = 'normalization')
            OR (stage = 'geography' AND capability = 'geography')
            OR (stage = 'entity_resolution' AND capability = 'entity_resolution')
            OR (stage = 'quality' AND capability = 'quality')
            OR (stage = 'export' AND capability = 'export')
        ),
        CONSTRAINT ck_work_units_semantic_key_format
            CHECK (semantic_key ~ '^sha256:[0-9a-f]{64}$'),
        CONSTRAINT ck_work_units_input_digest_format
            CHECK (input_digest ~ '^sha256:[0-9a-f]{64}$'),
        CONSTRAINT ck_work_units_source_policy_digest_format
            CHECK (
                source_policy_digest IS NULL
                OR source_policy_digest ~ '^sha256:[0-9a-f]{64}$'
            ),
        CONSTRAINT ck_work_units_output_digest_format
            CHECK (output_digest IS NULL OR output_digest ~ '^sha256:[0-9a-f]{64}$'),
        CONSTRAINT ck_work_units_priority CHECK (priority BETWEEN -1000000 AND 1000000),
        CONSTRAINT ck_work_units_attempt_budget
            CHECK (attempt_count BETWEEN 0 AND max_attempts AND max_attempts BETWEEN 1 AND 100),
        CONSTRAINT ck_work_units_retry_policy CHECK (
            retry_initial_delay_seconds >= 1
            AND retry_multiplier >= 1
            AND retry_max_delay_seconds >= retry_initial_delay_seconds
        ),
        CONSTRAINT ck_work_units_active_lease CHECK (
            (
                state = 'leased'
                AND active_lease_id IS NOT NULL
                AND active_lease_token IS NOT NULL
                AND active_worker_id IS NOT NULL
                AND lease_issued_at_utc IS NOT NULL
                AND lease_expires_at_utc IS NOT NULL
                AND heartbeat_deadline_utc IS NOT NULL
                AND attempt_count >= 1
                AND lease_issued_at_utc < heartbeat_deadline_utc
                AND heartbeat_deadline_utc <= lease_expires_at_utc
            )
            OR (
                state <> 'leased'
                AND active_lease_id IS NULL
                AND active_lease_token IS NULL
                AND active_worker_id IS NULL
                AND lease_issued_at_utc IS NULL
                AND lease_expires_at_utc IS NULL
                AND heartbeat_deadline_utc IS NULL
            )
        ),
        CONSTRAINT ck_work_units_source_permit CHECK (
            (
                source_key IS NULL
                AND source_policy_digest IS NULL
                AND source_permit_not_before_utc IS NULL
            )
            OR (
                source_key IS NOT NULL
                AND (
                    (
                        state = 'leased'
                        AND source_policy_digest IS NOT NULL
                        AND source_permit_not_before_utc IS NOT NULL
                    )
                    OR (
                        state <> 'leased'
                        AND source_policy_digest IS NULL
                        AND source_permit_not_before_utc IS NULL
                    )
                )
            )
        ),
        CONSTRAINT ck_work_units_output CHECK (
            (
                state = 'succeeded'
                AND output_contract IS NOT NULL
                AND output_digest IS NOT NULL
                AND completed_at_utc IS NOT NULL
            )
            OR (
                state <> 'succeeded'
                AND output_contract IS NULL
                AND output_digest IS NULL
                AND completed_at_utc IS NULL
            )
        ),
        CONSTRAINT ck_work_units_revision CHECK (revision >= 0),
        CONSTRAINT ck_work_units_time_order CHECK (updated_at_utc >= created_at_utc),
        FOREIGN KEY(source_key) REFERENCES sources.source_capacity_states (source_key),
        FOREIGN KEY(active_worker_id) REFERENCES work.worker_registrations (worker_id)
    )
    """,
    """
    CREATE INDEX ix_work_units_claim
    ON work.work_units (
        capability,
        available_at_utc,
        priority DESC,
        created_at_utc,
        work_id
    )
    WHERE state IN ('pending', 'retry_wait')
    """,
    """
    CREATE INDEX ix_work_units_lease_expiry
    ON work.work_units (lease_expires_at_utc)
    WHERE state = 'leased'
    """,
    """
    CREATE UNIQUE INDEX uq_work_units_active_lease_id
    ON work.work_units (active_lease_id)
    WHERE state = 'leased'
    """,
    """
    CREATE UNIQUE INDEX uq_work_units_active_lease_token
    ON work.work_units (active_lease_token)
    WHERE state = 'leased'
    """,
    """
    CREATE TABLE work.work_attempts (
        attempt_id UUID NOT NULL,
        work_id UUID NOT NULL,
        attempt_number INTEGER NOT NULL,
        lease_id UUID NOT NULL,
        lease_token UUID NOT NULL,
        worker_id TEXT NOT NULL,
        worker_build_identity TEXT NOT NULL,
        capability TEXT NOT NULL,
        input_digest TEXT NOT NULL,
        source_key TEXT,
        source_policy_digest TEXT,
        source_permit_not_before_utc TIMESTAMP WITH TIME ZONE,
        issued_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        expires_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        heartbeat_deadline_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        finished_at_utc TIMESTAMP WITH TIME ZONE,
        outcome TEXT NOT NULL,
        failure_kind TEXT,
        result_code TEXT,
        failure_owner TEXT,
        failure_message TEXT,
        required_action TEXT,
        output_contract TEXT,
        output_digest TEXT,
        correlation_id TEXT NOT NULL,
        PRIMARY KEY (attempt_id),
        CONSTRAINT uq_work_attempts_number UNIQUE (work_id, attempt_number),
        CONSTRAINT uq_work_attempts_lease_id UNIQUE (lease_id),
        CONSTRAINT uq_work_attempts_lease_token UNIQUE (lease_token),
        CONSTRAINT ck_work_attempts_number CHECK (attempt_number >= 1),
        CONSTRAINT ck_work_attempts_capability
            CHECK (capability IN (
                'manual_import', 'osm_query', 'http_fetch', 'browser_fetch',
                'extraction', 'normalization', 'geography', 'entity_resolution',
                'quality', 'export'
            )),
        CONSTRAINT ck_work_attempts_input_digest_format
            CHECK (input_digest ~ '^sha256:[0-9a-f]{64}$'),
        CONSTRAINT ck_work_attempts_source_policy_digest_format
            CHECK (
                source_policy_digest IS NULL
                OR source_policy_digest ~ '^sha256:[0-9a-f]{64}$'
            ),
        CONSTRAINT ck_work_attempts_output_digest_format
            CHECK (output_digest IS NULL OR output_digest ~ '^sha256:[0-9a-f]{64}$'),
        CONSTRAINT ck_work_attempts_outcome
            CHECK (outcome IN (
                'leased', 'succeeded', 'retry_scheduled', 'dead_lettered',
                'blocked_by_policy', 'released', 'expired'
            )),
        CONSTRAINT ck_work_attempts_failure_kind
            CHECK (
                failure_kind IS NULL
                OR failure_kind IN ('transient', 'permanent', 'policy_blocked', 'contract_invalid')
            ),
        CONSTRAINT ck_work_attempts_result_code_format
            CHECK (result_code IS NULL OR result_code ~ '^[A-Z][A-Z0-9_]{0,99}$'),
        CONSTRAINT ck_work_attempts_lease_time_order
            CHECK (
                issued_at_utc < heartbeat_deadline_utc
                AND heartbeat_deadline_utc <= expires_at_utc
            ),
        CONSTRAINT ck_work_attempts_source_permit CHECK (
            (
                source_key IS NULL
                AND source_policy_digest IS NULL
                AND source_permit_not_before_utc IS NULL
            )
            OR (
                source_key IS NOT NULL
                AND source_policy_digest IS NOT NULL
                AND source_permit_not_before_utc IS NOT NULL
            )
        ),
        CONSTRAINT ck_work_attempts_result_shape CHECK (
            (
                outcome = 'leased'
                AND finished_at_utc IS NULL
                AND failure_kind IS NULL
                AND result_code IS NULL
                AND failure_owner IS NULL
                AND failure_message IS NULL
                AND required_action IS NULL
                AND output_contract IS NULL
                AND output_digest IS NULL
            )
            OR (
                outcome = 'succeeded'
                AND finished_at_utc IS NOT NULL
                AND failure_kind IS NULL
                AND result_code IS NULL
                AND failure_owner IS NULL
                AND failure_message IS NULL
                AND required_action IS NULL
                AND output_contract IS NOT NULL
                AND output_digest IS NOT NULL
            )
            OR (
                outcome IN ('retry_scheduled', 'dead_lettered', 'blocked_by_policy')
                AND finished_at_utc IS NOT NULL
                AND failure_kind IS NOT NULL
                AND result_code IS NOT NULL
                AND failure_owner IS NOT NULL
                AND failure_message IS NOT NULL
                AND required_action IS NOT NULL
                AND output_contract IS NULL
                AND output_digest IS NULL
            )
            OR (
                outcome IN ('released', 'expired')
                AND finished_at_utc IS NOT NULL
                AND failure_kind IS NULL
                AND result_code IS NOT NULL
                AND failure_owner IS NULL
                AND failure_message IS NULL
                AND required_action IS NULL
                AND output_contract IS NULL
                AND output_digest IS NULL
            )
        ),
        FOREIGN KEY(work_id) REFERENCES work.work_units (work_id),
        FOREIGN KEY(worker_id) REFERENCES work.worker_registrations (worker_id),
        FOREIGN KEY(source_key) REFERENCES sources.source_capacity_states (source_key)
    )
    """,
    """
    CREATE TABLE work.dead_letters (
        work_id UUID NOT NULL,
        attempt_id UUID NOT NULL,
        failure_kind TEXT NOT NULL,
        code TEXT NOT NULL,
        owner TEXT NOT NULL,
        message TEXT NOT NULL,
        required_action TEXT NOT NULL,
        created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL,
        correlation_id TEXT NOT NULL,
        PRIMARY KEY (work_id),
        CONSTRAINT ck_dead_letters_failure_kind
            CHECK (failure_kind IN ('transient', 'permanent', 'contract_invalid')),
        CONSTRAINT uq_dead_letters_attempt_id UNIQUE (attempt_id),
        CONSTRAINT ck_dead_letters_code_format
            CHECK (code ~ '^[A-Z][A-Z0-9_]{0,99}$'),
        FOREIGN KEY(work_id) REFERENCES work.work_units (work_id),
        FOREIGN KEY(attempt_id) REFERENCES work.work_attempts (attempt_id)
    )
    """,
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA runs")
    op.execute("CREATE SCHEMA sources")
    op.execute("CREATE SCHEMA work")
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE work.dead_letters")
    op.execute("DROP TABLE work.work_attempts")
    op.execute("DROP TABLE work.work_units")
    op.execute("DROP TABLE work.worker_heartbeats")
    op.execute("DROP TABLE work.worker_capabilities")
    op.execute("DROP TABLE work.worker_registrations")
    op.execute("DROP TABLE sources.source_capacity_states")
    op.execute("DROP TABLE runs.stage_runs")
    op.execute("DROP TABLE runs.collection_runs")
    op.execute("DROP SCHEMA work")
    op.execute("DROP SCHEMA sources")
    op.execute("DROP SCHEMA runs")
