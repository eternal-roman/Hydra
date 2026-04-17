import { useState, useEffect, useRef, useCallback } from "react";
import "./App.css";

// ═══════════════════════════════════════════════════════════════
// HYDRA Live Dashboard — Connects to hydra_agent.py WebSocket
// ═══════════════════════════════════════════════════════════════

// Override at build time with VITE_HYDRA_WS_URL for non-localhost deployments.
const WS_URL = import.meta.env.VITE_HYDRA_WS_URL || "ws://localhost:8765";

const COLORS = {
  bg: "#0a0a0f",
  panel: "#111118",
  panelBorder: "#1e1e2e",
  accent: "#00ff88",
  danger: "#ff3366",
  warn: "#ffaa00",
  blue: "#3388ff",
  purple: "#8855ff",
  text: "#e8e8f0",
  textDim: "#888899",
  textMuted: "#555566",
  buy: "#00ff88",
  sell: "#ff3366",
  hold: "#ffaa00",
  trendUp: "#00ff88",
  trendDown: "#ff3366",
  ranging: "#ffaa00",
  volatile: "#8855ff",
};

const regimeColor = (r) =>
  ({ TREND_UP: COLORS.trendUp, TREND_DOWN: COLORS.trendDown, RANGING: COLORS.ranging, VOLATILE: COLORS.volatile }[r] || COLORS.textDim);

const getForexSession = () => {
  const h = new Date().getUTCHours();
  if (h >= 12 && h < 16) return { label: "London/NY", color: COLORS.accent };
  if (h >= 7 && h < 12) return { label: "London", color: COLORS.blue };
  if (h >= 16 && h < 21) return { label: "New York", color: COLORS.blue };
  if (h >= 0 && h < 7) return { label: "Asian", color: COLORS.warn };
  return { label: "Dead Zone", color: COLORS.danger };
};

const strategyIcon = (s) =>
  ({ MOMENTUM: "\u{1F680}", MEAN_REVERSION: "\u{1F504}", GRID: "\u{1F4CA}", DEFENSIVE: "\u{1F6E1}\uFE0F" }[s] || "\u26A1");

const signalColor = (s) =>
  ({ BUY: COLORS.buy, SELL: COLORS.sell, HOLD: COLORS.hold }[s] || COLORS.textDim);

const mono = "'JetBrains Mono', monospace";
const heading = "'Space Grotesk', 'JetBrains Mono', monospace";

