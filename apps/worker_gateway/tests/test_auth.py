from __future__ import annotations

import json
from pathlib import Path

import pytest
from worker_gateway.auth import (
    WorkerAuthenticationError,
    WorkerAuthenticator,
    WorkerPrincipal,
)

from collection_application import WorkCapability

_TOKEN = "worker-token-000000000000000000000001"


def _secret(path: Path, credentials: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps(
            {
                "contract": "worker-gateway-local-credentials",
                "contractRevision": "worker-gateway-local-credentials-v1",
                "credentials": credentials,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_secret_file_authenticates_exact_worker_scope(tmp_path: Path) -> None:
    authenticator = WorkerAuthenticator.from_secret_file(
        _secret(
            tmp_path / "worker-secret.json",
            [
                {
                    "token": _TOKEN,
                    "workerId": "worker-http-1",
                    "capabilities": ["http_fetch"],
                }
            ],
        )
    )

    principal = authenticator.authenticate(f"Bearer {_TOKEN}")

    assert principal == WorkerPrincipal(
        worker_id="worker-http-1",
        capabilities=frozenset({WorkCapability.HTTP_FETCH}),
    )


def test_rotated_tokens_must_preserve_worker_scope(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="retain the same capability scope"):
        WorkerAuthenticator.from_secret_file(
            _secret(
                tmp_path / "worker-secret.json",
                [
                    {
                        "token": _TOKEN,
                        "workerId": "worker-http-1",
                        "capabilities": ["http_fetch"],
                    },
                    {
                        "token": "worker-token-000000000000000000000002",
                        "workerId": "worker-http-1",
                        "capabilities": ["browser_fetch"],
                    },
                ],
            )
        )


def test_unknown_contract_revision_fails_startup(tmp_path: Path) -> None:
    path = tmp_path / "worker-secret.json"
    path.write_text(
        json.dumps(
            {
                "contract": "worker-gateway-local-credentials",
                "contractRevision": "unknown-revision",
                "credentials": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="contract revision is unsupported"):
        WorkerAuthenticator.from_secret_file(path)


def test_unknown_or_malformed_token_is_rejected_without_identity_context() -> None:
    authenticator = WorkerAuthenticator.from_plaintext_credentials(
        {
            _TOKEN: WorkerPrincipal(
                worker_id="worker-http-1",
                capabilities=frozenset({WorkCapability.HTTP_FETCH}),
            )
        }
    )

    with pytest.raises(WorkerAuthenticationError) as malformed:
        authenticator.authenticate("Basic value")
    with pytest.raises(WorkerAuthenticationError) as unknown:
        authenticator.authenticate("Bearer worker-token-000000000000000000009999")

    assert malformed.value.code == "WORKER_AUTHENTICATION_INVALID"
    assert malformed.value.context == {}
    assert unknown.value.code == "WORKER_AUTHENTICATION_INVALID"
    assert unknown.value.context == {}


def test_capability_scope_denies_an_unrelated_worker_capability() -> None:
    principal = WorkerPrincipal(
        worker_id="worker-http-1",
        capabilities=frozenset({WorkCapability.HTTP_FETCH}),
    )

    with pytest.raises(WorkerAuthenticationError) as denied:
        WorkerAuthenticator.require_capability(principal, WorkCapability.BROWSER_FETCH)

    assert denied.value.status_code == 403
    assert denied.value.code == "WORKER_CAPABILITY_FORBIDDEN"
    assert denied.value.context == {
        "workerId": "worker-http-1",
        "capability": "browser_fetch",
    }
