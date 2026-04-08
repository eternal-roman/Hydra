import { useState, useEffect, useRef, useCallback } from "react";
import "./App.css";

// ═══════════════════════════════════════════════════════════════
// HYDRA Live Dashboard — Connects to hydra_agent.py WebSocket
// ═══════════════════════════════════════════════════════════════

const WS_URL = "ws://localhost:8765";

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

// Determine currency prefix for a pair — "$" for USD-quoted, "" for XBT-quoted
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
  const [tradeLog, setTradeLog] = useState([]);
  const wsRef = useRef(null);
  const reconnectRef = useRef(null);

  const connect = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;
    ws.onopen = () => { setConnected(true); };
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        setState(data);
        if (data.pairs) {
          const liveTotal = data.balance_usd?.total_usd;
          const engineEquity = Object.values(data.pairs).reduce((sum, p) => sum + (p.portfolio?.equity || 0), 0);
          setHistory((prev) => [...prev, liveTotal != null ? liveTotal : engineEquity].slice(-500));
        }
        if (data.trade_log) setTradeLog(data.trade_log);
      } catch (e) { console.error("[HYDRA] Parse error:", e); }
    };
    ws.onclose = () => { setConnected(false); reconnectRef.current = setTimeout(connect, 3000); };
    ws.onerror = () => { ws.close(); };
  }, []);

  useEffect(() => {
    connect();
    return () => { clearTimeout(reconnectRef.current); wsRef.current?.close(); };
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
  // P&L: average of per-pair pnl_pct (each pair gets equal weight since balances
  // are allocated equally). Direct equity summation would mix USD and XBT.
  const pairPnls = Object.values(pairs).map(p => p.portfolio?.pnl_pct || 0);
  const totalPnl = pairPnls.length > 0 ? pairPnls.reduce((s, v) => s + v, 0) / pairPnls.length : 0;
  const maxDD = Math.max(...Object.values(pairs).map(p => p.portfolio?.max_drawdown_pct || 0), 0);
  const totalTrades = Object.values(pairs).reduce((s, p) => s + (p.performance?.total_trades || 0), 0);
  const totalWins = Object.values(pairs).reduce((s, p) => s + (p.performance?.win_count || 0), 0);
  const totalLosses = Object.values(pairs).reduce((s, p) => s + (p.performance?.loss_count || 0), 0);
  const overallWinRate = (totalWins + totalLosses) > 0 ? (totalWins / (totalWins + totalLosses) * 100) : 0;

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

      {!connected && !state && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "80vh", flexDirection: "column", gap: 16 }}>
          <img src="/favicon.svg" alt="Hydra" style={{ width: 80, height: 80, filter: "drop-shadow(0 0 12px rgba(126, 20, 255, 0.5))", marginBottom: 8 }} />
          <div style={{ fontSize: 48, fontWeight: 800, fontFamily: heading, color: COLORS.textMuted }}>HYDRA</div>
          <div style={{ fontSize: 14, color: COLORS.textDim, fontFamily: mono }}>Waiting for agent connection on {WS_URL}...</div>
          <div style={{ fontSize: 11, color: COLORS.textMuted, fontFamily: mono }}>python hydra_agent.py --pairs SOL/USDC,SOL/XBT,XBT/USDC</div>
        </div>
      )}

      {state && (
        <div style={{ padding: "16px 24px" }}>
          {/* Full grid — stats span top, then pair panels + sidebar below */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 280px", gap: 12, alignItems: "start" }}>
            {/* Stats Row — spans both columns for edge-to-edge alignment */}
            <div style={{ gridColumn: "1 / -1", display: "flex", gap: 8 }}>
              <StatCard label="Total Balance" value={`$${totalEquity.toFixed(2)}`} color={COLORS.text} />
              <StatCard label="P&L" value={`${totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(2)}`} unit="%" color={totalPnl >= 0 ? COLORS.buy : COLORS.sell} />
              <StatCard label="Max Drawdown" value={maxDD.toFixed(2)} unit="%" color={maxDD > 5 ? COLORS.danger : COLORS.warn} />
              <StatCard label="Trades" value={totalTrades} color={COLORS.blue} />
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
                      <div style={{ display: "flex", gap: 16, marginTop: 8, fontSize: 11, fontFamily: mono, color: COLORS.textDim }}>
                        <span>RSI <span style={{ color: ind.rsi > 70 ? COLORS.sell : ind.rsi < 30 ? COLORS.buy : COLORS.text, fontWeight: 600 }}>{ind.rsi}</span></span>
                        <span>MACD <span style={{ color: (ind.macd_histogram || 0) > 0 ? COLORS.buy : COLORS.sell, fontWeight: 600 }}>{fmtInd(ind.macd_histogram)}</span></span>
                        <span>BB <span style={{ color: COLORS.text }}>[{fmtInd(ind.bb_lower)} — {fmtInd(ind.bb_upper)}]</span></span>
                        <span>Width <span style={{ color: (ind.bb_width || 0) > 0.06 ? COLORS.volatile : COLORS.text, fontWeight: 600 }}>{((ind.bb_width || 0) * 100).toFixed(2)}%</span></span>
                      </div>
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
                  <MiniChart data={history} width={700} height={70} color={totalPnl >= 0 ? COLORS.accent : COLORS.danger} filled />
                </div>
              )}

              {/* Trade Log */}
              <div style={{ background: COLORS.panel, border: `1px solid ${COLORS.panelBorder}`, borderRadius: 10, overflow: "hidden" }}>
                <div style={{ padding: "8px 14px", borderBottom: `1px solid ${COLORS.panelBorder}`, fontSize: 10, fontWeight: 600, color: COLORS.textDim, fontFamily: mono, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  Trade Log ({tradeLog.length})
                </div>
                <div style={{ maxHeight: 180, overflowY: "auto" }}>
                  {tradeLog.length === 0 && (
                    <div style={{ color: COLORS.textMuted, fontSize: 10, padding: 12, fontFamily: mono }}>Awaiting first trade signal...</div>
                  )}
                  {tradeLog.slice().reverse().map((t, i) => (
                    <div key={i} style={{ display: "flex", alignItems: "center", gap: 6, padding: "5px 12px", borderBottom: `1px solid ${COLORS.panelBorder}`, fontSize: 9, fontFamily: mono }}>
                      <span style={{ width: 14, fontWeight: 700, color: t.status === "EXECUTED" ? COLORS.accent : COLORS.danger }}>
                        {t.status === "EXECUTED" ? "\u2713" : "\u2717"}
                      </span>
                      <span style={{ width: 30, fontWeight: 700, color: t.action === "BUY" ? COLORS.buy : COLORS.sell }}>{t.action}</span>
                      <span style={{ width: 75 }}>{(t.amount || 0).toFixed(6)}</span>
                      <span style={{ width: 65, color: COLORS.textDim }}>{t.pair}</span>
                      <span style={{ width: 85 }}>{fmtPrice(t.price || 0, pairPrefix(t.pair))}</span>
                      <span style={{ flex: 1, color: COLORS.textMuted, fontSize: 8, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.reason || ""}</span>
                    </div>
                  ))}
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
                const winRate = ((perf.win_count || 0) + (perf.loss_count || 0)) > 0
                  ? ((perf.win_count || 0) / ((perf.win_count || 0) + (perf.loss_count || 0)) * 100)
                  : 0;
                return (
                  <div key={pair} style={{ background: `${regimeColor(ps.regime)}08`, border: `1px solid ${regimeColor(ps.regime)}25`, borderRadius: 8, padding: 12 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                      <div style={{ width: 8, height: 8, borderRadius: "50%", background: regimeColor(ps.regime), boxShadow: `0 0 10px ${regimeColor(ps.regime)}80` }} />
                      <span style={{ fontSize: 12, fontWeight: 700, color: regimeColor(ps.regime), fontFamily: mono }}>{pair}</span>
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, fontSize: 10, fontFamily: mono }}>
                      <span style={{ color: COLORS.textDim }}>Trades</span>
                      <span style={{ color: COLORS.text, textAlign: "right" }}>{perf.total_trades || 0}</span>
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
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Footer */}
      <div style={{ padding: "10px 24px", borderTop: `1px solid ${COLORS.panelBorder}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 8, color: COLORS.textMuted, fontFamily: mono }}>
          HYDRA v2.4.0 | kraken-cli v0.2.3 (WSL) | {WS_URL}
        </div>
        <div style={{ fontSize: 8, color: COLORS.textMuted, fontFamily: mono }}>
          Not financial advice. Real money at risk.
        </div>
      </div>
    </div>
  );
}
