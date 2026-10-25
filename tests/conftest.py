"""Point Dixon at a throwaway DuckDB before the package reads config."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(tempfile.mkdtemp(prefix="dixon-pytest-"))
os.environ["DIXON_DB"] = str(_ROOT / "test.duckdb")
os.environ["DIXON_DATA_DIR"] = str(_ROOT)
os.environ["DIXON_API_KEY"] = ""
os.environ["DIXON_DEFAULT_OWNER"] = "paul.kf.leung@gmail.com"


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Each test gets its own file so inserts cannot leak across cases."""
    path = tmp_path / "dixon.duckdb"
    monkeypatch.setattr("dixon.db.DB_PATH", path)
    monkeypatch.setattr("dixon.config.DB_PATH", path)
    import dixon.api as api

    monkeypatch.setattr(api, "DB_PATH", path)
    yield path
