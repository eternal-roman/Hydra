"""Hydra pair registry — single source of truth for pair metadata.

WHY THIS MODULE EXISTS
──────────────────────
Pre-v2.19, "USDC" appeared 1048 times across 70 files because pair
identity was encoded as bare string literals scattered across the
engine, agent, brain, coordinator, dashboard, and every test fixture.
Switching from USDC to USD, or adding USDT support, was a 70-file
edit because there was no single place that owned "what is a pair."

This module is that place.

DESIGN
──────
1. `Pair` is a frozen value object. `(base, quote, formats, precision)`
   are all derived from upstream truth (Kraken's `kraken pairs` JSON
   plus a static fallback for offline/test).

2. `PairRegistry` is the only thing in Hydra that knows how to map an
   input string to a canonical pair. It absorbs every alias dialect
   Hydra previously open-coded:
     - slashed vs slashless ("SOL/USD" / "SOLUSD")
     - lowercase / mixed case
     - XBT → BTC (Kraken's legacy ticker for Bitcoin)
     - asset normalization for balance keys (ZUSD→USD, USDC.F→USDC, ...)

3. The registry is bootstrapped from a static catalog at construction
   time so it works in tests, backtests, and offline modes; live agents
   call `bootstrap_from_kraken(load_pair_constants(...))` at startup
   to absorb authoritative precision/ordermin/costmin from the
   exchange.

4. `STABLE_QUOTES` is the membership set used everywhere the engine
   previously open-coded `endswith("USDC") or endswith("USD")`.
   Adding USDT support is a one-line membership change.

5. The registry is intentionally NOT an enum. Pairs are runtime data
   (Kraken can list new pairs without our code changing); enums are
   compile-time identifiers. The triangle/role binding in
   `hydra_config.py` provides the type-safe role layer.

INVARIANTS
──────────
- Pair instances are frozen. Updates allocate new instances.
- `quote in STABLE_QUOTES` is the only legitimate way to ask
  "is this pair USD-equivalent for display formatting?" — the
  answer must include USDC and USDT.
- Asset normalization (`normalize_asset`) handles balance-side asset
  codes (e.g. "USDC.F" earn-flex). Pair-side resolution
  (`PairRegistry.resolve`) handles trade-side pair symbols. They are
  related but distinct surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Optional


# ═══════════════════════════════════════════════════════════════════
# Module-level constants
# ═══════════════════════════════════════════════════════════════════

# Stable quote currencies (USD-equivalent for display + sizing purposes).
# Adding a new fiat-like quote is a one-line edit here. EUR is intentionally
# excluded — Hydra is not certified for euro-denominated trading.
STABLE_QUOTES = frozenset({"USD", "USDC", "USDT"})

# Asset-name aliases. Kraken returns extended asset names (X-prefix for
# crypto, Z-prefix for fiat) in some endpoints; normalize them here.
ASSET_ALIASES = {
    "XXBT": "BTC",
    "XBTC": "BTC",
    "XBT":  "BTC",
    "XETH": "ETH",
    "XSOL": "SOL",
    "ZUSD": "USD",
    "ZUSDC": "USDC",
    "ZEUR": "EUR",  # tolerate even though we don't trade EUR
}

# Suffixes Kraken uses for non-tradable asset variants:
#   .B = bonded, .S = staked, .M = margin parking, .F = earn-flex
# v2.16.2 added .F to the suffix set (USDC.F was previously valued at $0
# in the dashboard balance chart because the .F was not stripped before
# the USDC→1.0 USD price lookup).
STAKED_SUFFIXES = (".B", ".S", ".M", ".F")


def normalize_asset(name: str) -> str:
    """Normalize a Kraken asset code to its canonical form.

    Strips staked suffix first, then applies the Z/X-prefix alias map.
    Examples:
      'XXBT'    → 'BTC'
      'ZUSD'    → 'USD'
      'USDC.F'  → 'USDC'
      'ZUSD.F'  → 'USD'
      'BTC.S'   → 'BTC'
      'ETH'     → 'ETH'  (passthrough for unknown)
      ''        → ''     (passthrough — caller decides what to do)
    """
    if not name:
        return name
    stripped = name
    for suf in STAKED_SUFFIXES:
        if stripped.endswith(suf):
            stripped = stripped[: -len(suf)]
            break
    return ASSET_ALIASES.get(stripped, stripped)


# ═══════════════════════════════════════════════════════════════════
# Pair value object
# ═══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Pair:
    """Immutable description of a tradable pair.

    Field semantics:
      cli_format     — what the `kraken` CLI accepts as `--pair` ("SOL/USD")
      api_format     — what Kraken's REST API returns as JSON keys ("SOLUSD")
      ws_format      — what Kraken WS v2 uses as wsname ("SOL/USD")
      base           — canonical base asset code ("SOL")
      quote          — canonical quote asset code ("USD")
      price_decimals — significant decimals for price (Kraken's pair_decimals)
      ordermin       — minimum order volume in base units
      costmin        — minimum order cost in quote units
      lot_decimals   — significant decimals for volume
      tick_size      — Kraken's per-pair tick size (string or None)
    """
    cli_format: str
    api_format: str
    ws_format: str
    base: str
    quote: str
    price_decimals: int
    ordermin: float
    costmin: float
    lot_decimals: int
    tick_size: Optional[str]

    @property
    def is_stable_quoted(self) -> bool:
        """True iff the quote currency is a stable (USD-equivalent)."""
        return self.quote in STABLE_QUOTES

    def format_price(self, price: float) -> str:
        """Round to native precision and format with trailing zeros to 8dp.

        Matches the behaviour of the legacy `KrakenCLI._format_price` that
        Kraken's order endpoint expects: rounded to `price_decimals`,
        padded to 8dp (Kraken accepts trailing zeros as insignificant).
        """
        rounded = round(float(price), self.price_decimals)
        return f"{rounded:.8f}"

    def __str__(self) -> str:
        return self.cli_format


# ═══════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════

class PairRegistry:
    """Resolves any input string form to a canonical Pair.

    Construction takes an iterable of Pair objects (the static fallback
    catalog). At runtime, `bootstrap_from_kraken(loaded)` overlays
    authoritative metadata from `KrakenCLI.load_pair_constants`.
    """

    def __init__(self, pairs: Iterable[Pair]):
        # canonical cli_format → Pair
        self._by_canonical: dict[str, Pair] = {}
        # any uppercase form → canonical cli_format
        self._aliases: dict[str, str] = {}
        for p in pairs:
            self._add(p)

    # ─── Mutation (controlled) ───

    def _add(self, pair: Pair) -> None:
        self._by_canonical[pair.cli_format] = pair
        self._index_aliases(pair)

    def _index_aliases(self, pair: Pair) -> None:
        """Generate every input form that must resolve to this pair."""
        canonical = pair.cli_format
        forms = {
            pair.cli_format,
            pair.cli_format.replace("/", ""),
            pair.api_format,
            pair.ws_format,
            pair.ws_format.replace("/", ""),
        }
        # XBT is Kraken's legacy ticker for BTC. Any form containing BTC
        # must also resolve when written with XBT.
        for f in list(forms):
            if "BTC" in f:
                forms.add(f.replace("BTC", "XBT"))
        for f in forms:
            self._aliases[f.upper()] = canonical

    # ─── Resolution ───

    def resolve(self, symbol: str) -> Pair:
        """Resolve a pair symbol; raise KeyError if unknown."""
        p = self.get(symbol)
        if p is None:
            raise KeyError(f"Unknown pair: {symbol!r}")
        return p

    def get(self, symbol: Optional[str]) -> Optional[Pair]:
        """Resolve a pair symbol; return None if unknown or empty input."""
        if not symbol:
            return None
        key = symbol.strip().upper()
        canonical = self._aliases.get(key)
        if canonical is None:
            return None
        return self._by_canonical[canonical]

    # ─── Queries ───

    def all(self) -> tuple[Pair, ...]:
        """All registered pairs (ordered by registration)."""
        return tuple(self._by_canonical.values())

    def pairs_by_quote(self, quote: str) -> tuple[Pair, ...]:
        """Filter pairs whose quote currency matches."""
        return tuple(p for p in self._by_canonical.values() if p.quote == quote)

    def pairs_by_base(self, base: str) -> tuple[Pair, ...]:
        """Filter pairs whose base currency matches."""
        return tuple(p for p in self._by_canonical.values() if p.base == base)

    def __contains__(self, symbol: str) -> bool:
        return self.get(symbol) is not None

    def __len__(self) -> int:
        return len(self._by_canonical)

    # ─── Bootstrap ───

    def bootstrap_from_kraken(self, loaded: dict) -> None:
        """Overlay Kraken-authoritative metadata onto the registry.

        `loaded` is the dict returned by `KrakenCLI.load_pair_constants`:
            {friendly_pair: {price_decimals, ordermin, costmin, base,
                             quote, lot_decimals, tick_size}}

        Existing pairs have their numeric fields updated; unknown pairs
        are added (lets the agent discover new Kraken pairs without code
        changes — bound to safety only by what the agent asks Kraken
        about in the first place).

        Idempotent: applying twice produces the same registry state.
        """
        for friendly, info in loaded.items():
            if not isinstance(info, dict):
                continue
            existing = self.get(friendly)
            if existing is not None:
                updated = replace(
                    existing,
                    price_decimals=int(info.get("price_decimals", existing.price_decimals)),
                    ordermin=float(info.get("ordermin", existing.ordermin)),
                    costmin=float(info.get("costmin", existing.costmin)),
                    lot_decimals=int(info.get("lot_decimals", existing.lot_decimals)),
                    tick_size=info.get("tick_size", existing.tick_size),
                )
                self._by_canonical[existing.cli_format] = updated
            else:
                base = normalize_asset(str(info.get("base", "")))
                quote = normalize_asset(str(info.get("quote", "")))
                if not base or not quote:
                    continue
                cli = f"{base}/{quote}"
                api = f"{base}{quote}"
                new = Pair(
                    cli_format=cli,
                    api_format=api,
                    ws_format=cli,
                    base=base,
                    quote=quote,
                    price_decimals=int(info.get("price_decimals", 8)),
                    ordermin=float(info.get("ordermin", 0.02)),
                    costmin=float(info.get("costmin", 0.5)),
                    lot_decimals=int(info.get("lot_decimals", 8)),
                    tick_size=info.get("tick_size"),
                )
                self._add(new)


# ═══════════════════════════════════════════════════════════════════
# Static fallback catalog
# ═══════════════════════════════════════════════════════════════════
#
# Values mirror the pre-v2.19 hardcoded tables in hydra_kraken_cli.py
# (PRICE_DECIMALS) and hydra_engine.py (min_costmin). Authoritative
# values come from Kraken's `pairs` endpoint at agent boot — these
# defaults exist so the registry is usable in offline contexts (tests,
# backtests, dashboard, paper mode without network).

_FALLBACK_PAIRS: tuple[Pair, ...] = (
    Pair(
        cli_format="SOL/USD",  api_format="SOLUSD",  ws_format="SOL/USD",
        base="SOL", quote="USD",
        price_decimals=2, ordermin=0.02, costmin=0.5,
        lot_decimals=8, tick_size=None,
    ),
    Pair(
        cli_format="SOL/USDC", api_format="SOLUSDC", ws_format="SOL/USDC",
        base="SOL", quote="USDC",
        price_decimals=2, ordermin=0.02, costmin=0.5,
        lot_decimals=8, tick_size=None,
    ),
    Pair(
        cli_format="BTC/USD",  api_format="BTCUSD",  ws_format="BTC/USD",
        base="BTC", quote="USD",
        price_decimals=1, ordermin=0.0001, costmin=0.5,
        lot_decimals=8, tick_size=None,
    ),
    Pair(
        cli_format="BTC/USDC", api_format="BTCUSDC", ws_format="BTC/USDC",
        base="BTC", quote="USDC",
        price_decimals=1, ordermin=0.0001, costmin=0.5,
        lot_decimals=8, tick_size=None,
    ),
    Pair(
        cli_format="SOL/BTC",  api_format="SOLBTC",  ws_format="SOL/BTC",
        base="SOL", quote="BTC",
        price_decimals=7, ordermin=0.02, costmin=0.00002,
        lot_decimals=8, tick_size=None,
    ),
)


def default_registry() -> PairRegistry:
    """Return a registry pre-loaded with the static fallback catalog.

    Live agents should call `bootstrap_from_kraken(...)` on the result
    once `KrakenCLI.load_pair_constants(...)` has been fetched.
    """
    return PairRegistry(_FALLBACK_PAIRS)
