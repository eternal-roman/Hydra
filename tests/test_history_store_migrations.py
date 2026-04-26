import sqlite3
from hydra_history_store import HistoryStore, SCHEMA_VERSION


def test_existing_db_with_lower_schema_version_raises(tmp_path):
    """Until v2 ships, opening a DB tagged < SCHEMA_VERSION must explicit-fail
    rather than silently corrupt."""
    db = tmp_path / "h.sqlite"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES('schema_version', '0')")
        conn.commit()
    try:
        HistoryStore(str(db))
    except RuntimeError as e:
        assert "schema_version=0" in str(e)
        return
    raise AssertionError("expected RuntimeError")
