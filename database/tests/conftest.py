from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

# --import-mode=importlib leaves sibling test modules off sys.path, so shared
# database test support must be registered explicitly before any test imports it.
_SUPPORT_ROOT = Path(__file__).resolve().parent
if str(_SUPPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_ROOT))

from db_support import database_url  # noqa: E402


@pytest.fixture
def database_connection() -> Iterator[sa.Connection]:
    """A live connection whose transaction is always rolled back."""
    engine = sa.create_engine(database_url("geometry integration tests"), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                yield connection
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
