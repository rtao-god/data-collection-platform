from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from collection_domain import (
    WorkInputArtifact,
    require_artifact_role,
    validate_artifact_binding_identity,
)


@dataclass(frozen=True, slots=True)
class WorkOutputArtifact:
    """Verified upload selected as one immutable output of a work completion."""

    upload_id: UUID
    role: str

    def __post_init__(self) -> None:
        require_artifact_role(self.role)


def validate_artifact_bindings(
    *,
    identities: tuple[UUID, ...],
    roles: tuple[str, ...],
    owner_name: str,
) -> None:
    validate_artifact_binding_identity(
        identities=identities,
        roles=roles,
        owner_name=owner_name,
    )


__all__ = ["WorkInputArtifact", "WorkOutputArtifact", "validate_artifact_bindings"]
