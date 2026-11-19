from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

_DATABASE_URL_VARIABLE = "COLLECTOR_DATABASE_URL"


def database_url(purpose: str) -> str:
    """Resolve the integration database URL, failing loudly when it is unconfigured."""
    value = os.environ.get(_DATABASE_URL_VARIABLE, "").strip()
    if not value:
        pytest.fail(f"{_DATABASE_URL_VARIABLE} is required for {purpose}.")
    return value


def unique_label(prefix: str) -> str:
    """Produce a per-run unique label so parallel or repeated runs cannot collide."""
    return f"{prefix}_{uuid4().hex}"


def digest(*parts: str) -> str:
    return f"sha256:{sha256(':'.join(parts).encode()).hexdigest()}"


def engine_fixture(purpose: str) -> Iterator[Engine]:
    value = sa.create_engine(database_url(purpose), poolclass=NullPool)
    try:
        yield value
    finally:
        value.dispose()


def insert_config_artifact(
    connection: sa.Connection,
    bundle_digest: str,
    *,
    recorded_at_utc: datetime | str,
) -> None:
    object_id = uuid4()
    artifact_id = uuid4()
    operation_id = uuid4()
    digest_value = bundle_digest.removeprefix("sha256:")
    connection.execute(
        sa.text(
            """
            INSERT INTO sources.artifact_objects (
                object_id, artifact_kind, content_digest, size_bytes, storage_reference,
                verified_at_utc, recorded_at_utc, correlation_id
            ) VALUES (
                :object_id, 'config_bundle', :bundle_digest, 1, :storage_reference,
                :recorded_at_utc, :recorded_at_utc, 'integration-config-artifact'
            )
            """
        ),
        {
            "object_id": object_id,
            "bundle_digest": bundle_digest,
            "storage_reference": (
                f"config-bundles/sha256/{digest_value[:2]}/{digest_value[2:4]}/{digest_value}"
            ),
            "recorded_at_utc": recorded_at_utc,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO sources.artifact_records (
                artifact_id, object_id, upload_id, work_id, attempt_id, worker_id,
                producer_kind, producer_identity, owner_operation_id, content_type,
                source_policy_digest, recorded_at_utc, correlation_id
            ) VALUES (
                :artifact_id, :object_id, NULL, NULL, NULL, NULL,
                'control_plane', 'integration-test', :operation_id, 'application/json',
                NULL, :recorded_at_utc, 'integration-config-artifact'
            )
            """
        ),
        {
            "artifact_id": artifact_id,
            "object_id": object_id,
            "operation_id": operation_id,
            "recorded_at_utc": recorded_at_utc,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO config.config_bundle_artifacts (
                bundle_digest, artifact_id, recorded_at_utc, correlation_id
            ) VALUES (
                :bundle_digest, :artifact_id, :recorded_at_utc,
                'integration-config-artifact'
            )
            """
        ),
        {
            "bundle_digest": bundle_digest,
            "artifact_id": artifact_id,
            "recorded_at_utc": recorded_at_utc,
        },
    )


def seed_collection_run(
    connection: sa.Connection,
    *,
    run_id: UUID,
    bundle_digest: str,
    recorded_at_utc: datetime,
    correlation_id: str = "integration-run",
) -> None:
    """Record the run that owns an acquisition, together with its sealed config bundle.

    Intake records the acquiring run on the candidate lineage, so that run has to exist
    before the lineage row can be written. Several runs may share one sealed bundle.
    """

    sealed = connection.execute(
        sa.text("SELECT 1 FROM config.config_bundles WHERE bundle_digest = :digest"),
        {"digest": bundle_digest},
    ).scalar_one_or_none()
    if sealed is not None:
        _seed_run(
            connection,
            run_id=run_id,
            bundle_digest=bundle_digest,
            recorded_at_utc=recorded_at_utc,
            correlation_id=correlation_id,
        )
        return
    connection.execute(
        sa.text(
            """
            INSERT INTO config.config_bundle_components
                (bundle_digest, position, path, component_digest)
            VALUES (:digest, 0, 'campaign.yaml', :component_digest)
            """
        ),
        {"digest": bundle_digest, "component_digest": digest("run-bundle-component")},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO config.config_bundles
                (bundle_digest, campaign_key, contract, contract_revision, readiness,
                 recorded_at_utc)
            VALUES (:digest, 'berlin_recording_services', 'collector-campaign-snapshot',
                    'campaign-snapshot-v1', 'ready', :now)
            """
        ),
        {"digest": bundle_digest, "now": recorded_at_utc},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO runs.collection_runs
                (run_id, campaign_key, config_bundle_digest, state, revision, created_at_utc,
                 updated_at_utc, correlation_id)
            VALUES (:run_id, 'berlin_recording_services', :digest, 'running', 0, :now, :now,
                    :correlation_id)
            """
        ),
        {
            "run_id": run_id,
            "digest": bundle_digest,
            "now": recorded_at_utc,
            "correlation_id": correlation_id,
        },
    )


