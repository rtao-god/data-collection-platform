from __future__ import annotations

import sqlalchemy as sa

from collection_application import WorkCapability, capability_requires_source_permit
from collection_infrastructure.postgres.work_metadata import work_attempts, work_units

_SOURCE_BOUND_CAPABILITIES = tuple(
    capability.value
    for capability in WorkCapability
    if capability_requires_source_permit(capability)
)


def _in_values(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def _source_capability_check() -> str:
    source_bound = _in_values("capability", _SOURCE_BOUND_CAPABILITIES)
    return (
        f"({source_bound} AND source_key IS NOT NULL) OR "
        f"(NOT ({source_bound}) AND source_key IS NULL)"
    )


WORK_UNIT_SOURCE_CAPABILITY_CONSTRAINT = sa.CheckConstraint(
    _source_capability_check(),
    name="ck_work_units_source_capability",
)
WORK_ATTEMPT_SOURCE_CAPABILITY_CONSTRAINT = sa.CheckConstraint(
    _source_capability_check(),
    name="ck_work_attempts_source_capability",
)

work_units.append_constraint(WORK_UNIT_SOURCE_CAPABILITY_CONSTRAINT)
work_attempts.append_constraint(WORK_ATTEMPT_SOURCE_CAPABILITY_CONSTRAINT)

SOURCE_CAPABILITY_CONSTRAINTS = (
    WORK_UNIT_SOURCE_CAPABILITY_CONSTRAINT,
    WORK_ATTEMPT_SOURCE_CAPABILITY_CONSTRAINT,
)
