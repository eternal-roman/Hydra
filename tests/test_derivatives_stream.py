"""Unit tests for hydra_derivatives_stream.DerivativesStream.

Covers: regime classification, delta computation, snapshot lifecycle,
basis parsing, synthetic SOL/BTC derivation, and the spot-only
invariant (no order-placement imports exist in the module).
"""
from collections import deque
import os
import time

import pytest

from hydra_derivatives_stream import (
    DerivativesSnapshot,
    DerivativesStream,
    SPOT_TO_DERIVATIVES,
    _delta_pct,
    _maybe_float,
    _prune_before,
)


# ─── Helpers ─────────────────────────────────────────────────


def test_maybe_float_handles_none_and_strings():
    assert _maybe_float(None) is None
    assert _maybe_float("1.5") == 1.5
    assert _maybe_float(2) == 2.0
    assert _maybe_float("not-a-number") is None
    assert _maybe_float([1, 2]) is None


def test_delta_pct_returns_none_when_empty_history():
    assert _delta_pct(deque(), 100.0, 50.0) is None
    assert _delta_pct(deque([(0.0, 100.0)]), 100.0, None) is None


def test_delta_pct_returns_none_when_chosen_baseline_zero():
    # target_ts=25 falls at or before the t=0 sample → baseline val=0 → None
    hist = deque([(0.0, 0.0), (50.0, 10.0)])
    assert _delta_pct(hist, 25.0, 20.0) is None


def test_delta_pct_picks_sample_at_or_before_target():
    # Samples at t=0, 60, 120; target_ts=90 → closest is t=60 val=110
    hist = deque([(0.0, 100.0), (60.0, 110.0), (120.0, 130.0)])
    assert _delta_pct(hist, 90.0, 130.0) == round(100.0 * (130 - 110) / 110, 2)


def test_prune_before_strips_old_entries():
    hist = deque([(0.0, 1.0), (50.0, 2.0), (100.0, 3.0), (200.0, 4.0)])
    _prune_before(hist, 60.0)
    assert list(hist) == [(100.0, 3.0), (200.0, 4.0)]


# ─── Regime classifier ───────────────────────────────────────


@pytest.fixture
def stream():
    return DerivativesStream(pairs=["BTC/USDC"])


@pytest.mark.parametrize(
    "oi_delta,px_delta,expected",
    [
        (1.5, 1.0, "trend_confirm_long"),      # OI↑ + Px↑
        (1.5, -1.0, "trend_confirm_short"),    # OI↑ + Px↓
        (-1.5, 1.0, "short_squeeze"),          # OI↓ + Px↑
        (-1.5, -1.0, "liquidation_cascade"),   # OI↓ + Px↓
        (0.1, 0.1, "balanced"),                # both under threshold
        (0.1, 2.0, "balanced"),                # OI under threshold
        (2.0, 0.1, "balanced"),                # Px under threshold
        (None, 1.0, "unknown"),
        (1.0, None, "unknown"),
    ],
)
def test_classify_oi_price_regime(stream, oi_delta, px_delta, expected):
    assert stream._classify_oi_price_regime(oi_delta, px_delta) == expected


# ─── Snapshot lifecycle ──────────────────────────────────────


def test_stream_instantiates_only_configured_pairs():
    s = DerivativesStream(pairs=["BTC/USDC", "SOL/USDC", "SOL/BTC", "ETH/USDC"])
    # ETH/USDC is not in SPOT_TO_DERIVATIVES and must be dropped
    assert set(s.pairs) == {"BTC/USDC", "SOL/USDC", "SOL/BTC"}


def test_latest_returns_none_for_unknown_pair():
    s = DerivativesStream(pairs=["BTC/USDC"])
    assert s.latest("ETH/USDC") is None


def test_latest_returns_initial_snapshot_with_nones(stream):
    snap = stream.latest("BTC/USDC")
    assert snap is not None
    assert snap.pair == "BTC/USDC"
    assert snap.perp_symbol == "PF_XBTUSD"
    assert snap.funding_bps_8h is None
    assert snap.open_interest is None
    assert snap.staleness_s == float("inf")


