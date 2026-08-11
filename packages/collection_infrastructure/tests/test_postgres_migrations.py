from __future__ import annotations

from pathlib import Path

import pytest
from alembic.util.exc import CommandError
from collection_infrastructure.postgres import migrations

from collection_contracts import OwnerContextError

_CORRELATION_ID = "test-correlation"
_VALID_URL = "postgresql+psycopg://collector:secret@localhost/collector_core"


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "alembic.ini"
    path.write_text("[alembic]\nscript_location = database/migrations\n", encoding="utf-8")
    return path


def test_upgrade_rejects_missing_config(tmp_path: Path) -> None:
    with pytest.raises(OwnerContextError) as raised:
        migrations.upgrade_database(
            alembic_config_path=tmp_path / "missing.ini",
            database_url=_VALID_URL,
            revision="head",
            correlation_id=_CORRELATION_ID,
        )

    assert raised.value.envelope.code == "MIGRATION_CONFIG_MISSING"


def test_upgrade_rejects_non_postgresql_owner(tmp_path: Path) -> None:
    with pytest.raises(OwnerContextError) as raised:
        migrations.upgrade_database(
            alembic_config_path=_config(tmp_path),
            database_url="sqlite:///collector.db",
            revision="head",
            correlation_id=_CORRELATION_ID,
        )

    assert raised.value.envelope.code == "DATABASE_URL_INVALID"
    assert raised.value.envelope.context["actualDriver"] == "sqlite"


def test_upgrade_rejects_invalid_revision_before_execution(tmp_path: Path) -> None:
    with pytest.raises(OwnerContextError) as raised:
        migrations.upgrade_database(
            alembic_config_path=_config(tmp_path),
            database_url=_VALID_URL,
            revision="head; drop schema config",
            correlation_id=_CORRELATION_ID,
        )

    assert raised.value.envelope.code == "MIGRATION_REVISION_INVALID"


def test_upgrade_passes_validated_configuration_to_alembic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, str] = {}

    def fake_upgrade(config: object, revision: str) -> None:
        observed["revision"] = revision
        observed["url"] = config.get_main_option("sqlalchemy.url")  # type: ignore[attr-defined]

    monkeypatch.setattr(migrations.command, "upgrade", fake_upgrade)

    migrations.upgrade_database(
        alembic_config_path=_config(tmp_path),
        database_url=_VALID_URL,
        revision="20260804_0001",
        correlation_id=_CORRELATION_ID,
    )

    assert observed == {"revision": "20260804_0001", "url": _VALID_URL}


def test_upgrade_wraps_alembic_failure_without_exposing_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_upgrade(config: object, revision: str) -> None:
        del config, revision
        raise CommandError("database rejected migration")

    monkeypatch.setattr(migrations.command, "upgrade", fail_upgrade)

    with pytest.raises(OwnerContextError) as raised:
        migrations.upgrade_database(
            alembic_config_path=_config(tmp_path),
            database_url=_VALID_URL,
            revision="head",
            correlation_id=_CORRELATION_ID,
        )

    envelope = raised.value.envelope
    assert envelope.code == "DATABASE_MIGRATION_FAILED"
    assert envelope.context == {"revision": "head", "causeType": "CommandError"}
    assert "secret" not in envelope.model_dump_json()
