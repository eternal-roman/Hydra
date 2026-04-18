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
    # v1.1: Apex gained deep content (formative incidents, intellectual
    # lineage, fallibility protocol, voice modes, human texture) and grew
    # substantially. Athena + Broski also gained the hybrid sections but
    # with re-shaped existing content. Per-soul ceilings reflect intended
    # depth; the hard ceiling prevents runaway growth without review.
    ceilings = {"apex": 32_000, "athena": 22_000, "broski": 22_000}
    for cid, compiled in souls.items():
        assert compiled.system_prompt.strip(), f"{cid}: empty system prompt"
        ceiling = ceilings.get(cid, 22_000)
        assert len(compiled.system_prompt) < ceiling, \
            f"{cid}: prompt too big ({len(compiled.system_prompt)} >= {ceiling})"


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


# ───────────────────────────────────────────────────────────────
# v1.1 CBP-hybrid section tests
# ───────────────────────────────────────────────────────────────

def test_v11_hybrid_sections_present_all_souls():
    """All three souls must expose the hybrid section flags (functional
    consistency across the compiler even if content depth varies).
    """
    souls = load_all_souls()
    for cid, compiled in souls.items():
        assert compiled.has_formative_incidents, f"{cid}: missing formative_incidents"
        assert compiled.has_intellectual_lineage, f"{cid}: missing intellectual_lineage"
        assert compiled.has_fallibility_protocol, f"{cid}: missing fallibility.self_correction_protocol"


def test_v11_tool_access_allowlist_populated_all_souls():
    souls = load_all_souls()
    expected_new = {"get_order_journal", "get_chart_snapshot", "get_chart_summary"}
    for cid, compiled in souls.items():
        assert len(compiled.tool_access) >= 9, f"{cid}: tool_access too small ({len(compiled.tool_access)})"
        assert expected_new.issubset(compiled.tool_access), \
            f"{cid}: missing v1.1 tools {expected_new - compiled.tool_access}"


def test_v11_voice_modes_present_all_souls():
    souls = load_all_souls()
    assert "desk_clipped" in souls["apex"].voice_modes
    assert "mentor" in souls["apex"].voice_modes
    assert "reflective" in souls["apex"].voice_modes
    assert "bro_vibes" in souls["broski"].voice_modes
    assert "serious_mode" in souls["broski"].voice_modes
    assert "warm_professional" in souls["athena"].voice_modes


def test_v11_apex_renders_formative_incidents_block():
    souls = load_all_souls()
    prompt = souls["apex"].system_prompt
    assert "## How I got here (formative incidents)" in prompt
    assert "2012" in prompt
    assert "2015" in prompt
    assert "2026-04-18" in prompt, "the chronological-inversion incident must be represented"


def test_v11_apex_renders_fallibility_protocol():
    souls = load_all_souls()
    prompt = souls["apex"].system_prompt
    assert "## Known fallibilities" in prompt
    assert "chronological-before-indictment" in prompt
    assert "I read that wrong" in prompt


def test_v11_apex_renders_lineage_block():
    souls = load_all_souls()
    prompt = souls["apex"].system_prompt
    assert "## Where my rules come from" in prompt
    assert "Van Tharp" in prompt
    assert "Peter Brandt" in prompt
    assert "Denny" in prompt


def test_v11_apex_renders_conditional_rules_block():
    souls = load_all_souls()
    prompt = souls["apex"].system_prompt
    assert "## Gated rules (conditional activation)" in prompt
    assert "AP01" in prompt
    assert "MEAN_REVERSION" in prompt


def test_v11_apex_renders_voice_modes_block():
    souls = load_all_souls()
    prompt = souls["apex"].system_prompt
    assert "## Voice modes" in prompt
    assert "desk_clipped" in prompt
    assert "mentor" in prompt
    assert "reflective" in prompt


def test_v11_human_texture_block_present_all_souls():
    souls = load_all_souls()
    for cid, compiled in souls.items():
        assert "## Human texture" in compiled.system_prompt, f"{cid}: missing human texture block"


def test_v11_athena_broski_hybrid_content_present():
    """Athena + Broski have re-shaped (not newly curated) hybrid content;
    verify representative elements render."""
    souls = load_all_souls()
    assert "Benjamin Graham" in souls["athena"].system_prompt
    assert "Jacob" in souls["athena"].system_prompt or "nephew" in souls["athena"].system_prompt
    assert "walk-before-revise" in souls["athena"].system_prompt

    assert "2017" in souls["broski"].system_prompt
    assert "vibe-check-before-size-up" in souls["broski"].system_prompt


def test_v11_condition_rendering_handles_always_and_leaves():
    from hydra_companions.compiler import _render_condition_plain
    assert _render_condition_plain("always") == "always"
    assert _render_condition_plain(None) == "always"
    leaf = {"field": "engine:regime.val", "op": "eq", "value": "RANGING"}
    out = _render_condition_plain(leaf)
    assert "engine:regime.val" in out
    assert "==" in out
    assert "RANGING" in out
    all_cond = {"all": [leaf, {"field": "x", "op": "gt", "value": 5}]}
    out = _render_condition_plain(all_cond)
    assert "AND" in out
    any_cond = {"any": [leaf, {"field": "x", "op": "gt", "value": 5}]}
    assert "OR" in _render_condition_plain(any_cond)
    not_cond = {"not": leaf}
    assert _render_condition_plain(not_cond).startswith("NOT")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all compiler tests passed")
