from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

_ROLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$")
_MAX_ARTIFACT_BINDINGS = 32


@dataclass(frozen=True, slots=True)
class WorkInputArtifact:
    """Immutable input artifact exposed by an acquired work lease."""

    artifact_id: UUID
    role: str

    def __post_init__(self) -> None:
        require_artifact_role(self.role)


def validate_artifact_binding_identity(
    *,
    identities: tuple[UUID, ...],
    roles: tuple[str, ...],
    owner_name: str,
) -> None:
    if len(identities) != len(roles):
        raise ValueError(f"{owner_name} artifact identities and roles must have equal length")
    if len(identities) > _MAX_ARTIFACT_BINDINGS:
        raise ValueError(
            f"{owner_name} cannot contain more than {_MAX_ARTIFACT_BINDINGS} artifact bindings"
        )
    if len(set(identities)) != len(identities):
        raise ValueError(f"{owner_name} artifact identities must be unique")
    if len(set(roles)) != len(roles):
        raise ValueError(f"{owner_name} artifact roles must be unique")


def require_artifact_role(value: str) -> None:
    if _ROLE_PATTERN.fullmatch(value) is None:
        raise ValueError("work artifact role has an invalid format")
