from hydra_streams import CandleStream


def test_on_candle_callback_invoked():
    stream = CandleStream(pairs=["BTC/USD"], paper=True)
    received = []
    stream.on_candle(lambda pair, candle: received.append((pair, candle)))
    # Inject a fake message via _on_message — simulating WS push.
    stream._on_message({
        "channel": "ohlc",
        "data": [{"symbol": "BTC/USD", "open": 1, "high": 2, "low": 1,
                  "close": 1.5, "volume": 10, "interval_begin": "2024-01-01T00:00:00.000Z"}],
    })
    assert len(received) == 1
    pair, candle = received[0]
    assert pair == "BTC/USD"
    assert candle["close"] == 1.5
