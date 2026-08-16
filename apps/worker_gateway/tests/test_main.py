from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker_gateway import __main__ as gateway_main


def _worker_secret(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "contract": "worker-gateway-local-credentials",
                "contractRevision": "worker-gateway-local-credentials-v1",
                "credentials": [
                    {
                        "token": "worker-token-000000000000000000000001",
                        "workerId": "worker-http-1",
                        "capabilities": ["http_fetch"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_runtime_requires_database_and_worker_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLLECTOR_DATABASE_URL", raising=False)
    monkeypatch.delenv("WORKER_GATEWAY_TOKEN_FILE", raising=False)

    with pytest.raises(RuntimeError, match="COLLECTOR_DATABASE_URL is required"):
        gateway_main.build_runtime()


def test_runtime_requires_object_store_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "COLLECTOR_DATABASE_URL",
        "postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
    )
    monkeypatch.setenv(
        "WORKER_GATEWAY_TOKEN_FILE",
        str(_worker_secret(tmp_path / "worker-secret.json")),
    )
    monkeypatch.delenv("COLLECTOR_OBJECT_STORE_ENDPOINT", raising=False)

    with pytest.raises(RuntimeError, match="COLLECTOR_OBJECT_STORE_ENDPOINT is required"):
        gateway_main.build_runtime()


def test_invalid_worker_secret_fails_before_object_store_composition(
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


def test_object_store_secret_file_is_trimmed_and_bounded(tmp_path: Path) -> None:
    valid = tmp_path / "valid.secret"
    valid.write_text(" access-key\n", encoding="utf-8")
    empty = tmp_path / "empty.secret"
    empty.write_text(" \n", encoding="utf-8")
    oversized = tmp_path / "oversized.secret"
    oversized.write_text("x" * 4_097, encoding="utf-8")

    assert gateway_main._read_secret_file(valid) == "access-key"
    with pytest.raises(RuntimeError, match="required secret file is empty"):
        gateway_main._read_secret_file(empty)
    with pytest.raises(RuntimeError, match="required secret file is too large"):
        gateway_main._read_secret_file(oversized)
    with pytest.raises(RuntimeError, match="cannot read required secret file"):
        gateway_main._read_secret_file(tmp_path / "missing.secret")


def test_non_local_bind_is_rejected_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_GATEWAY_HOST", "0.0.0.0")  # noqa: S104
    monkeypatch.delenv("WORKER_GATEWAY_BIND_MODE", raising=False)

    with pytest.raises(RuntimeError, match="WORKER_GATEWAY_BIND_MODE=container"):
        gateway_main._bind_host_from_environment()


def test_container_bind_mode_accepts_exact_all_interface_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_GATEWAY_BIND_MODE", "container")
    monkeypatch.setenv("WORKER_GATEWAY_HOST", "0.0.0.0")  # noqa: S104

    assert gateway_main._bind_host_from_environment() == "0.0.0.0"  # noqa: S104


def test_container_bind_mode_rejects_local_or_ambiguous_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_GATEWAY_BIND_MODE", "container")
    monkeypatch.setenv("WORKER_GATEWAY_HOST", "127.0.0.1")

    with pytest.raises(RuntimeError, match=r"requires WORKER_GATEWAY_HOST=0\.0\.0\.0"):
        gateway_main._bind_host_from_environment()


def test_unknown_bind_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_GATEWAY_BIND_MODE", "public")

    with pytest.raises(RuntimeError, match="must be local or container"):
        gateway_main._bind_host_from_environment()


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