def _seed_run(
    connection: sa.Connection,
    *,
    run_id: UUID,
    bundle_digest: str,
    recorded_at_utc: datetime,
    correlation_id: str,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO runs.collection_runs
                (run_id, campaign_key, config_bundle_digest, state, revision, created_at_utc,
                 updated_at_utc, correlation_id)
            VALUES (:run_id, 'berlin_recording_services', :digest, 'running', 0, :now, :now,
                    :correlation_id)
            ON CONFLICT (run_id) DO NOTHING
            """
        ),
        {
            "run_id": run_id,
            "digest": bundle_digest,
            "now": recorded_at_utc,
            "correlation_id": correlation_id,
        },
    )


def seed_resolution_quality(
    connection: sa.Connection,
    *,
    evaluation: Mapping[tuple[UUID, int], tuple[bool, tuple[str, ...]]],
    policy_digest: str,
    evaluated_at_utc: datetime,
    correlation_id: str = "test-resolution",
    evaluation_digest: str | None = None,
) -> UUID:
    """Persist one authoritative resolution and its per-candidate quality verdict.

    Export and review read quality only through the run's current resolution, so a fixture
    that wants an authoritative verdict must seed the resolution that produced it instead of
    writing a quality row on its own.
    """
    bundle_digest = digest("resolution-fixture", correlation_id, str(uuid4()))
    run_id = uuid4()
    stage_run_id = uuid4()
    work_id = uuid4()
    resolution_id = uuid4()
    # A bundle seals on insert, so its component ledger must already be complete.
    connection.execute(
        sa.text(
            """
            INSERT INTO config.config_bundle_components
                (bundle_digest, position, path, component_digest)
            VALUES (:digest, 0, 'campaign.yaml', :component_digest)
            """
        ),
        {"digest": bundle_digest, "component_digest": digest("resolution-fixture-component")},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO config.config_bundles
                (bundle_digest, campaign_key, contract, contract_revision, readiness,
                 recorded_at_utc)
            VALUES (:digest, 'berlin_recording_services', 'collector-campaign-snapshot',
                    'campaign-snapshot-v1', 'ready', :now)
            """
        ),
        {"digest": bundle_digest, "now": evaluated_at_utc},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO runs.collection_runs
                (run_id, campaign_key, config_bundle_digest, state, revision, created_at_utc,
                 updated_at_utc, correlation_id)
            VALUES (:run_id, 'berlin_recording_services', :digest, 'running', 0, :now, :now,
                    :correlation_id)
            """
        ),
        {
            "run_id": run_id,
            "digest": bundle_digest,
            "now": evaluated_at_utc,
            "correlation_id": correlation_id,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO runs.stage_runs
                (stage_run_id, run_id, stage, state, revision, created_at_utc, updated_at_utc,
                 correlation_id)
            VALUES (:stage_run_id, :run_id, 'entity_resolution', 'running', 0, :now, :now,
                    :correlation_id)
            """
        ),
        {
            "stage_run_id": stage_run_id,
            "run_id": run_id,
            "now": evaluated_at_utc,
            "correlation_id": correlation_id,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO work.work_units
                (work_id, run_id, stage_run_id, stage, capability, semantic_key, input_digest,
                 expected_output_contract, priority, state, attempt_count, max_attempts,
                 retry_initial_delay_seconds, retry_multiplier, retry_max_delay_seconds,
                 available_at_utc, output_contract, output_digest, completed_at_utc, revision,
                 created_at_utc, updated_at_utc, correlation_id, failure_count)
            VALUES (:work_id, :run_id, :stage_run_id, 'entity_resolution', 'entity_resolution',
                    :semantic_key, :digest, 'entity-resolution-snapshot@1', 0, 'succeeded', 1, 3,
                    30, 2, 900, :now, 'entity-resolution-snapshot@1', :digest, :now, 0, :now,
                    :now, :correlation_id, 0)
            """
        ),
        {
            "work_id": work_id,
            "run_id": run_id,
            "stage_run_id": stage_run_id,
            "semantic_key": digest("resolution-fixture-batch", str(resolution_id)),
            "digest": digest("resolution-fixture-snapshot", str(resolution_id)),
            "now": evaluated_at_utc,
            "correlation_id": correlation_id,
        },
    )
    batch_digest = digest("resolution-fixture-batch", str(resolution_id))
    snapshot_digest = digest("resolution-fixture-snapshot", str(resolution_id))
    connection.execute(
        sa.text(
            """
            INSERT INTO candidates.resolution_runs
                (resolution_id, run_id, work_id, batch_id, batch_digest, snapshot_digest,
                 market_area_revision, market_area_digest, quality_policy_digest,
                 supersedes_resolution_id, applied_at_utc, correlation_id)
            VALUES (:resolution_id, :run_id, :work_id, :batch_id, :batch_digest,
                    :snapshot_digest, 'berlin_official_boundary_v1', :market_area_digest,
                    :policy_digest, NULL, :now, :correlation_id)
            """
        ),
        {
            "resolution_id": resolution_id,
            "run_id": run_id,
            "work_id": work_id,
            "batch_id": uuid4(),
            "batch_digest": batch_digest,
            "snapshot_digest": snapshot_digest,
            "market_area_digest": digest("resolution-fixture-market-area"),
            "policy_digest": policy_digest,
            "now": evaluated_at_utc,
            "correlation_id": correlation_id,
        },
    )
    for (candidate_id, revision), (eligible, blockers) in sorted(evaluation.items()):
        cluster_id = uuid4()
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.resolution_clusters
                    (resolution_id, cluster_id, lineage_kind, parent_cluster_ids,
                     parent_membership_digest, blocked_match_edge_ids)
                VALUES (:resolution_id, :cluster_id, 'new', ARRAY[]::uuid[],
                        :membership_digest, ARRAY[]::text[])
                """
            ),
            {
                "resolution_id": resolution_id,
                "cluster_id": cluster_id,
                "membership_digest": digest("resolution-fixture-membership", str(cluster_id)),
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.resolution_cluster_members
                    (resolution_id, cluster_id, candidate_id)
                VALUES (:resolution_id, :cluster_id, :candidate_id)
                """
            ),
            {
                "resolution_id": resolution_id,
                "cluster_id": cluster_id,
                "candidate_id": candidate_id,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO quality.quality_evaluations
                    (evaluation_id, candidate_id, candidate_revision, policy_digest,
                     export_eligible, blockers, evaluation_digest, evaluated_at_utc,
                     correlation_id, resolution_id)
                VALUES (:evaluation_id, :candidate_id, :revision, :policy_digest, :eligible,
                        :blockers, :evaluation_digest, :now, :correlation_id, :resolution_id)
                """
            ).bindparams(sa.bindparam("blockers", type_=sa.ARRAY(sa.Text()))),
            {
                "evaluation_id": uuid4(),
                "candidate_id": candidate_id,
                "revision": revision,
                "policy_digest": policy_digest,
                "eligible": eligible,
                "blockers": list(blockers),
                "evaluation_digest": evaluation_digest
                or digest("resolution-fixture-evaluation", str(candidate_id), str(revision)),
                "now": evaluated_at_utc,
                "correlation_id": correlation_id,
                "resolution_id": resolution_id,
            },
        )
    return resolution_id


