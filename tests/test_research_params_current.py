"""T30A — research_params_current handler returns the tunable param schema."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.test_research_ws import _MockBroadcaster  # reuse existing fake


class ResearchParamsCurrentHandler(unittest.TestCase):
    def setUp(self):
        from hydra_backtest_server import mount_backtest_routes, BacktestWorkerPool
        from hydra_experiments import ExperimentStore
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.bcaster = _MockBroadcaster()
        self.pool = BacktestWorkerPool(
            store=ExperimentStore(self._tmp.name),
            max_workers=1, queue_depth=2,
        )
        mount_backtest_routes(self.bcaster, self.pool)

    def tearDown(self):
        self.pool.shutdown(timeout=2.0)
        self._tmp.cleanup()

    def test_returns_param_bounds_schema(self):
        reply = self.bcaster.handlers["research_params_current"]({"pair": "BTC/USD"})
        self.assertTrue(reply["success"], reply.get("error"))
        self.assertEqual(reply["pair"], "BTC/USD")
        schema = reply["data"]
        # Spot check known params from hydra_tuner.PARAM_BOUNDS
        self.assertIn("momentum_rsi_upper", schema)
        self.assertEqual(schema["momentum_rsi_upper"]["min"], 55.0)
        self.assertEqual(schema["momentum_rsi_upper"]["max"], 90.0)
        self.assertIn("current", schema["momentum_rsi_upper"])
        self.assertIn("step", schema["momentum_rsi_upper"])

    def test_default_pair_fallback(self):
        """No pair param → uses BTC/USD default."""
        reply = self.bcaster.handlers["research_params_current"]({})
        self.assertTrue(reply["success"])
        self.assertEqual(reply["pair"], "BTC/USD")


if __name__ == "__main__":
    unittest.main()
