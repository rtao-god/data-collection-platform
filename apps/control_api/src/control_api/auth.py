from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import cast

from review_application import Permission, ReviewerPrincipal


class ReviewAuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _Credential:
    token: str
    principal: ReviewerPrincipal


class TokenAuthenticator:
    def __init__(self, credentials: tuple[_Credential, ...]) -> None:
        if not credentials:
            raise ValueError("at least one reviewer credential is required")
        tokens = tuple(credential.token for credential in credentials)
        if len(tokens) != len(set(tokens)):
            raise ValueError("reviewer tokens must be unique")
        self._credentials = credentials

    @classmethod
    def from_json(cls, value: str) -> TokenAuthenticator:
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("CONTROL_API_REVIEWER_TOKENS_JSON is malformed") from exc
        if not isinstance(raw, dict) or not raw:
            raise ValueError("reviewer token configuration must be a non-empty object")
        credentials: list[_Credential] = []
        for token, payload in raw.items():
            if not isinstance(token, str) or len(token) < 32:
                raise ValueError("reviewer tokens must contain at least 32 characters")
            if not isinstance(payload, dict):
                raise ValueError("reviewer credential payload must be an object")
            if set(payload) != {"actorId", "permissions"}:
                raise ValueError("reviewer credential payload has an unexpected shape")
            actor_id = payload["actorId"]
            permissions = payload["permissions"]
            if not isinstance(actor_id, str) or not actor_id or len(actor_id) > 200:
                raise ValueError("reviewer actorId is invalid")
            if not isinstance(permissions, list) or not permissions:
                raise ValueError("reviewer permissions must be a non-empty array")
            allowed = {
                "review:read",
                "review:decide",
                "review:observe",
                "review:suppress",
            }
            parsed_permissions: list[Permission] = []
            for permission in permissions:
                if not isinstance(permission, str) or permission not in allowed:
                    raise ValueError("reviewer credential contains an unsupported permission")
                parsed_permissions.append(cast(Permission, permission))
            if len(parsed_permissions) != len(set(parsed_permissions)):
                raise ValueError("reviewer permissions must be unique")
            credentials.append(
                _Credential(
                    token=token,
                    principal=ReviewerPrincipal(
                        actor_id=actor_id,
                        permissions=frozenset(parsed_permissions),
                    ),
                )
            )
        return cls(tuple(credentials))

    def authenticate(self, token: str | None) -> ReviewerPrincipal:
        if token is None:
            raise ReviewAuthenticationError("reviewer bearer token is required")
        matched: ReviewerPrincipal | None = None
        for credential in self._credentials:
            if hmac.compare_digest(token, credential.token):
                matched = credential.principal
        if matched is None:
            raise ReviewAuthenticationError("reviewer bearer token is invalid")
        return matched
