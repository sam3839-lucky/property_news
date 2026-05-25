"""Test fixtures — isolated SQLite DB for each test."""
import sys
import tempfile
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    """Replace db.DB_PATH with a temp file for test isolation."""
    tmp = Path(tempfile.mkdtemp()) / "test_cache.db"
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp)
    # Re-initialize with clean schema
    db.init_db()
    yield
    # Cleanup
    tmp.unlink(missing_ok=True)
    tmp.parent.rmdir()
