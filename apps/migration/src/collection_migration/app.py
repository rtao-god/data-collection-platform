from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from collection_infrastructure.postgres import upgrade_database

from collection_contracts import OwnerContextError, owner_error

_DATABASE_URL_ENV = "COLLECTOR_DATABASE_URL"
_ALEMBIC_CONFIG_ENV = "COLLECTOR_ALEMBIC_CONFIG"


def _parser(default_config: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collection-migrate")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config,
        help="Repository-owned Alembic configuration file.",
    )
    command = parser.add_subparsers(dest="command", required=True)
    upgrade = command.add_parser("upgrade", help="Apply migrations up to an exact revision.")
    upgrade.add_argument("revision", nargs="?", default="head")
    return parser


def run(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    default_config = Path(environment.get(_ALEMBIC_CONFIG_ENV, "database/alembic.ini"))
    args = _parser(default_config).parse_args(argv)
    correlation_id = str(uuid4())

    try:
        database_url = environment.get(_DATABASE_URL_ENV, "").strip()
        if not database_url:
            raise owner_error(
                error_type="collection/database-url-missing",
                owner="DatabaseMigration",
                code="DATABASE_URL_MISSING",
                message="The Collection database URL was not provided.",
                context={"environmentVariable": _DATABASE_URL_ENV},
                required_action=(
                    "Provide COLLECTOR_DATABASE_URL through the migration process environment or "
                    "Docker secret-backed configuration."
                ),
                correlation_id=correlation_id,
            )

        upgrade_database(
            alembic_config_path=args.config,
            database_url=database_url,
            revision=args.revision,
            correlation_id=correlation_id,
        )
        print(
            json.dumps(
                {
                    "owner": "DatabaseMigration",
                    "status": "succeeded",
                    "requestedRevision": args.revision,
                    "correlationId": correlation_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except OwnerContextError as exc:
        print(
            json.dumps(
                exc.envelope.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


def main() -> None:
    raise SystemExit(run())
