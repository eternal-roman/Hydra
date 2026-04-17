"""Router tests \u2014 per-intent per-companion model selection."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.router import Router


def test_every_companion_intent_resolves():
    r = Router()
    intents = ["greeting", "small_talk", "market_state_query",
               "trade_proposal", "teaching_explanation", "banter_humor"]
    for cid in ("athena", "apex", "broski"):
        for intent in intents:
            d = r.pick(cid, intent)
            assert d.provider in ("anthropic", "xai")
            assert d.model_id
            assert d.max_tokens > 0
            assert 0.0 <= d.temperature <= 1.0


def test_athena_teaching_uses_claude():
    r = Router()
    d = r.pick("athena", "teaching_explanation")
    assert d.provider == "anthropic"


def test_broski_banter_uses_xai():
    r = Router()
    d = r.pick("broski", "banter_humor", seed=0)
    assert d.provider == "xai"


def test_broski_serious_mode_lowers_temperature():
    r = Router()
    normal = r.pick("broski", "trade_proposal", serious_mode=False)
    serious = r.pick("broski", "trade_proposal", serious_mode=True)
    assert serious.temperature < normal.temperature


def test_fallback_chain_returns_other_provider():
    r = Router()
    primary = r.pick("athena", "teaching_explanation")
    fb = r.fallback(primary)
    assert fb is not None
    assert (fb.provider, fb.model_id) != (primary.provider, primary.model_id)


def test_safety_cap_lookup():
    r = Router()
    assert r.safety_cap("athena", "max_trades_per_day") == 4
    assert r.safety_cap("apex", "max_trades_per_day") == 6
    assert r.safety_cap("broski", "max_trades_per_day") == 9


def test_daily_budget_nonzero():
    r = Router()
    for cid in ("athena", "apex", "broski"):
        assert r.daily_budget_usd(cid) > 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all router tests passed")
