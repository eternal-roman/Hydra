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

function MiniChart({ data, width = 280, height = 60, color = COLORS.accent, filled = false }) {
  if (!data || data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pts = data.map((v, i) => `${(i / (data.length - 1)) * width},${height - ((v - min) / range) * (height - 4) - 2}`);
  const pathD = `M${pts.join(" L")}`;
  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ display: "block" }}>
      {filled && <path d={`${pathD} L${width},${height} L0,${height} Z`} fill={color} opacity={0.1} />}
      <path d={pathD} fill="none" stroke={color} strokeWidth={1.5} />
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

function TabSwitcher({ activeTab, onChange, backtestRunning }) {
  const tabs = [
    { key: "LIVE",     label: "LIVE",     color: COLORS.accent },
    { key: "BACKTEST", label: "BACKTEST", color: COLORS.blue },
    { key: "COMPARE",  label: "COMPARE",  color: COLORS.purple },
  ];
  return (
    <div style={{ display: "flex", gap: 4, padding: "8px 0" }}>
      {tabs.map(t => {
        const active = activeTab === t.key;
        return (
          <button
            key={t.key}
            onClick={() => onChange(t.key)}
            style={{
              padding: "6px 14px",
              fontSize: 11,
              fontWeight: 700,
              fontFamily: mono,
              letterSpacing: "0.12em",
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

function FieldLabel({ children, hint }) {
  return (
    <div style={{ marginBottom: 4 }}>
      <div style={{ fontSize: 9, color: COLORS.textDim, textTransform: "uppercase",
                    letterSpacing: "0.1em", fontFamily: mono, fontWeight: 600 }}>
        {children}
      </div>
      {hint && <div style={{ fontSize: 10, color: COLORS.textMuted, fontFamily: mono, marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

function StyledInput({ value, onChange, placeholder, type = "text", ...rest }) {
  return (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      style={{
        width: "100%",
        padding: "7px 10px",
        background: COLORS.bg,
        color: COLORS.text,
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 4,
        fontSize: 12,
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

function StyledSelect({ value, onChange, options }) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      style={{
        width: "100%",
        padding: "7px 10px",
        background: COLORS.bg,
        color: COLORS.text,
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 4,
        fontSize: 12,
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

function StyledTextarea({ value, onChange, placeholder, minHeight = 70 }) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      style={{
        width: "100%",
        minHeight,
        padding: "8px 10px",
        background: COLORS.bg,
        color: COLORS.text,
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 4,
        fontSize: 12,
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
                                observerTotalTicks = 0, onObserverClose = null }) {
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
    <div style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: 16, alignItems: "start" }}>
      {/* LEFT: control form */}
      <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                    borderRadius: 8, padding: 16 }}>
        <div style={{ fontSize: 13, fontFamily: heading, fontWeight: 700, color: COLORS.text,
                      marginBottom: 12, display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: COLORS.blue }}>▶</span> Run Backtest
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div>
            <FieldLabel>Preset</FieldLabel>
            <StyledSelect value={preset} onChange={setPreset} options={PRESET_OPTIONS} />
          </div>

          <div>
            <FieldLabel hint="Min 8 chars. Logged + reviewed by the AI observer.">Hypothesis *</FieldLabel>
            <StyledTextarea
              value={hypothesis}
              onChange={setHypothesis}
              placeholder="e.g., tighter RSI upper should reduce false BUYs in VOLATILE regime"
            />
            {!hypothesisValid && hypothesis.length > 0 && (
              <div style={{ fontSize: 10, color: COLORS.danger, fontFamily: mono, marginTop: 4 }}>
                {8 - hypothesis.trim().length} more character(s) required
              </div>
            )}
          </div>

          <div>
            <FieldLabel>Pairs (comma-separated)</FieldLabel>
            <StyledInput value={pairs} onChange={setPairs} placeholder="SOL/USDC,BTC/USDC" />
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <div>
              <FieldLabel>Candles</FieldLabel>
              <StyledInput value={nCandles} onChange={setNCandles} type="number" />
              {!nCandlesValid && (
                <div style={{ fontSize: 10, color: COLORS.danger, fontFamily: mono, marginTop: 4 }}>
                  50-20000
                </div>
              )}
            </div>
            <div>
              <FieldLabel>Seed</FieldLabel>
              <StyledInput value={seed} onChange={setSeed} type="number" />
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
              padding: "10px 16px",
              fontSize: 12,
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
            <div style={{ fontSize: 10, color: COLORS.danger, fontFamily: mono }}>
              Disconnected — start hydra_agent.py to enable.
            </div>
          )}
        </div>
      </div>

      {/* RIGHT: status + ack feedback */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                      borderRadius: 8, padding: 16 }}>
          <div style={{ fontSize: 13, fontFamily: heading, fontWeight: 700, color: COLORS.text,
                        marginBottom: 10 }}>
            Backtest Status
          </div>
          {ackMsg ? (
            <div>
              <div style={{ fontFamily: mono, fontSize: 11,
                            color: ackMsg.success ? COLORS.accent : COLORS.danger }}>
                {ackMsg.success ? "✓ Submitted" : "✗ Rejected"}
              </div>
              {ackMsg.experiment_id && (
                <div style={{ fontFamily: mono, fontSize: 10, color: COLORS.textDim, marginTop: 4 }}>
                  experiment_id: <span style={{ color: COLORS.text }}>{ackMsg.experiment_id.slice(0, 16)}…</span>
                </div>
              )}
              {ackMsg.error && (
                <div style={{ fontFamily: mono, fontSize: 10, color: COLORS.danger, marginTop: 4 }}>
                  {ackMsg.error}
                </div>
              )}
            </div>
          ) : (
            <div style={{ fontFamily: mono, fontSize: 11, color: COLORS.textDim }}>
              No backtest submitted this session.
            </div>
          )}
          {(lastResultId || completedCount > 0) && (
            <div style={{ fontFamily: mono, fontSize: 10, color: COLORS.textDim, marginTop: 10,
                          paddingTop: 10, borderTop: `1px solid ${COLORS.panelBorder}` }}>
              {lastResultId && (
                <div>Last completed: <span style={{ color: COLORS.accent }}>{lastResultId.slice(0, 16)}…</span></div>
              )}
              <div style={{ marginTop: 4 }}>
                Session: <span style={{ color: COLORS.text }}>{completedCount}</span> completed
                {reviewedCount > 0 && (
                  <>, <span style={{ color: COLORS.purple }}>{reviewedCount}</span> reviewed</>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Phase 9: Dual-state Observer — backtest pair cards stream here
            live during a run, using the same visual language as LIVE. */}
        {(observerProgress || observerResult) ? (
          <ObserverModal
            progress={observerProgress}
            result={observerResult}
            review={observerReview}
            equityHistory={observerEquity}
            totalTicks={observerTotalTicks}
            variant="dock"
            onClose={onObserverClose}
          />
        ) : (
          <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                        borderRadius: 8, padding: 16, minHeight: 180 }}>
            <div style={{ fontSize: 13, fontFamily: heading, fontWeight: 700, color: COLORS.text,
                          marginBottom: 10 }}>
              Observer
            </div>
            <div style={{ fontFamily: mono, fontSize: 11, color: COLORS.textDim }}>
              Submit a backtest to stream per-tick pair state here in real time —
              the same pair cards, regime badges, and equity curves as the LIVE view.
            </div>
          </div>
        )}

        <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                      borderRadius: 8, padding: 16 }}>
          <div style={{ fontSize: 13, fontFamily: heading, fontWeight: 700, color: COLORS.text,
                        marginBottom: 10 }}>
            Rigor Gates
          </div>
          <div style={{ fontFamily: mono, fontSize: 10, color: COLORS.textDim, lineHeight: 1.6 }}>
            Every completed backtest is reviewed against 7 code-enforced gates:<br />
            <span style={{ color: COLORS.text }}>min_trades_50</span> • {" "}
            <span style={{ color: COLORS.text }}>mc_ci_lower_positive</span> • {" "}
            <span style={{ color: COLORS.text }}>wf_majority_improved</span><br />
            <span style={{ color: COLORS.text }}>oos_gap_acceptable</span> • {" "}
            <span style={{ color: COLORS.text }}>improvement_above_2se</span> • {" "}
            <span style={{ color: COLORS.text }}>cross_pair_majority</span> • {" "}
            <span style={{ color: COLORS.text }}>regime_not_concentrated</span><br /><br />
            A proposed param change is auto-apply-eligible ONLY when every gate passes. The AI reviewer
            cannot override gates via reasoning — anti-handwaving is architectural, not prompt-level.
          </div>
        </div>
      </div>
    </div>
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
function ObserverPairCard({ pair, state, equityHistory }) {
  if (!state) return null;
  const sig = state.signal || {};
  const port = state.portfolio || {};
  const pos = state.position || {};
  const regimeC = regimeColor(state.regime);
  const sigC = signalColor(sig.action);
  const px = pairPrefix(pair);

  return (
    <div style={{ background: COLORS.bg, border: `1px solid ${COLORS.panelBorder}`,
                  borderRadius: 6, padding: 10 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                    marginBottom: 8 }}>
        <div style={{ fontSize: 11, fontWeight: 700, fontFamily: mono, color: COLORS.text }}>
          {pair}
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <span style={{ fontSize: 9, fontFamily: mono, color: regimeC,
                         background: `${regimeC}18`, padding: "2px 6px", borderRadius: 3,
                         letterSpacing: "0.08em" }}>
            {state.regime || "—"}
          </span>
          <span style={{ fontSize: 9, fontFamily: mono, color: sigC, fontWeight: 700 }}>
            {sig.action || "HOLD"}
          </span>
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, fontSize: 10,
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
        <div style={{ marginTop: 8 }}>
          <MiniChart
            data={equityHistory}
            width={240}
            height={36}
            color={(port.pnl_pct || 0) >= 0 ? COLORS.accent : COLORS.danger}
            filled
          />
        </div>
      )}
    </div>
  );
}

function GatesSummary({ review }) {
  if (!review || !review.gates_passed) return null;
  const gates = review.gates_passed;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4,
                  fontFamily: mono, fontSize: 10 }}>
      {Object.entries(gates).map(([name, passed]) => (
        <div key={name} style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 10, height: 10, borderRadius: "50%",
                         background: passed ? COLORS.accent : COLORS.danger,
                         boxShadow: passed ? `0 0 4px ${COLORS.accent}80` : `0 0 4px ${COLORS.danger}80`,
                         display: "inline-block" }} />
          <span style={{ color: passed ? COLORS.text : COLORS.textDim,
                         textDecoration: passed ? "none" : "line-through" }}>
            {name}
          </span>
        </div>
      ))}
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
    : { };

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

      {/* Terminal summary (shown once result lands) */}
      {summary && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6,
                      fontFamily: mono, fontSize: 10, marginBottom: 10,
                      padding: "8px 10px", background: COLORS.bg,
                      border: `1px solid ${COLORS.panelBorder}`, borderRadius: 4 }}>
          <span style={{ color: COLORS.textDim }}>Trades</span>
          <span style={{ color: COLORS.text, textAlign: "right" }}>{summary.total_trades}</span>
          <span style={{ color: COLORS.textDim }}>Return</span>
          <span style={{ textAlign: "right",
                         color: (summary.total_return_pct || 0) >= 0 ? COLORS.accent : COLORS.danger }}>
            {(summary.total_return_pct || 0) >= 0 ? "+" : ""}{(summary.total_return_pct || 0).toFixed(2)}%
          </span>
          <span style={{ color: COLORS.textDim }}>Sharpe</span>
          <span style={{ color: COLORS.text, textAlign: "right" }}>{fmtInd(summary.sharpe)}</span>
          <span style={{ color: COLORS.textDim }}>Max DD</span>
          <span style={{ color: COLORS.warn, textAlign: "right" }}>{(summary.max_drawdown_pct || 0).toFixed(2)}%</span>
          {summary.profit_factor != null && (
            <>
              <span style={{ color: COLORS.textDim }}>Profit Factor</span>
              <span style={{ color: COLORS.text, textAlign: "right" }}>{summary.profit_factor.toFixed(2)}</span>
            </>
          )}
          {summary.win_rate_pct != null && (
            <>
              <span style={{ color: COLORS.textDim }}>Win Rate</span>
              <span style={{ color: COLORS.text, textAlign: "right" }}>{summary.win_rate_pct.toFixed(0)}%</span>
            </>
          )}
        </div>
      )}

      {/* Per-pair cards — same visual DNA as LIVE */}
      {pairNames.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {pairNames.map(pair => (
            <ObserverPairCard
              key={pair}
              pair={pair}
              state={pairs[pair]}
              equityHistory={equityHistory?.[pair] || []}
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

function CompareView() {
  return (
    <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`,
                  borderRadius: 8, padding: 24, display: "flex", flexDirection: "column",
                  alignItems: "center", justifyContent: "center", minHeight: 320 }}>
      <div style={{ fontSize: 32, color: COLORS.textMuted, marginBottom: 12 }}>⚖</div>
      <div style={{ fontSize: 14, fontFamily: heading, fontWeight: 700, color: COLORS.text, marginBottom: 8 }}>
        Compare View
      </div>
      <div style={{ fontSize: 11, fontFamily: mono, color: COLORS.textDim, textAlign: "center", maxWidth: 420 }}>
        Multi-experiment ranked comparison with per-metric winners and paired bootstrap p-values.
        Wiring lands in Phase 10 alongside the experiment library.
      </div>
    </div>
  );
}

function ConnectionStatus({ connected, tick }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{
        width: 8, height: 8, borderRadius: "50%",
        background: connected ? COLORS.accent : COLORS.danger,
        boxShadow: `0 0 8px ${connected ? COLORS.accent : COLORS.danger}80`,
        animation: connected ? "none" : "pulse 1.5s infinite",
      }} />
      <span style={{ fontSize: 11, fontFamily: mono, color: connected ? COLORS.accent : COLORS.danger }}>
        {connected ? `LIVE | Tick #${tick}` : "DISCONNECTED"}
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
              // Accumulate per-pair equity for the observer chart.
              if (msg.dashboard_state?.pairs) {
                setBtEquityHistory((prev) => {
                  const prior = prev[msg.experiment_id] || {};
                  const next = { ...prior };
                  for (const [p, ps] of Object.entries(msg.dashboard_state.pairs)) {
                    next[p] = [...(prior[p] || []), ps.portfolio?.equity || 0].slice(-500);
                  }
                  return { ...prev, [msg.experiment_id]: next };
                });
              }
              // Freshest run becomes the observer focus; re-open if the user closed it.
              setBtActiveExpId(msg.experiment_id);
              setObserverClosed(false);
              return;
            case "backtest_result":
              setBtResults((prev) => ({ ...prev, [msg.experiment_id]: msg }));
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
            case "error":
              // Backtest channel errors land here; keep quiet otherwise.
              if (msg.channel === "backtest") setBtLastAck(msg);
              return;
            default:
              // Unknown wrapped message → fall through to legacy raw-state path
              // below if and only if it doesn't look like wrapped signaling.
              if (msg.type.endsWith("_ack")) return;   // ignore other acks
              break;
          }
        }
        // Legacy: raw live-state dict (compat_mode=true on the broadcaster
        // side). Treat as the live tick state.
        applyLiveState(msg);
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
          <div style={{ padding: "4px 12px", borderRadius: 4, fontSize: 11, fontWeight: 700, fontFamily: mono, background: aiBrain ? `${COLORS.blue}20` : `${COLORS.danger}20`, color: aiBrain ? COLORS.blue : COLORS.danger, border: `1px solid ${aiBrain ? COLORS.blue : COLORS.danger}40`, textTransform: "uppercase", letterSpacing: "0.1em" }}>
            {aiBrain ? "AI LIVE" : "LIVE TRADING"}
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
                />
              </div>
            )}
            {activeTab === "COMPARE" && (
              <div style={{ padding: "16px 24px" }}>
                <CompareView />
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

      {/* Footer */}
      <div style={{ padding: "10px 24px", borderTop: `1px solid ${COLORS.panelBorder}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 8, color: COLORS.textMuted, fontFamily: mono }}>
          HYDRA v2.9.2 | kraken-cli v0.2.3 (WSL) | {WS_URL}
        </div>
        <div style={{ fontSize: 8, color: COLORS.textMuted, fontFamily: mono }}>
          Not financial advice. Real money at risk.
        </div>
      </div>
    </div>
  );
}
