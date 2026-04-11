"""Hydra live-execution test harness.

Drives HydraAgent._execute_trade directly across happy, failure, edge,
schema, rollback, and historical regression scenarios. Runs in four modes:

- smoke:   imports + construction only, <5s, zero API, zero cost
- mock:    every mockable scenario via monkey-patched KrakenCLI, ~30s, zero cost
- validate: real Kraken with --validate forced on order calls, ~90s, zero cost
- live:    real Kraken with real post-only orders + immediate cancel, ~3min, <$0.01

See tests/live_harness/README.md for the full scenario catalog and usage.
"""
