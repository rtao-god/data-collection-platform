from __future__ import annotations

import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from collection_contracts import owner_error

_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9_.@+-]+$")
_EXPECTED_DRIVER = "postgresql+psycopg"


def upgrade_database(
    *,
    alembic_config_path: Path,
    database_url: str,
    revision: str,
    correlation_id: str,
) -> None:
    """Apply a reviewed Alembic revision through the dedicated migration owner."""

    config_path = _resolve_config_path(alembic_config_path, correlation_id)
    target_revision = _validate_revision(revision, correlation_id)
    normalized_url = _validate_database_url(database_url, correlation_id)

    config = Config(str(config_path))
    config.set_main_option(
        "sqlalchemy.url",
        normalized_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    try:
        command.upgrade(config, target_revision)
    except (CommandError, SQLAlchemyError, OSError, ValueError) as exc:
        raise owner_error(
            error_type="collection/database-migration-failed",
            owner="DatabaseMigration",
            code="DATABASE_MIGRATION_FAILED",
            message="The requested database migration did not complete.",
            context={
                "revision": target_revision,
                "causeType": type(exc).__name__,
            },
            required_action=(
                "Inspect the migration log and database state, correct the owning migration or "
                "environment, and run the explicit migration command again."
            ),
            correlation_id=correlation_id,
        ) from exc


def _resolve_config_path(path: Path, correlation_id: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise owner_error(
            error_type="collection/migration-config-missing",
            owner="DatabaseMigration",
            code="MIGRATION_CONFIG_MISSING",
            message="The Alembic configuration file is unavailable.",
            context={"path": str(path)},
            required_action="Provide the repository-owned database/alembic.ini file.",
            correlation_id=correlation_id,
        ) from exc
    if not resolved.is_file():
        raise owner_error(
            error_type="collection/migration-config-invalid",
            owner="DatabaseMigration",
            code="MIGRATION_CONFIG_INVALID",
            message="The Alembic configuration path is not a regular file.",
            context={"path": str(resolved)},
            required_action="Provide the repository-owned database/alembic.ini file.",
            correlation_id=correlation_id,
        )
    return resolved


def _validate_revision(revision: str, correlation_id: str) -> str:
    normalized = revision.strip()
    if not normalized or _REVISION_PATTERN.fullmatch(normalized) is None:
        raise owner_error(
            error_type="collection/migration-revision-invalid",
            owner="DatabaseMigration",
            code="MIGRATION_REVISION_INVALID",
            message="The migration revision has an unsupported format.",
            context={"revision": revision},
            required_action="Use 'head' or an exact repository-owned Alembic revision identity.",
            correlation_id=correlation_id,
        )
    return normalized


def _validate_database_url(database_url: str, correlation_id: str) -> URL:
    try:
        parsed = make_url(database_url)
    except (TypeError, ValueError) as exc:
        raise owner_error(
            error_type="collection/database-url-invalid",
            owner="DatabaseMigration",
            code="DATABASE_URL_INVALID",
            message="The Collection database URL is invalid.",
            context={"expectedDriver": _EXPECTED_DRIVER},
            required_action=(
                "Provide a valid postgresql+psycopg URL through COLLECTOR_DATABASE_URL."
            ),
            correlation_id=correlation_id,
        ) from exc
    if parsed.drivername != _EXPECTED_DRIVER or not parsed.database:
        raise owner_error(
            error_type="collection/database-url-invalid",
            owner="DatabaseMigration",
            code="DATABASE_URL_INVALID",
            message="The Collection database URL does not target the required PostgreSQL driver.",
            context={
                "actualDriver": parsed.drivername,
                "expectedDriver": _EXPECTED_DRIVER,
                "databasePresent": bool(parsed.database),
            },
            required_action=(
                "Provide a postgresql+psycopg URL with an explicit collector database name."
            ),
            correlation_id=correlation_id,
        )
    return parsed