def disposable_database(purpose: str) -> Iterator[str]:
    """Create a throw-away database, apply the real migrations, then drop it.

    Integration suites that assert on a global scan need a candidate state that contains
    only their own fixtures. Deleting rows from a shared database cannot provide that: the
    candidate, quality and review tables reject `UPDATE`/`DELETE`/`TRUNCATE` through
    `trg_*_immutable`, and bypassing those guards would disable the very integrity the
    production contract relies on.

    Each caller therefore gets its own database, created empty, migrated with the project's
    migration runner, and dropped afterwards. The development database is never touched and
    no constraint or trigger is weakened.
    """

    base_url = database_url(purpose)
    database_name = f"test_{uuid4().hex}"

    admin = sa.create_engine(_admin_url(base_url), poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        migrated = env_with_database_url(_url_for(base_url, database_name))
        # Fixed argv from module constants; no shell and no external input.
        subprocess.run(  # noqa: S603
            [_uv_executable(), "run", "collection-migrate", "upgrade", "head"],
            cwd=_repository_root(),
            env=migrated,
            check=True,
            capture_output=True,
        )
        yield _url_for(base_url, database_name)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()


def _url_for(base_url: str, database_name: str) -> str:
    parsed = sa.engine.make_url(base_url)
    return parsed.set(database=database_name).render_as_string(hide_password=False)


def _admin_url(base_url: str) -> str:
    return _url_for(base_url, "postgres")


def _uv_executable() -> str:
    found = shutil.which("uv")
    if found is None:
        raise RuntimeError("uv is required to migrate the disposable test database")
    return found


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def env_with_database_url(database_url_value: str) -> dict[str, str]:
    value = dict(os.environ)
    value[_DATABASE_URL_VARIABLE] = database_url_value
    return value