const fmtPrice = (p, prefix = "$") => {
  if (!p || p === 0) return `${prefix}0`;
  if (p < 0.001) return `${prefix}${p.toFixed(8)}`;
  if (p < 0.01) return `${prefix}${p.toFixed(6)}`;
  if (p < 1) return `${prefix}${p.toFixed(4)}`;
  if (p >= 10000) return `${prefix}${p.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return `${prefix}${p.toFixed(2)}`;
};

// Determine currency prefix for a pair — "$" for USD-quoted, "" for BTC-quoted
const pairPrefix = (pair) => (pair && (pair.endsWith("USDC") || pair.endsWith("USD"))) ? "$" : "";

const fmtInd = (v) => {
  if (v === undefined || v === null) return "—";
  if (Math.abs(v) < 0.01) return v.toFixed(6);
  if (Math.abs(v) < 1) return v.toFixed(4);
  return v.toFixed(2);
};

// ─── Small Components ───

// QuantumIcon — a static nucleus with three electron dots swirling around it
// along three tilted elliptical orbits. Each orbit is drawn as a faint guide
// ring (static); the electrons move via SVG <animateMotion> on that same
// ellipse path, each with a different period + phase offset so they never
// cluster. Nucleus breathes in scale via index.css keyframe. When `active`
// is false the electrons freeze and the whole thing dims.
//
// Pinned to its parent via a constant viewBox + fixed size, so it always
// occupies the same footprint in the AI Brain pill regardless of which
// electron is currently at the far edge of its orbit.
function QuantumIcon({ active = true, size = 14, color }) {
  const c = color || COLORS.blue;
  const dim = !active;
  // Canonical horizontal ellipse centred at (12,12) with rx=9, ry=3.5 —
  // a closed arc through (3,12) and (21,12). Tilt each orbit by wrapping
  // in a rotated <g> so the same path reuses across all three.
  const orbitPath = "M 3 12 A 9 3.5 0 1 1 21 12 A 9 3.5 0 1 1 3 12";
  const orbits = [
    { tilt:   0, dur: "2.8s", phase: "0s"    },
    { tilt:  60, dur: "3.6s", phase: "-0.9s" },
    { tilt: -60, dur: "3.2s", phase: "-1.8s" },
  ];
  return (
    <svg width={size} height={size} viewBox="0 0 24 24"
         style={{ display: "inline-block", flexShrink: 0,
                  opacity: dim ? 0.5 : 1 }}
         aria-hidden="true">
      {/* Guide rings — faint, static. Give the electrons an orbit the eye
          can follow. Slightly bolder (0.35 opacity, 1px stroke) so the atom
          structure reads cleanly against the AI Brain pill's blue-tinted bg. */}
      {orbits.map((o, i) => (
        <ellipse key={`ring-${i}`}
                 cx="12" cy="12" rx="9" ry="3.5"
                 fill="none" stroke={c} strokeOpacity="0.35" strokeWidth="1"
                 transform={`rotate(${o.tilt} 12 12)`} />
      ))}
      {/* Electrons — one per orbit, traveling its tilted ellipse. Each
          <g> tilts the path frame; <animateMotion> drives the circle along
          the canonical ellipse expressed in that tilted frame. */}
      {orbits.map((o, i) => (
        <g key={`e-${i}`} transform={`rotate(${o.tilt} 12 12)`}>
          <circle r="1.7" fill={c}>
            {!dim && (
              <animateMotion
                dur={o.dur} begin={o.phase} repeatCount="indefinite"
                rotate="auto" path={orbitPath} />
            )}
            {/* When dim, freeze the electron at the leftmost point of its
                orbit so the icon still reads as "three electrons on three
                rings" even when no work is happening. */}
            {dim && <set attributeName="transform" to="translate(-9,0)" />}
          </circle>
        </g>
      ))}
      {/* Nucleus — subtle breath via CSS keyframe. */}
      <circle cx="12" cy="12" r="2.4" fill={c}
              style={{ transformOrigin: "12px 12px", transformBox: "fill-box",
                       animation: dim ? "none" : "q-nucleus 2.4s ease-in-out infinite" }} />
    </svg>
  );
}

function StatCard({ label, value, unit, color = COLORS.text }) {
  return (
    <div style={{ padding: "12px 16px", background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 8, flex: "1 1 0" }}>
      <div style={{ fontSize: 10, color: COLORS.textDim, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4, fontFamily: mono }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700, color, fontFamily: heading, letterSpacing: "-0.02em" }}>
        {value}<span style={{ fontSize: 11, fontWeight: 400, opacity: 0.6, marginLeft: 2 }}>{unit}</span>
      </div>
    </div>
  );
}

function MiniChart({ data, width = 280, height = 60, color = COLORS.accent, filled = false, fill = false }) {
  if (!data || data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pts = data.map((v, i) => `${(i / (data.length - 1)) * width},${height - ((v - min) / range) * (height - 4) - 2}`);
  const pathD = `M${pts.join(" L")}`;
  const svgStyle = fill
    ? { display: "block", width: "100%", height: "100%" }
    : { display: "block" };
  return (
    <svg width="100%" height={fill ? "100%" : undefined}
         viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={svgStyle}>
      {filled && <path d={`${pathD} L${width},${height} L0,${height} Z`} fill={color} opacity={0.1} vectorEffect="non-scaling-stroke" />}
      <path d={pathD} fill="none" stroke={color} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function CandleChart({ candles, width = 700, height = 120 }) {
  if (!candles || candles.length < 2) return null;
  const pad = 4;
  const allHigh = Math.max(...candles.map(c => c.h));
  const allLow = Math.min(...candles.map(c => c.l));
  const range = allHigh - allLow || 1;
  const n = candles.length;
  const candleW = Math.max(1, Math.min(8, (width - pad * 2) / n - 1));
  const gap = (width - pad * 2) / n;
  const yScale = (v) => pad + (height - pad * 2) * (1 - (v - allLow) / range);

  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ display: "block" }}>
      {[0.25, 0.5, 0.75].map(pct => {
        const y = pad + (height - pad * 2) * pct;
        return <line key={pct} x1={pad} x2={width - pad} y1={y} y2={y} stroke={COLORS.panelBorder} strokeWidth={0.5} />;
      })}
      {candles.map((c, i) => {
        const x = pad + i * gap + gap / 2;
        const bullish = c.c >= c.o;
        const color = bullish ? COLORS.buy : COLORS.sell;
        const bodyTop = yScale(Math.max(c.o, c.c));
        const bodyBot = yScale(Math.min(c.o, c.c));
        const bodyH = Math.max(1, bodyBot - bodyTop);
        return (
          <g key={i}>
            <line x1={x} x2={x} y1={yScale(c.h)} y2={yScale(c.l)} stroke={color} strokeWidth={0.8} opacity={0.6} />
            <rect x={x - candleW / 2} y={bodyTop} width={candleW} height={bodyH}
              fill={color} stroke={color} strokeWidth={0.5}
              opacity={bullish ? 0.9 : 0.7} rx={0.5}
            />
          </g>
        );
      })}
      <text x={width - pad} y={pad + 8} fill={COLORS.textMuted} fontSize={8} fontFamily={mono} textAnchor="end">
        {fmtInd(allHigh)}
      </text>
      <text x={width - pad} y={height - pad} fill={COLORS.textMuted} fontSize={8} fontFamily={mono} textAnchor="end">
        {fmtInd(allLow)}
      </text>
    </svg>
  );
}

function ConfidenceMeter({ confidence, signal }) {
  const w = Math.max(5, confidence * 100);
  return (
    <div style={{ padding: "8px 0" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontSize: 11, color: COLORS.textDim, fontFamily: mono, textTransform: "uppercase" }}>Signal Confidence</span>
        <span style={{ fontSize: 13, fontWeight: 700, color: signalColor(signal), fontFamily: mono }}>{signal} {(confidence * 100).toFixed(0)}%</span>
      </div>
      <div style={{ height: 4, background: COLORS.panelBorder, borderRadius: 2, overflow: "hidden" }}>
        <div style={{ width: `${w}%`, height: "100%", background: signalColor(signal), borderRadius: 2, transition: "width 0.3s", boxShadow: `0 0 8px ${signalColor(signal)}60` }} />
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// Phase 8 (v2.10.0): Backtest UI primitives
// ═══════════════════════════════════════════════════════════════

// Cap on the number of experiments whose per-pair equity history the
// dashboard keeps in memory. A long-running session otherwise leaks ~60
// floats/tick * pairs * experiments. LRU-ish: newest wins, oldest drop.
const MAX_EQUITY_HISTORY_EXPERIMENTS = 10;

// Known top-level keys on the legacy raw-state dict (compat_mode=true
// broadcaster shape, from hydra_agent._build_dashboard_state). Used to
// guard the fallback path from accidentally treating a malformed typed
// message as live state.
const LIVE_STATE_KEYS = [
  "pairs", "order_journal", "journal_stats", "balance", "balance_usd",
  "ai_brain", "timestamp", "running", "mode", "fee_tier",
];

// Must stay in lockstep with hydra_experiments.PRESET_LIBRARY keys +
// hydra_backtest_tool.BACKTEST_TOOLS enum. Order here = order shown in the UI.
const PRESET_OPTIONS = [
  { name: "default",          label: "Default",          desc: "Current live params (no overrides)" },
  { name: "ideal",            label: "Ideal (Tuner)",    desc: "Best params learned by the live tuner" },
  { name: "divergent",        label: "Divergent",        desc: "Loosened gates + wider RSI" },
  { name: "aggressive",       label: "Aggressive",       desc: "Competition sizing, lower threshold" },
  { name: "defensive",        label: "Defensive",        desc: "High conf threshold, narrower RSI" },
  { name: "regime_trending",  label: "Regime: Trending", desc: "Tuned for TREND_UP/DOWN" },
  { name: "regime_ranging",   label: "Regime: Ranging",  desc: "Tuned for RANGING" },
  { name: "regime_volatile",  label: "Regime: Volatile", desc: "Tuned for VOLATILE" },
];

// Rigor gates — 7 code-enforced checks that must all pass before a param
// tweak is auto-apply eligible. Backend keys (hydra_reviewer.py) ↔ plain-English
// pill labels + tooltips shown in the dashboard.
const RIGOR_GATES = [
  {
    key: "min_trades_50",
    label: "Sample Size",
    why: "Need ≥50 trades. Any metric built on fewer is statistical noise — you can't tell signal from randomness.",
  },
  {
    key: "mc_ci_lower_positive",
    label: "MC Confidence",
    why: "Monte Carlo bootstrap: resamples the trade list thousands of times. The 95% CI lower bound on return must stay positive — profits survive re-ordering the trades.",
  },
  {
    key: "wf_majority_improved",
    label: "Walk-Forward",
    why: "Slides train/test windows across the candle series. A majority of windows must improve vs. baseline — guards against curve-fitting to one specific period.",
  },
  {
    key: "oos_gap_acceptable",
    label: "OOS Gap",
    why: "Out-of-sample performance must stay within tolerance of in-sample. A big gap means the params memorised the training data instead of learning a pattern.",
  },
  {
    key: "improvement_above_2se",
    label: "Signal vs. Noise",
    why: "Improvement over baseline must exceed 2 standard errors — i.e., statistically meaningful, not just a lucky draw.",
  },
  {
    key: "cross_pair_majority",
    label: "Cross-Pair",
    why: "The edge must hold across a majority of traded pairs. Catches flukes where one pair (e.g., SOL) carries the win while BTC and SOL/BTC regress.",
  },
  {
    key: "regime_not_concentrated",
    label: "Regime Spread",
    why: "P&L must not be concentrated in one market regime. If all gains come from a single volatile week, the result is unlikely to repeat.",
  },
];

// ─── Companion subsystem (v2.10.4+) ───
// Renders an orb + drawer + chat UI. All WS messages use the `companion.*`
// namespace and do not interfere with LIVE/BACKTEST/COMPARE. When the
// backend subsystem is disabled the orb never receives a `companion.hello`
// and stays invisible.

// Companion themes drawn from the existing Hydra palette so the drawer
// visually belongs to the dashboard. Athena takes the regal purple
// (wise, mystical), Apex the precise blue (professional), Broski the
// fiery amber (high-energy, warm).
const COMPANION_THEMES = {
  athena: { primary: COLORS.purple,  accent: COLORS.purple, glow: COLORS.purple, sigil: "\u26B2" },
  apex:   { primary: COLORS.blue,    accent: COLORS.blue,   glow: COLORS.blue,   sigil: "\u25B2" },
  broski: { primary: COLORS.warn,    accent: COLORS.warn,   glow: COLORS.warn,   sigil: "\u2736" },
};
const COMPANION_ORDER = ["athena", "apex", "broski"];
const COMPANION_NAMES = { athena: "Athena", apex: "Apex", broski: "Broski" };

// Per-soul rhythm + easing. Each companion breathes at their own pace
// and shape — Athena is slow and deep (patient), Apex is steady and
// precise (metronome), Broski is quick and slightly irregular (excited).
// Regime acts as a subtle modulator on top: VOLATILE compresses the
// cycle, RANGING stretches it, so the orb still tracks market state.
const SOUL_RHYTHM = {
  athena: { baseSeconds: 4.2, scaleMax: 1.045, easing: "cubic-bezier(0.4, 0, 0.6, 1)" },
  apex:   { baseSeconds: 2.9, scaleMax: 1.038, easing: "ease-in-out" },
  broski: { baseSeconds: 2.1, scaleMax: 1.060, easing: "cubic-bezier(0.65, 0, 0.35, 1)" },
};

function CompanionOrb({ theme, onClick, regime, hasUnread, visible, soulId }) {
  if (!visible) return null;
  const rhythm = SOUL_RHYTHM[soulId] || SOUL_RHYTHM.apex;
  // Regime modulator: volatile compresses the cycle by ~25%, ranging
  // stretches by ~25%. TREND_* leave it at the soul's base cadence.
  const regimeMult = regime === "VOLATILE" ? 0.75 : regime === "RANGING" ? 1.25 : 1.0;
  const pulseDuration = `${(rhythm.baseSeconds * regimeMult).toFixed(2)}s`;
  // Base glow ring values bumped ~15% vs previous (16px/28px -> 18/32, plus
  // a third outer halo layer for depth). Alpha nudged up too.
  const restInset = `0 0 4px ${theme.primary}dd inset`;
  const peakInset = `0 0 6px ${theme.primary}ff inset`;
  const restGlow  = `0 0 18px ${theme.glow}80, 0 0 34px ${theme.glow}33`;
  const peakGlow  = `0 0 32px ${theme.glow}c0, 0 0 56px ${theme.glow}55`;
  // Broski gets an extra mid-cycle "catch" in the breathing curve so it
  // feels a touch irregular. Apex and Athena are symmetric.
  const breatheKeyframes = soulId === "broski"
    ? `@keyframes hc-breathe-${soulId} { 0%,100% { transform: scale(1.00);} 42% { transform: scale(${rhythm.scaleMax});} 58% { transform: scale(${(1 + (rhythm.scaleMax - 1) * 0.85).toFixed(4)});} }`
    : `@keyframes hc-breathe-${soulId} { 0%,100% { transform: scale(1.00);} 50% { transform: scale(${rhythm.scaleMax});} }`;
  const glowKeyframes =
    `@keyframes hc-glow-${soulId} { 0%,100% { box-shadow: ${restGlow}, ${restInset};} 50% { box-shadow: ${peakGlow}, ${peakInset};} }`;
  return (
    <>
      <style>{breatheKeyframes}{glowKeyframes}</style>
      <button
        onClick={onClick}
        aria-label={`Open companion drawer`}
        title="Click: open \u2022 \u2328 Esc: close"
        style={{
          position: "fixed", right: 24, bottom: 24, zIndex: 9000,
          width: 56, height: 56, borderRadius: "50%",
          background: `radial-gradient(circle at 35% 30%, ${theme.primary}, ${theme.primary}aa 55%, ${COLORS.panel})`,
          border: `2px solid ${theme.primary}`,
          cursor: "pointer", padding: 0,
          animation: `hc-breathe-${soulId} ${pulseDuration} ${rhythm.easing} infinite, hc-glow-${soulId} ${pulseDuration} ${rhythm.easing} infinite`,
          display: "flex", alignItems: "center", justifyContent: "center",
          color: COLORS.text, fontSize: 22, fontFamily: heading, fontWeight: 700,
          textShadow: `0 0 9px ${theme.glow}`,
        }}>
        <span style={{ pointerEvents: "none" }}>{theme.sigil}</span>
        {hasUnread && (
          <span style={{
            position: "absolute", top: 4, right: 4, width: 10, height: 10,
            borderRadius: "50%", background: theme.glow,
            boxShadow: `0 0 8px ${theme.glow}`,
          }} />
        )}
      </button>
    </>
  );
}

function CompanionSwitcher({ active, onSwitch }) {
  // The three IDs are well-known; always enabled. Metadata from the
  // backend just refines the display name / mood; clicking works even
  // before connect_ack lands.
  return (
    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
      {COMPANION_ORDER.map((cid) => {
        const theme = COMPANION_THEMES[cid];
        const isActive = active === cid;
        return (
          <button
            key={cid}
            onClick={() => onSwitch(cid)}
            title={COMPANION_NAMES[cid]}
            style={{
              width: 30, height: 30, borderRadius: "50%",
              background: isActive
                ? `radial-gradient(circle at 35% 30%, ${theme.primary}, ${theme.primary}88)`
                : "transparent",
              border: isActive
                ? `1px solid ${theme.glow}`
                : `1px solid ${COLORS.panelBorder}`,
              color: isActive ? COLORS.text : COLORS.textDim,
              fontFamily: heading, fontWeight: 700, fontSize: 13,
              cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
              transition: "all 180ms ease",
              padding: 0,
            }}>
            {theme.sigil}
          </button>
        );
      })}
    </div>
  );
}

function CompanionMessage({ m, theme, userTheme }) {
  const isUser = m.role === "user";
  const isProactive = m.proactive === true;
  return (
    <div style={{
      display: "flex", flexDirection: "column",
      alignItems: isUser ? "flex-end" : "flex-start",
      margin: "6px 0",
    }}>
      {!isUser && m.display_name && (
        <div style={{ fontSize: 9, color: theme.accent, fontFamily: mono,
                      letterSpacing: "0.08em", marginBottom: 2, marginLeft: 12, textTransform: "uppercase" }}>
          {m.display_name}{isProactive ? " \u00b7 unprompted" : ""}
        </div>
      )}
      <div style={{
        maxWidth: "85%",
        padding: "8px 12px",
        borderRadius: 8,
        borderLeft: isUser ? "none" : `2px solid ${theme.primary}`,
        background: isUser ? `${COLORS.accent}12` : `${COLORS.panel}`,
        border: isUser ? `1px solid ${COLORS.accent}33` : `1px solid ${COLORS.panelBorder}`,
        color: COLORS.text,
        fontFamily: mono,
        fontSize: 12, lineHeight: 1.5,
        whiteSpace: "pre-wrap", wordBreak: "break-word",
      }}>
        {m.text}
        {m.error && (
          <div style={{ marginTop: 6, fontSize: 10, color: COLORS.red, fontFamily: mono }}>
            {m.error}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Proposal cards (Phase 2+) ───
function fmtPxShort(p) {
  if (p == null) return "—";
  return Number(p) < 100 ? Number(p).toFixed(4) : Number(p).toFixed(2);
}

function ProposalCard({ proposal, kind, theme, onConfirm, onReject, status, onStatusReset }) {
  // kind: "trade" | "ladder"
  // status: null | "armed" | "submitting" | "filled" | "rejected" | "failed" | "expired"
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [armed, setArmed] = useState(false);
  const armRef = useRef(null);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 250);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 5000);
    armRef.current = t;
    return () => clearTimeout(t);
  }, [armed]);

  const ttlTotal = Math.max(1, (proposal.expires_at - proposal.created_at) || 60);
  const remaining = Math.max(0, proposal.expires_at - now);
  const pctLeft = Math.max(0, Math.min(1, remaining / ttlTotal));
  const ttlColor = pctLeft > 0.5 ? theme.primary : pctLeft > 0.2 ? COLORS.warn : COLORS.danger;
  const expired = remaining <= 0;

  const locked = !!status;  // once submitted/filled/rejected, disable buttons

  const handlePrimary = () => {
    if (locked || expired) return;
    if (!armed) {
      setArmed(true);
      return;
    }
    clearTimeout(armRef.current);
    setArmed(false);
    onConfirm();
  };

  const sideColor = proposal.side === "buy" ? COLORS.buy : COLORS.sell;

  return (
    <div style={{
      margin: "8px 0", border: `1px solid ${theme.primary}66`,
      borderRadius: 10, overflow: "hidden",
      background: `${theme.primary}10`,
      opacity: expired && !status ? 0.55 : 1,
      transition: "opacity 240ms",
    }}>
      {/* TTL bar */}
      <div style={{ height: 3, background: `${theme.primary}22` }}>
        <div style={{ height: "100%", width: `${pctLeft * 100}%`, background: ttlColor,
                      transition: "width 250ms linear" }} />
      </div>
      <div style={{ padding: "10px 12px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <span style={{
            background: sideColor, color: COLORS.bg, fontWeight: 700,
            padding: "2px 8px", borderRadius: 4, fontFamily: mono,
            fontSize: 10, letterSpacing: "0.08em",
          }}>{proposal.side.toUpperCase()}</span>
          <span style={{ color: COLORS.text, fontFamily: mono, fontSize: 12, fontWeight: 700 }}>
            {proposal.pair}
          </span>
          <span style={{ color: COLORS.textMuted, fontFamily: mono, fontSize: 10, marginLeft: "auto" }}>
            {kind === "ladder" ? `${proposal.rungs.length} rungs` : "1R"}
          </span>
        </div>

        {kind === "trade" ? (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 12px",
                        fontSize: 11, fontFamily: mono, color: COLORS.text }}>
            <div><span style={{ color: COLORS.textMuted }}>Size:</span> {proposal.size}</div>
            <div><span style={{ color: COLORS.textMuted }}>Limit:</span> ${fmtPxShort(proposal.limit_price)}</div>
            <div><span style={{ color: COLORS.textMuted }}>Stop:</span> ${fmtPxShort(proposal.stop_loss)}</div>
            <div><span style={{ color: COLORS.textMuted }}>Cost:</span> ${fmtPxShort(proposal.estimated_cost)}</div>
            <div style={{ gridColumn: "1 / span 2" }}>
              <span style={{ color: COLORS.textMuted }}>Risk:</span>{" "}
              ${Number(proposal.risk_usd || 0).toFixed(2)}
              {proposal.risk_pct_equity ? ` (${Number(proposal.risk_pct_equity).toFixed(2)}% equity)` : ""}
            </div>
          </div>
        ) : (
          <div>
            <div style={{ fontSize: 10, color: COLORS.textMuted, marginBottom: 4, fontFamily: mono }}>
              {`total ${proposal.total_size} \u00b7 stop $${fmtPxShort(proposal.stop_loss)} \u00b7 invalidate $${fmtPxShort(proposal.invalidation_price)}`}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "auto 1fr auto", gap: "2px 8px",
                          fontSize: 10, fontFamily: mono }}>
              {proposal.rungs.flatMap((r, i) => [
                <span key={`l-${i}`} style={{ color: COLORS.textMuted }}>R{i + 1}</span>,
                <span key={`p-${i}`} style={{ color: COLORS.text }}>${fmtPxShort(r.limit_price)}</span>,
                <span key={`w-${i}`} style={{ color: COLORS.textDim }}>{Math.round(r.pct_of_total * 100)}%</span>,
              ])}
            </div>
          </div>
        )}

        {proposal.rationale && (
          <div style={{ marginTop: 8, fontSize: 11, fontStyle: "italic",
                        color: theme.accent, lineHeight: 1.35,
                        borderLeft: `2px solid ${theme.primary}44`, paddingLeft: 8 }}>
            "{proposal.rationale}"
          </div>
        )}

        {status && (
          <div style={{
            marginTop: 8, padding: "4px 8px", borderRadius: 4, display: "inline-block",
            background: status === "filled" ? `${COLORS.accent}22`
                     : status === "failed" || status === "rejected" ? `${COLORS.danger}22`
                     : `${theme.primary}22`,
            color: status === "filled" ? COLORS.accent
                : status === "failed" || status === "rejected" ? COLORS.danger
                : theme.primary,
            fontSize: 10, fontFamily: mono, fontWeight: 700, letterSpacing: "0.08em",
            textTransform: "uppercase",
          }}>{status}</div>
        )}

        {!locked && !expired && (
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <button onClick={handlePrimary} style={{
              flex: 1, padding: "8px 12px", borderRadius: 4,
              background: armed ? COLORS.danger : theme.primary,
              color: COLORS.bg, border: "none", cursor: "pointer",
              fontFamily: mono, fontSize: 11, fontWeight: 700,
              letterSpacing: "0.08em", textTransform: "uppercase",
              transition: "background 160ms",
            }}>
              {armed ? "\u25B6 send (5s)" : "arm"}
            </button>
            <button onClick={onReject} style={{
              padding: "8px 12px", borderRadius: 5,
              background: "transparent", color: COLORS.textMuted,
              border: `1px solid ${COLORS.panelBorder}`, cursor: "pointer",
              fontFamily: mono, fontSize: 11,
            }}>reject</button>
          </div>
        )}
        {expired && !locked && (
          <div style={{ marginTop: 10, fontSize: 10, color: COLORS.textMuted, fontFamily: mono }}>
            {"expired \u2014 ask again"}
          </div>
        )}
      </div>
    </div>
  );
}

function CompanionTypingBubble({ theme, name }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", margin: "6px 0" }}>
      <div style={{ fontSize: 9, color: theme.accent, fontFamily: mono,
                    letterSpacing: "0.08em", marginBottom: 2, marginLeft: 12, textTransform: "uppercase" }}>
        {name}
      </div>
      <div style={{
        padding: "8px 14px", borderRadius: 8, borderLeft: `2px solid ${theme.primary}`,
        background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
        display: "inline-flex", gap: 4,
      }}>
        <style>{`@keyframes hc-dot { 0%,80%,100% { opacity: 0.3; transform: translateY(0);} 40% { opacity: 1; transform: translateY(-3px);} }`}</style>
        {[0, 1, 2].map((i) => (
          <span key={i} style={{
            width: 6, height: 6, borderRadius: "50%", background: theme.primary,
            animation: `hc-dot 1.2s ease-in-out ${i * 0.15}s infinite`,
          }} />
        ))}
      </div>
    </div>
  );
}

function CompanionDrawer({
  open, onClose, active, onSwitch, companions, messages, typing,
  onSend, onProposalConfirm, onProposalReject, connected, drawerWidth, onResize, costAlerts,
}) {
  const theme = COMPANION_THEMES[active] || COMPANION_THEMES.apex;
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  // Auto-scroll to bottom on new messages / typing
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, typing, active]);

  // Focus composer on open / active-change
  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open, active]);

  // Esc closes
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const submit = () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    onSend(text);
    setDraft("");
    setTimeout(() => setSending(false), 250);
  };

  const onKey = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  if (!open) return null;
  const comp = companions[active];
  const name = comp?.display_name || COMPANION_NAMES[active] || "Companion";
  const alert = costAlerts[active];

  return (
    <div style={{
      position: "fixed", top: 0, right: 0, bottom: 0, width: drawerWidth,
      zIndex: 9000, background: `${COLORS.panel}f0`, backdropFilter: "blur(14px)",
      borderLeft: `1px solid ${theme.primary}66`,
      boxShadow: `-8px 0 32px rgba(0,0,0,0.5), inset 2px 0 0 ${theme.primary}44`,
      display: "flex", flexDirection: "column",
      animation: `hc-slide-in 260ms cubic-bezier(0.32, 0.72, 0, 1), hc-drawer-glow-${active} ${(SOUL_RHYTHM[active]?.baseSeconds || 3)}s ${SOUL_RHYTHM[active]?.easing || "ease-in-out"} infinite`,
      fontFamily: mono,
    }}>
      <style>{`
        @keyframes hc-slide-in { from { transform: translateX(100%);} to { transform: translateX(0);} }
        @keyframes hc-drawer-glow-${active} {
          0%,100% {
            box-shadow: -8px 0 32px rgba(0,0,0,0.5),
                        inset 2px 0 0 ${theme.primary}44,
                        inset 0 0 40px ${theme.primary}08;
            border-left-color: ${theme.primary}66;
          }
          50% {
            box-shadow: -8px 0 44px rgba(0,0,0,0.55),
                        inset 3px 0 0 ${theme.primary}88,
                        inset 0 0 70px ${theme.primary}16;
            border-left-color: ${theme.primary}aa;
          }
        }
      `}</style>

      {/* Header */}
      <div style={{
        padding: "12px 14px", display: "flex", alignItems: "center", gap: 10,
        borderBottom: `1px solid ${theme.primary}33`,
        background: `linear-gradient(90deg, ${theme.primary}22, transparent)`,
      }}>
        <div style={{
          width: 32, height: 32, borderRadius: "50%",
          background: `radial-gradient(circle at 35% 30%, ${theme.accent}, ${theme.primary})`,
          display: "flex", alignItems: "center", justifyContent: "center",
          color: "#fff", fontFamily: heading, fontWeight: 700, fontSize: 14,
        }}>{theme.sigil}</div>
        <div style={{ flex: 1 }}>
          <div style={{ fontFamily: heading, fontSize: 14, fontWeight: 700, color: COLORS.text }}>{name}</div>
          <div style={{ fontSize: 9, color: theme.accent, letterSpacing: "0.08em", textTransform: "uppercase" }}>
            {comp?.mood || "calm"}{comp?.serious_mode ? " \u00b7 serious" : ""}
          </div>
        </div>
        <CompanionSwitcher active={active} onSwitch={onSwitch} />
        <button onClick={onClose} aria-label="Close drawer" title="Close" style={{
          background: "transparent", border: `1px solid ${COLORS.panelBorder}`,
          color: COLORS.textMuted, cursor: "pointer", borderRadius: 4,
          padding: "4px 10px", fontFamily: mono, fontSize: 13, lineHeight: 1,
        }}>{"\u00D7"}</button>
      </div>

      {alert && (
        <div style={{
          padding: "6px 14px", fontSize: 10, background: `${theme.glow}22`,
          borderBottom: `1px solid ${theme.glow}44`, color: theme.accent, fontFamily: mono,
        }}>
          budget alert: ${alert.daily_cost_usd} of ${alert.hard_stop_usd} used
        </div>
      )}

      {/* Messages */}
      <div ref={scrollRef} style={{
        flex: 1, overflowY: "auto", padding: "10px 14px",
      }}>
        {messages.length === 0 && !typing && (
          <div style={{ color: COLORS.textMuted, fontSize: 11, marginTop: 20, textAlign: "center" }}>
            {`say hi to ${name.toLowerCase()} \u2014 or type `}
            <code style={{ color: theme.accent }}>/help</code>
          </div>
        )}
        {messages.map((m) => {
          if (m.role === "proposal") {
            return (
              <ProposalCard
                key={m.id}
                proposal={m.proposal}
                kind={m.kind || "trade"}
                theme={theme}
                status={m.status}
                onConfirm={() => onProposalConfirm(m)}
                onReject={() => onProposalReject(m)}
                onStatusReset={() => {}}
              />
            );
          }
          return <CompanionMessage key={m.id} m={m} theme={theme} userTheme={COLORS.accent} />;
        })}
        {typing && <CompanionTypingBubble theme={theme} name={name} />}
      </div>

      {/* Composer */}
      <div style={{
        borderTop: `1px solid ${theme.primary}33`,
        padding: "10px 12px", background: `${COLORS.panel}ee`,
      }}>
        <textarea
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          placeholder={active === "apex" ? "message apex \u2014"
                     : active === "athena" ? "speak to Athena\u2026"
                     : "yo what's up"}
          disabled={!connected}
          style={{
            width: "100%", minHeight: 40, maxHeight: 140, resize: "none",
            background: `${COLORS.bg}cc`, color: COLORS.text,
            border: `1px solid ${theme.primary}55`, borderRadius: 6,
            padding: "8px 10px", fontFamily: mono, fontSize: 13, lineHeight: 1.4,
            outline: "none",
          }}
        />
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 6 }}>
          <div style={{ fontSize: 9, color: COLORS.textMuted, fontFamily: mono }}>
            {"\u21B5 send \u00b7 Shift+\u21B5 newline \u00b7 Esc close"}
          </div>
          <button
            onClick={submit}
            disabled={!draft.trim() || !connected || sending}
            style={{
              background: draft.trim() && connected ? theme.primary : `${theme.primary}44`,
              color: "#fff", border: "none", borderRadius: 4,
              padding: "6px 14px", fontFamily: mono, fontSize: 11, fontWeight: 700,
              cursor: draft.trim() && connected ? "pointer" : "default",
              letterSpacing: "0.08em", textTransform: "uppercase",
            }}>
            send
          </button>
        </div>
      </div>
    </div>
  );
}

function TabSwitcher({ activeTab, onChange, backtestRunning }) {
  const tabs = [
    { key: "LIVE",     label: "LIVE",     color: COLORS.accent },
    { key: "BACKTEST", label: "BACKTEST", color: COLORS.blue },
    { key: "COMPARE",  label: "COMPARE",  color: COLORS.purple },
  ];
  return (
    // Gap: 10 puts visible air between each tab so the row breathes.
    // minHeight: 38 + flex centers content to match the AI Brain pill's
    // icon-bearing height exactly.
    <div style={{ display: "flex", gap: 10, padding: "8px 0" }}>
      {tabs.map(t => {
        const active = activeTab === t.key;
        return (
          <button
            key={t.key}
            onClick={() => onChange(t.key)}
            style={{
              padding: "0 18px",
              minHeight: 38,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 12,
              fontWeight: 700,
              fontFamily: mono,
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              background: active ? `${t.color}18` : "transparent",
              color: active ? t.color : COLORS.textDim,
              border: `1px solid ${active ? t.color + "60" : COLORS.panelBorder}`,
              borderRadius: 4,
              cursor: "pointer",
              outline: "none",
              transition: "all 0.15s ease",
            }}
          >
            {t.label}
            {t.key === "BACKTEST" && backtestRunning ? (
              <span style={{ marginLeft: 6, display: "inline-block", width: 6, height: 6,
                             borderRadius: "50%", background: COLORS.blue, boxShadow: `0 0 4px ${COLORS.blue}` }} />
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

function FieldLabel({ children, hint, labelSize = 9, hintSize = 10 }) {
  return (
    <div style={{ marginBottom: 4 }}>
      <div style={{ fontSize: labelSize, color: COLORS.textDim, textTransform: "uppercase",
                    letterSpacing: "0.1em", fontFamily: mono, fontWeight: 600 }}>
        {children}
      </div>
      {hint && <div style={{ fontSize: hintSize, color: COLORS.textMuted, fontFamily: mono, marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

function StyledInput({ value, onChange, placeholder, type = "text", fontSize = 12, padding = "7px 10px", ...rest }) {
  return (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      style={{
        width: "100%",
        padding,
        background: COLORS.bg,
        color: COLORS.text,
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 4,
        fontSize,
        fontFamily: mono,
        outline: "none",
        boxSizing: "border-box",
      }}
      onFocus={(e) => (e.target.style.borderColor = COLORS.blue)}
      onBlur={(e) => (e.target.style.borderColor = COLORS.panelBorder)}
      {...rest}
    />
  );
}

function StyledSelect({ value, onChange, options, fontSize = 12, padding = "7px 10px" }) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      style={{
        width: "100%",
        padding,
        background: COLORS.bg,
        color: COLORS.text,
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 4,
        fontSize,
        fontFamily: mono,
        outline: "none",
      }}
    >
      {options.map(o => (
        <option key={o.name} value={o.name} style={{ background: COLORS.panel }}>
          {o.label} — {o.desc}
        </option>
      ))}
    </select>
  );
}

function StyledTextarea({ value, onChange, placeholder, minHeight = 70, fontSize = 12, padding = "8px 10px" }) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      style={{
        width: "100%",
        minHeight,
        padding,
        background: COLORS.bg,
        color: COLORS.text,
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 4,
        fontSize,
        fontFamily: mono,
        outline: "none",
        resize: "vertical",
        boxSizing: "border-box",
      }}
      onFocus={(e) => (e.target.style.borderColor = COLORS.blue)}
      onBlur={(e) => (e.target.style.borderColor = COLORS.panelBorder)}
    />
  );
}

function Checkbox({ checked, onChange, label, hint }) {
  return (
    <label style={{ display: "flex", alignItems: "flex-start", gap: 8, cursor: "pointer", fontFamily: mono, fontSize: 11, color: COLORS.text }}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)}
             style={{ marginTop: 2, accentColor: COLORS.blue, cursor: "pointer" }} />
      <span>
        {label}
        {hint && <div style={{ fontSize: 9, color: COLORS.textMuted, marginTop: 2 }}>{hint}</div>}
      </span>
    </label>
  );
}

function BacktestControlPanel({ onSubmit, connected, disabled, ackMsg, lastResultId,
                                completedCount = 0, reviewedCount = 0,
                                observerProgress = null, observerResult = null,
                                observerReview = null, observerEquity = null,
                                observerTotalTicks = 0, onObserverClose = null,
                                onCompareThisRun = null }) {
  const [preset, setPreset] = useState("default");
  const [hypothesis, setHypothesis] = useState("");
  const [pairs, setPairs] = useState("SOL/USDC");
  const [nCandles, setNCandles] = useState(500);
  const [seed, setSeed] = useState(42);
  const [withMC, setWithMC] = useState(true);
  const [withWF, setWithWF] = useState(false);

  const hypothesisValid = hypothesis.trim().length >= 8;
  const nCandlesNum = Number(nCandles);
  const nCandlesValid = Number.isFinite(nCandlesNum) && nCandlesNum >= 50 && nCandlesNum <= 20000;
  const canSubmit = connected && !disabled && hypothesisValid && nCandlesValid;

  const submit = () => {
    if (!canSubmit) return;
    // Mirrors hydra_backtest_server._start handler payload.
    // BacktestConfig requires JSON-encoded dict fields (frozen-safe) — this
    // keeps `config` shape identical to what `BacktestConfig(...)` accepts.
    const config = {
      name: `dashboard:${preset}`,
      description: "dashboard-submitted run",
      hypothesis: hypothesis.trim(),
      pairs: pairs.split(",").map(p => p.trim()).filter(Boolean),
      initial_balance_per_pair: 100.0,
      candle_interval: 15,
      mode: preset === "aggressive" ? "competition" : "conservative",
      param_overrides_json: "{}",
      coordinator_enabled: true,
      data_source: "synthetic",
      data_source_params_json: JSON.stringify({
        kind: "gbm", n_candles: nCandlesNum, seed: Number(seed), volatility: 0.02,
      }),
      fill_model: "realistic",
      maker_fee_bps: 16.0,
      real_time_factor: 0.0,
      random_seed: Number(seed),
      max_ticks: 200000,
    };
    onSubmit({
      type: "backtest_start",
      config,
      hypothesis: hypothesis.trim(),
      triggered_by: "dashboard",
      tags: ["caller:dashboard", `preset:${preset}`, ...(withMC ? ["mc"] : []), ...(withWF ? ["wf"] : [])],
    });
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "360px 1fr", gap: 16, alignItems: "stretch",
                   height: "calc(100vh - 140px)", minHeight: 520 }}>
      {/* LEFT: control form */}
      <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                    borderRadius: 8, padding: 20, alignSelf: "stretch",
                    overflowY: "auto", minHeight: 0 }}>
        <div style={{ fontSize: 15, fontFamily: heading, fontWeight: 700, color: COLORS.text,
                      marginBottom: 18, display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: COLORS.blue }}>▶</span> Run Backtest
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <div>
            <FieldLabel labelSize={11} hintSize={12}>Preset</FieldLabel>
            <StyledSelect value={preset} onChange={setPreset} options={PRESET_OPTIONS} fontSize={14} padding="8px 10px" />
          </div>

          <div>
            <FieldLabel labelSize={11} hintSize={11} hint="Min 8 chars · AI-reviewed.">Hypothesis *</FieldLabel>
            <StyledTextarea
              value={hypothesis}
              onChange={setHypothesis}
              placeholder="e.g., tighter RSI upper should reduce false BUYs in VOLATILE regime"
              fontSize={14}
              padding="9px 12px"
            />
            {!hypothesisValid && hypothesis.length > 0 && (
              <div style={{ fontSize: 12, color: COLORS.danger, fontFamily: mono, marginTop: 4 }}>
                {8 - hypothesis.trim().length} more character(s) required
              </div>
            )}
          </div>

          <div>
            <FieldLabel labelSize={11} hintSize={12}>Pairs (comma-separated)</FieldLabel>
            <StyledInput value={pairs} onChange={setPairs} placeholder="SOL/USDC,BTC/USDC" fontSize={14} padding="8px 12px" />
          </div>

          {/* Candles + Seed: label row, input row, hint row — each grid row aligned
              so the two inputs sit on the same Y regardless of hint wrapping. */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr",
                        columnGap: 12, rowGap: 6 }}>
            <FieldLabel labelSize={11} hintSize={12}>
              <span title="Number of simulated 15-minute candles the synthetic GBM generator will produce for this run. Not historical market data.">
                Synthetic Candles ⓘ
              </span>
            </FieldLabel>
            <FieldLabel labelSize={11} hintSize={12}>
              <span title="Experiment seed for the synthetic (GBM) price generator. Identical seed + identical params reproduce an identical candle series, so two runs are directly comparable.">
                Experiment Seed ⓘ
              </span>
            </FieldLabel>

            <StyledInput value={nCandles} onChange={setNCandles} type="number" fontSize={14} padding="8px 12px" />
            <StyledInput value={seed} onChange={setSeed} type="number" fontSize={14} padding="8px 12px" />

            <div style={{ fontSize: 11, color: COLORS.textMuted, fontFamily: mono, lineHeight: 1.4 }}>
              Synthetic (GBM) — not historical Kraken data.
              {!nCandlesValid && (
                <div style={{ color: COLORS.danger, marginTop: 2 }}>50–20000</div>
              )}
            </div>
            <div style={{ fontSize: 11, color: COLORS.textMuted, fontFamily: mono, lineHeight: 1.4 }}>
              PRNG seed — same seed = same price path.
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 4 }}>
            <Checkbox
              checked={withMC} onChange={setWithMC}
              label="Monte Carlo bootstrap"
              hint="Block-resample trade profits; computes CIs for the rigor gates."
            />
            <Checkbox
              checked={withWF} onChange={setWithWF}
              label="Walk-forward re-test"
              hint="Slides train/test windows; slower but required for wf_majority_improved gate."
            />
          </div>

          <button
            onClick={submit}
            disabled={!canSubmit}
            style={{
              marginTop: 6,
              padding: "12px 18px",
              fontSize: 14,
              fontWeight: 700,
              fontFamily: mono,
              textTransform: "uppercase",
              letterSpacing: "0.1em",
              background: canSubmit ? COLORS.blue : COLORS.panelBorder,
              color: canSubmit ? "#0a0a0f" : COLORS.textMuted,
              border: `1px solid ${canSubmit ? COLORS.blue : COLORS.panelBorder}`,
              borderRadius: 4,
              cursor: canSubmit ? "pointer" : "not-allowed",
              outline: "none",
              boxShadow: canSubmit ? `0 0 10px ${COLORS.blue}40` : "none",
              transition: "all 0.15s ease",
            }}
          >
            Run Backtest
          </button>

          {!connected && (
            <div style={{ fontSize: 12, color: COLORS.danger, fontFamily: mono }}>
              Disconnected — start hydra_agent.py to enable.
            </div>
          )}

        </div>
      </div>

      {/* RIGHT: tri-panel (Last Result + Status + Rigor Gates) above the observer chart */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12, alignSelf: "stretch",
                    minHeight: 0 }}>
        {/* Tri-panel: three equal panels sharing the same width as the observer
            chart beneath them. */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12,
                      flex: "0 0 auto" }}>
          {/* Last Result */}
          <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                        borderRadius: 8, padding: 16 }}>
            <div style={{ fontSize: 14, fontFamily: heading, fontWeight: 700, color: COLORS.text,
                          marginBottom: 12 }}>
              Last Result
            </div>
            {observerResult?.metrics ? (
              <div style={{ display: "grid", gridTemplateColumns: "1fr auto",
                            rowGap: 6, columnGap: 12, fontFamily: mono, fontSize: 12 }}>
                <span style={{ color: COLORS.textDim }}>Trades</span>
                <span style={{ color: COLORS.text, textAlign: "right" }}>
                  {observerResult.metrics.total_trades}
                </span>
                <span style={{ color: COLORS.textDim }}>Return</span>
                <span style={{ textAlign: "right",
                               color: (observerResult.metrics.total_return_pct || 0) >= 0 ? COLORS.accent : COLORS.danger }}>
                  {(observerResult.metrics.total_return_pct || 0) >= 0 ? "+" : ""}
                  {(observerResult.metrics.total_return_pct || 0).toFixed(2)}%
                </span>
                <span style={{ color: COLORS.textDim }}>Sharpe</span>
                <span style={{ color: COLORS.text, textAlign: "right" }}>
                  {fmtInd(observerResult.metrics.sharpe)}
                </span>
                <span style={{ color: COLORS.textDim }}>Max DD</span>
                <span style={{ color: COLORS.warn, textAlign: "right" }}>
                  {(observerResult.metrics.max_drawdown_pct || 0).toFixed(2)}%
                </span>
                {observerResult.metrics.profit_factor != null && (
                  <>
                    <span style={{ color: COLORS.textDim }}>Profit Factor</span>
                    <span style={{ color: COLORS.text, textAlign: "right" }}>
                      {observerResult.metrics.profit_factor.toFixed(2)}
                    </span>
                  </>
                )}
                {observerResult.metrics.win_rate_pct != null && (
                  <>
                    <span style={{ color: COLORS.textDim }}>Win Rate</span>
                    <span style={{ color: COLORS.text, textAlign: "right" }}>
                      {observerResult.metrics.win_rate_pct.toFixed(0)}%
                    </span>
                  </>
                )}
              </div>
            ) : (
              <div style={{ fontFamily: mono, fontSize: 12, color: COLORS.textDim }}>
                No completed backtest yet this session.
              </div>
            )}
          </div>

          {/* Run Status — lifecycle of the most recent submission. Derives a
              single state from (ackMsg × observerProgress × observerResult). */}
          {(() => {
            // State machine:
            //   idle       — never submitted
            //   rejected   — server refused (validation, quota, etc.)
            //   queued     — accepted, not started yet
            //   running    — tick stream active
            //   complete   — terminal result received
            const stage = observerProgress?.stage;
            const runState =
              ackMsg && ackMsg.success === false ? "rejected" :
              observerResult ? "complete" :
              (observerProgress && (stage === "running" || stage === "started")) ? "running" :
              ackMsg?.success ? "queued" :
              "idle";

            const paletteByState = {
              idle:     { dot: COLORS.textMuted, fg: COLORS.textDim, label: "Idle" },
              queued:   { dot: COLORS.blue,      fg: COLORS.blue,    label: "Queued" },
              running:  { dot: COLORS.blue,      fg: COLORS.blue,    label: "Running" },
              complete: { dot: COLORS.accent,    fg: COLORS.accent,  label: "Complete" },
              rejected: { dot: COLORS.danger,    fg: COLORS.danger,  label: "Rejected" },
            };
            const p = paletteByState[runState];

            const bodyByState = {
              idle: "Fill in the form on the left and click Run Backtest. This panel will track the run from submit → queued → running → complete.",
              queued: "Accepted by the server. Waiting for a worker slot to pick it up.",
              running: observerProgress
                ? `Tick ${observerProgress.tick ?? 0}${observerTotalTicks ? ` of ${observerTotalTicks}` : ""} — live data streams into the Observer chart below.`
                : "Executing. Live data streams into the Observer chart below.",
              complete: "Finished. Metrics are in Last Result, Rigor Gates reflect which checks passed, and the equity curve is in the Observer below. This run is now saved in the Compare tab's library — use the button below to open it side-by-side with other runs.",
              rejected: ackMsg?.error || "Server refused the submission. Check the error below and adjust the form.",
            };

            const expId = observerResult?.experiment_id
                       || observerProgress?.experiment_id
                       || ackMsg?.experiment_id;

            return (
              <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                            borderRadius: 8, padding: 16 }}>
                <div style={{ fontSize: 14, fontFamily: heading, fontWeight: 700, color: COLORS.text,
                              marginBottom: 4 }}>
                  Run Status
                </div>
                <div style={{ fontSize: 11, fontFamily: mono, color: COLORS.textMuted,
                              marginBottom: 12 }}>
                  Lifecycle of your most recent submission.
                </div>

                {/* State badge */}
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                  <span style={{ width: 10, height: 10, borderRadius: "50%",
                                 background: p.dot,
                                 boxShadow: runState === "running"
                                   ? `0 0 8px ${p.dot}, 0 0 4px ${p.dot}`
                                   : `0 0 4px ${p.dot}80`,
                                 animation: runState === "running" ? "pulse 1.4s ease-in-out infinite" : "none" }} />
                  <span style={{ fontFamily: mono, fontSize: 13, fontWeight: 700,
                                 color: p.fg, letterSpacing: "0.04em" }}>
                    {p.label}
                  </span>
                </div>

                {/* Body — plain-English description of what this state means */}
                <div style={{ fontFamily: mono, fontSize: 11, color: COLORS.textDim,
                              lineHeight: 1.5, marginBottom: expId || (completedCount > 0) ? 10 : 0 }}>
                  {bodyByState[runState]}
                </div>

                {/* Experiment id — only when a run has actually been accepted */}
                {expId && (
                  <div style={{ fontFamily: mono, fontSize: 10, color: COLORS.textMuted,
                                marginBottom: completedCount > 0 ? 10 : 0,
                                whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
                       title={`Full experiment id: ${expId}`}>
                    id: {expId.slice(0, 12)}…
                  </div>
                )}

                {/* Complete-state CTA: surface the connection to COMPARE.
                    The just-finished experiment is already in the library;
                    this jumps tabs, refreshes, and pre-selects it. */}
                {runState === "complete" && expId && onCompareThisRun && (
                  <button
                    onClick={() => onCompareThisRun(expId)}
                    title="Jump to the Compare tab with this experiment already selected — pick one more to see them ranked side-by-side."
                    style={{
                      display: "flex", alignItems: "center", justifyContent: "center",
                      gap: 6, width: "100%", padding: "8px 12px",
                      marginBottom: completedCount > 0 ? 10 : 0,
                      fontSize: 12, fontWeight: 700, fontFamily: mono,
                      textTransform: "uppercase", letterSpacing: "0.08em",
                      background: `${COLORS.purple}20`, color: COLORS.purple,
                      border: `1px solid ${COLORS.purple}60`, borderRadius: 4,
                      cursor: "pointer", outline: "none",
                    }}
                  >
                    Compare this run →
                  </button>
                )}

                {/* Session totals — across the current browser session */}
                {(completedCount > 0 || reviewedCount > 0) && (
                  <div style={{ fontFamily: mono, fontSize: 11, color: COLORS.textDim,
                                paddingTop: 10, borderTop: `1px solid ${COLORS.panelBorder}` }}
                       title="Totals since you opened the dashboard. 'Reviewed' = the AI reviewer finished scoring the run against the Rigor Gates.">
                    <span style={{ color: COLORS.text, fontWeight: 700 }}>{completedCount}</span>
                    {" "}completed
                    {reviewedCount > 0 && (
                      <>
                        {" · "}
                        <span style={{ color: COLORS.purple, fontWeight: 700 }}>{reviewedCount}</span>
                        {" AI-reviewed"}
                      </>
                    )}
                    {" "}this session
                  </div>
                )}
              </div>
            );
          })()}

          {/* Rigor Gates — color-coded pills driven by the latest review's
              gates_passed dict. Grey = no result yet, green = passed, red = failed.
              Hover a pill for the plain-English explanation. */}
          <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                        borderRadius: 8, padding: 16 }}>
            <div style={{ fontSize: 14, fontFamily: heading, fontWeight: 700, color: COLORS.text,
                          marginBottom: 10 }}>
              Rigor Gates
            </div>
            {(() => {
              const gp = observerReview?.gates_passed;
              const hasReview = gp && typeof gp === "object";
              const summary = hasReview
                ? (() => {
                    const pass = RIGOR_GATES.filter(g => gp[g.key] === true).length;
                    const fail = RIGOR_GATES.filter(g => gp[g.key] === false).length;
                    return `${pass}/${RIGOR_GATES.length} passed${fail > 0 ? ` · ${fail} failed` : ""}`;
                  })()
                : "No review yet — hover a pill for what it checks.";
              return (
                <>
                  <div style={{ fontFamily: mono, fontSize: 11, color: COLORS.textDim,
                                marginBottom: 10 }}>
                    {summary}
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
                    {RIGOR_GATES.map(g => {
                      const state = !hasReview ? "neutral"
                                  : gp[g.key] === true ? "pass"
                                  : gp[g.key] === false ? "fail"
                                  : "neutral";
                      const bg = state === "pass" ? `${COLORS.accent}18`
                               : state === "fail" ? `${COLORS.danger}18`
                               : COLORS.bg;
                      const border = state === "pass" ? COLORS.accent
                                   : state === "fail" ? COLORS.danger
                                   : COLORS.panelBorder;
                      const fg = state === "pass" ? COLORS.accent
                               : state === "fail" ? COLORS.danger
                               : COLORS.textDim;
                      const icon = state === "pass" ? "✓" : state === "fail" ? "✗" : "○";
                      return (
                        <span
                          key={g.key}
                          title={`${g.label} (${g.key})\n\n${g.why}`}
                          style={{ display: "inline-flex", alignItems: "center", gap: 4,
                                   padding: "4px 8px", borderRadius: 999,
                                   background: bg, border: `1px solid ${border}`,
                                   color: fg, fontFamily: mono, fontSize: 11,
                                   fontWeight: 600, cursor: "help",
                                   whiteSpace: "nowrap" }}
                        >
                          <span style={{ fontSize: 10 }}>{icon}</span>
                          {g.label}
                        </span>
                      );
                    })}
                  </div>
                </>
              );
            })()}
          </div>
        </div>

        {/* Phase 9: Dual-state Observer — backtest pair cards stream here
            live during a run, using the same visual language as LIVE.
            flex: 1 so the chart expands to fill the column down to the
            bottom of the adjacent (left) control panel. */}
        {(observerProgress || observerResult) ? (
          <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
            <ObserverModal
              progress={observerProgress}
              result={observerResult}
              review={observerReview}
              equityHistory={observerEquity}
              totalTicks={observerTotalTicks}
              variant="dock"
              onClose={onObserverClose}
            />
          </div>
        ) : (
          <div style={{ flex: 1, background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                        borderRadius: 8, padding: 16, minHeight: 180,
                        display: "flex", flexDirection: "column" }}>
            <div style={{ fontSize: 14, fontFamily: heading, fontWeight: 700, color: COLORS.text,
                          marginBottom: 12 }}>
              Observer
            </div>
            <div style={{ fontFamily: mono, fontSize: 12, color: COLORS.textDim, lineHeight: 1.5 }}>
              Submit a backtest to stream per-tick pair state here in real time —
              the same pair cards, regime badges, and equity curves as the LIVE view.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// Shared visual primitives (live + observer — prevents drift)
// ═══════════════════════════════════════════════════════════════

// Regime badge: identical coloring + typography in LIVE and observer.
// size: "compact" for the observer dock, "regular" for the LIVE pair panel.
function RegimeBadge({ regime, size = "regular" }) {
  const c = regimeColor(regime);
  const compact = size === "compact";
  return (
    <span style={{
      fontSize: compact ? 9 : 10,
      fontFamily: mono,
      color: c,
      background: `${c}18`,
      padding: compact ? "2px 6px" : "3px 8px",
      borderRadius: 3,
      letterSpacing: "0.08em",
    }}>
      {regime || "—"}
    </span>
  );
}

// Signal chip: same HOLD/BUY/SELL palette everywhere.
function SignalChip({ action, size = "regular" }) {
  const c = signalColor(action);
  const compact = size === "compact";
  return (
    <span style={{
      fontSize: compact ? 9 : 10,
      fontFamily: mono,
      color: c,
      fontWeight: 700,
      letterSpacing: "0.04em",
    }}>
      {action || "HOLD"}
    </span>
  );
}

// ═══════════════════════════════════════════════════════════════
// Phase 9: Dual-state Observer Modal
// ═══════════════════════════════════════════════════════════════

// Stage color map mirrors LIVE signal/regime palette so the observer
// reads at a glance as a variant of the live view.
function stageColor(stage) {
  if (stage === "running") return COLORS.blue;
  if (stage === "started") return COLORS.textDim;
  if (stage === "cancelled") return COLORS.warn;
  if (stage === "failed") return COLORS.danger;
  if (stage === "complete") return COLORS.accent;
  return COLORS.textDim;
}

function ObserverProgressBar({ tick, totalTicks, stage }) {
  const pct = totalTicks > 0 ? Math.min(100, Math.max(0, (tick / totalTicks) * 100)) : 0;
  const color = stageColor(stage);
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10,
                    fontFamily: mono, color: COLORS.textDim, marginBottom: 4 }}>
        <span>
          tick <span style={{ color: COLORS.text }}>{tick}</span>
          {totalTicks > 0 && <> / {totalTicks}</>}
        </span>
        <span style={{ color, textTransform: "uppercase", letterSpacing: "0.1em" }}>
          {stage || "—"}
        </span>
      </div>
      <div style={{ height: 4, background: COLORS.panelBorder, borderRadius: 2, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${pct}%`, background: color,
                      boxShadow: `0 0 6px ${color}80`, transition: "width 0.2s ease" }} />
      </div>
    </div>
  );
}

