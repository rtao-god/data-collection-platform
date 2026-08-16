from __future__ import annotations

import json
from pathlib import Path

import pytest

from collection_migration import app


def test_missing_database_url_returns_typed_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = app.run(["upgrade"], environ={})

    output = capsys.readouterr()
    payload = json.loads(output.err)
    assert exit_code == 2
    assert payload["owner"] == "DatabaseMigration"
    assert payload["code"] == "DATABASE_URL_MISSING"


def test_upgrade_uses_explicit_config_and_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "alembic.ini"
    config_path.write_text("[alembic]\n", encoding="utf-8")
    observed: dict[str, object] = {}

    def fake_upgrade_database(**kwargs: object) -> None:
        observed.update(kwargs)

    monkeypatch.setattr(app, "upgrade_database", fake_upgrade_database)

    exit_code = app.run(
        ["--config", str(config_path), "upgrade", "20260804_0001"],
        environ={
            "COLLECTOR_DATABASE_URL": (
                "postgresql+psycopg://collector:secret@localhost/collector_core"
            )
        },
    )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_code == 0
    assert payload["status"] == "succeeded"
    assert payload["requestedRevision"] == "20260804_0001"
    assert observed["alembic_config_path"] == config_path
    assert observed["revision"] == "20260804_0001"
    assert observed["database_url"] == (
        "postgresql+psycopg://collector:secret@localhost/collector_core"
    )
    assert isinstance(observed["correlation_id"], str)
