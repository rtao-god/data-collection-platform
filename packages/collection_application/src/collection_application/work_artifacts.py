from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

_ROLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$")


@dataclass(frozen=True, slots=True)
class WorkInputArtifact:
    artifact_id: UUID
    role: str

    def __post_init__(self) -> None:
        _require_role(self.role)


@dataclass(frozen=True, slots=True)
class WorkOutputArtifact:
    upload_id: UUID
    role: str

    def __post_init__(self) -> None:
        _require_role(self.role)


def validate_artifact_bindings(
    *,
    identities: tuple[UUID, ...],
    roles: tuple[str, ...],
    owner_name: str,
) -> None:
    if len(identities) > 32:
        raise ValueError(f"{owner_name} cannot contain more than 32 artifact bindings")
    if len(set(identities)) != len(identities):
        raise ValueError(f"{owner_name} artifact identities must be unique")
    if len(set(roles)) != len(roles):
        raise ValueError(f"{owner_name} artifact roles must be unique")


def _require_role(value: str) -> None:
    if _ROLE_PATTERN.fullmatch(value) is None:
        raise ValueError("work artifact role has an invalid format")
