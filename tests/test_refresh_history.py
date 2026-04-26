import time
from hydra_history_store import HistoryStore, CandleRow
from tools.refresh_history import refresh_pair


class _StubCli:
    def __init__(self, rows):
        self._rows = rows

    def ohlc(self, pair, interval=60):
        return self._rows


def test_refresh_inserts_rest_rows(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    cli = _StubCli([
        {"timestamp": 1_700_000_000, "open": 10, "high": 11, "low": 9,
         "close": 10.5, "volume": 1.0},
    ])
    n = refresh_pair(store, "BTC/USD", grain_sec=3600, cli=cli)
    assert n == 1
    [got] = list(store.fetch("BTC/USD", 3600, 0, 9_999_999_999))
    assert got.source == "kraken_rest"
    assert got.close == 10.5


def test_refresh_does_not_overwrite_archive(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite"))
    store.upsert_candles([CandleRow("BTC/USD", 3600, 1_700_000_000,
                                    1, 1, 1, 1, 1, "kraken_archive")])
    cli = _StubCli([
        {"timestamp": 1_700_000_000, "open": 99, "high": 99, "low": 99,
         "close": 99, "volume": 99},
    ])
    refresh_pair(store, "BTC/USD", grain_sec=3600, cli=cli)
    [got] = list(store.fetch("BTC/USD", 3600, 0, 9_999_999_999))
    assert got.close == 1  # archive preserved
