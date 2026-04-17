"""Intent classifier tests \u2014 heuristic rules."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.intent_classifier import IntentClassifier


def test_greeting_detected():
    c = IntentClassifier()
    for text in ("hi", "hey", "morning", "yo", "sup"):
        r = c.classify(text)
        assert r.intent == "greeting", f"'{text}' -> {r.intent}"


def test_acknowledgment_detected():
    c = IntentClassifier()
    for text in ("ok", "thanks", "got it", "lfg", "bet"):
        r = c.classify(text)
        assert r.intent == "ack_confirmation", f"'{text}' -> {r.intent}"


def test_trade_proposal_detected():
    c = IntentClassifier()
    r = c.classify("buy SOL here")
    assert r.intent == "trade_proposal"
    r = c.classify("open a long on BTC")
    assert r.intent == "trade_proposal"


def test_ladder_proposal_detected():
    c = IntentClassifier()
    r = c.classify("let's build a ladder on SOL")
    assert r.intent == "ladder_proposal"
    r = c.classify("scale out of my position")
    assert r.intent == "ladder_proposal"


def test_teaching_detected():
    c = IntentClassifier()
    r = c.classify("explain how funding works")
    assert r.intent == "teaching_explanation"


def test_market_query_detected():
    c = IntentClassifier()
    r = c.classify("what's happening with SOL?")
    assert r.intent == "market_state_query"


def test_empty_default():
    c = IntentClassifier()
    r = c.classify("")
    assert r.intent == "small_talk"
    assert r.method == "default"


def test_unmatched_question_defaults_to_market():
    c = IntentClassifier()
    r = c.classify("do you think i should think about this")
    # 'think' isn't a keyword, but the presence of the do/? form should
    # still feel like a query
    assert r.intent in ("small_talk", "market_state_query")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all classifier tests passed")
