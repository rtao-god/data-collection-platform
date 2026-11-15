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
    content_digest: str | None = None
    size_bytes: int | None = None
    content_type: str | None = None

    def __post_init__(self) -> None:
        require_artifact_role(self.role)
        metadata = (self.content_digest, self.size_bytes, self.content_type)
        if any(item is not None for item in metadata) and not all(
            item is not None for item in metadata
        ):
            raise ValueError("work input artifact metadata must be complete")
        if (
            self.content_digest is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", self.content_digest) is None
        ):
            raise ValueError("work input artifact digest must be canonical SHA-256")
        if self.size_bytes is not None and self.size_bytes < 1:
            raise ValueError("work input artifact size must be positive")
        if self.content_type is not None and not self.content_type.strip():
            raise ValueError("work input artifact content type cannot be empty")


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
