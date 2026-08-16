from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from collection_application.pipeline_advancement import PipelineAdvancementConflict
from collection_infrastructure.postgres.pipeline_advancement_metadata import (
    pipeline_advancements,
)
from collection_infrastructure.postgres.succeeded_work_catalog import (
    PostgresSucceededWorkCatalog,
)

_WORK_ID = UUID("00000000-0000-0000-0000-000000000601")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000602")
_STAGE_RUN_ID = UUID("00000000-0000-0000-0000-000000000603")
_INPUT_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000604")
_OUTPUT_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000605")
_INPUT_DIGEST = f"sha256:{'1' * 64}"
_OUTPUT_DIGEST = f"sha256:{'2' * 64}"


class _Result:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> object | None:
        if len(self._rows) > 1:
            raise AssertionError("scripted result contains more than one row")
        return self._rows[0] if self._rows else None

    def all(self) -> list[object]:
        return list(self._rows)

    def __iter__(self) -> Iterator[object]:
        return iter(self._rows)


class _Connection:
    def __init__(self, results: tuple[_Result, ...]) -> None:
        self._results = list(results)

    def execute(self, _statement: object) -> _Result:
        if not self._results:
            raise AssertionError("unexpected SQL execution")
        return self._results.pop(0)

    def assert_consumed(self) -> None:
        assert not self._results


def _work(*, state: str = "succeeded", output_digest: str = _OUTPUT_DIGEST) -> dict[str, object]:
    return {
        "run_id": _RUN_ID,
        "stage_run_id": _STAGE_RUN_ID,
        "state": state,
        "capability": "manual_import",
        "output_contract": "manual-import-plan@1",
        "output_digest": output_digest,
    }


def _stage() -> dict[str, object]:
    return {
        "run_id": _RUN_ID,
        "stage": "discovery",
    }


def _artifact(
    *,
    artifact_id: UUID,
    role: str,
    digest: str,
) -> dict[str, object]:
    return {
        "binding_position": 0,
        "binding_role": role,
        "artifact_id": artifact_id,
        "content_digest": digest,
        "size_bytes": 128,
        "content_type": "application/json",
    }


def test_read_rehydrates_exact_canonical_succeeded_work() -> None:
    connection = _Connection(
        (
            _Result((_work(),)),
            _Result((_stage(),)),
            _Result(
                (
                    _artifact(
                        artifact_id=_INPUT_ARTIFACT_ID,
                        role="manual_source:csv:reject_all",
                        digest=_INPUT_DIGEST,
                    ),
                )
            ),
            _Result(
                (
                    _artifact(
                        artifact_id=_OUTPUT_ARTIFACT_ID,
                        role="manual_import_plan",
                        digest=_OUTPUT_DIGEST,
                    ),
                )
            ),
        )
    )

    result = PostgresSucceededWorkCatalog(object()).read(connection, _WORK_ID)  # type: ignore[arg-type]

    assert result.source_work_unit_id == _WORK_ID
    assert result.run_id == _RUN_ID
    assert result.stage_run_id == _STAGE_RUN_ID
    assert result.stage.value == "discovery"
    assert result.capability == "manual_import"
    assert result.output_contract == "manual-import-plan@1"
    assert result.output_digest == _OUTPUT_DIGEST
    assert result.output_artifact.artifact_id == _OUTPUT_ARTIFACT_ID
    assert result.output_artifact.role == "manual_import_plan"
    assert tuple(item.artifact_id for item in result.input_artifacts) == (_INPUT_ARTIFACT_ID,)
    connection.assert_consumed()


def test_read_rejects_work_that_is_not_succeeded() -> None:
    connection = _Connection((_Result((_work(state="pending"),)),))

    with pytest.raises(PipelineAdvancementConflict) as error:
        PostgresSucceededWorkCatalog(object()).read(  # type: ignore[arg-type]
            connection,
            _WORK_ID,
        )

    assert error.value.code == "PIPELINE_SOURCE_WORK_NOT_SUCCEEDED"
    connection.assert_consumed()


def test_read_rejects_output_digest_conflict() -> None:
    connection = _Connection(
        (
            _Result((_work(output_digest=f"sha256:{'3' * 64}"),)),
            _Result((_stage(),)),
            _Result(()),
            _Result(
                (
                    _artifact(
                        artifact_id=_OUTPUT_ARTIFACT_ID,
                        role="manual_import_plan",
                        digest=_OUTPUT_DIGEST,
                    ),
                )
            ),
        )
    )

    with pytest.raises(PipelineAdvancementConflict) as error:
        PostgresSucceededWorkCatalog(object()).read(  # type: ignore[arg-type]
            connection,
            _WORK_ID,
        )

    assert error.value.code == "PIPELINE_OUTPUT_DIGEST_CONFLICT"
    connection.assert_consumed()


def test_discovery_limit_is_fail_closed_before_storage_access() -> None:
    catalog = PostgresSucceededWorkCatalog(object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="between 1 and 1000"):
        catalog.list_unregistered_succeeded(limit=0, correlation_id="pipeline-test")


def test_advancement_metadata_foreign_key_targets_canonical_work_id() -> None:
    foreign_key = next(iter(pipeline_advancements.c.source_work_unit_id.foreign_keys))

    assert foreign_key.target_fullname == "work.work_units.work_id"
    assert foreign_key.ondelete == "RESTRICT"
