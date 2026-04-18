"""Error-path tests for hydra_journal_migrator.

Covers the typed-error guarantees added during the 2026-04-18 audit so a
corrupt legacy file produces an actionable RuntimeError (with file path)
instead of a raw json.JSONDecodeError.
"""
import json
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_journal_migrator import migrate_legacy_trade_log_file


def test_corrupt_legacy_journal_raises_runtimeerror():
    with tempfile.TemporaryDirectory() as td:
        legacy = pathlib.Path(td) / "hydra_trades_live.json"
        legacy.write_text("{not json", encoding="utf-8")
        with pytest.raises(RuntimeError, match=r"failed to read legacy journal"):
            migrate_legacy_trade_log_file(td, verbose=False)


def test_corrupt_session_snapshot_raises_runtimeerror():
    with tempfile.TemporaryDirectory() as td:
        snap = pathlib.Path(td) / "hydra_session_snapshot.json"
        snap.write_text("{broken", encoding="utf-8")
        with pytest.raises(RuntimeError, match=r"failed to read session snapshot"):
            migrate_legacy_trade_log_file(td, verbose=False)


def test_clean_dir_is_noop():
    """Empty dir produces a clean report, not a crash."""
    with tempfile.TemporaryDirectory() as td:
        report = migrate_legacy_trade_log_file(td, verbose=False)
        assert report["converted_rolling"] == 0
        assert report["converted_snapshot"] == 0


def test_existing_new_journal_skips_legacy_migration():
    """When the new file already exists, legacy migration is a no-op."""
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        (tdp / "hydra_trades_live.json").write_text(
            json.dumps([{"timestamp": 1.0, "side": "BUY", "pair": "SOL/USDC",
                         "amount": 0.1, "price": 100.0, "balance": 100.0,
                         "regime": "RANGING", "strategy": "GRID"}]),
            encoding="utf-8",
        )
        (tdp / "hydra_order_journal.json").write_text("[]", encoding="utf-8")
        report = migrate_legacy_trade_log_file(td, verbose=False)
        assert any("skipping rolling migration" in a for a in report["actions"])


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
