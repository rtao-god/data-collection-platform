from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker_gateway import __main__ as gateway_main


def test_runtime_requires_database_and_worker_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLLECTOR_DATABASE_URL", raising=False)
    monkeypatch.delenv("WORKER_GATEWAY_TOKEN_FILE", raising=False)

    with pytest.raises(RuntimeError, match="COLLECTOR_DATABASE_URL is required"):
        gateway_main.build_runtime()


def test_invalid_secret_fails_before_runtime_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = tmp_path / "worker-secret.json"
    secret.write_text(
        json.dumps(
            {
                "contract": "worker-gateway-local-credentials",
                "contractRevision": "unsupported",
                "credentials": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "COLLECTOR_DATABASE_URL",
        "postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
    )
    monkeypatch.setenv("WORKER_GATEWAY_TOKEN_FILE", str(secret))

    with pytest.raises(ValueError, match="contract revision is unsupported"):
        gateway_main.build_runtime()


def test_non_local_bind_is_rejected_before_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_GATEWAY_HOST", "0.0.0.0")
    monkeypatch.delenv("COLLECTOR_DATABASE_URL", raising=False)
    monkeypatch.delenv("WORKER_GATEWAY_TOKEN_FILE", raising=False)

    with pytest.raises(RuntimeError, match="refuses a non-local bind"):
        gateway_main.main()


@pytest.mark.parametrize("value", ["0", "65536", "not-an-integer"])
def test_port_must_be_a_valid_tcp_port(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("WORKER_GATEWAY_PORT", value)

    with pytest.raises(RuntimeError, match="WORKER_GATEWAY_PORT"):
        gateway_main._integer_environment(
            "WORKER_GATEWAY_PORT",
            8080,
            minimum=1,
            maximum=65_535,
        )


@pytest.mark.parametrize("value", ["-1", "3601", "not-numeric"])
def test_expiry_interval_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("WORKER_GATEWAY_EXPIRY_INTERVAL_SECONDS", value)

    with pytest.raises(RuntimeError, match="WORKER_GATEWAY_EXPIRY_INTERVAL_SECONDS"):
        gateway_main._float_environment(
            "WORKER_GATEWAY_EXPIRY_INTERVAL_SECONDS",
            5.0,
            minimum=0,
            maximum=3_600,
        )
