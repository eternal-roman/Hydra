"""Validator + token + executor contract tests (Phase 2)."""
import sys
import pathlib
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.executor import (
    TradeProposal, LadderProposal, LadderRung, ProposalValidator,
    MockExecutor, new_proposal_id, new_ladder_id,
)
from hydra_companions.router import Router
from hydra_companions.tokens import TokenBroker


class FakeBroadcaster:
    def __init__(self, state=None):
        self.latest_state = state or {
            "pairs": {"SOL/USDC": {"price": 142.0, "portfolio": {"equity": 100.0}}},
            "balance_usd": {"total_usd": 100.0},
        }
        self.msgs = []

    def broadcast_message(self, msg_type, payload):
        self.msgs.append((msg_type, payload))


class FakeAgent:
    def __init__(self, broadcaster=None):
        self.broadcaster = broadcaster or FakeBroadcaster()
        self.kraken_cli = None


def _valid_trade():
    return TradeProposal(
        proposal_id=new_proposal_id(), companion_id="apex", user_id="local",
        pair="SOL/USDC", side="buy", size=0.1, limit_price=141.0,
        stop_loss=139.0, rationale="test",
    )


def test_valid_trade_passes():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    result = v.validate_trade(_valid_trade())
    assert result.ok, result.reason


def test_missing_stop_rejected():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    p = TradeProposal(**{**_valid_trade().to_dict(), "stop_loss": 0})
    result = v.validate_trade(p)
    assert not result.ok
    assert "stop" in result.reason.lower()


def test_buy_stop_above_entry_rejected():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    p = TradeProposal(**{**_valid_trade().to_dict(), "stop_loss": 145.0})
    assert not v.validate_trade(p).ok


def test_price_band_enforced():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    # Apex cap is 4%; put limit 20% off mid.
    p = TradeProposal(**{**_valid_trade().to_dict(), "limit_price": 170.0, "stop_loss": 168.0})
    result = v.validate_trade(p)
    assert not result.ok
    assert "band" in result.reason.lower()


def test_risk_cap_enforced():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    # 100 equity, 10% risk attempted -> fails apex 1% cap.
    p = TradeProposal(**{**_valid_trade().to_dict(), "size": 5.0, "stop_loss": 139.0})
    result = v.validate_trade(p)
    assert not result.ok
    assert "risk" in result.reason.lower()


def test_broski_higher_risk_cap():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    # Size that would fail apex (1%) but pass broski (1.5%).
    # 142*size=usd, risk=size*(141-139)=2*size. Equity=100. apex cap=1% of eq=1usd.
    # 2*size=1 -> size=0.5 just below apex cap. 2*size=1.5 -> 0.75 is broski cap.
    base = _valid_trade().to_dict()
    mid_size = TradeProposal(**{**base, "size": 0.7, "companion_id": "apex"})
    assert not v.validate_trade(mid_size).ok
    bro_size = TradeProposal(**{**base, "size": 0.7, "companion_id": "broski"})
    assert v.validate_ladder is not None  # sanity
    assert v.validate_trade(bro_size).ok


def test_ladder_rung_sum():
    agent = FakeAgent()
    r = Router()
    v = ProposalValidator(agent=agent, router=r)
    bad = LadderProposal(
        proposal_id=new_ladder_id(), companion_id="apex", user_id="local",
        pair="SOL/USDC", side="buy", total_size=0.2,
        rungs=(LadderRung(0.5, 141.0), LadderRung(0.3, 140.0)),  # sums to 0.8
        stop_loss=138.0, invalidation_price=138.0, rationale="",
    )
    assert not v.validate_ladder(bad).ok


def test_token_mint_verify():
    broker = TokenBroker(ttl_seconds=30.0)
    b = broker.mint("prop-abc")
    assert broker.verify(proposal_id="prop-abc", token=b.token, nonce=b.nonce, expires_at=b.expires_at)


def test_token_rejects_bad_signature():
    broker = TokenBroker(ttl_seconds=30.0)
    b = broker.mint("prop-abc")
    assert not broker.verify(proposal_id="prop-abc", token="0" * 64,
                             nonce=b.nonce, expires_at=b.expires_at)


def test_token_rejects_expired():
    broker = TokenBroker(ttl_seconds=0.001)
    b = broker.mint("prop-abc")
    time.sleep(0.05)
    assert not broker.verify(proposal_id="prop-abc", token=b.token,
                             nonce=b.nonce, expires_at=b.expires_at)


def test_mock_executor_broadcasts_executed():
    bc = FakeBroadcaster()
    m = MockExecutor(broadcaster=bc)
    p = _valid_trade()
    m.execute_trade(p)
    assert any(msg_type == "companion.trade.executed" for msg_type, _ in bc.msgs)


def test_no_write_tools_in_tools_readonly_registry():
    from hydra_companions import tools_readonly
    for name in tools_readonly.TOOL_REGISTRY:
        assert "place" not in name
        assert "cancel" not in name
        assert "propose" not in name


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all proposal tests passed")
