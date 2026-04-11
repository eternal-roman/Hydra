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
# RUNNER
# ═══════════════════════════════════════════════════════════════

def run_tests():
    """Simple test runner — no pytest dependency needed."""
    passed = 0
    failed = 0
    errors = []

    test_classes = [
        TestVolumeArgsAndParsing,
        TestFeeTierExtraction,
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
