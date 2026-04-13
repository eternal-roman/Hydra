"""
HYDRA Balance & Asset Conversion Test Suite
Validates staked asset detection, asset name normalization,
USD conversion, and engine balance initialization from exchange data.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import KrakenCLI, HydraAgent
from hydra_engine import HydraEngine


# ═══════════════════════════════════════════════════════════════
# TEST: Staked asset detection
# ═══════════════════════════════════════════════════════════════

class TestStakedAssets:
    def test_bonded_suffix_detected(self):
        assert KrakenCLI._is_staked("XBT.B") is True

    def test_staked_suffix_detected(self):
        assert KrakenCLI._is_staked("SOL.S") is True

    def test_margin_suffix_detected(self):
        assert KrakenCLI._is_staked("ETH.M") is True

    def test_plain_asset_not_staked(self):
        assert KrakenCLI._is_staked("XBT") is False

    def test_usdc_not_staked(self):
        assert KrakenCLI._is_staked("USDC") is False

    def test_asset_with_dot_in_middle_not_staked(self):
        """Only trailing suffixes count — 'B.XBT' should not match."""
        assert KrakenCLI._is_staked("B.XBT") is False

    def test_single_letter_asset_not_false_positive(self):
        """Asset name 'B' alone should not be considered staked."""
        assert KrakenCLI._is_staked("B") is False

    def test_empty_string_not_staked(self):
        assert KrakenCLI._is_staked("") is False


# ═══════════════════════════════════════════════════════════════
# TEST: Asset name normalization
# ═══════════════════════════════════════════════════════════════

class TestNormalizeAsset:
    def test_xxbt_normalizes_to_btc(self):
        assert KrakenCLI._normalize_asset("XXBT") == "BTC"

    def test_xbt_normalizes_to_btc(self):
        assert KrakenCLI._normalize_asset("XBT") == "BTC"

    def test_btc_passes_through(self):
        assert KrakenCLI._normalize_asset("BTC") == "BTC"

    def test_usdc_passes_through(self):
        assert KrakenCLI._normalize_asset("USDC") == "USDC"

    def test_zusdc_normalizes(self):
        assert KrakenCLI._normalize_asset("ZUSDC") == "USDC"

    def test_zusd_normalizes(self):
        assert KrakenCLI._normalize_asset("ZUSD") == "USD"

    def test_sol_passes_through(self):
        assert KrakenCLI._normalize_asset("SOL") == "SOL"

    def test_xsol_normalizes(self):
        assert KrakenCLI._normalize_asset("XSOL") == "SOL"

    def test_staked_suffix_stripped_then_normalized(self):
        """XBT.B → strip .B → XBT → normalize to BTC."""
        assert KrakenCLI._normalize_asset("XBT.B") == "BTC"

    def test_staked_xxbt_suffix_stripped_then_normalized(self):
        """XXBT.B → strip .B → XXBT → normalize to BTC."""
        assert KrakenCLI._normalize_asset("XXBT.B") == "BTC"

    def test_sol_staked_normalizes(self):
        assert KrakenCLI._normalize_asset("SOL.S") == "SOL"

    def test_unknown_asset_passes_through(self):
        assert KrakenCLI._normalize_asset("DOGE") == "DOGE"


# ═══════════════════════════════════════════════════════════════
# TEST: USD balance computation
# ═══════════════════════════════════════════════════════════════

class TestComputeBalanceUsd:
    """Tests _compute_balance_usd using a minimal HydraAgent with mocked engines."""

    def _make_agent(self):
        """Create a HydraAgent-like object with engines that have known prices."""
        agent = object.__new__(HydraAgent)
        agent.engines = {}

        # Create engines with known prices (no full init, just set prices)
        for pair, price in [("SOL/USDC", 130.0), ("BTC/USDC", 84000.0), ("SOL/BTC", 0.001547)]:
            engine = object.__new__(HydraEngine)
            engine.prices = [price]
            agent.engines[pair] = engine

        return agent

    def test_usdc_valued_at_one_dollar(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({"USDC": 500.0})
        assert result["total_usd"] == 500.0
        assert result["tradable_usd"] == 500.0
        assert result["staked_usd"] == 0

    def test_sol_converted_using_engine_price(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({"SOL": 10.0})
        assert result["total_usd"] == 1300.0  # 10 * 130

    def test_btc_converted_using_engine_price(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({"BTC": 1.0})
        assert result["total_usd"] == 84000.0

    def test_staked_excluded_from_tradable(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({
            "BTC": 1.0,
            "BTC.B": 0.5,
            "USDC": 100.0,
        })
        # Total includes staked: 84000 + 42000 + 100 = 126100
        assert result["total_usd"] == 126100.0
        # Tradable excludes staked: 84000 + 100 = 84100
        assert result["tradable_usd"] == 84100.0
        # Staked: 0.5 * 84000 = 42000
        assert result["staked_usd"] == 42000.0

    def test_multiple_staked_assets(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({
            "BTC.B": 0.5,
            "SOL.S": 5.0,
        })
        expected_staked = 0.5 * 84000.0 + 5.0 * 130.0  # 42650
        assert result["staked_usd"] == expected_staked
        assert result["tradable_usd"] == 0

    def test_unknown_asset_valued_at_zero(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({"DOGE": 1000.0})
        assert result["total_usd"] == 0
        # Asset still appears in breakdown
        assert len(result["assets"]) == 1
        assert result["assets"][0]["usd_value"] == 0

    def test_empty_balance_returns_zeros(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({})
        assert result["total_usd"] == 0
        assert result["tradable_usd"] == 0
        assert result["staked_usd"] == 0
        assert result["assets"] == []

    def test_assets_sorted_tradable_first(self):
        agent = self._make_agent()
        result = agent._compute_balance_usd({
            "BTC.B": 0.5,
            "USDC": 100.0,
            "SOL": 10.0,
        })
        assets = result["assets"]
        # Tradable assets first (SOL, USDC alphabetical), then staked (BTC.B)
        assert assets[0]["asset"] == "SOL"
        assert assets[0]["staked"] is False
        assert assets[1]["asset"] == "USDC"
        assert assets[1]["staked"] is False
        assert assets[2]["asset"] == "BTC.B"
        assert assets[2]["staked"] is True

    def test_xxbt_normalized_for_price_lookup(self):
        """Kraken returns 'XXBT' — should normalize and find BTC price."""
        agent = self._make_agent()
        result = agent._compute_balance_usd({"XXBT": 1.0})
        assert result["total_usd"] == 84000.0

    def test_staked_xxbt_normalized_for_price_lookup(self):
        """XXBT.B should strip .B, normalize XXBT→BTC, use BTC price."""
        agent = self._make_agent()
        result = agent._compute_balance_usd({"XXBT.B": 1.0})
        assert result["total_usd"] == 84000.0
        assert result["staked_usd"] == 84000.0
        assert result["tradable_usd"] == 0


# ═══════════════════════════════════════════════════════════════
# TEST: Asset price derivation
# ═══════════════════════════════════════════════════════════════

class TestGetAssetPrices:
    def test_usdc_always_one(self):
        agent = object.__new__(HydraAgent)
        agent.engines = {}
        prices = agent._get_asset_prices()
        assert prices["USDC"] == 1.0
        assert prices["USD"] == 1.0

    def test_prices_from_usdc_pairs(self):
        agent = object.__new__(HydraAgent)
        agent.engines = {}
        for pair, price in [("SOL/USDC", 130.0), ("BTC/USDC", 84000.0)]:
            engine = object.__new__(HydraEngine)
            engine.prices = [price]
            agent.engines[pair] = engine
        prices = agent._get_asset_prices()
        assert prices["SOL"] == 130.0
        assert prices["BTC"] == 84000.0

    def test_btc_derived_from_sol_btc_when_no_direct_pair(self):
        """If BTC/USDC engine has no prices, derive BTC from SOL/USDC and SOL/BTC."""
        agent = object.__new__(HydraAgent)
        agent.engines = {}

        sol_usdc = object.__new__(HydraEngine)
        sol_usdc.prices = [130.0]
        agent.engines["SOL/USDC"] = sol_usdc

        sol_btc = object.__new__(HydraEngine)
        sol_btc.prices = [0.001547]  # 1 SOL = 0.001547 BTC
        agent.engines["SOL/BTC"] = sol_btc

        btc_usdc = object.__new__(HydraEngine)
        btc_usdc.prices = []  # No data yet
        agent.engines["BTC/USDC"] = btc_usdc

        prices = agent._get_asset_prices()
        # BTC = SOL_USD / SOL_BTC = 130 / 0.001547 ≈ 84034
        assert abs(prices["BTC"] - 130.0 / 0.001547) < 1.0

    def test_empty_engine_prices_skipped(self):
        agent = object.__new__(HydraAgent)
        engine = object.__new__(HydraEngine)
        engine.prices = []
        agent.engines = {"SOL/USDC": engine}
        prices = agent._get_asset_prices()
        assert "SOL" not in prices


# ═══════════════════════════════════════════════════════════════
# TEST: Engine balance initialization from exchange
# ═══════════════════════════════════════════════════════════════

class TestEngineBalanceInit:
    def test_engine_balance_overwritten_by_tradable_balance(self):
        """Simulates the startup flow: engines start with CLI default,
        then get overwritten with real exchange balance."""
        # Create engines with default $100 balance (as CLI arg would)
        engines = {}
        pairs = ["SOL/USDC", "BTC/USDC", "SOL/BTC"]
        for pair in pairs:
            engine = HydraEngine(initial_balance=33.33, asset=pair)
            engines[pair] = engine

        # Simulate what run() does: overwrite with real balance
        tradable_usd = 1500.0
        per_pair = tradable_usd / len(pairs)
        for pair in pairs:
            engine = engines[pair]
            engine.initial_balance = per_pair
            engine.balance = per_pair
            engine.peak_equity = per_pair

        # Verify all engines updated
        for pair in pairs:
            assert engines[pair].balance == 500.0
            assert engines[pair].initial_balance == 500.0
            assert engines[pair].peak_equity == 500.0

    def test_engine_position_sizing_uses_real_balance(self):
        """With real balance ($500), position sizer should produce tradeable sizes."""
        engine = HydraEngine(initial_balance=500.0, asset="SOL/USDC")
        # At confidence 0.7, Kelly edge = 0.4, quarter-Kelly = 0.10
        # Position value = 0.10 * 500 = $50, size = 50/130 ≈ 0.38 SOL
        size = engine.sizer.calculate(0.7, engine.balance, 130.0, "SOL/USDC")
        assert size > 0, "Real balance should produce tradeable position size"
        assert size >= 0.1, "Position should meet SOL minimum order size (0.1)"

    def test_tiny_balance_produces_zero_size(self):
        """With a very small balance, position sizer can't meet exchange minimums."""
        engine = HydraEngine(initial_balance=10.0, asset="SOL/USDC")
        # At confidence 0.7: edge=0.4, quarter-Kelly=0.10, value=$1.00,
        # size = 1.0/130 ≈ 0.008 — below SOL ordermin of 0.02
        size = engine.sizer.calculate(0.7, engine.balance, 130.0, "SOL/USDC")
        assert size == 0, "Tiny balance should fail to meet minimum order size"

    def test_btc_quoted_pair_balance_converted_from_usd(self):
        """SOL/BTC engine balance must be in BTC, not USD.
        Without conversion, position sizes are ~60,000x too large because
        the sizer divides a USD balance by a BTC-denominated price."""
        agent = object.__new__(HydraAgent)
        agent.pairs = ["SOL/USDC", "BTC/USDC", "SOL/BTC"]
        agent.engines = {}
        for pair, price in [("SOL/USDC", 130.0), ("BTC/USDC", 60000.0), ("SOL/BTC", 0.002167)]:
            engine = HydraEngine(initial_balance=100.0, asset=pair)
            engine.prices = [price]
            agent.engines[pair] = engine

        per_pair_usd = 100.0
        agent._set_engine_balances(per_pair_usd)

        # SOL/USDC and BTC/USDC: balance stays in USD
        assert agent.engines["SOL/USDC"].balance == 100.0
        assert agent.engines["BTC/USDC"].balance == 100.0

        # SOL/BTC: balance converted from $100 USD to BTC
        btc_balance = agent.engines["SOL/BTC"].balance
        expected_btc = 100.0 / 60000.0  # ~0.001667 BTC
        assert abs(btc_balance - expected_btc) < 1e-8, \
            f"SOL/BTC balance should be ~{expected_btc:.8f} BTC, got {btc_balance:.8f}"

    def test_btc_quoted_pair_produces_sane_position_size(self):
        """After balance conversion, SOL/BTC position size should be reasonable,
        not the inflated 4000+ SOL that caused the 'api' failures."""
        agent = object.__new__(HydraAgent)
        agent.pairs = ["SOL/USDC", "BTC/USDC", "SOL/BTC"]
        agent.engines = {}
        for pair, price in [("SOL/USDC", 130.0), ("BTC/USDC", 60000.0), ("SOL/BTC", 0.002167)]:
            engine = HydraEngine(initial_balance=100.0, asset=pair)
            engine.prices = [price]
            agent.engines[pair] = engine

        agent._set_engine_balances(100.0)

        # Now calculate position size for SOL/BTC
        engine = agent.engines["SOL/BTC"]
        size = engine.sizer.calculate(0.58, engine.balance, 0.002167, "SOL/BTC")

        # With ~0.00167 BTC balance, position should be small (< 1 SOL),
        # NOT the 4000+ SOL that was happening before
        assert size < 10, f"Position size should be small, got {size:.4f} SOL"

    def test_btc_quoted_no_conversion_without_btc_price(self):
        """If BTC price unavailable, SOL/BTC balance stays in USD (safe fallback)."""
        agent = object.__new__(HydraAgent)
        agent.pairs = ["SOL/BTC"]
        agent.engines = {}
        engine = HydraEngine(initial_balance=100.0, asset="SOL/BTC")
        engine.prices = [0.002167]
        agent.engines["SOL/BTC"] = engine

        # No BTC/USDC or SOL/USDC engines → can't derive BTC price
        agent._set_engine_balances(100.0)
        assert agent.engines["SOL/BTC"].balance == 100.0  # Unchanged (no price to convert)

    def test_btc_quoted_resumed_with_position_pnl_sane(self):
        """Resumed SOL/BTC engine with existing position must NOT show insane P&L.
        Bug: _set_engine_balances set initial_balance to converted cash only,
        ignoring the position value — causing equity >> initial_balance."""
        agent = object.__new__(HydraAgent)
        agent.pairs = ["SOL/USDC", "BTC/USDC", "SOL/BTC"]
        agent.engines = {}
        for pair, price in [("SOL/USDC", 130.0), ("BTC/USDC", 60000.0), ("SOL/BTC", 0.002167)]:
            engine = HydraEngine(initial_balance=100.0, asset=pair)
            engine.prices = [price]
            agent.engines[pair] = engine

        # Simulate resumed position: 0.5 SOL held in SOL/BTC engine
        agent.engines["SOL/BTC"].position.size = 0.5
        agent.engines["SOL/BTC"].position.avg_entry = 0.002100

        agent._set_engine_balances(100.0)

        engine = agent.engines["SOL/BTC"]
        current_price = 0.002167
        equity = engine.balance + engine.position.size * current_price
        pnl_pct = ((equity - engine.initial_balance) / engine.initial_balance * 100)

        # P&L must be near 0%, NOT +239000%
        assert abs(pnl_pct) < 5.0, \
            f"P&L should be near 0% after balance reset, got {pnl_pct:+.2f}%"
        # initial_balance must include position value
        assert engine.initial_balance > engine.balance, \
            "initial_balance should include position value, not just cash"

    def test_equity_history_clean_after_balance_reset(self):
        """Engine that only had candles ingested (no ticks) should have empty equity history."""
        engine = HydraEngine(initial_balance=33.33, asset="SOL/USDC")
        # Simulate warmup: ingest candles without ticking
        for i in range(50):
            engine.ingest_candle({
                "open": 130 + i * 0.1, "high": 131 + i * 0.1,
                "low": 129 + i * 0.1, "close": 130.5 + i * 0.1,
                "volume": 1000, "time": 1000 + i * 300,
            })
        # Equity history should be empty (ingest_candle doesn't call tick)
        assert len(engine.equity_history) == 0
        # Now reset balance like startup does
        engine.initial_balance = 500.0
        engine.balance = 500.0
        engine.peak_equity = 500.0
        # First tick should use new balance
        state = engine.tick()
        assert state["portfolio"]["equity"] == 500.0 or state["portfolio"]["equity"] > 490


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    """Simple test runner — no pytest dependency needed."""
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestStakedAssets,
        TestNormalizeAsset,
        TestComputeBalanceUsd,
        TestGetAssetPrices,
        TestEngineBalanceInit,
    ]

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            test_name = f"{cls.__name__}.{method_name}"
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  PASS  {test_name}")
            except AssertionError as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name}: {e}")
            except Exception as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name} (error): {e}")

    print(f"\n  {'='*60}")
    print(f"  Balance Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