// Compact per-pair card for the observer. Intentionally a separate visual
// from LIVE's PairPanel (simpler, smaller) because the observer coexists
// with the LIVE grid on the LIVE tab — we want a distinct affordance.
function ObserverPairCard({ pair, state, equityHistory, expand = false }) {
  if (!state) return null;
  const sig = state.signal || {};
  const port = state.portfolio || {};
  const pos = state.position || {};
  const px = pairPrefix(pair);

  return (
    <div style={{ background: COLORS.bg, border: `1px solid ${COLORS.panelBorder}`,
                  borderRadius: 6, padding: 10,
                  flex: expand ? 1 : "0 0 auto",
                  display: "flex", flexDirection: "column", minHeight: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                    marginBottom: 8 }}>
        <div style={{ fontSize: 13, fontWeight: 700, fontFamily: mono, color: COLORS.text }}>
          {pair}
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <RegimeBadge regime={state.regime} size="compact" />
          <SignalChip action={sig.action} size="compact" />
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 5, fontSize: 12,
                    fontFamily: mono }}>
        <span style={{ color: COLORS.textDim }}>Price</span>
        <span style={{ color: COLORS.text, textAlign: "right" }}>{fmtPrice(state.price, px)}</span>
        <span style={{ color: COLORS.textDim }}>Equity</span>
        <span style={{ color: COLORS.text, textAlign: "right" }}>{fmtPrice(port.equity, px)}</span>
        <span style={{ color: COLORS.textDim }}>Position</span>
        <span style={{ color: pos.size > 0 ? COLORS.accent : COLORS.textMuted, textAlign: "right" }}>
          {fmtInd(pos.size)}
        </span>
        <span style={{ color: COLORS.textDim }}>P&L%</span>
        <span style={{ color: (port.pnl_pct || 0) >= 0 ? COLORS.buy : COLORS.sell, textAlign: "right" }}>
          {(port.pnl_pct || 0).toFixed(2)}%
        </span>
      </div>
      {equityHistory && equityHistory.length >= 2 && (
        <div style={{ marginTop: 8,
                      flex: expand ? 1 : "0 0 auto",
                      minHeight: expand ? 80 : 36,
                      display: "flex" }}>
          <MiniChart
            data={equityHistory}
            width={240}
            height={expand ? 160 : 36}
            color={(port.pnl_pct || 0) >= 0 ? COLORS.accent : COLORS.danger}
            filled
            fill={expand}
          />
        </div>
      )}
    </div>
  );
}

