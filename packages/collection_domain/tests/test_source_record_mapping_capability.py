from __future__ import annotations

from collection_domain import (
    WorkCapability,
    WorkStage,
    capability_belongs_to_stage,
    capability_requires_source_permit,
)


def test_source_record_mapping_is_normalization_work_without_source_permit() -> None:
    """Mapping reads an immutable source artifact; the source permit belongs upstream."""

    assert capability_belongs_to_stage(
        WorkStage.NORMALIZATION, WorkCapability.SOURCE_RECORD_MAPPING
    )
    assert not capability_requires_source_permit(WorkCapability.SOURCE_RECORD_MAPPING)


def test_source_record_mapping_does_not_replace_legacy_normalization() -> None:
    assert capability_belongs_to_stage(WorkStage.NORMALIZATION, WorkCapability.NORMALIZATION)
    assert WorkCapability.SOURCE_RECORD_MAPPING is not WorkCapability.NORMALIZATION


def test_source_record_mapping_is_not_a_discovery_or_acquisition_capability() -> None:
    for stage in (WorkStage.DISCOVERY, WorkStage.ACQUISITION, WorkStage.EXTRACTION):
        assert not capability_belongs_to_stage(stage, WorkCapability.SOURCE_RECORD_MAPPING)
