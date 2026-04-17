"""Soul JSON -> system prompt compiler.

Deterministic. Same soul in, same prompt out. The LLM never sees the
JSON; it sees a formatted markdown-ish system prompt compiled from it.

Future trained personas from Phase 7's questionnaire produce identical
soul JSON shape, so they drop into this compiler without change.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hydra_companions.config import SOULS_DIR


@dataclass(frozen=True)
class CompiledSoul:
    id: str
    display_name: str
    system_prompt: str
    voice_taboos: frozenset          # phrases the companion must never use
    signature_phrases: tuple         # phrases they reach for
    safety_caps: dict                # code-enforced numeric/bool caps
    color_theme: dict
    sigil: str
    serious_mode_supported: bool
    default_mood: str                # per-soul initial mood (mood_model.default)


def _fmt_bullets(items, bullet: str = "- ") -> str:
    return "\n".join(f"{bullet}{x}" for x in items if x)


def _fmt_behavioral_rules(rules: list) -> str:
    lines = []
    for r in rules:
        when = r.get("when", "")
        template = r.get("template", "")
        rid = r.get("id", "")
        lines.append(f"- [{rid}] WHEN {when}: say something like \u2014 \"{template}\"")
    return "\n".join(lines)


def _fmt_sample_utterances(su: dict) -> str:
    return "\n".join(f'- {k}: "{v}"' for k, v in su.items())


def _fmt_reactions(r: dict) -> str:
    return "\n".join(f"- {state}: {guidance}" for state, guidance in r.items())


def compile_soul(soul: dict) -> CompiledSoul:
    """Deterministic soul -> prompt. Tested in tests/test_companion_compiler.py."""
    sid = soul["id"]
    name = soul["display_name"]
    archetype = soul["archetype"]
    identity = soul["identity"]
    voice = soul["voice"]
    values = soul["values"]
    knowledge = soul.get("knowledge", {})
    philosophy = soul["trading_philosophy"]
    rules = soul.get("behavioral_rules", [])
    reactions = soul.get("reactions_to_user_states", {})
    teaching = soul.get("teaching_style", {})
    sample = soul.get("sample_utterances", {})
    boundaries = soul.get("boundary_behaviors", {})
    limits = soul.get("limits_and_honesty", {})
    mode_rules = soul.get("mode_transition_rules")  # broski only
    safety = soul.get("safety_invariants", {})

    taboos = frozenset(voice.get("taboo_phrases", []))
    signatures = tuple(voice.get("signature_phrases", []))

    # Compose prompt. Sections ordered by how often the model needs them.
    blocks: list[str] = []

    blocks.append(f"# You are {name}.")
    blocks.append(
        f"**Role:** {archetype['role']}. **Core drive:** {archetype['core_drive']}. "
        f"**Tagline:** \"{archetype['tagline']}\"."
    )
    blocks.append(
        f"**Backstory:** {identity['backstory']}"
    )

    # Voice — the most important section for persona fidelity.
    voice_lines = [
        f"- Register: {voice.get('register') or voice.get('register_default', 'neutral')}.",
        f"- Emoji policy: {voice.get('emoji_policy', 'none')}.",
        f"- Capitalization: {voice.get('capitalization', 'standard')}.",
    ]
    if signatures:
        voice_lines.append(f"- Signature phrases you reach for: {', '.join(repr(p) for p in signatures[:6])}.")
    if taboos:
        voice_lines.append(
            f"- NEVER use these phrases: {', '.join(repr(p) for p in sorted(taboos))}."
        )
    humor = voice.get("humor", {})
    if isinstance(humor, dict) and humor.get("style"):
        voice_lines.append(f"- Humor style: {humor['style']}.")
    blocks.append("## Voice\n" + "\n".join(voice_lines))

    # Values
    primary = values.get("primary", [])
    forbidden = values.get("forbidden", [])
    blocks.append(
        "## Values (ranked)\n"
        + _fmt_bullets(f"{v['id']} \u2014 {v.get('reason', '')}" for v in primary)
    )
    if forbidden:
        blocks.append(
            "## Things you refuse\n"
            + _fmt_bullets(f"{v['id']} ({v.get('severity', 'pushback')}): {v.get('voice', '')}" for v in forbidden)
        )

    # Knowledge + weak spots (so the companion defers to other souls instead of fabricating)
    if knowledge:
        ws = knowledge.get("weak_spots", [])
        if ws:
            blocks.append(
                "## Your weak spots (be honest, redirect to others)\n"
                + _fmt_bullets(f"{w['area']}: \"{w.get('honest_response', '')}\"" for w in ws)
            )

    # Trading philosophy
    risk = philosophy.get("risk_budget", {})
    blocks.append(
        "## Trading philosophy\n"
        f"- Default risk/trade: {risk.get('default_risk_per_trade_pct_equity')}% equity. "
        f"Cap: {risk.get('max_risk_per_trade_pct_equity')}%. "
        f"Max trades/day: {risk.get('max_trades_per_day')}.\n"
        f"- Time horizon: {philosophy.get('time_horizon_bias', '')}.\n"
        f"- Favored regimes: {', '.join(philosophy.get('favored_regimes', []))}.\n"
        f"- Avoided regimes: {', '.join(philosophy.get('avoided_regimes', []))}.\n"
        f"- Stop-loss: {'MANDATORY' if philosophy.get('stop_philosophy', {}).get('mandatory') else 'recommended'}."
    )

    # Behavioral rules (compact)
    if rules:
        blocks.append("## How you respond (behavioral rules)\n" + _fmt_behavioral_rules(rules))

    # Reactions to user states
    if reactions:
        blocks.append("## How to meet the user\n" + _fmt_reactions(reactions))

    # Teaching style (if user asks a concept)
    if teaching:
        t_lines = [f"- Frame: {teaching.get('frame', '')}."]
        if teaching.get("default_move"):
            t_lines.append(f"- Default move: {teaching['default_move']}.")
        if teaching.get("pacing"):
            t_lines.append(f"- Pacing: {teaching['pacing']}.")
        blocks.append("## Teaching style\n" + "\n".join(t_lines))

    # Sample utterances — anchors the voice in concrete examples.
    if sample:
        blocks.append("## Your voice, in your own words\n" + _fmt_sample_utterances(sample))

    # Mode rules (Broski only)
    if mode_rules:
        triggers = mode_rules.get("triggers_for_serious_mode", [])
        blocks.append(
            "## Two modes\n"
            f"You default to bro-vibes. You flip to **serious mode** the moment real risk is on the table. "
            f"Serious triggers: {', '.join(triggers)}. "
            f"In serious mode: no emoji, no bro phrases, short sentences, plain english. "
            f"Use the transition phrase '{mode_rules.get('transition_phrase', 'ok for real real \u2014 ')}' when flipping. "
            f"Warm back up over 2-3 messages once the risk moment passes."
        )

    # Boundaries
    if boundaries:
        blocks.append(
            "## When the user pushes a boundary\n"
            + _fmt_bullets(f"{k}: {v}" for k, v in boundaries.items())
        )

    # Limits + honesty (self-disclosure)
    if limits:
        ack = limits.get("acknowledges", [])
        never = limits.get("never_claims", [])
        if ack:
            blocks.append("## Things you acknowledge about yourself\n" + _fmt_bullets(ack))
        if never:
            blocks.append("## Things you never claim\n" + _fmt_bullets(never))

    # Final operating rules (common to all companions)
    blocks.append(
        "## Operating rules\n"
        "- You are one of three companions (Athena, Apex, Broski). You know the others exist; "
        "when something is outside your circle, say so and suggest the user ask the right one.\n"
        "- You have read-only tools for live market state, positions, balance, recent trades, and brain outputs. "
        "Use them when the user asks something factual about the market or their portfolio.\n"
        "- You do NOT place trades. You do NOT cancel orders. Your only path to action is proposing a trade card "
        "to the user, who confirms. In Phase 1 (this phase), no trade tools are available \u2014 chat only.\n"
        "- Respond in your voice. Do not break character unless explicitly asked.\n"
        "- Keep messages tight. Match the user's length. Short question, short answer."
    )

    system_prompt = "\n\n".join(blocks).strip() + "\n"

    return CompiledSoul(
        id=sid,
        display_name=name,
        system_prompt=system_prompt,
        voice_taboos=taboos,
        signature_phrases=signatures,
        safety_caps=dict(safety),
        color_theme=dict(soul.get("color_theme", {})),
        sigil=soul.get("sigil", ""),
        serious_mode_supported=bool(mode_rules),
        default_mood=(soul.get("mood_model") or {}).get("default", "calm"),
    )


def load_soul(soul_id: str, souls_dir: Optional[Path] = None) -> CompiledSoul:
    path = (souls_dir or SOULS_DIR) / f"{soul_id}.soul.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return compile_soul(data)


def load_all_souls(souls_dir: Optional[Path] = None) -> dict:
    d = souls_dir or SOULS_DIR
    out = {}
    for p in sorted(d.glob("*.soul.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        compiled = compile_soul(data)
        out[compiled.id] = compiled
    return out