function GatesSummary({ review }) {
  if (!review || !review.gates_passed) return null;
  const gp = review.gates_passed;
  // Prefer the canonical RIGOR_GATES ordering + labels; fall back to any
  // keys present on the review that we don't recognise so nothing is hidden.
  const known = new Set(RIGOR_GATES.map(g => g.key));
  const extras = Object.keys(gp).filter(k => !known.has(k))
    .map(k => ({ key: k, label: k, why: "(unrecognised gate — shown for completeness)" }));
  const all = [...RIGOR_GATES, ...extras];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4,
                  fontFamily: mono, fontSize: 10 }}>
      {all.map(g => {
        const passed = gp[g.key];
        const present = g.key in gp;
        return (
          <div key={g.key} title={`${g.label} (${g.key})\n\n${g.why}`}
               style={{ display: "flex", alignItems: "center", gap: 6, cursor: "help" }}>
            <span style={{ width: 10, height: 10, borderRadius: "50%",
                           background: !present ? COLORS.panelBorder
                                    : passed ? COLORS.accent : COLORS.danger,
                           boxShadow: !present ? "none"
                                    : passed ? `0 0 4px ${COLORS.accent}80`
                                    : `0 0 4px ${COLORS.danger}80`,
                           display: "inline-block" }} />
            <span style={{ color: passed ? COLORS.text : COLORS.textDim,
                           textDecoration: present && !passed ? "line-through" : "none" }}>
              {g.label}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function ReviewPanel({ review }) {
  if (!review) return null;
  const verdictColor = {
    NO_CHANGE:          COLORS.textDim,
    PARAM_TWEAK:        COLORS.accent,
    CODE_REVIEW:        COLORS.blue,
    RESULT_ANOMALOUS:   COLORS.warn,
    HYPOTHESIS_REFUTED: COLORS.danger,
  }[review.verdict] || COLORS.textDim;
  return (
    <div style={{ marginTop: 10, paddingTop: 10, borderTop: `1px solid ${COLORS.panelBorder}` }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: 9, fontFamily: mono, color: COLORS.textDim,
                       textTransform: "uppercase", letterSpacing: "0.1em" }}>
          AI Reviewer
        </span>
        <span style={{ fontSize: 10, fontFamily: mono, fontWeight: 700,
                       color: verdictColor,
                       background: `${verdictColor}18`,
                       padding: "2px 6px", borderRadius: 3, letterSpacing: "0.05em" }}>
          {review.verdict}
        </span>
        {review.all_gates_passed && (
          <span style={{ fontSize: 9, fontFamily: mono, color: COLORS.accent }}>
            ✓ all gates passed
          </span>
        )}
        {review.original_verdict && review.original_verdict !== review.verdict && (
          <span style={{ fontSize: 9, fontFamily: mono, color: COLORS.warn }}>
            (downgraded from {review.original_verdict})
          </span>
        )}
      </div>

      <GatesSummary review={review} />

      {review.reasoning && (
        <div style={{ fontSize: 10, fontFamily: mono, color: COLORS.textDim,
                      marginTop: 8, lineHeight: 1.5 }}>
          {review.reasoning.length > 280 ? review.reasoning.slice(0, 277) + "…" : review.reasoning}
        </div>
      )}

      {Array.isArray(review.proposed_changes) && review.proposed_changes.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontSize: 9, fontFamily: mono, color: COLORS.textDim,
                        textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>
            Proposed
          </div>
          {review.proposed_changes.map((pc, i) => (
            <div key={i} style={{ fontFamily: mono, fontSize: 10, color: COLORS.text,
                                   marginBottom: 4, paddingLeft: 6,
                                   borderLeft: `2px solid ${verdictColor}` }}>
              <span style={{ color: COLORS.textDim }}>{pc.scope}</span>{" "}
              <span style={{ color: COLORS.blue }}>{pc.target}</span>
              {pc.current_value != null && pc.proposed_value != null && (
                <>: {pc.current_value} → <span style={{ color: COLORS.accent }}>{pc.proposed_value}</span></>
              )}
              {pc.expected_impact?.sharpe != null && (
                <span style={{ color: COLORS.textMuted }}>
                  {" "}(Δsharpe {pc.expected_impact.sharpe >= 0 ? "+" : ""}{pc.expected_impact.sharpe.toFixed(2)})
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {Array.isArray(review.risk_flags) && review.risk_flags.length > 0 && (
        <div style={{ marginTop: 6, fontSize: 9, fontFamily: mono, color: COLORS.warn }}>
          ⚠ {review.risk_flags.slice(0, 3).join(" | ")}
          {review.risk_flags.length > 3 && ` (+${review.risk_flags.length - 3})`}
        </div>
      )}
    </div>
  );
}

function ObserverModal({
  progress,          // latest backtest_progress message: {experiment_id, tick, stage, dashboard_state}
  result,            // backtest_result summary when complete
  review,            // backtest_review payload when reviewed
  equityHistory,     // {pair -> [equity...]}  accumulated from progress stream
  totalTicks,        // best-effort total (from result candles_processed or dashboard_state.max)
  variant = "dock",  // "dock" (fills column) | "floating" (slide-in on LIVE tab)
  onClose,
}) {
  if (!progress && !result) return null;
  const expId = progress?.experiment_id || result?.experiment_id || "—";
  const stage = result ? (result.status || "complete") : (progress?.stage || "running");
  const tick = progress?.tick ?? result?.metrics?.total_trades ?? 0;
  const pairs = progress?.dashboard_state?.pairs || {};
  const pairNames = Object.keys(pairs);
  const summary = result?.metrics;
  const hypothesis = result?.hypothesis || "";
  const shellStyle = variant === "floating"
    ? { position: "fixed", right: 16, top: 80, width: 360, maxHeight: "calc(100vh - 100px)",
        overflowY: "auto", zIndex: 20,
        boxShadow: "0 8px 32px rgba(0,0,0,0.45)" }
    : { flex: 1, display: "flex", flexDirection: "column", minHeight: 0 };

  return (
    <div
      style={{
        ...shellStyle,
        background: COLORS.panel,
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 8,
        padding: 14,
      }}
    >
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start",
                    marginBottom: 10 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 2 }}>
            <span style={{ fontSize: 11, fontFamily: heading, fontWeight: 800,
                           color: COLORS.blue, letterSpacing: "0.04em",
                           textTransform: "uppercase" }}>
              Observer
            </span>
            <span style={{ fontSize: 9, fontFamily: mono, color: COLORS.textMuted }}>
              {expId.slice(0, 16)}…
            </span>
          </div>
          {hypothesis && (
            <div style={{ fontSize: 10, fontFamily: mono, color: COLORS.textDim,
                          fontStyle: "italic", lineHeight: 1.4, marginTop: 2,
                          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              "{hypothesis}"
            </div>
          )}
        </div>
        {onClose && (
          <button
            onClick={onClose}
            style={{ background: "transparent", color: COLORS.textDim, border: "none",
                     cursor: "pointer", padding: 4, fontSize: 14, lineHeight: 1,
                     fontFamily: mono }}
            title="Close observer"
          >
            ×
          </button>
        )}
      </div>

      {/* Progress bar */}
      <div style={{ marginBottom: 10 }}>
        <ObserverProgressBar tick={tick} totalTicks={totalTicks || 0} stage={stage} />
      </div>

      {/* Terminal summary is rendered in the left control panel (BacktestResultMetrics)
          to give the equity chart more headroom. */}

      {/* Per-pair cards — same visual DNA as LIVE.
          flex: 1 + minHeight: 0 lets the card stack fill the panel height so
          the equity chart stretches down to the bottom of the adjacent
          control panel on the left. */}
      {pairNames.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6,
                      flex: variant === "dock" ? 1 : "0 0 auto", minHeight: 0 }}>
          {pairNames.map(pair => (
            <ObserverPairCard
              key={pair}
              pair={pair}
              state={pairs[pair]}
              equityHistory={equityHistory?.[pair] || []}
              expand={variant === "dock"}
            />
          ))}
        </div>
      )}

      {!pairNames.length && !summary && (
        <div style={{ fontFamily: mono, fontSize: 10, color: COLORS.textMuted,
                      textAlign: "center", padding: "20px 0" }}>
          Waiting for first tick…
        </div>
      )}

      {/* AI Reviewer verdict */}
      {review && <ReviewPanel review={review} />}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// Phase 10: Experiment Library + Compare View
// ═══════════════════════════════════════════════════════════════

// Color mapping for verdict badges (matches ReviewPanel colors)
const VERDICT_COLORS = {
  NO_CHANGE:          "textDim",
  PARAM_TWEAK:        "accent",
  CODE_REVIEW:        "blue",
  RESULT_ANOMALOUS:   "warn",
  HYPOTHESIS_REFUTED: "danger",
};

function ExperimentLibrary({ experiments, selectedIds, onToggleSelect, onRefresh, onClearSelection,
                             onView, loading,
                             onCompare, canCompare, compareInFlight, onGoToBacktest,
                             totalInStore, compact = false }) {
  const count = experiments?.length || 0;
  const maxSelect = 8;
  const selCount = selectedIds.length;
  // Map selected IDs back to their rows so we can chip-render them by name.
  const selectedRows = selectedIds
    .map((id) => experiments.find((e) => e.id === id))
    .filter(Boolean);

  return (
    // `overflow: hidden` clips the internal row list to the panel's rounded
    // border-radius so rows never visually escape past the panel edge, even
    // under rapid viewport resize. The row list still scrolls internally
    // via its own overflowY: auto.
    <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                  borderRadius: 8, padding: 16,
                  display: "flex", flexDirection: "column",
                  minHeight: 0, flex: 1, overflow: "hidden" }}>
      {/* Header row */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                    marginBottom: 14 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
          <div style={{ fontSize: 14, fontFamily: heading, fontWeight: 700, color: COLORS.text }}>
            Experiment Library
          </div>
          <span style={{ fontSize: 11, fontFamily: mono, color: COLORS.textDim }}>
            {totalInStore != null && count !== totalInStore
              ? <>{count} comparable · {totalInStore} total</>
              : <>{count} comparable experiment{count === 1 ? "" : "s"}</>}
            {selCount > 0 && (
              <>
                {" "}· <span style={{ color: COLORS.purple }}>{selCount}</span> selected
                {selCount >= maxSelect && <span style={{ color: COLORS.warn }}> (max)</span>}
              </>
            )}
          </span>
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          {selCount > 0 && (
            <button
              onClick={onClearSelection}
              style={{ padding: "6px 12px", fontSize: 11, fontFamily: mono, fontWeight: 700,
                       background: "transparent", color: COLORS.textDim,
                       border: `1px solid ${COLORS.panelBorder}`, borderRadius: 4,
                       cursor: "pointer", letterSpacing: "0.1em", textTransform: "uppercase" }}
            >
              Clear
            </button>
          )}
          <button
            onClick={onRefresh}
            disabled={loading}
            style={{ padding: "6px 12px", fontSize: 11, fontFamily: mono, fontWeight: 700,
                     background: COLORS.blue + "20", color: COLORS.blue,
                     border: `1px solid ${COLORS.blue}40`, borderRadius: 4,
                     cursor: loading ? "wait" : "pointer", letterSpacing: "0.1em",
                     textTransform: "uppercase", opacity: loading ? 0.5 : 1 }}
          >
            {loading ? "…" : "Refresh"}
          </button>
          {/* Primary inline Compare button — visible right next to the selection
              so the user never has to hunt for it. Disabled state explains why. */}
          {onCompare && (
            <button
              onClick={onCompare}
              disabled={!canCompare}
              title={
                selCount < 2 ? "Tick at least 2 rows below to enable Compare."
                             : selCount > 8 ? "Max 8 experiments per comparison."
                             : "Run the comparison."
              }
              style={{ padding: "6px 14px", fontSize: 12, fontFamily: mono, fontWeight: 700,
                       background: canCompare ? COLORS.purple : `${COLORS.purple}20`,
                       color: canCompare ? "#0a0a0f" : COLORS.textMuted,
                       border: `1px solid ${canCompare ? COLORS.purple : `${COLORS.purple}40`}`,
                       borderRadius: 4,
                       cursor: canCompare ? "pointer" : "not-allowed",
                       letterSpacing: "0.1em", textTransform: "uppercase",
                       boxShadow: canCompare ? `0 0 10px ${COLORS.purple}40` : "none" }}
            >
              {compareInFlight
                ? "Comparing…"
                : selCount >= 2 ? `Compare ${selCount} →` : "Compare →"}
            </button>
          )}
        </div>
      </div>

      {/* Selection chip bar — confirms which rows are selected by name, with a
          per-chip deselect. Gives the user an explicit visual that selections
          past 1 are registering. */}
      {selCount > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6,
                      padding: "8px 10px", marginBottom: 10,
                      background: `${COLORS.purple}10`,
                      border: `1px solid ${COLORS.purple}40`, borderRadius: 4 }}>
          <span style={{ fontFamily: mono, fontSize: 11, color: COLORS.textDim,
                         textTransform: "uppercase", letterSpacing: "0.08em",
                         marginRight: 4 }}>
            Selected ({selCount}/{maxSelect})
          </span>
          {selectedRows.map((e) => (
            <span key={e.id}
                  style={{ display: "inline-flex", alignItems: "center", gap: 6,
                           padding: "3px 8px", borderRadius: 999,
                           background: `${COLORS.purple}25`,
                           border: `1px solid ${COLORS.purple}60`,
                           color: COLORS.text, fontFamily: mono, fontSize: 11,
                           fontWeight: 600 }}>
              {e.name}
              <button
                onClick={() => onToggleSelect(e.id)}
                title="Remove from selection"
                style={{ background: "transparent", border: "none", padding: 0,
                         color: COLORS.purple, cursor: "pointer",
                         fontSize: 13, lineHeight: 1, fontFamily: mono }}
              >
                ×
              </button>
            </span>
          ))}
          {selCount < 2 && (
            <span style={{ fontFamily: mono, fontSize: 11, color: COLORS.warn,
                           marginLeft: 4 }}>
              Tick at least one more row to enable Compare.
            </span>
          )}
        </div>
      )}

      {/* List */}
      {count === 0 ? (
        // Flex-fill + overflow-hidden so the dashed-border dark panel always
        // has symmetric breathing room inside the library panel, never
        // clipped at the bottom regardless of available vertical space.
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center",
                      justifyContent: "center", gap: 14, padding: "32px 20px",
                      textAlign: "center",
                      background: COLORS.bg, border: `1px dashed ${COLORS.panelBorder}`,
                      borderRadius: 6,
                      flex: 1, minHeight: 0, overflow: "hidden" }}>
          {loading ? (
            <div style={{ fontFamily: mono, fontSize: 13, color: COLORS.textDim }}>
              Loading…
            </div>
          ) : (totalInStore || 0) > 0 ? (
            <>
              <div style={{ fontSize: 28, opacity: 0.5 }}>🧪</div>
              <div style={{ fontFamily: heading, fontSize: 15, fontWeight: 700,
                            color: COLORS.text }}>
                No comparable experiments yet
              </div>
              <div style={{ fontFamily: mono, fontSize: 12, color: COLORS.textDim,
                            lineHeight: 1.5, maxWidth: 440 }}>
                You have <span style={{ color: COLORS.purple }}>{totalInStore}</span> in
                the store, but none are in a comparable state yet (a run must
                be <span style={{ color: COLORS.accent }}>complete</span> with
                valid metrics). Wait for the current backtest to finish, or
                re-run it from the BACKTEST tab if it failed.
              </div>
            </>
          ) : (
            <>
              <div style={{ fontSize: 36, opacity: 0.6 }}>🧪</div>
              <div style={{ fontFamily: heading, fontSize: 16, fontWeight: 700,
                            color: COLORS.text }}>
                You don't have any experiments yet
              </div>
              <div style={{ fontFamily: mono, fontSize: 12, color: COLORS.textDim,
                            lineHeight: 1.5, maxWidth: 420 }}>
                Compare needs at least two completed backtests. Head to the
                BACKTEST tab, submit a run (or two), then come back here to
                rank them side-by-side.
              </div>
              {onGoToBacktest && (
                <button
                  onClick={onGoToBacktest}
                  style={{ padding: "10px 18px", fontSize: 13, fontFamily: mono,
                           fontWeight: 700, letterSpacing: "0.1em",
                           textTransform: "uppercase",
                           background: COLORS.blue, color: "#0a0a0f",
                           border: `1px solid ${COLORS.blue}`, borderRadius: 4,
                           cursor: "pointer", outline: "none",
                           boxShadow: `0 0 12px ${COLORS.blue}50` }}
                >
                  Go to Backtest Tab →
                </button>
              )}
            </>
          )}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 5,
                      flex: 1, minHeight: 0, overflowY: "auto",
                      scrollbarGutter: "stable" }}>
          {/* Column legend — placed INSIDE the scroll container as a sticky
              header so its horizontal bounds match the rows (same scrollbar
              gutter, same effective width). Eliminates the "black inner
              panel offset" where legend and rows drifted by the scrollbar
              width. */}
          <div style={{ display: "grid",
                        gridTemplateColumns: "24px 1fr 100px 72px 80px 72px 80px 24px",
                        gap: 6, padding: "0 10px 6px",
                        fontFamily: mono, fontSize: 11, color: COLORS.textDim,
                        textTransform: "uppercase", letterSpacing: "0.08em",
                        fontWeight: 600,
                        position: "sticky", top: 0, zIndex: 1,
                        background: COLORS.panel }}>
            <span />
            <span>Name / ID</span>
            <span style={{ textAlign: "right" }}>Status</span>
            <span style={{ textAlign: "right" }}>Trades</span>
            <span style={{ textAlign: "right" }}>Return</span>
            <span style={{ textAlign: "right" }}>Sharpe</span>
            <span style={{ textAlign: "right" }}>Max DD</span>
            <span />
          </div>
          {experiments.map((e) => {
            const selected = selectedIds.includes(e.id);
            const canSelect = selected || selCount < maxSelect;
            const statusColor = {
              complete: COLORS.accent, running: COLORS.blue, pending: COLORS.textDim,
              failed: COLORS.danger, cancelled: COLORS.warn,
            }[e.status] || COLORS.textDim;
            const m = e.metrics || {};
            const retColor = (m.total_return_pct || 0) >= 0 ? COLORS.buy : COLORS.sell;
            return (
              <div
                key={e.id}
                onClick={() => canSelect && onToggleSelect(e.id)}
                style={{
                  display: "grid",
                  gridTemplateColumns: "24px 1fr 100px 72px 80px 72px 80px 24px",
                  gap: 6, alignItems: "center",
                  padding: "8px 10px",
                  background: selected ? `${COLORS.purple}12` : COLORS.bg,
                  border: `1px solid ${selected ? COLORS.purple + "60" : COLORS.panelBorder}`,
                  borderRadius: 4, fontFamily: mono, fontSize: 12,
                  cursor: canSelect ? "pointer" : "not-allowed",
                  opacity: canSelect ? 1 : 0.5,
                }}
              >
                <input
                  type="checkbox"
                  checked={selected}
                  disabled={!canSelect}
                  onChange={() => canSelect && onToggleSelect(e.id)}
                  onClick={(ev) => ev.stopPropagation()}
                  style={{ accentColor: COLORS.purple, cursor: canSelect ? "pointer" : "not-allowed" }}
                />
                <div style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  <span style={{ color: COLORS.text, fontWeight: 600 }}>{e.name}</span>
                  <span style={{ color: COLORS.textMuted, marginLeft: 8, fontSize: 11 }}
                        title={`Experiment id: ${e.id}`}>
                    <span style={{ marginRight: 3, filter: "saturate(0.85)" }}>🧪</span>
                    {e.id.slice(0, 8)}
                  </span>
                  {e.base_preset && (
                    <span style={{ color: COLORS.blue, marginLeft: 6, fontSize: 11 }}>
                      [{e.base_preset}]
                    </span>
                  )}
                </div>
                <span style={{ color: statusColor, textAlign: "right", fontSize: 11,
                               textTransform: "uppercase", letterSpacing: "0.05em" }}>
                  {e.status}
                </span>
                <span style={{ color: COLORS.text, textAlign: "right" }}>
                  {m.total_trades != null ? m.total_trades : "—"}
                </span>
                <span style={{ color: retColor, textAlign: "right" }}>
                  {m.total_return_pct != null
                    ? `${m.total_return_pct >= 0 ? "+" : ""}${m.total_return_pct.toFixed(1)}%`
                    : "—"}
                </span>
                <span style={{ color: COLORS.text, textAlign: "right" }}>
                  {m.sharpe != null ? m.sharpe.toFixed(2) : "—"}
                </span>
                <span style={{ color: COLORS.warn, textAlign: "right" }}>
                  {m.max_drawdown_pct != null ? `${m.max_drawdown_pct.toFixed(1)}%` : "—"}
                </span>
                <button
                  onClick={(ev) => { ev.stopPropagation(); onView(e.id); }}
                  style={{ background: "transparent", border: "none", color: COLORS.textDim,
                           cursor: "pointer", fontSize: 14, padding: 0, fontFamily: mono,
                           lineHeight: 1 }}
                  title="View details"
                >
                  ›
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function CompareResults({ report, experimentsById, onDismiss }) {
  if (!report || !Array.isArray(report.rows) || report.rows.length === 0) return null;
  const winners = report.winner_per_metric || {};
  const metrics = [
    { key: "total_return_pct", label: "Return",     suffix: "%",  higherBetter: true  },
    { key: "sharpe",           label: "Sharpe",     suffix: "",   higherBetter: true  },
    { key: "max_drawdown_pct", label: "Max DD",     suffix: "%",  higherBetter: false },
    { key: "profit_factor",    label: "Profit Fct", suffix: "",   higherBetter: true  },
  ];

  return (
    // With the library hidden while results are displayed, this panel fills
    // remaining vertical space (flex: 1 + minHeight: 0) and scrolls its own
    // contents if the comparison is tall.
    <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                  borderRadius: 8, padding: 16, marginTop: 12,
                  flex: 1, minHeight: 0,
                  overflowY: "auto", overflowX: "hidden" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                    marginBottom: 12 }}>
        <div style={{ fontSize: 14, fontFamily: heading, fontWeight: 700, color: COLORS.text }}>
          Comparison
        </div>
        {onDismiss && (
          <button
            onClick={onDismiss}
            title="Dismiss these results and return to the experiment library to pick a different set."
            style={{ padding: "6px 12px", fontSize: 11, fontFamily: mono, fontWeight: 700,
                     letterSpacing: "0.1em", textTransform: "uppercase",
                     background: "transparent", color: COLORS.textDim,
                     border: `1px solid ${COLORS.panelBorder}`, borderRadius: 4,
                     cursor: "pointer", outline: "none" }}
          >
            ← Change Selection
          </button>
        )}
      </div>

      {/* Ranked table */}
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: mono, fontSize: 12 }}>
          <thead>
            <tr style={{ color: COLORS.textDim, fontSize: 11, textTransform: "uppercase",
                         letterSpacing: "0.08em", fontWeight: 600 }}>
              <th style={{ textAlign: "left",  padding: "10px 8px", borderBottom: `1px solid ${COLORS.panelBorder}` }}>
                Experiment
              </th>
              <th style={{ textAlign: "right", padding: "10px 8px", borderBottom: `1px solid ${COLORS.panelBorder}` }}>
                Trades
              </th>
              {metrics.map(m => (
                <th key={m.key} style={{ textAlign: "right", padding: "10px 8px",
                                          borderBottom: `1px solid ${COLORS.panelBorder}` }}>
                  {m.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {report.rows.map((row, idx) => (
              <tr key={row.experiment_id}
                  style={{ borderBottom: idx < report.rows.length - 1
                             ? `1px solid ${COLORS.panelBorder}`
                             : "none",
                           background: idx % 2 === 1 ? `${COLORS.bg}50` : "transparent" }}>
                <td style={{ padding: "10px 8px", minWidth: 160 }}>
                  <div style={{ color: COLORS.text, fontWeight: 600 }}>{row.name}</div>
                  <div style={{ color: COLORS.textMuted, fontSize: 11 }}>
                    {row.experiment_id.slice(0, 16)}
                  </div>
                </td>
                <td style={{ textAlign: "right", padding: "10px 8px", color: COLORS.text }}>
                  {row.total_trades}
                </td>
                {metrics.map(m => {
                  const val = row[m.key];
                  const isWinner = winners[m.key] === row.experiment_id;
                  const good = m.higherBetter ? (val > 0) : (val < 10);
                  const color = isWinner ? COLORS.accent : (good ? COLORS.text : COLORS.textDim);
                  return (
                    <td key={m.key} style={{ textAlign: "right", padding: "10px 8px",
                                              color, fontWeight: isWinner ? 700 : 400 }}>
                      {val != null && Number.isFinite(val)
                        ? `${m.higherBetter && val > 0 ? "+" : ""}${val.toFixed(2)}${m.suffix}`
                        : "—"}
                      {isWinner && (
                        <span style={{ marginLeft: 4, fontSize: 11, color: COLORS.accent }}>★</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Per-metric winners */}
      <div style={{ marginTop: 14, paddingTop: 12, borderTop: `1px solid ${COLORS.panelBorder}`,
                    display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10 }}>
        {metrics.map(m => {
          const winnerId = winners[m.key];
          const winner = winnerId
            ? (report.rows.find(r => r.experiment_id === winnerId) || experimentsById?.[winnerId])
            : null;
          return (
            <div key={m.key} style={{ background: COLORS.bg, padding: "10px 12px",
                                       border: `1px solid ${COLORS.panelBorder}`, borderRadius: 4 }}>
              <div style={{ fontSize: 11, color: COLORS.textDim, textTransform: "uppercase",
                            letterSpacing: "0.1em", fontFamily: mono, fontWeight: 600 }}>
                {m.label} winner
              </div>
              <div style={{ fontSize: 13, fontFamily: mono, color: COLORS.accent, fontWeight: 700,
                            marginTop: 5, whiteSpace: "nowrap", overflow: "hidden",
                            textOverflow: "ellipsis" }}>
                {winner ? (winner.name || winner.id?.slice(0, 12) || "—") : "—"}
              </div>
            </div>
          );
        })}
      </div>

      {/* Pairwise p-values (significance). "__" is the key separator.
          Resolve short IDs back to experiment names so chips are readable. */}
      {report.pairwise_sharpe_p_values && Object.keys(report.pairwise_sharpe_p_values).length > 0 && (
        <div style={{ marginTop: 14, paddingTop: 12, borderTop: `1px solid ${COLORS.panelBorder}` }}>
          <div style={{ fontSize: 11, color: COLORS.textDim, textTransform: "uppercase",
                        letterSpacing: "0.1em", fontFamily: mono, marginBottom: 8, fontWeight: 600 }}>
            Paired bootstrap p-values · per-tick return diffs
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {Object.entries(report.pairwise_sharpe_p_values).map(([key, p]) => {
              const [a, b] = key.split("__");
              const significant = p < 0.05;
              const nameFor = (id) =>
                report.rows.find(r => r.experiment_id === id || r.experiment_id?.startsWith(id))?.name
                || experimentsById?.[id]?.name
                || id.slice(0, 8);
              return (
                <div key={key} style={{ fontFamily: mono, fontSize: 12,
                                         padding: "6px 10px",
                                         background: significant ? `${COLORS.accent}12` : COLORS.bg,
                                         border: `1px solid ${significant ? COLORS.accent : COLORS.panelBorder}`,
                                         borderRadius: 3,
                                         color: significant ? COLORS.accent : COLORS.textDim }}>
                  <span style={{ fontWeight: 600 }}>{nameFor(a)}</span>
                  <span style={{ opacity: 0.6, margin: "0 6px" }}>vs</span>
                  <span style={{ fontWeight: 600 }}>{nameFor(b)}</span>
                  <span style={{ marginLeft: 8 }}>p={p.toFixed(3)}</span>
                  {significant && <span style={{ marginLeft: 4 }}>✓</span>}
                </div>
              );
            })}
          </div>
          <div style={{ fontSize: 11, color: COLORS.textMuted, fontFamily: mono, marginTop: 8 }}>
            <span style={{ color: COLORS.accent }}>✓</span> = sharpe difference statistically
            significant at p&lt;0.05 (not just noise from random variation).
          </div>
        </div>
      )}
    </div>
  );
}

function CompareView({ experiments, selectedIds, onToggleSelect, onClearSelection, onRefresh,
                       onView, onCompare, compareReport, loading,
                       compareInFlight, onGoToBacktest, totalInStore, onDismissReport }) {
  const canCompare = selectedIds.length >= 2 && selectedIds.length <= 8 && !compareInFlight;
  // Step 1's "do you have any experiments" check is against the full store,
  // not the filtered subset, so active filters don't mask a populated library.
  const expCount = totalInStore != null ? totalInStore : (experiments?.length || 0);
  const hasEnoughForCompare = expCount >= 2;

  // Derive the current step so the banner can highlight where the user is.
  // 1 = need more backtests, 2 = browse/filter, 3 = select 2–8, 4 = click Compare
  const currentStep = !hasEnoughForCompare ? 1
                    : selectedIds.length < 2 ? 2
                    : canCompare ? 3
                    : 0;

  const Step = ({ n, title, body, active, done }) => {
    const tone = done ? COLORS.accent : active ? COLORS.purple : COLORS.textMuted;
    return (
      <div style={{ display: "flex", gap: 12, alignItems: "flex-start",
                    padding: "8px 0",
                    borderTop: n === 1 ? "none" : `1px solid ${COLORS.panelBorder}60` }}>
        <div style={{ flex: "0 0 28px", height: 28, borderRadius: "50%",
                      background: done ? `${COLORS.accent}20` : active ? `${COLORS.purple}25` : "transparent",
                      border: `1px solid ${tone}`,
                      color: tone, fontFamily: mono, fontSize: 12, fontWeight: 700,
                      display: "flex", alignItems: "center", justifyContent: "center" }}>
          {done ? "✓" : n}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontFamily: heading, fontSize: 13, fontWeight: 700,
                        color: active ? COLORS.text : done ? COLORS.textDim : COLORS.textMuted,
                        marginBottom: 2 }}>
            {title}
          </div>
          <div style={{ fontFamily: mono, fontSize: 12, color: COLORS.textDim,
                        lineHeight: 1.5 }}>
            {body}
          </div>
        </div>
      </div>
    );
  };

  return (
    // Bound the whole CompareView to the viewport height (minus the top tab
    // bar + page padding). With this, no element scrolls the window — the
    // library's internal row list is the single flexible child and absorbs
    // whatever vertical space remains. Kills the "vertical wobble" caused by
    // multiple elements competing for page height.
    <div style={{ display: "flex", flexDirection: "column",
                   height: "calc(100vh - 160px)", minHeight: 0, gap: 0,
                   overflow: "hidden" }}>
      {/* How-it-works banner — spells out the COMPARE workflow as discrete,
          left-justified steps so a first-time user knows what to do.
          Stays visible even after running a comparison — the stepper doubles
          as live status (completed step ticks, active step highlights). */}
      <div style={{ marginBottom: 12, padding: "14px 18px",
                    background: `${COLORS.purple}10`, border: `1px solid ${COLORS.purple}40`,
                    borderRadius: 8 }}>
        <div style={{ fontSize: 15, fontFamily: heading, fontWeight: 700,
                      color: COLORS.purple, marginBottom: 4, letterSpacing: "0.02em" }}>
          Compare Past Backtests
        </div>
        <div style={{ fontFamily: mono, fontSize: 12, color: COLORS.textDim,
                      lineHeight: 1.55, marginBottom: 10 }}>
          Rank two or more completed backtests side-by-side on Return, Sharpe,
          Max DD, and Profit Factor — and see which Sharpe differences are real
          signal vs. noise via paired-bootstrap p-values.
        </div>

        <Step
          n={1}
          active={currentStep === 1}
          done={currentStep > 1}
          title="Run some backtests first"
          body={
            hasEnoughForCompare ? (
              <>
                <span style={{ color: COLORS.accent }}>{expCount}</span>{" "}
                experiment{expCount === 1 ? "" : "s"} available in the library below.
              </>
            ) : (
              <>
                You need at least 2 completed runs before you can compare.
                Head to the <span style={{ color: COLORS.blue, fontWeight: 700 }}>BACKTEST</span> tab,
                submit a run, wait for it to finish, then come back here.
                Currently: <span style={{ color: COLORS.warn }}>{expCount}</span> in the library.
              </>
            )
          }
        />
        <Step
          n={2}
          active={currentStep === 2}
          done={currentStep > 2}
          title="Filter and find the experiments you want"
          body="Use the Status / Triggered-By / Tag filters in the library below to narrow the list. Click Refresh if you just finished a run and don't see it."
        />
        <Step
          n={3}
          active={currentStep === 3}
          done={compareReport?.success}
          title="Select 2–8 and click Compare"
          body={
            selectedIds.length === 0 ? (
              "Tick the checkbox on each row you want to include. Maximum 8."
            ) : selectedIds.length === 1 ? (
              <>
                <span style={{ color: COLORS.warn }}>1 selected</span> — pick at least one more.
              </>
            ) : (
              <>
                <span style={{ color: COLORS.purple, fontWeight: 700 }}>{selectedIds.length}</span>
                {" "}selected · hit the <span style={{ color: COLORS.purple, fontWeight: 700 }}>Compare →</span>
                {" "}button under the library to run the analysis.
              </>
            )
          }
        />
      </div>

      {/* Hide the library when a successful comparison is on screen —
          the results panel is the focus; user can hit Change Selection to
          bring the library back. Failed reports keep the library visible
          so the user can adjust their selection without losing the grid. */}
      {!(compareReport && compareReport.success) && (
        <ExperimentLibrary
          experiments={experiments}
          selectedIds={selectedIds}
          onToggleSelect={onToggleSelect}
          onClearSelection={onClearSelection}
          onRefresh={onRefresh}
          onView={onView}
          loading={loading}
          onCompare={onCompare}
          canCompare={canCompare}
          compareInFlight={compareInFlight}
          onGoToBacktest={onGoToBacktest}
          totalInStore={totalInStore}
          compact={!!compareReport}
        />
      )}

      {/* Compare action row removed — the inline Compare button in the
          library header + the selection chip bar + the guided stepper Step 3
          already cover both the action and the "N selected · ready" status,
          so the separate action-row panel was just eating vertical room. */}

      {compareReport && compareReport.success ? (
        <CompareResults report={compareReport}
                        experimentsById={Object.fromEntries(experiments.map(e => [e.id, e]))}
                        onDismiss={onDismissReport} />
      ) : compareReport && !compareReport.success ? (
        <div style={{ marginTop: 10, padding: "12px 16px", background: COLORS.panel,
                      border: `1px solid ${COLORS.warn}60`, borderRadius: 6,
                      fontFamily: mono, fontSize: 12, lineHeight: 1.5,
                      display: "flex", gap: 12, alignItems: "flex-start" }}>
          <span style={{ fontSize: 18, lineHeight: 1 }}>⚠</span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ color: COLORS.warn, fontWeight: 700, marginBottom: 4 }}>
              Comparison couldn't be computed
            </div>
            <div style={{ color: COLORS.textDim }}>
              {compareReport.missing_ids && compareReport.missing_ids.length > 0 ? (
                <>
                  One or more of the selected experiments are no longer in the
                  store. Click <span style={{ color: COLORS.blue, fontWeight: 700 }}>Refresh</span>{" "}
                  on the library header to resync, then re-select.
                  <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 4 }}>
                    Missing: {compareReport.missing_ids.join(", ")}
                  </div>
                </>
              ) : (
                <>
                  {compareReport.error || "Unknown error."}
                  <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 6,
                                lineHeight: 1.5 }}>
                    This almost always means one of the selected experiments
                    is a <b>legacy run</b> from before the metrics-sanitiser fix
                    — its on-disk metrics contain non-finite values that compare()
                    can't rank. Try:
                    <ul style={{ margin: "4px 0 0 18px", padding: 0 }}>
                      <li>Pick a different pair of experiments, or</li>
                      <li>Re-run one of them from the BACKTEST tab to refresh
                          it with the fixed sanitiser.</li>
                    </ul>
                  </div>
                </>
              )}
            </div>
          </div>
          <button
            onClick={onClearSelection}
            style={{ background: "transparent", border: "none", color: COLORS.textDim,
                     cursor: "pointer", fontFamily: mono, fontSize: 11,
                     letterSpacing: "0.08em", textTransform: "uppercase",
                     padding: "4px 8px" }}
            title="Clear the current selection to pick a different set."
          >
            Reset
          </button>
        </div>
      ) : null}

      {/* Tip — reading guide for first-time users once a comparison is pending */}
      {!compareReport && (
        <div style={{ marginTop: 12, padding: "12px 16px",
                      background: COLORS.bg, border: `1px solid ${COLORS.panelBorder}`,
                      borderRadius: 6, fontSize: 12, fontFamily: mono, color: COLORS.textDim,
                      lineHeight: 1.55 }}>
          <span style={{ color: COLORS.text, fontWeight: 600 }}>How to read results:</span>{" "}
          per-metric winners are marked with <span style={{ color: COLORS.accent }}>★</span>.
          Pairwise p-values use a paired bootstrap on per-tick return diffs —
          <span style={{ color: COLORS.accent }}> p&lt;0.05</span> means the sharpe gap isn't just noise.
        </div>
      )}
    </div>
  );
}

function ConnectionStatus({ connected, tick }) {
  // The colored, optionally-pulsing dot conveys the live/disconnected state
  // visually. The text redundantly saying "LIVE" on top of that competes
  // with the LIVE tab label and the AI/engine pill, so we drop it and show
  // just the tick count — the thing the user actually can't infer from
  // anywhere else.
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}
         title={connected ? "Connected to the agent. Tick number increments each engine tick." : "Disconnected — the agent isn't running or the WebSocket dropped."}>
      <div style={{
        width: 8, height: 8, borderRadius: "50%",
        background: connected ? COLORS.accent : COLORS.danger,
        boxShadow: `0 0 8px ${connected ? COLORS.accent : COLORS.danger}80`,
        animation: connected ? "none" : "pulse 1.5s infinite",
      }} />
      <span style={{ fontSize: 11, fontFamily: mono, color: connected ? COLORS.accent : COLORS.danger,
                     letterSpacing: "0.04em" }}>
        {connected ? `Tick #${tick}` : "DISCONNECTED"}
      </span>
    </div>
  );
}

// ─── Main App ───

export default function App() {
  const [connected, setConnected] = useState(false);
  const [state, setState] = useState(null);
  const [history, setHistory] = useState([]);
  const [orderJournal, setOrderJournal] = useState([]);
  // Phase 8: tab switcher + backtest message stash
  const [activeTab, setActiveTab] = useState("LIVE");   // LIVE | BACKTEST | COMPARE
  const [btProgress, setBtProgress] = useState({});     // experiment_id -> progress msg
  const [btResults, setBtResults] = useState({});       // experiment_id -> result summary
  const [btReviews, setBtReviews] = useState({});       // experiment_id -> review
  const [btLastAck, setBtLastAck] = useState(null);     // most recent backtest_start_ack
  // Phase 9: per-experiment rolling equity history for the observer modal.
  // Shape: {experiment_id -> {pair -> [equity...]}}. Bounded to 500 pts/pair.
  const [btEquityHistory, setBtEquityHistory] = useState({});
  const [btActiveExpId, setBtActiveExpId] = useState(null);  // which exp the observer is focused on
  const [observerClosed, setObserverClosed] = useState(false); // user dismissed → hide until a new run
  // Phase 10: experiment library + compare state
  const [libExperiments, setLibExperiments] = useState([]);    // full list from WS
  const [libLoading, setLibLoading] = useState(false);
  const [compareSelected, setCompareSelected] = useState([]);  // ids chosen for compare
  const [compareReport, setCompareReport] = useState(null);    // last compare ack
  const [viewingExpId, setViewingExpId] = useState(null);      // single-experiment detail view (stretch)
  // ─── Companion state (Phase 1+) ───
  const [companions, setCompanions] = useState({});             // companion_id -> meta
  const [activeCompanion, setActiveCompanion] = useState(() => {
    try { return localStorage.getItem("hydra.companion.active") || "apex"; }
    catch { return "apex"; }
  });
  const [companionDrawerOpen, setCompanionDrawerOpen] = useState(() => {
    try { return localStorage.getItem("hydra.companion.drawer.open") === "1"; }
    catch { return false; }
  });
  const [companionDrawerWidth, setCompanionDrawerWidth] = useState(() => {
    try { return parseInt(localStorage.getItem("hydra.companion.drawer.width") || "380", 10); }
    catch { return 380; }
  });
  // Per-companion state as INDEPENDENT useState hooks so updates to one
  // companion physically cannot leak into another. A prior object-keyed
  // state had a subtle cross-contamination bug where user-echo messages
  // appeared in all three drawers.
  const [athenaMessages, setAthenaMessages] = useState([]);
  const [apexMessages, setApexMessages] = useState([]);
  const [broskiMessages, setBroskiMessages] = useState([]);
  const [athenaTyping, setAthenaTyping] = useState(false);
  const [apexTyping, setApexTyping] = useState(false);
  const [broskiTyping, setBroskiTyping] = useState(false);
  const [athenaUnread, setAthenaUnread] = useState(false);
  const [apexUnread, setApexUnread] = useState(false);
  const [broskiUnread, setBroskiUnread] = useState(false);
  // Unified read/write helpers. The setter IS a single companion's setter,
  // so overlapping state updates are impossible.
  const getMessages = useCallback((cid) =>
    cid === "athena" ? athenaMessages
    : cid === "apex" ? apexMessages
    : broskiMessages,
    [athenaMessages, apexMessages, broskiMessages]
  );
  const getMessageSetter = useCallback((cid) =>
    cid === "athena" ? setAthenaMessages
    : cid === "apex" ? setApexMessages
    : setBroskiMessages,
    []
  );
  const getTypingSetter = useCallback((cid) =>
    cid === "athena" ? setAthenaTyping
    : cid === "apex" ? setApexTyping
    : setBroskiTyping,
    []
  );
  const getUnreadSetter = useCallback((cid) =>
    cid === "athena" ? setAthenaUnread
    : cid === "apex" ? setApexUnread
    : setBroskiUnread,
    []
  );
  const getTyping = (cid) =>
    cid === "athena" ? athenaTyping
    : cid === "apex" ? apexTyping
    : broskiTyping;
  const getUnread = (cid) =>
    cid === "athena" ? athenaUnread
    : cid === "apex" ? apexUnread
    : broskiUnread;
  const [companionCostAlerts, setCompanionCostAlerts] = useState({});
  const [companionVisible, setCompanionVisible] = useState(true);    // optimistic \u2014 orb shows immediately; hides on failed connect
  const wsRef = useRef(null);
  const reconnectRef = useRef(null);
  // Latest `connect` closure — the setTimeout reconnect callback reads
  // through this ref instead of the stale closure it captured at
  // definition-time (otherwise ESLint flags a use-before-declare and the
  // retry can fire against an outdated applyLiveState handler after HMR).
  const connectRef = useRef(null);
  // mountedRef guards against setState-on-unmounted warnings (noticeable
  // in StrictMode which double-mounts in dev). WS callbacks capture the
  // ref closure and bail out cleanly when the component has unmounted.
  const mountedRef = useRef(true);

  // Shared state applier — invoked by BOTH the legacy raw-state path and
  // the new wrapped {type:"state", data:state} path.
  const applyLiveState = useCallback((data) => {
    setState(data);
    if (data.pairs) {
      const liveTotal = data.balance_usd?.total_usd;
      const engineEquity = Object.values(data.pairs).reduce((sum, p) => sum + (p.portfolio?.equity || 0), 0);
      setHistory((prev) => [...prev, liveTotal != null ? liveTotal : engineEquity].slice(-500));
    }
    if (data.order_journal) setOrderJournal(data.order_journal);
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;
    ws.onopen = () => { if (mountedRef.current) setConnected(true); };
    ws.onmessage = (event) => {
      if (!mountedRef.current) return;
      try {
        const msg = JSON.parse(event.data);

        // Phase 6+ wrapped state: {type:"state", data:{...}}
        if (msg && msg.type === "state" && msg.data) {
          applyLiveState(msg.data);
          return;
        }
        // New typed messages (Phase 6+)
        if (msg && typeof msg.type === "string") {
          switch (msg.type) {
            case "backtest_progress":
              setBtProgress((prev) => ({ ...prev, [msg.experiment_id]: msg }));
              // Accumulate per-pair equity for the observer chart. Cap total
              // stored experiments at MAX_EQUITY_HISTORY_EXPERIMENTS (LRU-ish)
              // so long sessions don't leak memory across many runs.
              if (msg.dashboard_state?.pairs) {
                setBtEquityHistory((prev) => {
                  const prior = prev[msg.experiment_id] || {};
                  const next = { ...prior };
                  for (const [p, ps] of Object.entries(msg.dashboard_state.pairs)) {
                    next[p] = [...(prior[p] || []), ps.portfolio?.equity || 0].slice(-500);
                  }
                  const merged = { ...prev, [msg.experiment_id]: next };
                  const keys = Object.keys(merged);
                  if (keys.length <= MAX_EQUITY_HISTORY_EXPERIMENTS) return merged;
                  // Drop oldest by insertion order; the freshly-written key
                  // is last, so slicing preserves it.
                  const keep = keys.slice(-MAX_EQUITY_HISTORY_EXPERIMENTS);
                  const trimmed = {};
                  for (const k of keep) trimmed[k] = merged[k];
                  return trimmed;
                });
              }
              // Freshest run becomes the observer focus; re-open if the user closed it.
              setBtActiveExpId(msg.experiment_id);
              setObserverClosed(false);
              return;
            case "backtest_result":
              setBtResults((prev) => ({ ...prev, [msg.experiment_id]: msg }));
              // Auto-refresh the library so the freshly-completed run is
              // present when the user next opens the COMPARE tab — without
              // this, the library only refreshes on tab-switch or manual
              // Refresh, which made completed runs look "missing".
              if (wsRef.current?.readyState === WebSocket.OPEN) {
                try {
                  wsRef.current.send(JSON.stringify({
                    type: "experiment_list_request", limit: 100,
                  }));
                } catch { /* swallow — next tab switch will refetch */ }
              }
              return;
            case "backtest_review":
              setBtReviews((prev) => ({ ...prev, [msg.experiment_id]: msg.review }));
              return;
            case "backtest_start_ack":
              setBtLastAck(msg);
              if (msg.experiment_id) {
                setBtActiveExpId(msg.experiment_id);
                setObserverClosed(false);
              }
              return;
            case "experiment_list_request_ack":
              setLibLoading(false);
              if (msg.success && Array.isArray(msg.experiments)) {
                setLibExperiments(msg.experiments);
              }
              return;
            case "experiment_compare_request_ack":
              setCompareReport(msg);
              setCompareInFlight(false);
              return;
            case "experiment_get_request_ack":
              // Single-experiment fetch — Phase 10 stretches this via the
              // viewing drawer; for now we stash the raw payload so a
              // future modal can render the full BacktestResult.
              if (msg.success && msg.experiment) {
                setViewingExpId(msg.experiment.id);
              }
              setViewInFlight(null);
              return;
            // ─── Companion channel ───
            case "companion.connect_ack": {
              if (msg.success) {
                const metas = {};
                for (const c of (msg.all_companions || [])) metas[c.id] = c;
                if (msg.companion) metas[msg.companion.id] = msg.companion;
                setCompanions((prev) => ({ ...prev, ...metas }));
                setCompanionVisible(true);
                // Seed history for the specific companion the server named.
                // (Was previously in the else-branch by mistake, which meant
                // initial-open history never populated.)
                if (msg.companion && Array.isArray(msg.history_tail)) {
                  const seeded = msg.history_tail.map((t, i) => ({
                    id: `seed-${msg.companion.id}-${i}`,
                    role: t.role, text: t.content,
                    display_name: t.role === "assistant" ? msg.companion.display_name : null,
                  }));
                  getMessageSetter(msg.companion.id)(seeded);
                }
              } else {
                setCompanionVisible(false);
              }
              return;
            }
            case "companion.switch_ack": {
              if (msg.success && msg.companion) {
                setCompanions((prev) => ({ ...prev, [msg.companion.id]: msg.companion }));
                if (Array.isArray(msg.history_tail)) {
                  const seeded = msg.history_tail.map((t, i) => ({
                    id: `seed-${msg.companion.id}-${i}`,
                    role: t.role, text: t.content,
                    display_name: t.role === "assistant" ? msg.companion.display_name : null,
                  }));
                  getMessageSetter(msg.companion.id)(seeded);
                }
              }
              return;
            }
            case "companion.typing": {
              const cid = msg.companion_id;
              if (cid) {
                getTypingSetter(cid)(msg.state === "thinking");
              }
              return;
            }
            case "companion.message.complete": {
              const cid = msg.companion_id;
              if (cid) {
                getTypingSetter(cid)(false);
                getMessageSetter(cid)((list) => [...list, {
                  id: msg.message_id || `m-${Date.now()}-${Math.random()}`,
                  role: "assistant",
                  text: msg.text || "",
                  display_name: companions[cid]?.display_name || COMPANION_NAMES[cid],
                  error: msg.error,
                  intent: msg.intent,
                  model_used: msg.model_used,
                  proactive: msg.proactive === true,
                }].slice(-200));
                if (!companionDrawerOpen || activeCompanion !== cid) {
                  getUnreadSetter(cid)(true);
                }
              }
              return;
            }
            case "companion.cost_alert": {
              const cid = msg.companion_id;
              if (cid) {
                setCompanionCostAlerts((prev) => ({ ...prev, [cid]: msg }));
              }
              return;
            }
            case "companion.trade.proposal":
            case "companion.ladder.proposal": {
              const cid = msg.companion_id;
              const kind = msg.type === "companion.ladder.proposal" ? "ladder" : "trade";
              if (cid) {
                const proposalEntry = {
                  id: msg.proposal_id, role: "proposal", kind,
                  proposal: msg.card, token: msg.confirmation_token,
                  nonce: msg.nonce, ttl: msg.ttl_expires_at, status: null,
                };
                getMessageSetter(cid)((list) => [...list, proposalEntry].slice(-200));
                if (!companionDrawerOpen || activeCompanion !== cid) {
                  getUnreadSetter(cid)(true);
                }
              }
              return;
            }
            case "companion.trade.executed":
            case "companion.ladder.executed": {
              const cid = msg.companion_id;
              if (cid) {
                getMessageSetter(cid)((list) =>
                  list.map((m) => m.id === msg.proposal_id
                    ? { ...m, status: msg.status || "filled" }
                    : m));
              }
              return;
            }
            case "companion.trade.failed": {
              const cid = msg.companion_id;
              if (cid) {
                getMessageSetter(cid)((list) =>
                  list.map((m) => m.id === msg.proposal_id
                    ? { ...m, status: "failed" }
                    : m));
              }
              return;
            }
            case "companion.system_note": {
              const cid = activeCompanion;
              getMessageSetter(cid)((list) => [...list, {
                id: `sys-${Date.now()}`,
                role: "system", text: msg.text || "", display_name: null,
              }].slice(-200));
              return;
            }
            case "error":
              // Backtest channel errors land here; keep quiet otherwise.
              if (msg.channel === "backtest") setBtLastAck(msg);
              // Release any in-flight gate so the button re-enables.
              setCompareInFlight(false);
              setViewInFlight(null);
              return;
            default:
              // Unknown typed message → drop silently. Do NOT fall through
              // to applyLiveState: a malformed backtest-side message with
              // a misnamed `type` could otherwise overwrite live fields
              // (e.g., pairs, brain) with partial/stale data. The legacy
              // raw-state shape has no `type` field at all.
              return;
          }
        }
        // Legacy raw live-state dict: only accept payloads WITHOUT a `type`
        // field AND with at least one recognizable top-level live-state key.
        // This guards against typos in new typed-message names corrupting
        // the LIVE view during the one-release compat window.
        if (msg && typeof msg === "object" && msg.type === undefined
            && LIVE_STATE_KEYS.some((k) => k in msg)) {
          applyLiveState(msg);
        }
      } catch (e) { console.error("[HYDRA] Parse error:", e); }
    };
    ws.onclose = () => {
      if (!mountedRef.current) return;
      setConnected(false);
      reconnectRef.current = setTimeout(() => connectRef.current?.(), 3000);
    };
    ws.onerror = () => { ws.close(); };
  }, [applyLiveState]);

  // Keep `connectRef` pointing at the freshest connect closure
  useEffect(() => { connectRef.current = connect; }, [connect]);

  // Phase 8: send a typed WS message (used by BacktestControlPanel).
  const sendMessage = useCallback((msg) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    try {
      ws.send(JSON.stringify(msg));
      return true;
    } catch (e) {
      console.error("[HYDRA] WS send error:", e);
      return false;
    }
  }, []);

  // ─── Companion send/switch + connect kickoff ───
  const companionConnect = useCallback(() => {
    sendMessage({ type: "companion.connect", companion_id: activeCompanion });
  }, [sendMessage, activeCompanion]);

  const companionProposalConfirm = useCallback((m) => {
    const type = m.kind === "ladder" ? "companion.ladder.confirm" : "companion.trade.confirm";
    sendMessage({
      type,
      proposal_id: m.id,
      confirmation_token: m.token,
      nonce: m.nonce,
      ttl_expires_at: m.ttl,
    });
    // Optimistic: mark submitting so buttons hide
    getMessageSetter(activeCompanion)((list) =>
      list.map((x) => x.id === m.id ? { ...x, status: "submitting" } : x));
  }, [sendMessage, activeCompanion, getMessageSetter]);

  const companionProposalReject = useCallback((m) => {
    const type = m.kind === "ladder" ? "companion.ladder.reject" : "companion.trade.reject";
    sendMessage({ type, proposal_id: m.id });
    getMessageSetter(activeCompanion)((list) =>
      list.map((x) => x.id === m.id ? { ...x, status: "rejected" } : x));
  }, [sendMessage, activeCompanion, getMessageSetter]);

  const companionSend = useCallback((text) => {
    const msgId = `u-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const cid = activeCompanion;

    // ─── Slash-command interception (no LLM call) ───
    const trimmed = text.trim();
    if (trimmed.startsWith("/")) {
      const [cmd, ...rest] = trimmed.slice(1).split(/\s+/);
      const arg = rest.join(" ").trim();

      const sysNote = (note) => ({
        id: `sys-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        role: "system", text: note,
      });

      if (cmd === "clear") {
        const scope = arg === "all" ? "all" : "one";
        sendMessage({
          type: "companion.transcript.clear",
          companion_id: cid, scope,
        });
        if (scope === "all") {
          setAthenaMessages([sysNote("(all three transcripts cleared)")]);
          setApexMessages([]);
          setBroskiMessages([]);
        } else {
          getMessageSetter(cid)([sysNote("(transcript cleared)")]);
        }
        return;
      }

      if (cmd === "help") {
        getMessageSetter(cid)((list) => [...list,
          { id: msgId, role: "user", text },
          sysNote(
            "commands:\n" +
            "  /clear        \u2014 clear this companion's transcript\n" +
            "  /clear all    \u2014 clear all three transcripts\n" +
            "  /help         \u2014 show this list"
          ),
        ].slice(-200));
        return;
      }
    }

    // Optimistic: add the user message immediately to the ACTIVE companion only.
    getMessageSetter(cid)((list) => [...list, { id: msgId, role: "user", text }].slice(-200));
    getTypingSetter(cid)(true);
    const ok = sendMessage({
      type: "companion.message",
      companion_id: cid,
      text, message_id: msgId,
    });
    if (!ok) {
      getTypingSetter(cid)(false);
      getMessageSetter(cid)((list) => [...list, {
        id: `err-${msgId}`, role: "system",
        text: "(not connected to agent \u2014 restart Hydra or refresh)",
      }].slice(-200));
      return;
    }
    // 30s timeout \u2014 helpful error if no reply arrives.
    setTimeout(() => {
      getMessageSetter(cid)((list) => {
        if (list.some((m) => m.id === `timeout-${msgId}`)) return list;
        return [...list, {
          id: `timeout-${msgId}`, role: "system",
          text: "(no response in 30s \u2014 check the agent console for errors; API key may be missing or model rate-limited)",
        }].slice(-200);
      });
      getTypingSetter(cid)(false);
    }, 30000);
  }, [sendMessage, activeCompanion, getMessageSetter, getTypingSetter]);

  const companionSwitch = useCallback((cid) => {
    setActiveCompanion(cid);
    getUnreadSetter(cid)(false);
    try { localStorage.setItem("hydra.companion.active", cid); } catch {}
    sendMessage({ type: "companion.switch", to_id: cid });
  }, [sendMessage, getUnreadSetter]);

  const companionToggle = useCallback(() => {
    setCompanionDrawerOpen((prev) => {
      const next = !prev;
      try { localStorage.setItem("hydra.companion.drawer.open", next ? "1" : "0"); } catch {}
      if (next) getUnreadSetter(activeCompanion)(false);
      return next;
    });
  }, [activeCompanion, getUnreadSetter]);

  // On WS connect, probe the companion subsystem. If unmounted server-side,
  // no connect_ack arrives and the orb stays invisible.
  useEffect(() => {
    if (connected) companionConnect();
  }, [connected, companionConnect]);

  // Phase 10 — library + compare helpers
  const fetchLibrary = useCallback(() => {
    setLibLoading(true);
    sendMessage({ type: "experiment_list_request", limit: 100 });
  }, [sendMessage]);

  const toggleSelectExperiment = useCallback((id) => {
    if (!id) return;
    setCompareSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id].slice(0, 8)
    );
  }, []);

  const clearSelection = useCallback(() => setCompareSelected([]), []);

  // Prune stale/ghost IDs from the selection whenever the library updates.
  // Prevents a pre-selected experiment_id (from "Compare this run →") that
  // hasn't landed in the server's list yet from occupying a slot silently.
  useEffect(() => {
    if (!Array.isArray(libExperiments) || libExperiments.length === 0) return;
    const known = new Set(libExperiments.map((e) => e.id));
    setCompareSelected((prev) => {
      const kept = prev.filter((id) => known.has(id));
      return kept.length === prev.length ? prev : kept;
    });
  }, [libExperiments]);

  // Debounce for compare and detail-fetch to prevent a trigger-happy user
  // from flooding the backend with duplicate requests before the ack lands.
  const [compareInFlight, setCompareInFlight] = useState(false);
  const [viewInFlight, setViewInFlight] = useState(null);  // experiment_id

  const runCompare = useCallback(() => {
    if (compareSelected.length < 2 || compareInFlight) return;
    setCompareReport(null);     // show spinner until ack lands
    setCompareInFlight(true);
    sendMessage({
      type: "experiment_compare_request",
      experiment_ids: compareSelected,
    });
  }, [compareSelected, sendMessage, compareInFlight]);

  const viewExperiment = useCallback((id) => {
    if (!id || viewInFlight === id) return;   // ignore re-clicks on pending id
    setViewInFlight(id);
    sendMessage({ type: "experiment_get_request", experiment_id: id });
  }, [sendMessage, viewInFlight]);

  // Auto-refresh library whenever COMPARE tab activates (freshest state wins).
  useEffect(() => {
    if (activeTab === "COMPARE" && connected) fetchLibrary();
  }, [activeTab, connected, fetchLibrary]);

  // Compare only works on experiments that are in a comparable state —
  // the run must be "complete" AND have at least one non-null primary metric.
  // Any other state (running, pending, failed, cancelled, or complete-but-
  // missing-metrics) would either reject server-side or produce a nonsense
  // row, so hide them from the library UI entirely.
  const filteredExperiments = (libExperiments || []).filter((e) => {
    if (e.status !== "complete") return false;
    const m = e.metrics;
    if (!m) return false;
    // At least one primary metric must be a number (null is the
    // sanitiser's "non-finite" marker). If all are null the row is dead.
    return m.total_return_pct != null || m.sharpe != null || m.max_drawdown_pct != null;
  });

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const pairs = state?.pairs || {};
  const pairNames = Object.keys(pairs);
  const balance = state?.balance || {};
  const balanceUsd = state?.balance_usd || null;
  const aiBrain = state?.ai_brain || null;
  const tick = state?.tick || 0;
  const elapsed = state?.elapsed || 0;
  const remaining = state?.remaining || 0;

  // Total Balance: use real exchange balance when available, fall back to engine equity
  const totalEquity = balanceUsd?.total_usd != null ? balanceUsd.total_usd : Object.values(pairs).reduce((s, p) => s + (p.portfolio?.equity || 0), 0);
  // P&L: journal-derived realized + unrealized, converted to USD. Authoritative
  // across --resume (engine pnl_pct resets because initial_balance gets re-split).
  const journalPnlUsd = state?.journal_stats?.total_pnl_usd ?? 0;
  // Max drawdown: engine tracks historical max per pair (persists across --resume).
  // Supplement with max drawdown from the dashboard's own balance history so
  // exchange-level drops (across all pairs) are also captured.
  const engineDD = Math.max(...Object.values(pairs).map(p => p.portfolio?.max_drawdown_pct || 0), 0);
  let histDD = 0;
  if (history.length > 1) {
    let peak = history[0];
    for (let i = 1; i < history.length; i++) {
      if (history[i] > peak) peak = history[i];
      const dd = peak > 0 ? ((peak - history[i]) / peak * 100) : 0;
      if (dd > histDD) histDD = dd;
    }
  }
  const maxDD = Math.max(engineDD, histDD);
  // Engine round-trip trades (position fully closed)
  const totalTrades = Object.values(pairs).reduce((s, p) => s + (p.performance?.total_trades || 0), 0);
  const totalWins = Object.values(pairs).reduce((s, p) => s + (p.performance?.win_count || 0), 0);
  const totalLosses = Object.values(pairs).reduce((s, p) => s + (p.performance?.loss_count || 0), 0);
  const engineWinRate = (totalWins + totalLosses) > 0 ? (totalWins / (totalWins + totalLosses) * 100) : 0;
  // Journal fill stats — computed from FULL journal on the backend (not the
  // 20-entry window shown in the order list). Reflects actual exchange activity.
  const jStats = state?.journal_stats || {};
  const totalFills = jStats.total_fills || 0;
  const fillsByPair = jStats.fills_by_pair || {};
  const fillWinRate = jStats.fill_win_rate || 0;
  // Win rate: prefer engine round-trip rate when available, fall back to
  // journal fill-derived rate so the stat updates as soon as sells execute.
  const overallWinRate = totalTrades > 0 ? engineWinRate : fillWinRate;

  return (
    <div style={{ background: COLORS.bg, minHeight: "100vh", color: COLORS.text, padding: 0 }}>
      {/* Header */}
      <div style={{ borderBottom: `1px solid ${COLORS.panelBorder}`, padding: "16px 24px", display: "flex", alignItems: "center", justifyContent: "space-between", background: `${COLORS.panel}cc`, backdropFilter: "blur(12px)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <img src="/favicon.svg" alt="Hydra" style={{ width: 38, height: 38, filter: "drop-shadow(0 0 6px rgba(126, 20, 255, 0.4))" }} />
          <div style={{ fontSize: 26, fontWeight: 800, fontFamily: heading, letterSpacing: "-0.04em" }}>
            <span style={{ color: COLORS.accent }}>H</span><span style={{ color: COLORS.text }}>YDRA</span>
          </div>
          <div style={{ fontSize: 10, color: COLORS.textMuted, fontFamily: mono, lineHeight: 1.3, borderLeft: `1px solid ${COLORS.panelBorder}`, paddingLeft: 10, maxWidth: 220 }}>
            Hyper-adaptive Dynamic<br />Regime-switching Universal Agent
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <TabSwitcher
            activeTab={activeTab}
            onChange={setActiveTab}
            backtestRunning={Object.values(btProgress).some(p => p?.stage === "running")}
          />
          {/* Mode pill — indicates whether the AI brain is attached on top of
              the engine. Prior copy was "AI LIVE" / "LIVE TRADING" which
              collided with the LIVE tab and the connection indicator. */}
          {/* Pill dimensions (padding + fontSize + letterSpacing) match the
              TabSwitcher buttons so the whole header reads as one consistent
              row of controls. QuantumIcon uses COLORS.text (near-white) when
              the brain is active so it pops cleanly against the blue-tinted
              pill background instead of blending with the blue border/text. */}
          <div title={aiBrain
                ? "Claude Analyst + Risk Manager + Grok Strategist are reasoning over engine signals."
                : "Pure engine execution — no AI brain attached. Signals run straight from the engine to the order layer."}
               style={{ padding: "0 14px", minHeight: 38, borderRadius: 4,
                        fontSize: 12, fontWeight: 700, fontFamily: mono,
                        display: "inline-flex", alignItems: "center", gap: 8,
                        background: aiBrain ? `${COLORS.blue}18` : "transparent",
                        color: aiBrain ? COLORS.blue : COLORS.textDim,
                        border: `1px solid ${aiBrain ? `${COLORS.blue}60` : COLORS.panelBorder}`,
                        textTransform: "uppercase", letterSpacing: "0.1em" }}>
            <QuantumIcon active={!!aiBrain} size={22}
                         color={aiBrain ? COLORS.text : COLORS.textDim} />
            {aiBrain ? "AI Brain" : "Engine Only"}
          </div>
          <ConnectionStatus connected={connected} tick={tick} />
          {elapsed > 0 && (
            <span style={{ fontSize: 11, fontFamily: mono, color: COLORS.textDim }}>
              {Math.floor(elapsed / 60)}m{Math.floor(elapsed % 60)}s{remaining > 0 ? ` / ${Math.floor((elapsed + remaining) / 60)}m` : ""}
            </span>
          )}
        </div>
      </div>

      {/* Phase 8/9: BACKTEST + COMPARE tab content. LIVE falls through to the
          existing grid below. Phase 9: observer modal is also surfaced as a
          floating right-side panel on LIVE when a run is mid-flight. */}
      {(() => {
        // Pick the freshest experiment to observe: the one the user most
        // recently kicked off, or the freshest progress / result in memory.
        const obsId = btActiveExpId
                   || Object.keys(btProgress).slice(-1)[0]
                   || Object.keys(btResults).slice(-1)[0]
                   || null;
        const obsProgress = obsId ? btProgress[obsId] : null;
        const obsResult = obsId ? btResults[obsId] : null;
        const obsReview = obsId ? btReviews[obsId] : null;
        const obsEquity = obsId ? btEquityHistory[obsId] : null;
        // Best-effort total-ticks hint: parse data_source_params n_candles
        // from config if we have it on the result; else 0 → indeterminate bar.
        let totalTicks = 0;
        if (obsResult?.config?.data_source_params_json) {
          try { totalTicks = JSON.parse(obsResult.config.data_source_params_json).n_candles || 0; }
          catch { /* ignore */ }
        }

        return (
          <>
            {activeTab === "BACKTEST" && (
              <div style={{ padding: "16px 24px" }}>
                <BacktestControlPanel
                  onSubmit={sendMessage}
                  connected={connected}
                  disabled={false}
                  ackMsg={btLastAck}
                  lastResultId={Object.keys(btResults).slice(-1)[0] || null}
                  completedCount={Object.keys(btResults).length}
                  reviewedCount={Object.keys(btReviews).length}
                  observerProgress={observerClosed ? null : obsProgress}
                  observerResult={observerClosed ? null : obsResult}
                  observerReview={observerClosed ? null : obsReview}
                  observerEquity={obsEquity}
                  observerTotalTicks={totalTicks}
                  onObserverClose={() => setObserverClosed(true)}
                  onCompareThisRun={(expId) => {
                    // Jump to COMPARE, refresh the library so the fresh run
                    // is present, and pre-select it so the user only needs
                    // to pick one more to comparison against.
                    setCompareSelected((prev) =>
                      prev.includes(expId) ? prev : [...prev, expId].slice(0, 8)
                    );
                    setActiveTab("COMPARE");
                    fetchLibrary();
                  }}
                />
              </div>
            )}
            {activeTab === "COMPARE" && (
              <div style={{ padding: "16px 24px" }}>
                {viewingExpId && (
                  <div style={{ marginBottom: 12, padding: "10px 16px",
                                background: `${COLORS.blue}10`,
                                border: `1px solid ${COLORS.blue}40`, borderRadius: 4,
                                fontFamily: mono, fontSize: 12, color: COLORS.blue,
                                display: "flex", justifyContent: "space-between",
                                alignItems: "center" }}>
                    <span>Fetched experiment detail: {viewingExpId.slice(0, 16)}…</span>
                    <button onClick={() => setViewingExpId(null)}
                            style={{ background: "transparent", border: "none",
                                     color: COLORS.blue, cursor: "pointer",
                                     fontFamily: mono, fontSize: 12 }}>
                      dismiss
                    </button>
                  </div>
                )}
                <CompareView
                  experiments={filteredExperiments}
                  selectedIds={compareSelected}
                  onToggleSelect={toggleSelectExperiment}
                  onClearSelection={clearSelection}
                  onRefresh={fetchLibrary}
                  onView={viewExperiment}
                  onCompare={runCompare}
                  compareReport={compareReport}
                  loading={libLoading}
                  compareInFlight={compareInFlight}
                  onGoToBacktest={() => setActiveTab("BACKTEST")}
                  totalInStore={libExperiments.length}
                  onDismissReport={() => {
                    // "Change Selection" path: clear the last result so the
                    // library reappears and the user can pick a different
                    // set. Selection itself is kept — likely the user wants
                    // to swap one row, not start from scratch.
                    setCompareReport(null);
                  }}
                />
              </div>
            )}
            {/* Floating observer on LIVE tab — dual-state view. Appears
                whenever a backtest is mid-run or just completed; user can
                dismiss. Shares the exact same ObserverModal component as
                the BACKTEST dock so visuals match. */}
            {activeTab === "LIVE" && !observerClosed && obsId && (obsProgress || obsResult) && (
              <ObserverModal
                progress={obsProgress}
                result={obsResult}
                review={obsReview}
                equityHistory={obsEquity}
                totalTicks={totalTicks}
                variant="floating"
                onClose={() => setObserverClosed(true)}
              />
            )}
          </>
        );
      })()}

      {activeTab === "LIVE" && ((!connected && !state) || (state && pairNames.length === 0)) ? (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "80vh", flexDirection: "column", gap: 16 }}>
          <img src="/favicon.svg" alt="Hydra" style={{ width: 80, height: 80, filter: "drop-shadow(0 0 12px rgba(126, 20, 255, 0.5))", marginBottom: 8 }} />
          <div style={{ fontSize: 48, fontWeight: 800, fontFamily: heading, color: COLORS.textMuted }}>HYDRA</div>
          <div style={{ fontSize: 14, color: COLORS.textDim, fontFamily: mono }}>
            {connected ? "Waiting for first tick data..." : `Waiting for agent connection on ${WS_URL}...`}
          </div>
          <div style={{ fontSize: 11, color: COLORS.textMuted, fontFamily: mono }}>python hydra_agent.py --pairs SOL/USDC,SOL/BTC,BTC/USDC</div>
        </div>
      ) : null}

      {activeTab === "LIVE" && state && pairNames.length > 0 && (
        <div style={{ padding: "16px 24px" }}>
          {/* Full grid — stats span top, then pair panels + sidebar below */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 280px", gap: 12, alignItems: "start" }}>
            {/* Stats Row — spans both columns for edge-to-edge alignment */}
            <div style={{ gridColumn: "1 / -1", display: "flex", gap: 8 }}>
              <StatCard label="Total Balance" value={`$${totalEquity.toFixed(2)}`} color={COLORS.text} />
              <StatCard label="P&L" value={`${journalPnlUsd >= 0 ? "+$" : "-$"}${Math.abs(journalPnlUsd).toFixed(2)}`} color={journalPnlUsd >= 0 ? COLORS.buy : COLORS.sell} />
              <StatCard label="Max Drawdown" value={maxDD.toFixed(2)} unit="%" color={maxDD > 5 ? COLORS.danger : COLORS.warn} />
              <StatCard label="Fills" value={totalFills} color={COLORS.blue} />
              <StatCard label="Win Rate" value={overallWinRate.toFixed(0)} unit="%" color={overallWinRate > 55 ? COLORS.buy : overallWinRate > 0 ? COLORS.warn : COLORS.textDim} />
            </div>
            {/* LEFT: Pair panels + equity + trade log */}
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {pairNames.map((pair) => {
                const ps = pairs[pair];
                const sig = ps.signal || {};
                const port = ps.portfolio || {};
                const pos = ps.position || {};
                const ind = ps.indicators || {};

                return (
                  <div key={pair} style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 10, padding: 16 }}>
                    {/* Pair header */}
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                      <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
                        <span style={{ fontSize: 16, fontWeight: 700, fontFamily: heading, color: COLORS.text }}>{pair}</span>
                        <span style={{ fontSize: 22, fontWeight: 700, fontFamily: mono, color: COLORS.text }}>{fmtPrice(ps.price || 0, pairPrefix(pair))}</span>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <div style={{ width: 7, height: 7, borderRadius: "50%", background: regimeColor(ps.regime), boxShadow: `0 0 8px ${regimeColor(ps.regime)}80` }} />
                        <span style={{ fontSize: 11, fontWeight: 700, color: regimeColor(ps.regime), fontFamily: mono, textTransform: "uppercase" }}>
                          {(ps.regime || "").replace("_", " ")}
                        </span>
                        <span style={{ fontSize: 10, color: COLORS.textDim, fontFamily: mono }}>
                          {strategyIcon(ps.strategy)} {(ps.strategy || "").replace("_", " ")}
                        </span>
                      </div>
                    </div>

                    {/* Candlestick Chart */}
                    {(ps.candles && ps.candles.length > 5) && (
                      <CandleChart candles={ps.candles.slice(-80)} width={700} height={80} />
                    )}

                    {/* Signal + Position + Equity row */}
                    <div style={{ display: "flex", gap: 16, marginTop: 10 }}>
                      {/* Signal */}
                      <div style={{ flex: 1 }}>
                        <ConfidenceMeter confidence={sig.confidence || 0} signal={sig.action || "HOLD"} />
                        <div style={{ fontSize: 11, color: COLORS.textMuted, fontFamily: mono, lineHeight: 1.4 }}>{sig.reason || ""}</div>
                      </div>
                      {/* Position */}
                      <div style={{ minWidth: 170, borderLeft: `1px solid ${COLORS.panelBorder}`, paddingLeft: 16 }}>
                        <div style={{ fontSize: 10, color: COLORS.textDim, textTransform: "uppercase", fontFamily: mono, marginBottom: 4 }}>Position</div>
                        {pos.size > 0 ? (
                          <>
                            <div style={{ fontSize: 14, fontWeight: 700, fontFamily: mono }}>{pos.size.toFixed(8)}</div>
                            <div style={{ fontSize: 10, color: COLORS.textDim, fontFamily: mono }}>@ {fmtPrice(pos.avg_entry || 0, pairPrefix(pair))}</div>
                            <div style={{ fontSize: 12, fontWeight: 700, fontFamily: mono, color: (pos.unrealized_pnl || 0) >= 0 ? COLORS.buy : COLORS.sell, marginTop: 2 }}>
                              {fmtPrice(Math.abs(pos.unrealized_pnl || 0), (pos.unrealized_pnl || 0) >= 0 ? "+" + pairPrefix(pair) : "-" + pairPrefix(pair))}
                            </div>
                          </>
                        ) : (
                          <div style={{ fontSize: 11, color: COLORS.textMuted, fontFamily: mono }}>Flat</div>
                        )}
                      </div>
                      {/* Equity */}
                      <div style={{ minWidth: 110, borderLeft: `1px solid ${COLORS.panelBorder}`, paddingLeft: 16 }}>
                        <div style={{ fontSize: 10, color: COLORS.textDim, textTransform: "uppercase", fontFamily: mono, marginBottom: 4 }}>Balance</div>
                        <div style={{ fontSize: 14, fontWeight: 700, fontFamily: mono }}>{fmtPrice(port.equity || 0, pairPrefix(pair))}</div>
                        <div style={{ fontSize: 11, fontFamily: mono, color: (port.pnl_pct || 0) >= 0 ? COLORS.buy : COLORS.sell }}>
                          {(port.pnl_pct || 0) >= 0 ? "+" : ""}{(port.pnl_pct || 0).toFixed(2)}%
                        </div>
                      </div>
                    </div>

                    {/* Indicators */}
                    {ind.rsi !== undefined && (
                      <div style={{ display: "flex", gap: 16, marginTop: 8, fontSize: 11, fontFamily: mono, color: COLORS.textDim, flexWrap: "wrap" }}>
                        <span>RSI <span style={{ color: ind.rsi > 70 ? COLORS.sell : ind.rsi < 30 ? COLORS.buy : COLORS.text, fontWeight: 600 }}>{ind.rsi}</span></span>
                        <span>MACD <span style={{ color: (ind.macd_histogram || 0) > 0 ? COLORS.buy : COLORS.sell, fontWeight: 600 }}>{fmtInd(ind.macd_histogram)}</span></span>
                        <span>BB <span style={{ color: COLORS.text }}>[{fmtInd(ind.bb_lower)} — {fmtInd(ind.bb_upper)}]</span></span>
                        <span>Width <span style={{ color: (ind.bb_width || 0) > 0.06 ? COLORS.volatile : COLORS.text, fontWeight: 600 }}>{((ind.bb_width || 0) * 100).toFixed(2)}%</span></span>
                        {(() => {
                          const fees = state?.fee_tier?.pair_fees?.[pair];
                          if (!fees) return null;
                          const m = fees.maker_pct;
                          const t = fees.taker_pct;
                          // Only render when at least one side has a real numeric value —
                          // otherwise null would silently collapse to "0.00%" via `?? 0`,
                          // misleading the user into thinking fees are zero.
                          if (m == null && t == null) return null;
                          const fmt = (v) => (v == null ? "—" : v.toFixed(2));
                          return (
                            <span>Fee M/T <span style={{ color: COLORS.text, fontWeight: 600 }}>
                              {fmt(m)}/{fmt(t)}%
                            </span></span>
                          );
                        })()}
                      </div>
                    )}

                    {/* Spread from TickerStream */}
                    {ps.spread && ps.spread.spread_bps != null && (
                      <span style={{ marginLeft: 12, color: COLORS.textMuted, fontSize: 11 }}>
                        Spread <span style={{ color: COLORS.text, fontWeight: 600 }}>{(ps.spread.spread_bps || 0).toFixed(1)}</span> bps
                      </span>
                    )}

                    {/* AI Reasoning */}
                    {ps.ai_decision && !ps.ai_decision.fallback && (
                      <div style={{ marginTop: 8, padding: "8px 10px", background: `${COLORS.purple}10`, border: `1px solid ${COLORS.purple}25`, borderRadius: 6 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                          <span style={{ fontSize: 9, fontWeight: 700, fontFamily: mono, textTransform: "uppercase", letterSpacing: "0.08em", padding: "2px 6px", borderRadius: 3,
                            background: ps.ai_decision.action === "CONFIRM" ? `${COLORS.buy}20` : ps.ai_decision.action === "ADJUST" ? `${COLORS.warn}20` : `${COLORS.sell}20`,
                            color: ps.ai_decision.action === "CONFIRM" ? COLORS.buy : ps.ai_decision.action === "ADJUST" ? COLORS.warn : COLORS.sell,
                          }}>AI {ps.ai_decision.action}</span>
                          {ps.ai_decision.portfolio_health && ps.ai_decision.portfolio_health !== "HEALTHY" && (
                            <span style={{ fontSize: 8, fontFamily: mono, color: ps.ai_decision.portfolio_health === "DANGER" ? COLORS.sell : COLORS.warn }}>
                              {ps.ai_decision.portfolio_health}
                            </span>
                          )}
                          {ps.ai_decision.latency_ms > 0 && (
                            <span style={{ fontSize: 8, fontFamily: mono, color: COLORS.textMuted, marginLeft: "auto" }}>{ps.ai_decision.latency_ms}ms</span>
                          )}
                        </div>
                        <div style={{ fontSize: 10, fontFamily: mono, color: COLORS.text, lineHeight: 1.4 }}>{ps.ai_decision.analyst_reasoning}</div>
                        {ps.ai_decision.risk_reasoning && (
                          <div style={{ fontSize: 9, fontFamily: mono, color: COLORS.textDim, marginTop: 3, lineHeight: 1.3 }}>{ps.ai_decision.risk_reasoning}</div>
                        )}
                        {ps.ai_decision.escalated && ps.ai_decision.strategist_reasoning && (
                          <div style={{ marginTop: 4, padding: "4px 6px", background: `${COLORS.warn}10`, borderRadius: 3 }}>
                            <span style={{ fontSize: 8, fontWeight: 700, fontFamily: mono, color: COLORS.warn, textTransform: "uppercase", marginRight: 6 }}>GROK STRATEGIST</span>
                            <span style={{ fontSize: 9, fontFamily: mono, color: COLORS.text, lineHeight: 1.3 }}>{ps.ai_decision.strategist_reasoning}</span>
                          </div>
                        )}
                        {ps.ai_decision.risk_flags && ps.ai_decision.risk_flags.length > 0 && (
                          <div style={{ display: "flex", gap: 4, marginTop: 4, flexWrap: "wrap" }}>
                            {ps.ai_decision.risk_flags.map((flag, fi) => (
                              <span key={fi} style={{ fontSize: 8, fontFamily: mono, padding: "1px 5px", borderRadius: 3, background: `${COLORS.warn}15`, color: COLORS.warn }}>{flag}</span>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}

              {/* Balance History */}
              {history.length > 5 && (
                <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 10, padding: 14 }}>
                  <div style={{ fontSize: 10, fontWeight: 600, color: COLORS.textDim, marginBottom: 6, fontFamily: mono, textTransform: "uppercase", letterSpacing: "0.08em" }}>Balance History</div>
                  <MiniChart data={history} width={700} height={70} color={journalPnlUsd >= 0 ? COLORS.accent : COLORS.danger} filled />
                </div>
              )}

              {/* Order Journal */}
              <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 10, overflow: "hidden" }}>
                <div style={{ padding: "8px 14px", borderBottom: `1px solid ${COLORS.panelBorder}`, fontSize: 10, fontWeight: 600, color: COLORS.textDim, fontFamily: mono, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  Order Journal ({orderJournal.length})
                </div>
                <div style={{ maxHeight: 180, overflowY: "auto" }}>
                  {orderJournal.length === 0 && (
                    <div style={{ color: COLORS.textMuted, fontSize: 10, padding: 12, fontFamily: mono }}>Awaiting first order...</div>
                  )}
                  {orderJournal.slice().reverse().map((entry, i) => {
                    const lifecycle = entry.lifecycle || {};
                    const intent = entry.intent || {};
                    const decision = entry.decision || {};
                    // Renamed from `state` to `entryState` to avoid shadowing
                    // the outer `state` component state variable.
                    const entryState = lifecycle.state || "PLACED";
                    const isFilled = entryState === "FILLED";
                    const _isTerminal = entryState === "FILLED" || entryState === "PARTIALLY_FILLED";  // reserved for terminal-specific styling
                    const icon = isFilled ? "\u2713" : (entryState === "PLACED" ? "\u22ef" : "\u2717");
                    const iconColor = isFilled ? COLORS.accent : (entryState === "PLACED" ? COLORS.textDim : COLORS.danger);
                    const amount = intent.amount || 0;
                    const price = lifecycle.avg_fill_price || intent.limit_price || 0;
                    const reasonLine = lifecycle.terminal_reason
                      ? `${entryState}: ${lifecycle.terminal_reason}`
                      : (decision.reason || entryState);
                    return (
                      <div key={i} style={{ display: "flex", alignItems: "center", gap: 6, padding: "5px 12px", borderBottom: `1px solid ${COLORS.panelBorder}`, fontSize: 9, fontFamily: mono }}>
                        <span style={{ width: 14, fontWeight: 700, color: iconColor }}>{icon}</span>
                        <span style={{ width: 30, fontWeight: 700, color: entry.side === "BUY" ? COLORS.buy : COLORS.sell }}>{entry.side}</span>
                        <span style={{ width: 75 }}>{amount.toFixed(6)}</span>
                        <span style={{ width: 65, color: COLORS.textDim }}>{entry.pair}</span>
                        <span style={{ width: 85 }}>{fmtPrice(price, pairPrefix(entry.pair))}</span>
                        <span style={{ flex: 1, color: COLORS.textMuted, fontSize: 8, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{reasonLine}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            {/* RIGHT SIDEBAR — aligned with first pair panel */}
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {/* Kraken Account */}
              <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 8, padding: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <div style={{ fontSize: 10, color: COLORS.textDim, textTransform: "uppercase", letterSpacing: "0.08em", fontFamily: mono }}>Kraken Account</div>
                  {balanceUsd && (
                    <div style={{ fontSize: 11, color: COLORS.text, fontWeight: 700, fontFamily: mono }}>${balanceUsd.total_usd?.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
                  )}
                </div>
                {balanceUsd?.assets?.length > 0 ? balanceUsd.assets.map((a) => (
                  <div key={a.asset} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontFamily: mono, fontSize: 11, padding: "2px 0", opacity: a.staked ? 0.5 : 1 }}>
                    <span style={{ color: COLORS.textDim }}>
                      {a.asset}{a.staked && <span style={{ fontSize: 8, color: COLORS.warn, marginLeft: 4, textTransform: "uppercase" }}>staked</span>}
                    </span>
                    <span style={{ display: "flex", gap: 8 }}>
                      <span style={{ color: COLORS.textMuted }}>{a.amount.toFixed(6)}</span>
                      {a.usd_value > 0 && <span style={{ color: COLORS.text, fontWeight: 600 }}>${a.usd_value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>}
                    </span>
                  </div>
                )) : Object.entries(balance).length > 0 ? Object.entries(balance).map(([asset, amount]) => (
                  <div key={asset} style={{ display: "flex", justifyContent: "space-between", fontFamily: mono, fontSize: 11, padding: "2px 0" }}>
                    <span style={{ color: COLORS.textDim }}>{asset}</span>
                    <span style={{ color: COLORS.text, fontWeight: 600 }}>{typeof amount === "number" ? amount.toFixed(6) : amount}</span>
                  </div>
                )) : (
                  <div style={{ fontSize: 9, color: COLORS.textMuted, fontFamily: mono }}>Loading...</div>
                )}
                {balanceUsd && balanceUsd.staked_usd > 0 && (
                  <div style={{ marginTop: 6, paddingTop: 6, borderTop: `1px solid ${COLORS.panelBorder}`, display: "flex", justifyContent: "space-between", fontFamily: mono, fontSize: 10 }}>
                    <span style={{ color: COLORS.textMuted }}>Tradable</span>
                    <span style={{ color: COLORS.accent, fontWeight: 600 }}>${balanceUsd.tradable_usd?.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                  </div>
                )}
              </div>

              {/* Strategy Matrix */}
              <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 8, padding: 12 }}>
                <div style={{ fontSize: 10, color: COLORS.textDim, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8, fontFamily: mono }}>Strategy Matrix</div>
                {[
                  { regime: "TREND_UP", strategy: "MOMENTUM" },
                  { regime: "TREND_DOWN", strategy: "DEFENSIVE" },
                  { regime: "RANGING", strategy: "MEAN_REVERSION" },
                  { regime: "VOLATILE", strategy: "GRID" },
                ].map(({ regime, strategy }) => {
                  const activeForPairs = pairNames.filter(p => pairs[p]?.regime === regime);
                  const active = activeForPairs.length > 0;
                  return (
                    <div key={regime} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0", opacity: active ? 1 : 0.35, fontFamily: mono }}>
                      <div style={{ width: 6, height: 6, borderRadius: "50%", background: regimeColor(regime), boxShadow: active ? `0 0 8px ${regimeColor(regime)}` : "none" }} />
                      <span style={{ fontSize: 10, color: regimeColor(regime), width: 75 }}>{regime.replace("_", " ")}</span>
                      <span style={{ fontSize: 10, color: COLORS.textDim }}>{"\u2192"}</span>
                      <span style={{ fontSize: 10, color: COLORS.text }}>{strategyIcon(strategy)} {strategy.replace("_", " ")}</span>
                      {active && (
                        <span style={{ fontSize: 8, color: regimeColor(regime), marginLeft: "auto" }}>
                          {activeForPairs.join(", ")}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>

              {/* Per-Pair Stats */}
              {pairNames.map((pair) => {
                const ps = pairs[pair];
                const perf = ps.performance || {};
                const engineWR = ((perf.win_count || 0) + (perf.loss_count || 0)) > 0
                  ? ((perf.win_count || 0) / ((perf.win_count || 0) + (perf.loss_count || 0)) * 100)
                  : 0;
                const pf = fillsByPair[pair] || { buys: 0, sells: 0, sell_wins: 0, sell_losses: 0 };
                const pairSellTotal = (pf.sell_wins || 0) + (pf.sell_losses || 0);
                const pairFillWR = pairSellTotal > 0 ? ((pf.sell_wins || 0) / pairSellTotal * 100) : 0;
                const winRate = (perf.total_trades || 0) > 0 ? engineWR : pairFillWR;
                const pairFills = pf.buys + pf.sells;
                const pairPnl = (jStats.pnl_by_pair || {})[pair] || {};
                const pairNetUsd = pairPnl.net_usd || 0;
                return (
                  <div key={pair} style={{ background: `${regimeColor(ps.regime)}08`, border: `1px solid ${regimeColor(ps.regime)}25`, borderRadius: 8, padding: 12 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                      <div style={{ width: 8, height: 8, borderRadius: "50%", background: regimeColor(ps.regime), boxShadow: `0 0 10px ${regimeColor(ps.regime)}80` }} />
                      <span style={{ fontSize: 12, fontWeight: 700, color: regimeColor(ps.regime), fontFamily: mono }}>{pair}</span>
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, fontSize: 10, fontFamily: mono }}>
                      <span style={{ color: COLORS.textDim }}>Fills</span>
                      <span style={{ color: COLORS.text, textAlign: "right" }}>{pairFills}{pairFills > 0 ? ` (${pf.buys}B/${pf.sells}S)` : ""}</span>
                      <span style={{ color: COLORS.textDim }}>P&L</span>
                      <span style={{ color: pairNetUsd >= 0 ? COLORS.buy : COLORS.sell, textAlign: "right", fontWeight: 600 }}>
                        {pairNetUsd >= 0 ? "+$" : "-$"}{Math.abs(pairNetUsd).toFixed(2)}
                      </span>
                      <span style={{ color: COLORS.textDim }}>Win Rate</span>
                      <span style={{ color: winRate > 55 ? COLORS.buy : winRate > 0 ? COLORS.warn : COLORS.textMuted, textAlign: "right" }}>{winRate.toFixed(0)}%</span>
                      <span style={{ color: COLORS.textDim }}>Sharpe</span>
                      <span style={{ color: COLORS.text, textAlign: "right" }}>{(perf.sharpe_estimate || 0).toFixed(2)}</span>
                      <span style={{ color: COLORS.textDim }}>Drawdown</span>
                      <span style={{ color: (ps.portfolio?.max_drawdown_pct || 0) > 5 ? COLORS.danger : COLORS.text, textAlign: "right" }}>
                        {(ps.portfolio?.max_drawdown_pct || 0).toFixed(2)}%
                      </span>
                    </div>
                  </div>
                );
              })}

              {/* AI Brain */}
              {aiBrain && (
                <div style={{ background: `${COLORS.blue}08`, border: `1px solid ${COLORS.blue}25`, borderRadius: 8, padding: 12 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
                    <div style={{ width: 6, height: 6, borderRadius: "50%", background: aiBrain.active ? COLORS.accent : COLORS.danger, boxShadow: `0 0 6px ${aiBrain.active ? COLORS.accent : COLORS.danger}` }} />
                    <span style={{ fontSize: 10, color: COLORS.blue, textTransform: "uppercase", letterSpacing: "0.08em", fontFamily: mono, fontWeight: 700 }}>AI Brain</span>
                  </div>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, fontSize: 10, fontFamily: mono }}>
                    <span style={{ color: COLORS.textDim }}>Decisions</span>
                    <span style={{ color: COLORS.text, textAlign: "right" }}>{aiBrain.decisions_today}</span>
                    <span style={{ color: COLORS.textDim }}>Overrides</span>
                    <span style={{ color: aiBrain.overrides_today > 0 ? COLORS.warn : COLORS.text, textAlign: "right" }}>{aiBrain.overrides_today}</span>
                    <span style={{ color: COLORS.textDim }}>Escalations</span>
                    <span style={{ color: aiBrain.escalations_today > 0 ? COLORS.warn : COLORS.text, textAlign: "right" }}>{aiBrain.escalations_today || 0}</span>
                    <span style={{ color: COLORS.textDim }}>Strategist</span>
                    <span style={{ color: aiBrain.has_strategist ? COLORS.accent : COLORS.textMuted, textAlign: "right" }}>{aiBrain.has_strategist ? "Grok 4" : "None"}</span>
                    <span style={{ color: COLORS.textDim }}>Cost Today</span>
                    <span style={{ color: COLORS.text, textAlign: "right" }}>${aiBrain.cost_today?.toFixed(3)}</span>
                    <span style={{ color: COLORS.textDim }}>Latency</span>
                    <span style={{ color: COLORS.text, textAlign: "right" }}>{aiBrain.avg_latency_ms}ms</span>
                    <span style={{ color: COLORS.textDim }}>Status</span>
                    <span style={{ color: aiBrain.active ? COLORS.accent : COLORS.danger, textAlign: "right" }}>{aiBrain.active ? "Active" : "Offline"}</span>
                  </div>
                </div>
              )}

              {/* Session Info */}
              <div style={{ background: `${COLORS.purple}08`, border: `1px solid ${COLORS.purple}25`, borderRadius: 8, padding: 12 }}>
                <div style={{ fontSize: 10, color: COLORS.purple, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6, fontFamily: mono, fontWeight: 700 }}>Session</div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, fontSize: 10, fontFamily: mono }}>
                  <span style={{ color: COLORS.textDim }}>Orders</span>
                  <span style={{ color: COLORS.text, textAlign: "right" }}>Limit Post-Only</span>
                  <span style={{ color: COLORS.textDim }}>Interval</span>
                  <span style={{ color: COLORS.text, textAlign: "right" }}>{state?.interval ? `${state.interval}s` : "—"}</span>
                  <span style={{ color: COLORS.textDim }}>Pairs</span>
                  <span style={{ color: COLORS.text, textAlign: "right" }}>{pairNames.length}</span>
                  <span style={{ color: COLORS.textDim }}>Circuit Brk</span>
                  <span style={{ color: COLORS.text, textAlign: "right" }}>15% DD</span>
                  <span style={{ color: COLORS.textDim }}>Dead Man</span>
                  <span style={{ color: COLORS.accent, textAlign: "right" }}>Active</span>
                  <span style={{ color: COLORS.textDim }}>Sizing</span>
                  <span style={{ color: COLORS.text, textAlign: "right" }}>{state?.mode === "competition" ? "Half-Kelly" : "Quarter-Kelly"}</span>
                  <span style={{ color: COLORS.textDim }}>FX Session</span>
                  <span style={{ color: getForexSession().color, textAlign: "right" }}>{getForexSession().label}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ─── Companion Orb + Drawer ─── */}
      <CompanionOrb
        theme={COMPANION_THEMES[activeCompanion] || COMPANION_THEMES.apex}
        onClick={companionToggle}
        regime={state?.pairs ? (Object.values(state.pairs).map(p => p.regime).find(r => r === "VOLATILE") || "TREND") : "TREND"}
        hasUnread={getUnread(activeCompanion)}
        visible={companionVisible && !companionDrawerOpen}
        soulId={activeCompanion}
      />
      <CompanionDrawer
        open={companionDrawerOpen && companionVisible}
        onClose={companionToggle}
        active={activeCompanion}
        onSwitch={companionSwitch}
        companions={companions}
        messages={getMessages(activeCompanion) || []}
        typing={getTyping(activeCompanion)}
        onSend={companionSend}
        onProposalConfirm={companionProposalConfirm}
        onProposalReject={companionProposalReject}
        connected={connected}
        drawerWidth={companionDrawerWidth}
        onResize={setCompanionDrawerWidth}
        costAlerts={companionCostAlerts}
      />

      {/* Footer */}
      <div style={{ padding: "10px 24px", borderTop: `1px solid ${COLORS.panelBorder}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 8, color: COLORS.textMuted, fontFamily: mono }}>
          HYDRA v2.10.10 | kraken-cli v0.2.3 (WSL) | {WS_URL}
        </div>
        <div style={{ fontSize: 8, color: COLORS.textMuted, fontFamily: mono }}>
          Not financial advice. Real money at risk.
        </div>
      </div>
    </div>
  );
}
