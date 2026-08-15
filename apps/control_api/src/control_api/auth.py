from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import Literal, cast

from review_application import Permission, ReviewerPrincipal

OperatorPermission = Literal[
    "review:read",
    "review:decide",
    "review:observe",
    "review:suppress",
    "runs:create",
    "runs:read",
    "runs:control",
    "sources:read",
    "sources:control",
    "exports:create",
    "exports:read",
    "exports:verify",
    "exports:seal",
    "exports:download",
]

_ALLOWED_PERMISSIONS = frozenset(
    {
        "review:read",
        "review:decide",
        "review:observe",
        "review:suppress",
        "runs:create",
        "runs:read",
        "runs:control",
        "sources:read",
        "sources:control",
        "exports:create",
        "exports:read",
        "exports:verify",
        "exports:seal",
        "exports:download",
    }
)
_REVIEW_PERMISSIONS = frozenset(
    {"review:read", "review:decide", "review:observe", "review:suppress"}
)


class ControlAuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    actor_id: str
    permissions: frozenset[OperatorPermission]

    def require(self, permission: OperatorPermission) -> None:
        if permission not in self.permissions:
            raise PermissionError(permission)

    def as_reviewer(self) -> ReviewerPrincipal:
        return ReviewerPrincipal(
            actor_id=self.actor_id,
            permissions=frozenset(
                cast(Permission, permission)
                for permission in self.permissions
                if permission in _REVIEW_PERMISSIONS
            ),
        )


@dataclass(frozen=True, slots=True)
class _Credential:
    token: str
    principal: OperatorPrincipal


class TokenAuthenticator:
    def __init__(self, credentials: tuple[_Credential, ...]) -> None:
        if not credentials:
            raise ValueError("at least one operator credential is required")
        tokens = tuple(credential.token for credential in credentials)
        if len(tokens) != len(set(tokens)):
            raise ValueError("operator tokens must be unique")
        self._credentials = credentials

    @classmethod
    def from_json(cls, value: str) -> TokenAuthenticator:
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("CONTROL_API_OPERATOR_TOKENS_JSON is malformed") from exc
        if not isinstance(raw, dict) or not raw:
            raise ValueError("operator token configuration must be a non-empty object")
        credentials: list[_Credential] = []
        for token, payload in raw.items():
            if not isinstance(token, str) or len(token) < 32:
                raise ValueError("operator tokens must contain at least 32 characters")
            if not isinstance(payload, dict):
                raise ValueError("operator credential payload must be an object")
            if set(payload) != {"actorId", "permissions"}:
                raise ValueError("operator credential payload has an unexpected shape")
            actor_id = payload["actorId"]
            permissions = payload["permissions"]
            if not isinstance(actor_id, str) or not actor_id or len(actor_id) > 200:
                raise ValueError("operator actorId is invalid")
            if not isinstance(permissions, list) or not permissions:
                raise ValueError("operator permissions must be a non-empty array")
            parsed_permissions: list[OperatorPermission] = []
            for permission in permissions:
                if not isinstance(permission, str) or permission not in _ALLOWED_PERMISSIONS:
                    raise ValueError("operator credential contains an unsupported permission")
                parsed_permissions.append(cast(OperatorPermission, permission))
            if len(parsed_permissions) != len(set(parsed_permissions)):
                raise ValueError("operator permissions must be unique")
            credentials.append(
                _Credential(
                    token=token,
                    principal=OperatorPrincipal(
                        actor_id=actor_id,
                        permissions=frozenset(parsed_permissions),
                    ),
                )
            )
        return cls(tuple(credentials))

    def authenticate(self, token: str | None) -> OperatorPrincipal:
        if token is None:
            raise ControlAuthenticationError("operator bearer token is required")
        matched: OperatorPrincipal | None = None
        for credential in self._credentials:
            if hmac.compare_digest(token, credential.token):
                matched = credential.principal
        if matched is None:
            raise ControlAuthenticationError("operator bearer token is invalid")
        return matched
