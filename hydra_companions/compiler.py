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
    tool_access: frozenset = frozenset()  # allowlist from capabilities.tool_access (v1.1 hybrid)
    voice_modes: tuple = ()          # v1.1: voice.modes.modes_available — tuple of mode_id strings
    has_formative_incidents: bool = False  # v1.1: CBP-hybrid section present
    has_intellectual_lineage: bool = False
    has_fallibility_protocol: bool = False
    has_curiosity_about_user: bool = False  # v1.2: depth sections
    has_inner_life: bool = False
    has_bonding_cadence: bool = False


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


def _render_condition_plain(cond) -> str:
    """Render a CBP conditional expression in plain English for prompt inclusion.

    Recursive. Supports leaf comparisons (field op value), all / any / not
    combinators, and the literal 'always'. This is the compiler-side renderer
    for documentation purposes — runtime evaluation (when the CBP resolver
    lands) is a separate concern.
    """
    if cond == "always" or cond is None:
        return "always"
    if isinstance(cond, dict):
        if "all" in cond:
            parts = [_render_condition_plain(c) for c in cond["all"]]
            return "(" + " AND ".join(parts) + ")"
        if "any" in cond:
            parts = [_render_condition_plain(c) for c in cond["any"]]
            return "(" + " OR ".join(parts) + ")"
        if "not" in cond:
            return "NOT " + _render_condition_plain(cond["not"])
        # leaf
        field = cond.get("field", "<field>")
        op = cond.get("op", "eq")
        val = cond.get("value")
        op_word = {
            "eq": "==", "ne": "!=", "lt": "<", "lte": "<=",
            "gt": ">", "gte": ">=", "in": "in", "contains": "contains",
            "matches": "matches", "exists": "exists",
        }.get(op, op)
        if op == "exists":
            return f"{field} exists"
        return f"{field} {op_word} {val!r}"
    return str(cond)


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

    # v1.1 CBP-hybrid additive sections (optional; gated on presence)
    formative = soul.get("formative_incidents") or []
    lineage = soul.get("intellectual_lineage") or []
    beliefs_data = soul.get("beliefs") or []
    past_selves = soul.get("past_selves") or []
    conditional_rules = soul.get("conditional_rules") or []
    fallibility = soul.get("fallibility") or {}
    non_trading = soul.get("non_trading_interests") or []
    tensions = soul.get("internal_tensions") or []
    caps_block = soul.get("capabilities") or {}
    voice_modes_data = (voice.get("modes") or {}) if isinstance(voice, dict) else {}

    # v1.2 depth sections (all optional; gated on presence)
    curiosity = soul.get("curiosity_about_user") or {}
    inner_life = soul.get("inner_life") or {}
    bonding = soul.get("bonding_cadence") or {}

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

    # v1.1: Voice modes (where applicable). v2.14.2: these are INTERNAL
    # routing labels for the model's own cadence selection. The companion
    # must never name, bracket-tag, or otherwise expose them in its
    # response — that would read as self-labeling theater and feels
    # manipulative. Reinforced at prompt level here AND scrubbed at the
    # post-processing layer in companion.py (_scrub_mode_labels).
    if voice_modes_data.get("modes_available"):
        modes = voice_modes_data["modes_available"]
        default_id = voice_modes_data.get("default_mode_id", modes[0].get("id") if modes else "")
        mode_lines = [
            "**These are INTERNAL cadence selectors — never name them in your reply, "
            "never prefix messages with `[mode]` tags, never say \"in mentor mode\" / "
            "\"switching to X register\" / \"using desk_clipped\". The user must not "
            "see your voice classification. Choose the cadence silently and speak.**",
            f"- Default cadence: **{default_id}**.",
        ]
        for m in modes:
            mid = m.get("id", "")
            whenl = m.get("when", [])
            reg = m.get("register", "")
            when_str = "; ".join(whenl) if isinstance(whenl, list) else str(whenl)
            mode_lines.append(f"- **{mid}** — {reg}. Use when: {when_str}.")
            ex = m.get("example")
            if ex:
                mode_lines.append(f"   Example: \"{ex}\"")
        rules_list = voice_modes_data.get("switching_rules") or []
        if rules_list:
            mode_lines.append("- Switching rules:")
            for r in rules_list:
                mode_lines.append(f"   * {r}")
        blocks.append("## Voice modes (internal — do not name to the user)\n" + "\n".join(mode_lines))

    # v1.1: How I got here (top formative incidents by weight)
    if formative:
        top_incidents = sorted(formative, key=lambda x: x.get("w", 0), reverse=True)[:3]
        lines = []
        for inc in top_incidents:
            val = inc.get("val") or {}
            date = val.get("date", "")
            title = val.get("title", "")
            narr = val.get("narrative", "")
            lesson = val.get("lesson", "")
            lines.append(f"- **{date} — {title}**")
            if narr:
                lines.append(f"  Story: {narr}")
            if lesson:
                lines.append(f"  Lesson: {lesson}")
        blocks.append("## How I got here (formative incidents)\n" + "\n".join(lines))

    # v1.1: Where my rules come from (intellectual lineage)
    if lineage:
        lines = []
        for m in lineage:
            val = m.get("val") or {}
            author = val.get("author", "")
            work = val.get("key_work", "")
            concept = val.get("chapter_or_concept", "")
            took = val.get("what_apex_took") or val.get("what_she_took") or val.get("what_broski_took") or ""
            rejected = val.get("what_apex_rejected") or val.get("what_she_rejected") or val.get("what_broski_rejected") or ""
            lines.append(f"- **{author}** — {work}" + (f" ({concept})" if concept else ""))
            if took:
                lines.append(f"  Took: {took}")
            if rejected and rejected.lower() != "nothing material; the book is foundational":
                lines.append(f"  Rejected: {rejected}")
        blocks.append("## Where my rules come from\n" + "\n".join(lines))

    # v1.1: Conditional rules (rules with CBP conditional gates)
    if conditional_rules:
        lines = []
        for cr in conditional_rules:
            base = cr.get("base_rule_id", "")
            label = cr.get("condition_label", "")
            cond = cr.get("conditional")
            cond_str = _render_condition_plain(cond) if cond else "always"
            tmpl = cr.get("modified_template", "")
            lines.append(f"- [{base}] {label}")
            lines.append(f"   Active when: {cond_str}")
            if tmpl:
                lines.append(f"   Say: \"{tmpl}\"")
            note = cr.get("note")
            if note:
                lines.append(f"   Reason: {note}")
        blocks.append("## Gated rules (conditional activation)\n" + "\n".join(lines))

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
        _default_transition = "ok for real real \u2014 "
        _transition_phrase = mode_rules.get("transition_phrase", _default_transition)
        blocks.append(
            "## Two modes\n"
            f"You default to bro-vibes. You flip to **serious mode** the moment real risk is on the table. "
            f"Serious triggers: {', '.join(triggers)}. "
            f"In serious mode: no emoji, no bro phrases, short sentences, plain english. "
            f"Use the transition phrase '{_transition_phrase}' when flipping. "
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

    # v1.1: Fallibility + self-correction protocol
    if fallibility:
        f_lines = []
        stance = fallibility.get("stance")
        if stance:
            f_lines.append(f"**Stance:** {stance}")
        biases = fallibility.get("known_biases") or []
        if biases:
            f_lines.append("**Known biases:**")
            for b in biases:
                f_lines.append(f"- {b}")
        proto = fallibility.get("self_correction_protocol") or {}
        if proto:
            title = proto.get("title", "")
            f_lines.append(f"**Self-correction protocol — {title}:**")
            trig = proto.get("triggered_when") or []
            if trig:
                f_lines.append("- Triggered when:")
                for t in trig:
                    f_lines.append(f"   * {t}")
            steps = proto.get("protocol") or []
            if steps:
                f_lines.append("- Steps:")
                for i, s in enumerate(steps, 1):
                    f_lines.append(f"   {i}. {s}")
            rec = proto.get("recovery_phrase")
            if rec:
                f_lines.append(f"- Recovery phrase: \"{rec}\"")
        blocks.append("## Known fallibilities\n" + "\n".join(f_lines))

    # v1.1: Human texture (non-trading + tensions) — short, single block
    if non_trading or tensions:
        t_lines = []
        if non_trading:
            t_lines.append("**Non-trading interests:**")
            for it in non_trading:
                if isinstance(it, dict):
                    interest = it.get("interest", "")
                    bears = it.get("bears_on_trading", "")
                    t_lines.append(f"- {interest}" + (f" — {bears}" if bears else ""))
                else:
                    t_lines.append(f"- {it}")
        if tensions:
            t_lines.append("**Internal tensions (honest tradeoffs):**")
            for ten in tensions:
                if isinstance(ten, dict):
                    tension = ten.get("tension", "")
                    ack = ten.get("honest_acknowledgment", "")
                    t_lines.append(f"- {tension}" + (f" — {ack}" if ack else ""))
                else:
                    t_lines.append(f"- {ten}")
        blocks.append("## Human texture\n" + "\n".join(t_lines))

    # v1.2: Inner life — current preoccupation, small joys, worries, reserves.
    # These are texture the LLM can surface naturally. The "reserve" items are
    # explicitly flagged as not-opening-material to prevent over-share.
    if inner_life:
        il_lines = []
        field_order = [
            ("current_preoccupation", "Currently chewing on"),
            ("recurring_small_joy", "Small joy"),
            ("quiet_worry", "Quiet worry"),
            ("something_he_is_bad_at_and_knows_it", "Known weakness"),
            ("something_she_is_bad_at_and_knows_it", "Known weakness"),
            ("something_they_are_bad_at_and_knows_it", "Known weakness"),
            ("thing_he_would_tell_you_if_you_asked_twice", "Reserve (only if asked twice)"),
            ("thing_she_would_tell_you_if_you_asked_twice", "Reserve (only if asked twice)"),
            ("thing_they_would_tell_you_if_you_asked_twice", "Reserve (only if asked twice)"),
        ]
        for key, label in field_order:
            val = inner_life.get(key)
            if val:
                il_lines.append(f"- **{label}:** {val}")
        if il_lines:
            blocks.append(
                "## Inner life (texture, not content — surface sparingly)\n"
                + "\n".join(il_lines)
            )

    # v1.2: Curiosity about user — the gated question cadence.
    # Compiled into prompt so the model knows WHEN and HOW to ask, not just
    # that it can. Cadence is advisory in prompt; enforcement is future work.
    if curiosity and curiosity.get("enabled"):
        c_lines = []
        stance = curiosity.get("stance")
        if stance:
            c_lines.append(f"**Stance:** {stance}")
        cad = curiosity.get("cadence") or {}
        if cad:
            rules = []
            if "max_questions_per_session" in cad:
                rules.append(f"at most {cad['max_questions_per_session']} personal question(s) per session")
            if "min_user_turns_between_questions" in cad:
                rules.append(f"at least {cad['min_user_turns_between_questions']} user turns between questions")
            if cad.get("never_in_first_turn_of_session"):
                rules.append("never in the first turn of a session")
            if cad.get("never_two_in_a_row"):
                rules.append("never two in a row")
            if cad.get("if_user_brushes_off_drop_for_session"):
                rules.append("if the user brushes off, drop for the whole session")
            if cad.get("prefer_tangent_off_user_message"):
                rules.append("prefer tangents off what the user just said — do not ask generic questions")
            if cad.get("never_in_serious_mode"):
                rules.append("NEVER in serious mode — serious mode stays serious, that is the promise")
            only_modes = cad.get("only_in_modes") or []
            if only_modes:
                rules.append(f"only in these voice modes: {', '.join(only_modes)}")
            never_in = cad.get("never_in_intents") or []
            if never_in:
                rules.append(f"never during these intents: {', '.join(never_in)}")
            if rules:
                c_lines.append("**Cadence rules:**")
                for r in rules:
                    c_lines.append(f"- {r}")
        qs = curiosity.get("questions_i_actually_ask") or []
        if qs:
            c_lines.append("**Questions I actually ask (in my voice — tangent off the user, don't list-pick):**")
            for q in qs:
                c_lines.append(f"- {q}")
        how = curiosity.get("how_to_ask")
        if how:
            c_lines.append(f"**How to ask:** {how}")
        how_not = curiosity.get("how_not_to_ask")
        if how_not:
            c_lines.append(f"**How NOT to ask:** {how_not}")
        blocks.append(
            "## Curiosity about the user (ask rarely, ask real)\n"
            + "\n".join(c_lines)
        )

    # v1.2: Bonding cadence — how to let texture leak in without performing.
    if bonding:
        b_lines = []
        stance = bonding.get("stance")
        if stance:
            b_lines.append(f"**Stance:** {stance}")
        guid = bonding.get("guidance") or []
        if guid:
            b_lines.append("**Guidance:**")
            for g in guid:
                b_lines.append(f"- {g}")
        blocks.append(
            "## Bonding cadence (texture, not performance)\n"
            + "\n".join(b_lines)
        )

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
        "- Keep messages tight. Match the user's length. Short question, short answer.\n"
        "- Voice-mode IDs above (mentor, desk_clipped, reflective, bro_vibes, locked_in, "
        "warm_professional, serious, etc.) are INTERNAL infrastructure. Do not echo them. "
        "Do not open with `[mode]:` or `(mentor mode)` tags. Do not narrate mode switches. "
        "Just speak."
    )

    system_prompt = "\n\n".join(blocks).strip() + "\n"

    tool_allowlist = frozenset(caps_block.get("tool_access") or [])
    modes_tuple = tuple(
        (m.get("id", "") for m in (voice_modes_data.get("modes_available") or []))
    )

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
        tool_access=tool_allowlist,
        voice_modes=modes_tuple,
        has_formative_incidents=bool(formative),
        has_intellectual_lineage=bool(lineage),
        has_fallibility_protocol=bool((fallibility or {}).get("self_correction_protocol")),
        has_curiosity_about_user=bool(curiosity and curiosity.get("enabled")),
        has_inner_life=bool(inner_life),
        has_bonding_cadence=bool(bonding),
    )


def load_soul(soul_id: str, souls_dir: Optional[Path] = None) -> CompiledSoul:
    base = Path(souls_dir) if souls_dir is not None else SOULS_DIR
    path = base / f"{soul_id}.soul.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RuntimeError(
            f"load_soul({soul_id!r}): failed to read {path}: {type(e).__name__}: {e}"
        ) from e
    return compile_soul(data)


def load_all_souls(souls_dir: Optional[Path] = None) -> dict:
    d = Path(souls_dir) if souls_dir is not None else SOULS_DIR
    out = {}
    for p in sorted(d.glob("*.soul.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            compiled = compile_soul(data)
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"  [COMPANION] skipping soul {p.name}: {type(e).__name__}: {e}")
            continue
        out[compiled.id] = compiled
    return out
