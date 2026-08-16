from __future__ import annotations

import pytest

from control_api.auth import ControlAuthenticationError, TokenAuthenticator

TOKEN = "a" * 40


def authenticator() -> TokenAuthenticator:
    return TokenAuthenticator.from_json(
        '{"' + TOKEN + '":{"actorId":"reviewer-1","permissions":["review:read"]}}'
    )


def test_authenticator_returns_configured_principal() -> None:
    principal = authenticator().authenticate(TOKEN)
    assert principal.actor_id == "reviewer-1"
    assert principal.permissions == frozenset({"review:read"})


def test_authenticator_rejects_missing_and_invalid_tokens() -> None:
    value = authenticator()
    with pytest.raises(ControlAuthenticationError):
        value.authenticate(None)
    with pytest.raises(ControlAuthenticationError):
        value.authenticate("b" * 40)


def test_authenticator_rejects_unknown_permission() -> None:
    with pytest.raises(ValueError):
        TokenAuthenticator.from_json(
            '{"' + TOKEN + '":{"actorId":"reviewer-1","permissions":["admin"]}}'
        )
