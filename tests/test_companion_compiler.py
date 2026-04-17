"""Compiler tests \u2014 soul JSON -> system prompt."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.compiler import load_all_souls, compile_soul
from hydra_companions.config import COMPANION_IDS


def test_all_three_souls_compile():
    souls = load_all_souls()
    assert set(souls.keys()) == set(COMPANION_IDS), f"missing souls: {set(COMPANION_IDS) - set(souls.keys())}"


def test_compiled_prompt_non_empty_and_bounded():
    souls = load_all_souls()
    for cid, compiled in souls.items():
        assert compiled.system_prompt.strip(), f"{cid}: empty system prompt"
        # Keep each prompt under 12 KB \u2014 cheap on context.
        assert len(compiled.system_prompt) < 12_000, f"{cid}: prompt too big ({len(compiled.system_prompt)})"


def test_taboo_phrases_not_in_quoted_utterance_values():
    """Taboos may appear in metadata key names (e.g. 'responding_to_moon_talk')
    but must not appear inside the quoted utterance strings the companion
    would actually say."""
    import re
    souls = load_all_souls()
    for cid, compiled in souls.items():
        taboos = compiled.voice_taboos
        assert taboos, f"{cid}: expected at least one taboo phrase"
        sample_marker = "## Your voice, in your own words"
        if sample_marker in compiled.system_prompt:
            sample_block = compiled.system_prompt.split(sample_marker, 1)[1].split("## ", 1)[0]
            # Extract only the quoted values ("...") \u2014 these are what
            # the companion might actually say.
            quoted_values = re.findall(r'"([^"]*)"', sample_block)
            for taboo in taboos:
                for val in quoted_values:
                    assert taboo.lower() not in val.lower(), \
                        f"{cid}: taboo '{taboo}' appears in sample utterance value: {val!r}"


def test_safety_invariants_present():
    souls = load_all_souls()
    for cid, compiled in souls.items():
        assert compiled.safety_caps, f"{cid}: missing safety_invariants"
        assert compiled.safety_caps.get("always_require_stop") is True, \
            f"{cid}: must require stop-loss"
        assert compiled.safety_caps.get("never_propose_market_order") is True, \
            f"{cid}: must never propose market orders"


def test_broski_has_serious_mode_supported():
    souls = load_all_souls()
    assert souls["broski"].serious_mode_supported is True
    assert souls["athena"].serious_mode_supported is False
    assert souls["apex"].serious_mode_supported is False


def test_compile_is_deterministic():
    import json
    souls_dir = pathlib.Path("hydra_companions/souls")
    raw = json.loads((souls_dir / "apex.soul.json").read_text(encoding="utf-8"))
    a = compile_soul(raw)
    b = compile_soul(raw)
    assert a.system_prompt == b.system_prompt
    assert a.voice_taboos == b.voice_taboos


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all compiler tests passed")