def test_populate_from_ticker_updates_snapshot(stream):
    snap = stream._snapshots["BTC/USDC"]
    tick = {
        "symbol": "PF_XBTUSD",
        "markPrice": "95000.5",
        "indexPrice": "94990.0",
        "fundingRate": "0.00005",          # 0.5 bps / 8h
        "fundingRatePrediction": "0.00004",
        "openInterest": "12345.67",
    }
    now = time.time()
    stream._populate_from_ticker(snap, tick, now)
    assert snap.mark_price == 95000.5
    assert snap.spot_price == 94990.0
    assert snap.funding_bps_8h == 0.5
    assert snap.funding_predicted_bps == 0.4
    assert snap.open_interest == 12345.67
    assert snap.last_updated_ts == now
    assert snap.fetch_errors == 0


def test_oi_delta_computes_against_history(stream):
    snap = stream._snapshots["BTC/USDC"]
    base = time.time() - 3700  # slightly over 1h ago
    # Seed history: OI went from 10000 (1h ago) to 10500 (now) → +5%
    t0 = {"symbol": "PF_XBTUSD", "markPrice": "95000", "indexPrice": "95000",
          "fundingRate": "0", "fundingRatePrediction": "0", "openInterest": "10000"}
    stream._populate_from_ticker(snap, t0, base)
    t1 = {"symbol": "PF_XBTUSD", "markPrice": "95500", "indexPrice": "95500",
          "fundingRate": "0", "fundingRatePrediction": "0", "openInterest": "10500"}
    stream._populate_from_ticker(snap, t1, time.time())
    assert snap.oi_delta_1h_pct == 5.0
    assert snap.oi_price_regime == "trend_confirm_long"


def test_synthetic_sol_btc_computes_from_usd_perps(stream):
    s = DerivativesStream(pairs=["SOL/BTC"])
    snap = s._snapshots["SOL/BTC"]
    sol = {"fundingRate": "0.0001", "markPrice": "150.0"}  # 1 bps SOL funding
    btc = {"fundingRate": "0.00005", "markPrice": "60000.0"}  # 0.5 bps BTC funding
    s._populate_synthetic(snap, sol, btc, time.time())
    # Delta funding in bps: (0.0001 - 0.00005) * 10000 = 0.5
    assert snap.funding_bps_8h == 0.5
    # Ratio: 150 / 60000 = 0.0025
    assert snap.mark_price == 0.0025


# ─── Basis parsing ───────────────────────────────────────────


def test_find_quarterly_returns_earliest(stream):
    by_symbol = {
        "PF_XBTUSD": {},
        "PI_XBTUSD_260927": {},
        "PI_XBTUSD_260328": {},   # earliest
        "PI_XBTUSD_260627": {},
        "PI_SOLUSD_260328": {},
    }
    assert stream._find_quarterly(by_symbol, "PI_XBTUSD") == "PI_XBTUSD_260328"


def test_find_quarterly_returns_none_when_no_match(stream):
    assert stream._find_quarterly({"PF_XBTUSD": {}}, "PI_XBTUSD") is None
    assert stream._find_quarterly({}, None) is None


def test_compute_basis_annualizes_premium(stream):
    snap = stream._snapshots["BTC/USDC"]
    # 30 days to expiry → 2% premium → 24% APR
    import datetime
    expiry_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    suffix = expiry_dt.strftime("%y%m%d")
    q_symbol = f"PI_XBTUSD_{suffix}"
    perp_tick = {"markPrice": "100.0"}
    q_tick = {"markPrice": "102.0"}
    stream._compute_basis(snap, perp_tick, q_tick, q_symbol, time.time())
    # Expected: (102 - 100) / 100 = 0.02 → 0.02 * 365/30 * 100 ≈ 24.33%
    assert snap.basis_apr_pct is not None
    assert 23.0 < snap.basis_apr_pct < 26.0


# ─── Spot-only invariant (meta-test) ─────────────────────────


def test_module_contains_no_order_placement_calls():
    """Verifies the hard invariant: hydra_derivatives_stream.py must
    never place orders on Kraken Futures. We grep the source for any
    order-placement call patterns that would indicate a bug."""
    path = os.path.join(
        os.path.dirname(__file__), "..", "hydra_derivatives_stream.py"
    )
    src = open(path, encoding="utf-8").read()
    forbidden_patterns = [
        "sendOrder",
        "sendorder",
        "api_key",           # no auth credentials belong here
        "apiKey",
        "Authent",
        "editOrder",
        "cancelOrder",
        "/sendorder",
        "/sendOrder",
    ]
    for pat in forbidden_patterns:
        assert pat not in src, (
            f"SPOT-ONLY INVARIANT VIOLATED: '{pat}' appears in "
            f"hydra_derivatives_stream.py. This module must stay read-only."
        )
