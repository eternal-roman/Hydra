"""Mode-label scrub tests.

The companion compiler exposes internal voice-mode IDs (mentor,
desk_clipped, reflective, bro_vibes, locked_in, serious,
warm_professional, ...) to the LLM in the system prompt so it can pick
its cadence. Those labels must never reach the user — self-labeling
reads as manipulative theater. Defense in depth:
  (a) `hydra_companions/compiler.py` injects an explicit "do not name"
      rule into the system prompt.
  (b) `hydra_companions/companion.py::_scrub_mode_labels` strips any
      leakage that slips through, before the text reaches TurnResult OR
      the on-disk transcript.

This file tests (b) — the scrubber — plus a light check that (a)
remains present in every compiled prompt.
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.companion import (
    _build_mode_scrub_patterns,
    _scrub_mode_labels,
)
from hydra_companions.compiler import load_all_souls


APEX_MODES = ("mentor", "desk_clipped", "reflective")
BROSKI_MODES = ("bro_vibes", "locked_in", "serious_mode")
ATHENA_MODES = ("warm_professional",)


def _scrub(text: str, modes: tuple[str, ...]) -> str:
    return _scrub_mode_labels(text, _build_mode_scrub_patterns(modes))


# ─── bracket / paren tags ─────────────────────────────────────────────

def test_strips_leading_bracket_tag():
    out = _scrub("[mentor] Ranging regime, RSI 23 — mean-reversion trigger.", APEX_MODES)
    assert "mentor" not in out.lower()
    assert out.startswith("Ranging regime")


def test_strips_leading_paren_tag():
    out = _scrub("(mentor) Size off remaining balance.", APEX_MODES)
    assert "mentor" not in out.lower()
    assert out.startswith("Size off")


def test_strips_mode_prefix_bracket():
    out = _scrub("[mode: desk_clipped] copy. stop at 68.4.", APEX_MODES)
    assert "desk_clipped" not in out.lower() and "mode" not in out.lower()
    assert out.startswith("copy.")


def test_strips_voice_prefix_bracket():
    out = _scrub("[voice: reflective] Copper futures, 2012.", APEX_MODES)
    assert "reflective" not in out.lower()
    assert out.startswith("Copper futures")


def test_strips_bracket_with_mode_suffix():
    out = _scrub("[mentor mode] price pinned to the lower band.", APEX_MODES)
    assert "mentor" not in out.lower()
    assert "mode" not in out.lower()
    assert "price pinned" in out


def test_strips_mid_sentence_paren_tag():
    out = _scrub("That's the trade. (mentor)", APEX_MODES)
    assert "mentor" not in out.lower()
    assert "That's the trade." in out


def test_handles_hyphenated_mode_id():
    # Model might emit `desk-clipped` or `desk clipped` instead of
    # `desk_clipped`. The scrubber should normalize.
    out = _scrub("[desk-clipped] fill at 68.4.", APEX_MODES)
    assert out.startswith("fill at")
    out2 = _scrub("[desk clipped] fill at 68.4.", APEX_MODES)
    assert out2.startswith("fill at")


# ─── line-leading labels ──────────────────────────────────────────────

def test_strips_leading_colon_label():
    out = _scrub("mentor: Ranging regime, RSI 23.", APEX_MODES)
    assert not out.lower().startswith("mentor")
    assert out.startswith("Ranging regime")


def test_strips_leading_dash_label():
    out = _scrub("Mentor Mode — Ranging, RSI 23.", APEX_MODES)
    assert "mentor" not in out.lower()
    assert "mode" not in out.lower()
    assert out.startswith("Ranging")


def test_strips_leading_emdash_label():
    out = _scrub("reflective \u2014 First account I blew was copper.", APEX_MODES)
    assert "reflective" not in out.lower()
    assert out.startswith("First account")


# ─── inline meta phrases ──────────────────────────────────────────────

def test_strips_inline_mode_phrase():
    out = _scrub("Let me switch to mentor mode here. Size off balance.", APEX_MODES)
    assert "mentor" not in out.lower()
    assert "Size off balance." in out


def test_strips_using_register_phrase():
    out = _scrub("Using reflective register: copper, 2012, $4k.", APEX_MODES)
    # phrase stripped, then the leading label pattern kicks in on the
    # residual "register: " shape — both should be gone.
    assert "reflective" not in out.lower()
    assert "2012" in out


def test_strips_my_voice_phrase():
    out = _scrub("In my desk_clipped voice: copy, stop at 68.4.", APEX_MODES)
    assert "desk_clipped" not in out.lower()
    assert "stop at 68.4" in out


# ─── natural-English survival (no false positives) ────────────────────

def test_bare_mentor_survives_when_natural_usage():
    """`mentor` is Apex's word for Denny. Bare use outside of a mode/
    bracket context must survive — scrubber targets the label pattern,
    not the English word."""
    text = "Denny was my mentor. He closed the position from my terminal."
    out = _scrub(text, APEX_MODES)
    assert "mentor" in out
    assert out == text  # unchanged


def test_bare_reflective_adjective_survives():
    text = "That was a reflective answer to a hard question."
    out = _scrub(text, APEX_MODES)
    assert "reflective" in out


def test_serious_adjective_survives_broski():
    """`serious` is natural English — survives when no mode/brackets
    surround it. This is the false-positive guard for Broski's
    `serious_mode` ID."""
    text = "This is serious. Size down."
    out = _scrub(text, BROSKI_MODES)
    assert "serious" in out
    assert out == text


def test_scrub_strips_serious_mode_label_broski():
    """But the `serious_mode` / `serious mode` label itself must strip.
    Broski's mode ID ends in `mode`, which is the edge case pattern 4
    was designed for."""
    out = _scrub("Switching to serious mode now. Size down.", BROSKI_MODES)
    assert "serious mode" not in out.lower()
    assert "Size down." in out
    out2 = _scrub("[serious_mode] yo, this is real risk.", BROSKI_MODES)
    assert "serious_mode" not in out2.lower()
    assert "yo, this is real risk." in out2
    out3 = _scrub("Going into serious_mode. Real risk on the table.", BROSKI_MODES)
    assert "serious_mode" not in out3.lower()
    assert "Real risk on the table." in out3


def test_empty_input_returns_empty():
    assert _scrub("", APEX_MODES) == ""


def test_no_modes_returns_unchanged():
    text = "[mentor] something"
    out = _scrub(text, ())
    assert out == text


# ─── multi-soul coverage ──────────────────────────────────────────────

def test_scrub_works_for_broski():
    out = _scrub("[bro_vibes] yo, this setup is clean.", BROSKI_MODES)
    assert "bro_vibes" not in out.lower()
    assert out.startswith("yo,")


def test_scrub_works_for_locked_in():
    out = _scrub("Switching to locked_in mode now.", BROSKI_MODES)
    assert "locked_in" not in out.lower()
    assert "mode" not in out.lower()


def test_scrub_works_for_athena():
    out = _scrub("[warm_professional] capital preservation first.", ATHENA_MODES)
    assert "warm_professional" not in out.lower()
    assert "capital preservation" in out


# ─── combined / adversarial ───────────────────────────────────────────

def test_strips_multiple_tags_in_single_response():
    text = (
        "[mentor] Ranging regime, RSI 23.\n"
        "Switching to desk_clipped mode.\n"
        "copy. stop at 68.4."
    )
    out = _scrub(text, APEX_MODES)
    assert "mentor" not in out.lower()
    assert "desk_clipped" not in out.lower()
    assert "mode" not in out.lower()
    assert "Ranging regime" in out and "copy." in out


def test_collapses_whitespace_after_strip():
    out = _scrub("[mentor]   Ranging.", APEX_MODES)
    # No double-spaces left behind.
    assert "  " not in out


# ─── layer-1 (prompt) safeguard still present ─────────────────────────

def test_compiled_prompt_contains_do_not_name_rule():
    """Every compiled soul with voice modes must carry the explicit
    internal-only directive. This is the prompt-level safeguard; if it
    regresses, the scrubber stands alone and we lose defense in depth."""
    souls = load_all_souls()
    for cid, compiled in souls.items():
        prompt = compiled.system_prompt
        # Either the voice-modes block is absent (soul has no modes) or
        # the internal-only warning must be present.
        if "## Voice modes" in prompt:
            assert "INTERNAL" in prompt or "internal" in prompt, \
                f"{cid}: voice-modes block missing internal-only directive"
            assert "never name" in prompt.lower() or "do not name" in prompt.lower() \
                or "never prefix" in prompt.lower(), \
                f"{cid}: voice-modes block missing explicit 'do not name' rule"


def test_operating_rules_warn_about_mode_leakage():
    """The common '## Operating rules' block must call out mode-label
    non-leakage for every soul (not just those with voice modes — the
    warning is universal and cheap)."""
    souls = load_all_souls()
    for cid, compiled in souls.items():
        prompt = compiled.system_prompt
        assert "INTERNAL infrastructure" in prompt, \
            f"{cid}: operating rules missing the mode-label safeguard"
