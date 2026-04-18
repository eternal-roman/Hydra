"""Error-path tests for hydra_companions.compiler.

Covers the fail-soft / typed-error guarantees added during the 2026-04-18
audit so future regressions are caught early.
"""
import json
import pathlib
import shutil
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.compiler import load_all_souls, load_soul


def _seed_good_souls(dst: pathlib.Path) -> None:
    src = ROOT / "hydra_companions" / "souls"
    for f in src.glob("*.soul.json"):
        shutil.copy(f, dst / f.name)


def test_load_all_souls_skips_corrupt_file():
    """A single corrupt soul JSON must NOT kill the whole iteration."""
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        _seed_good_souls(tdp)
        (tdp / "broken.soul.json").write_text("{not json", encoding="utf-8")
        souls = load_all_souls(tdp)
        assert set(souls.keys()) == {"apex", "athena", "broski"}


def test_load_all_souls_skips_missing_required_keys():
    """Soul missing required fields raises in compile_soul → caught + skipped."""
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        _seed_good_souls(tdp)
        (tdp / "incomplete.soul.json").write_text(
            json.dumps({"id": "incomplete"}), encoding="utf-8"
        )
        souls = load_all_souls(tdp)
        assert "incomplete" not in souls
        assert set(souls.keys()) == {"apex", "athena", "broski"}


def test_load_all_souls_accepts_str_path():
    """souls_dir typed as Optional[Path] but callers may pass str."""
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        _seed_good_souls(tdp)
        # Pass as plain string — must not TypeError on Path operator
        souls = load_all_souls(td)
        assert set(souls.keys()) == {"apex", "athena", "broski"}


def test_load_soul_raises_runtimeerror_on_missing_file():
    """Single-soul loader raises typed RuntimeError, not raw OSError."""
    with tempfile.TemporaryDirectory() as td:
        with pytest.raises(RuntimeError, match=r"load_soul.*failed to read"):
            load_soul("nonexistent", pathlib.Path(td))


def test_load_soul_raises_runtimeerror_on_corrupt_json():
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        (tdp / "evil.soul.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(RuntimeError, match=r"load_soul.*failed to read"):
            load_soul("evil", tdp)


def test_load_soul_accepts_str_path():
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        _seed_good_souls(tdp)
        soul = load_soul("apex", td)  # str, not Path
        assert soul.id == "apex"


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
