from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from collection_application import WorkCapability

_SECRET_CONTRACT = "worker-gateway-local-credentials"
_SECRET_CONTRACT_REVISION = "worker-gateway-local-credentials-v1"
_MAX_SECRET_BYTES = 1_048_576
_WORKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")


@dataclass(frozen=True, slots=True)
class WorkerPrincipal:
    worker_id: str
    capabilities: frozenset[WorkCapability]


class WorkerAuthenticationError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        context: Mapping[str, object],
        required_action: str,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.context = dict(context)
        self.required_action = required_action
        super().__init__(message)


class WorkerAuthenticator:
    """Authenticates local workers from a mounted capability-scoped secret file."""

    def __init__(self, principals_by_token_digest: Mapping[str, WorkerPrincipal]) -> None:
        if not principals_by_token_digest:
            raise ValueError("worker credential set cannot be empty")
        self._principals_by_token_digest = dict(principals_by_token_digest)

    @classmethod
    def from_plaintext_credentials(
        cls,
        credentials: Mapping[str, WorkerPrincipal],
    ) -> WorkerAuthenticator:
        if not credentials:
            raise ValueError("worker credential set cannot be empty")
        principals: dict[str, WorkerPrincipal] = {}
        for token, principal in credentials.items():
            _validate_token(token)
            _validate_principal(principal)
            digest = _token_digest(token)
            if digest in principals:
                raise ValueError("worker credential tokens must be unique")
            principals[digest] = principal
        return cls(principals)

    @classmethod
    def from_secret_file(cls, path: Path) -> WorkerAuthenticator:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ValueError(f"worker credential secret is unavailable: {type(exc).__name__}") from exc
        if not 1 <= size <= _MAX_SECRET_BYTES:
            raise ValueError("worker credential secret size is outside the supported range")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"worker credential secret is invalid: {type(exc).__name__}") from exc
        if not isinstance(document, dict) or set(document) != {
            "contract",
            "contractRevision",
            "credentials",
        }:
            raise ValueError("worker credential secret has an invalid document shape")
        if document["contract"] != _SECRET_CONTRACT:
            raise ValueError("worker credential secret contract identity is unsupported")
        if document["contractRevision"] != _SECRET_CONTRACT_REVISION:
            raise ValueError("worker credential secret contract revision is unsupported")
        raw_credentials = document["credentials"]
        if not isinstance(raw_credentials, list) or not raw_credentials:
            raise ValueError("worker credential secret requires at least one credential")

        credentials: dict[str, WorkerPrincipal] = {}
        principal_by_worker: dict[str, WorkerPrincipal] = {}
        for raw_credential in raw_credentials:
            if not isinstance(raw_credential, dict) or set(raw_credential) != {
                "token",
                "workerId",
                "capabilities",
            }:
                raise ValueError("worker credential entry has an invalid shape")
            token = raw_credential["token"]
            worker_id = raw_credential["workerId"]
            raw_capabilities = raw_credential["capabilities"]
            if not isinstance(token, str) or not isinstance(worker_id, str):
                raise ValueError("worker credential token and worker ID must be strings")
            if not isinstance(raw_capabilities, list) or not raw_capabilities or not all(
                isinstance(value, str) for value in raw_capabilities
            ):
                raise ValueError("worker credential capabilities must be a non-empty string list")
            try:
                capabilities = frozenset(WorkCapability(value) for value in raw_capabilities)
            except ValueError as exc:
                raise ValueError("worker credential contains an unsupported capability") from exc
            principal = WorkerPrincipal(worker_id=worker_id, capabilities=capabilities)
            _validate_token(token)
            _validate_principal(principal)
            previous = principal_by_worker.get(worker_id)
            if previous is not None and previous != principal:
                raise ValueError("rotated tokens for one worker must retain the same capability scope")
            principal_by_worker[worker_id] = principal
            digest = _token_digest(token)
            if digest in credentials:
                raise ValueError("worker credential tokens must be unique")
            credentials[digest] = principal
        return cls(credentials)

    def authenticate(self, authorization_header: str | None) -> WorkerPrincipal:
        if authorization_header is None:
            raise WorkerAuthenticationError(
                status_code=401,
                code="WORKER_AUTHENTICATION_REQUIRED",
                message="A worker bearer credential is required.",
                context={},
                required_action="Provide the capability-scoped token mounted for this worker.",
            )
        scheme, separator, token = authorization_header.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token:
            raise WorkerAuthenticationError(
                status_code=401,
                code="WORKER_AUTHENTICATION_INVALID",
                message="The worker authorization header is invalid.",
                context={},
                required_action="Use exactly one Bearer credential issued for this worker.",
            )
        principal = self._principals_by_token_digest.get(_token_digest(token))
        if principal is None:
            raise WorkerAuthenticationError(
                status_code=401,
                code="WORKER_AUTHENTICATION_INVALID",
                message="The worker credential is unknown or revoked.",
                context={},
                required_action="Mount a current capability-scoped worker credential.",
            )
        return principal

    @staticmethod
    def require_capability(principal: WorkerPrincipal, capability: WorkCapability) -> None:
        if capability not in principal.capabilities:
            raise WorkerAuthenticationError(
                status_code=403,
                code="WORKER_CAPABILITY_FORBIDDEN",
                message="The worker credential does not authorize the requested capability.",
                context={
                    "workerId": principal.worker_id,
                    "capability": capability.value,
                },
                required_action="Use the credential scoped to the requested worker capability.",
            )

    @staticmethod
    def require_registration_scope(
        principal: WorkerPrincipal,
        capabilities: frozenset[WorkCapability],
    ) -> None:
        if capabilities != principal.capabilities:
            raise WorkerAuthenticationError(
                status_code=403,
                code="WORKER_REGISTRATION_SCOPE_FORBIDDEN",
                message="Worker registration capabilities do not match the credential scope.",
                context={
                    "workerId": principal.worker_id,
                    "authorizedCapabilities": sorted(
                        capability.value for capability in principal.capabilities
                    ),
                    "requestedCapabilities": sorted(
                        capability.value for capability in capabilities
                    ),
                },
                required_action="Register exactly the capabilities authorized by this credential.",
            )


def _validate_principal(principal: WorkerPrincipal) -> None:
    if _WORKER_ID_PATTERN.fullmatch(principal.worker_id) is None:
        raise ValueError("worker credential has an invalid worker ID")
    if not principal.capabilities:
        raise ValueError("worker credential requires at least one capability")


def _validate_token(token: str) -> None:
    if not 32 <= len(token) <= 512 or token.strip() != token or any(
        character.isspace() for character in token
    ):
        raise ValueError("worker credential token has an invalid format")


def _token_digest(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()
