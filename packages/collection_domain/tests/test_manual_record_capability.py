from __future__ import annotations

from collection_domain import (
    WorkCapability,
    WorkStage,
    capability_belongs_to_stage,
    capability_requires_source_permit,
)


def test_manual_record_is_discovery_work_without_source_permit_ownership() -> None:
    assert capability_belongs_to_stage(WorkStage.DISCOVERY, WorkCapability.MANUAL_RECORD)
    assert not capability_requires_source_permit(WorkCapability.MANUAL_RECORD)
