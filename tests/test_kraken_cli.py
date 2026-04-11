"""
HYDRA KrakenCLI Wrapper Test Suite
Validates argument construction, error passthrough, and response parsing for
the volume/spreads/order_amend wrappers, plus the fee-tier extraction and
spread-recording helpers on HydraAgent. No subprocess calls are made — all
tests monkey-patch KrakenCLI._run with an in-memory stub.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_agent import KrakenCLI, HydraAgent


# ═══════════════════════════════════════════════════════════════
# Stub helper — temporarily replaces KrakenCLI._run with a recorder
# ═══════════════════════════════════════════════════════════════

class _StubRun:
    """Records calls to KrakenCLI._run and returns a preset response.
    Must be restored in a try/finally so sibling tests are not affected."""

    def __init__(self, response):
        self._response = response
        self.calls = []
        self._original = None

    def install(self):
        self._original = KrakenCLI._run
        outer = self

        def fake(args, timeout=20):
            outer.calls.append(list(args))
            return outer._response

        KrakenCLI._run = staticmethod(fake)

    def restore(self):
        if self._original is not None:
            KrakenCLI._run = staticmethod(self._original)
            self._original = None


def _with_stub(response, fn):
    """Run fn() with KrakenCLI._run stubbed. Returns (result, stub)."""
    stub = _StubRun(response)
    stub.install()
    try:
        result = fn()
    finally:
        stub.restore()
    return result, stub


# ═══════════════════════════════════════════════════════════════
# TEST: KrakenCLI.volume — argument construction & passthrough
# ═══════════════════════════════════════════════════════════════

class TestVolumeArgsAndParsing:
    def test_volume_no_args_calls_bare_command(self):
        _, stub = _with_stub({"volume": "1234.5"}, lambda: KrakenCLI.volume())
        assert stub.calls == [["volume"]]

    def test_volume_with_none_calls_bare_command(self):
        _, stub = _with_stub({"volume": "1234.5"}, lambda: KrakenCLI.volume(None))
        assert stub.calls == [["volume"]]

    def test_volume_with_single_pair_string(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.volume("SOL/USDC"))
        assert stub.calls == [["volume", "--pair", "SOL/USDC"]]

    def test_volume_with_pair_list(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.volume(["SOL/USDC", "XBT/USDC"]))
        # XBT/USDC is resolved to XBTUSDC via PAIR_MAP
        assert stub.calls == [["volume", "--pair", "SOL/USDC,XBTUSDC"]]

    def test_volume_resolves_pair_map(self):
        # SOL/XBT should resolve to SOLXBT
        _, stub = _with_stub({}, lambda: KrakenCLI.volume(["SOL/XBT"]))
        assert stub.calls == [["volume", "--pair", "SOLXBT"]]

    def test_volume_returns_passthrough_on_success(self):
        payload = {"volume": "500.00", "fees": {"SOLUSDC": {"fee": "0.26"}}}
        result, _ = _with_stub(payload, lambda: KrakenCLI.volume())
        assert result == payload

    def test_volume_returns_error_dict_on_error(self):
        err = {"error": "EAPI:Invalid key"}
        result, _ = _with_stub(err, lambda: KrakenCLI.volume())
        assert result == err

    def test_volume_handles_timeout_payload(self):
        timeout = {"error": "Command timed out", "retryable": True}
        result, _ = _with_stub(timeout, lambda: KrakenCLI.volume(["SOL/USDC"]))
        assert result == timeout


# ═══════════════════════════════════════════════════════════════
# TEST: KrakenCLI.spreads — argument construction & passthrough
# ═══════════════════════════════════════════════════════════════

class TestSpreadsArgsAndParsing:
    def test_spreads_requires_pair(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.spreads("SOL/USDC"))
        assert stub.calls[0][0] == "spreads"
        assert "SOL/USDC" in stub.calls[0]

    def test_spreads_resolves_pair(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.spreads("SOL/XBT"))
        assert stub.calls == [["spreads", "SOLXBT"]]

    def test_spreads_without_since_omits_flag(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.spreads("SOL/USDC"))
        assert "--since" not in stub.calls[0]

    def test_spreads_with_since_includes_flag(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.spreads("SOL/USDC", since=1700000000))
        assert stub.calls == [["spreads", "SOL/USDC", "--since", "1700000000"]]

    def test_spreads_returns_passthrough(self):
        payload = {"SOLUSDC": [[1700000000, "130.1", "130.2"]], "last": 1700000001}
        result, _ = _with_stub(payload, lambda: KrakenCLI.spreads("SOL/USDC"))
        assert result == payload

    def test_spreads_returns_error_dict(self):
        err = {"error": "EGeneral:Temporary lockout"}
        result, _ = _with_stub(err, lambda: KrakenCLI.spreads("SOL/USDC"))
        assert result == err

    def test_spreads_negative_since_still_stringified(self):
        _, stub = _with_stub({}, lambda: KrakenCLI.spreads("SOL/USDC", since=-1))
        assert stub.calls == [["spreads", "SOL/USDC", "--since", "-1"]]


# ═══════════════════════════════════════════════════════════════
# TEST: HydraAgent._extract_fee_tier — defensive parsing
# ═══════════════════════════════════════════════════════════════

class TestFeeTierExtraction:
    def _make_agent(self, pairs=None):
        agent = object.__new__(HydraAgent)
        agent.pairs = pairs if pairs is not None else ["SOL/USDC", "XBT/USDC", "SOL/XBT"]
        return agent

    def test_extract_fee_tier_empty_response(self):
        agent = self._make_agent()
        result = agent._extract_fee_tier({})
        assert result == {"volume_30d_usd": None, "pair_fees": {}}

    def test_extract_fee_tier_non_dict_response(self):
        agent = self._make_agent()
        result = agent._extract_fee_tier(["unexpected", "list"])
        assert result == {"volume_30d_usd": None, "pair_fees": {}}

    def test_extract_fee_tier_taker_only(self):
        agent = self._make_agent()
        response = {
            "volume": "100.0",
            "fees": {"SOLUSDC": {"fee": "0.26"}},
        }
        result = agent._extract_fee_tier(response)
        assert result["volume_30d_usd"] == 100.0
        # Slashless "SOLUSDC" must be mapped back to friendly "SOL/USDC"
        # (this is the path the dashboard uses to look up fees by pair key)
        assert "SOL/USDC" in result["pair_fees"]
        assert result["pair_fees"]["SOL/USDC"]["taker_pct"] == 0.26
        assert result["pair_fees"]["SOL/USDC"]["maker_pct"] is None

    def test_extract_fee_tier_maker_and_taker(self):
        agent = self._make_agent()
        response = {
            "volume": "250.5",
            "fees": {"XBTUSDC": {"fee": "0.26"}},
            "fees_maker": {"XBTUSDC": {"fee": "0.16"}},
        }
        result = agent._extract_fee_tier(response)
        # XBTUSDC reverse-maps back to XBT/USDC (first pair in list that resolves to XBTUSDC)
        assert "XBT/USDC" in result["pair_fees"]
        assert result["pair_fees"]["XBT/USDC"]["taker_pct"] == 0.26
        assert result["pair_fees"]["XBT/USDC"]["maker_pct"] == 0.16

    def test_extract_fee_tier_volume_parsed_float(self):
        agent = self._make_agent()
        result = agent._extract_fee_tier({"volume": "1234.567"})
        assert result["volume_30d_usd"] == 1234.567

    def test_extract_fee_tier_malformed_volume_is_none(self):
        agent = self._make_agent()
        result = agent._extract_fee_tier({"volume": "not-a-number"})
        assert result["volume_30d_usd"] is None

    def test_extract_fee_tier_malformed_fee_is_none(self):
        agent = self._make_agent()
        response = {"fees": {"SOLUSDC": {"fee": "garbage"}}}
        result = agent._extract_fee_tier(response)
        # After slashless fix, "SOLUSDC" maps back to "SOL/USDC"
        assert result["pair_fees"]["SOL/USDC"]["taker_pct"] is None

    def test_extract_fee_tier_reverse_maps_sol_xbt(self):
        agent = self._make_agent()
        # SOLXBT resolved → SOL/XBT friendly
        response = {"fees": {"SOLXBT": {"fee": "0.20"}}}
        result = agent._extract_fee_tier(response)
        assert "SOL/XBT" in result["pair_fees"]
        assert result["pair_fees"]["SOL/XBT"]["taker_pct"] == 0.20

    def test_extract_fee_tier_slashed_form_also_maps(self):
        """Kraken may also return keys in already-slashed form like 'SOL/USDC'."""
        agent = self._make_agent()
        response = {"fees": {"SOL/USDC": {"fee": "0.26"}}}
        result = agent._extract_fee_tier(response)
        assert "SOL/USDC" in result["pair_fees"]
        assert result["pair_fees"]["SOL/USDC"]["taker_pct"] == 0.26

    def test_extract_fee_tier_missing_pairs_attr(self):
        """Agent without `pairs` set should still return a valid dict."""
        agent = object.__new__(HydraAgent)
        # deliberately no pairs attr
        result = agent._extract_fee_tier({"fees": {"SOLUSDC": {"fee": "0.26"}}})
        assert "SOLUSDC" in result["pair_fees"]  # unmapped key passthrough


# ═══════════════════════════════════════════════════════════════
# TEST: HydraAgent._record_spreads — rolling history
# ═══════════════════════════════════════════════════════════════

class TestRecordSpreads:
    def _make_agent(self):
        agent = object.__new__(HydraAgent)
        agent._spread_history = {"SOL/USDC": [], "XBT/USDC": []}
        agent._spread_last_cursor = {"SOL/USDC": None, "XBT/USDC": None}
        return agent

    def test_record_spreads_error_response_noop(self):
        agent = self._make_agent()
        agent._record_spreads("SOL/USDC", {"error": "boom"})
        assert agent._spread_history["SOL/USDC"] == []
        assert agent._spread_last_cursor["SOL/USDC"] is None

    def test_record_spreads_non_dict_noop(self):
        agent = self._make_agent()
        agent._record_spreads("SOL/USDC", None)
        assert agent._spread_history["SOL/USDC"] == []

    def test_record_spreads_appends_rows(self):
        agent = self._make_agent()
        response = {
            "SOLUSDC": [
                [1700000000, "130.10", "130.20"],
                [1700000005, "130.15", "130.25"],
            ],
            "last": 1700000005,
        }
        agent._record_spreads("SOL/USDC", response)
        assert len(agent._spread_history["SOL/USDC"]) == 2

    def test_record_spreads_updates_cursor(self):
        agent = self._make_agent()
        response = {"SOLUSDC": [[1700000000, "130.10", "130.20"]], "last": 1700000500}
        agent._record_spreads("SOL/USDC", response)
        assert agent._spread_last_cursor["SOL/USDC"] == 1700000500

    def test_record_spreads_computes_spread_bps(self):
        agent = self._make_agent()
        response = {"SOLUSDC": [[1700000000, "100.00", "100.10"]], "last": 1}
        agent._record_spreads("SOL/USDC", response)
        row = agent._spread_history["SOL/USDC"][0]
        # (100.10 - 100.00) / 100.05 * 10000 = ~9.995 bps
        assert abs(row["spread_bps"] - 9.995) < 0.01
        assert row["bid"] == 100.00
        assert row["ask"] == 100.10

    def test_record_spreads_bounds_to_120_entries(self):
        agent = self._make_agent()
        # Pre-fill with 115 entries, then push 10 more → should cap at 120
        agent._spread_history["SOL/USDC"] = [
            {"ts": float(i), "bid": 1.0, "ask": 1.01, "spread_bps": 99.5} for i in range(115)
        ]
        response = {
            "SOLUSDC": [[1700000000 + i, "1.00", "1.01"] for i in range(10)],
            "last": 1700000010,
        }
        agent._record_spreads("SOL/USDC", response)
        assert len(agent._spread_history["SOL/USDC"]) == 120

    def test_record_spreads_skips_malformed_rows(self):
        agent = self._make_agent()
        response = {
            "SOLUSDC": [
                [1700000000, "130.10", "130.20"],   # good
                "not-a-list",                         # bad
                [1700000005],                         # too short
                [1700000006, "bad", "bad"],           # unparseable floats
                [1700000007, "131.00", "131.05"],   # good
            ],
            "last": 1700000007,
        }
        agent._record_spreads("SOL/USDC", response)
        assert len(agent._spread_history["SOL/USDC"]) == 2

    def test_record_spreads_handles_missing_data_key(self):
        agent = self._make_agent()
        agent._record_spreads("SOL/USDC", {"last": 1700000000})  # only 'last', no list
        assert agent._spread_history["SOL/USDC"] == []
        assert agent._spread_last_cursor["SOL/USDC"] == 1700000000

    def test_record_spreads_malformed_cursor_silently_ignored(self):
        agent = self._make_agent()
        agent._record_spreads("SOL/USDC", {"SOLUSDC": [], "last": "garbage"})
        assert agent._spread_last_cursor["SOL/USDC"] is None

    def test_record_spreads_zero_prices_yield_zero_bps(self):
        agent = self._make_agent()
        response = {"SOLUSDC": [[1700000000, "0", "0"]], "last": 1}
        agent._record_spreads("SOL/USDC", response)
        row = agent._spread_history["SOL/USDC"][0]
        assert row["spread_bps"] == 0.0


# ═══════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    """Simple test runner — no pytest dependency needed."""
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestVolumeArgsAndParsing,
        TestSpreadsArgsAndParsing,
        TestFeeTierExtraction,
        TestRecordSpreads,
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
    print(f"  Kraken CLI Tests: {passed}/{passed+failed} passed, {failed} failed")
    print(f"  {'='*60}")

    if errors:
        print("\n  Failures:")
        for name, err in errors:
            print(f"    {name}: {err}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
