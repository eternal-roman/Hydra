"""PLACEMENT_FAILED entries must not survive snapshot/rolling persistence."""
from collections import deque
from hydra_agent import HydraAgent


def _agent_with_journal(entries):
    a = HydraAgent.__new__(HydraAgent)
    a.order_journal = list(entries)
    return a


def test_journal_for_persistence_drops_placement_failed():
    a = _agent_with_journal([
        {"lifecycle": {"state": "FILLED"}},
        {"lifecycle": {"state": "PLACEMENT_FAILED"}},
        {"lifecycle": {"state": "CANCELLED_UNFILLED"}},
        {"lifecycle": {"state": "PARTIALLY_FILLED"}},
        {"lifecycle": {"state": "PLACEMENT_FAILED"}},
    ])
    out = a._journal_for_persistence()
    states = [e["lifecycle"]["state"] for e in out]
    assert "PLACEMENT_FAILED" not in states
    assert states == ["FILLED", "CANCELLED_UNFILLED", "PARTIALLY_FILLED"]


def test_journal_for_persistence_preserves_in_memory():
    """In-memory journal still contains everything; only the persisted view filters."""
    entries = [
        {"lifecycle": {"state": "PLACEMENT_FAILED"}},
        {"lifecycle": {"state": "FILLED"}},
    ]
    a = _agent_with_journal(entries)
    a._journal_for_persistence()  # call shouldn't mutate
    assert len(a.order_journal) == 2
    assert a.order_journal[0]["lifecycle"]["state"] == "PLACEMENT_FAILED"


def test_journal_for_persistence_caps_at_200():
    entries = [{"lifecycle": {"state": "FILLED"}} for _ in range(250)]
    a = _agent_with_journal(entries)
    out = a._journal_for_persistence()
    assert len(out) == 200


def test_journal_for_persistence_handles_missing_lifecycle():
    """Defensive: legacy entries without lifecycle key should be kept (we only
    filter explicit PLACEMENT_FAILED)."""
    a = _agent_with_journal([
        {"foo": "bar"},  # no lifecycle key — keep
        {"lifecycle": {}},  # no state — keep
        {"lifecycle": {"state": "PLACEMENT_FAILED"}},  # drop
    ])
    out = a._journal_for_persistence()
    assert len(out) == 2
