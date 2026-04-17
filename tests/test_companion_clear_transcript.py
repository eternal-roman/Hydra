"""Transcript clear tests \u2014 /clear slash command."""
import sys
import pathlib
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.compiler import load_all_souls
from hydra_companions.intent_classifier import IntentClassifier
from hydra_companions.providers import ProviderClient, ProviderResponse
from hydra_companions.router import Router
from hydra_companions.companion import Companion
import hydra_companions.config as cfg


class FakeBC:
    latest_state = {}
    msgs = []

    def broadcast_message(self, t, p):
        self.msgs.append((t, p))


class FakeAgent:
    broadcaster = FakeBC()


class StubProvider:
    def call(self, **kw):
        return ProviderResponse(text="ok", tokens_in=1, tokens_out=1,
                                 cost_usd=0, model_id="fake", provider="fake")


def _fresh_comp(tmp_dir, sid="apex"):
    cfg.TRANSCRIPTS_DIR = tmp_dir
    # companion.py captured the original TRANSCRIPTS_DIR at import time
    import hydra_companions.companion as comp_mod
    comp_mod.TRANSCRIPTS_DIR = tmp_dir
    souls = load_all_souls()
    return Companion(soul=souls[sid], agent=FakeAgent(), router=Router(),
                     classifier=IntentClassifier(), provider=StubProvider(),
                     user_id="test")


def test_clear_transcript_empties_memory_and_file():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        comp = _fresh_comp(tmp)
        comp.respond("hi")
        comp.respond("again")
        # transcript has user+assistant per turn -> 4 entries
        assert len(comp.transcript) == 4
        removed = comp.clear_transcript()
        assert removed == 4
        assert comp.transcript == []
        assert not (tmp / "test_apex.jsonl").exists()


def test_clear_isolates_per_companion():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        a = _fresh_comp(tmp, "athena")
        b = _fresh_comp(tmp, "broski")
        a.respond("a message")
        b.respond("b message")
        a.clear_transcript()
        assert a.transcript == []
        # Broski's transcript untouched
        assert any(t.get("content") == "b message" for t in b.transcript)
        assert (tmp / "test_broski.jsonl").exists()


def test_coordinator_clear_one():
    # integration-light: the coordinator's handler dispatches to the
    # right companion and returns removed count
    from hydra_companions.coordinator import CompanionCoordinator
    import hydra_companions.companion as comp_mod
    with tempfile.TemporaryDirectory() as td:
        cfg.TRANSCRIPTS_DIR = pathlib.Path(td)
        comp_mod.TRANSCRIPTS_DIR = pathlib.Path(td)
        class Agent:
            class BC:
                latest_state = {}
                def broadcast_message(self, *a, **kw): pass
            broadcaster = BC()
        coord = CompanionCoordinator(Agent())
        coord.get("apex").respond("seeded")
        r = coord.handle_clear_transcript({"companion_id": "apex"})
        assert r["success"]
        assert r["removed"] >= 1
        assert coord.get("apex").transcript == []


def test_coordinator_clear_all():
    from hydra_companions.coordinator import CompanionCoordinator
    import hydra_companions.companion as comp_mod
    with tempfile.TemporaryDirectory() as td:
        cfg.TRANSCRIPTS_DIR = pathlib.Path(td)
        comp_mod.TRANSCRIPTS_DIR = pathlib.Path(td)
        class Agent:
            class BC:
                latest_state = {}
                def broadcast_message(self, *a, **kw): pass
            broadcaster = BC()
        coord = CompanionCoordinator(Agent())
        for sid in ("athena", "apex", "broski"):
            coord.get(sid).respond(f"seed {sid}")
        r = coord.handle_clear_transcript({"scope": "all"})
        assert r["success"]
        assert r["scope"] == "all"
        for sid in ("athena", "apex", "broski"):
            assert coord.get(sid).transcript == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all clear-transcript tests passed")
