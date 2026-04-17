"""UTC-midnight rollover tests \u2014 daily trades + cost counters both
zero out when the day key flips."""
import sys
import pathlib
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.coordinator import CompanionCoordinator
import hydra_companions.config as cfg
import hydra_companions.companion as comp_mod


class _BC:
    latest_state = {}
    def broadcast_message(self, *a, **kw): pass


class _Agent:
    broadcaster = _BC()
    engines = {}
    _last_kraken_status = "online"


def test_rollover_clears_daily_trades_and_costs():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        cfg.TRANSCRIPTS_DIR = tmp
        comp_mod.TRANSCRIPTS_DIR = tmp
        coord = CompanionCoordinator(_Agent())
        # Seed state as if some trades and costs already landed today.
        coord._daily_trades[("local", "apex")] = 3
        coord._daily_costs[("local", "apex")] = 1.25
        coord._alert_fired.add(("local", "apex"))
        # Force a day change.
        coord._day_key = "1999-01-01"
        coord._maybe_rollover()
        assert coord._daily_trades == {}
        assert coord._daily_costs == {}
        assert coord._alert_fired == set()
        # Day key advanced.
        assert coord._day_key != "1999-01-01"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all rollover tests passed")
