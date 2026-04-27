// dashboard/src/components/research/LabPane.jsx
//
// Mode B pane of the Research tab — hypothesis lab. Walk-forward param diff.
//
// T30A: JSON textareas replaced with schema-driven slider rows. Each of the 8
// tunable engine params (from PARAM_BOUNDS in hydra_tuner) renders as two sliders
// side-by-side (Baseline | Candidate) with the live current value shown alongside
// the range and a numeric readout. The backend handler `research_params_current`
// is fetched on mount (and on pair change) to populate the schema.
//
// T30B: Mode B is now functional. research_lab_run dispatches a daemon thread;
// the synchronous ack returns {success, job_id, n_folds, pair}. The daemon
// streams research_lab_progress per-fold and research_lab_result on completion.
// labProgress (array|null) is passed down from App.jsx via ResearchTab.
//
// Mirrors the props-based ws pattern established by DatasetPane: parent owns
// the WS, passes `sendMessage` for outbound + `labResult` / `paramsSchema` /
// `labProgress` inbound.

import React, { useState, useEffect } from "react";

const PAIRS = ["BTC/USD", "SOL/USD", "SOL/BTC"];

const LABELS = {
  volatile_atr_mult: "Volatile ATR multiplier",
  volatile_bb_mult: "Volatile Bollinger multiplier",
  trend_ema_ratio: "Trend EMA ratio",
  momentum_rsi_lower: "Momentum RSI lower",
  momentum_rsi_upper: "Momentum RSI upper",
  mean_reversion_rsi_buy: "Mean-rev RSI buy",
  mean_reversion_rsi_sell: "Mean-rev RSI sell",
  min_confidence_threshold: "Min confidence threshold",
};

export default function LabPane({ sendMessage, labResult, paramsSchema, labProgress, clearLabRunState }) {
  const [pair, setPair] = useState("BTC/USD");
  const [baselineValues, setBaselineValues] = useState({});
  const [candidateValues, setCandidateValues] = useState({});
  const [running, setRunning] = useState(false);

  // Whenever the pair changes (or on mount), re-fetch the schema for it
  // AND clear any stale lab-run state from a previous pair.
  useEffect(() => {
    sendMessage({ type: "research_params_current", pair });
    setBaselineValues({});
    setCandidateValues({});
    if (typeof clearLabRunState === "function") {
      clearLabRunState();
    }
    setRunning(false);
  }, [pair, sendMessage, clearLabRunState]);

  // When the schema arrives for the active pair, populate both sides with
  // the current live values. User can then drag sliders to diff candidate.
  const schema =
    paramsSchema && paramsSchema.success && paramsSchema.pair === pair
      ? paramsSchema.data
      : null;

  useEffect(() => {
    if (!schema) return;
    const init = {};
    for (const [k, def] of Object.entries(schema)) {
      init[k] = def.current ?? def.default ?? def.min;
    }
    setBaselineValues((b) => (Object.keys(b).length === 0 ? init : b));
    setCandidateValues((c) => (Object.keys(c).length === 0 ? init : c));
  }, [schema]);

  // Derive streaming state from labProgress array passed from App.jsx.
  const progressMsgs = labProgress || [];
  const startMsg = progressMsgs.find((m) => m.phase === "started");
  const doneMsg = progressMsgs.find((m) => m.phase === "done");
  const errorMsg = progressMsgs.find((m) => m.phase === "error");
  const foldMetricsMsgs = progressMsgs.filter((m) => "fold_idx" in m);
  const foldsCompleted = new Set(foldMetricsMsgs.map((m) => `${m.fold_idx}|${m.side}`)).size;
  const totalSteps = (startMsg?.n_folds || labResult?.n_folds || 1) * 2; // both sides
  // job_id from the synchronous ack.
  const ackJobId =
    labResult?.success && labResult.job_id ? labResult.job_id : null;

  // Clear running state when the daemon thread signals done or error.
  useEffect(() => {
    if (doneMsg || errorMsg) setRunning(false);
  }, [doneMsg, errorMsg]);

  const run = () => {
    setRunning(true);
    sendMessage({
      type: "research_lab_run",
      pair,
      baseline_params: baselineValues,
      candidate_params: candidateValues,
      spec: { fold_kind: "quarterly", is_lookback_quarters: 8 },
    });
  };

  return (
    <div style={{ padding: 16 }}>
      <h3 style={{ marginTop: 0 }}>Hypothesis Lab</h3>
      <p style={{ color: "#888", fontSize: 12, marginTop: -4 }}>
        Mode B — paired walk-forward of candidate params vs baseline on real history.
        Sliders show live current values; drag to set candidate.
      </p>

      {labResult && labResult.success === false && (
        <div
          style={{
            background: "#3a0000",
            border: "1px solid #6a0000",
            color: "#ffb4b4",
            padding: 12,
            marginBottom: 12,
            borderRadius: 4,
            fontSize: 13,
          }}
        >
          <strong>Error:</strong> {labResult.error}
        </div>
      )}

      <div style={{ marginBottom: 12 }}>
        <label style={{ fontSize: 12, color: "#aaa", marginRight: 8 }}>Pair</label>
        <select
          value={pair}
          onChange={(e) => setPair(e.target.value)}
          style={{
            padding: 6,
            background: "#1a1a1a",
            color: "#fff",
            border: "1px solid #333",
          }}
        >
          {PAIRS.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
      </div>

      {!schema ? (
        <div style={{ color: "#888", fontSize: 12 }}>Loading params for {pair}…</div>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr
              style={{
                textAlign: "left",
                borderBottom: "1px solid #333",
                color: "#aaa",
              }}
            >
              <th style={{ padding: "6px 8px" }}>Parameter</th>
              <th style={{ padding: "6px 8px" }}>Range</th>
              <th style={{ padding: "6px 8px" }}>Live</th>
              <th style={{ padding: "6px 8px" }} colSpan={2}>
                Baseline
              </th>
              <th style={{ padding: "6px 8px" }} colSpan={2}>
                Candidate
              </th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(schema).map(([name, def]) => (
              <tr key={name} style={{ borderBottom: "1px solid #222" }}>
                <td style={{ padding: "6px 8px" }}>{LABELS[name] || name}</td>
                <td
                  style={{
                    padding: "6px 8px",
                    color: "#888",
                    fontFamily: "monospace",
                  }}
                >
                  [{def.min}, {def.max}]
                </td>
                <td
                  style={{
                    padding: "6px 8px",
                    fontFamily: "monospace",
                    color: "#aaa",
                  }}
                >
                  {def.current != null ? def.current.toFixed(3) : "—"}
                </td>
                <td style={{ padding: "6px 8px", width: "20%" }}>
                  <input
                    type="range"
                    min={def.min}
                    max={def.max}
                    step={def.step}
                    value={baselineValues[name] ?? def.current}
                    onChange={(e) =>
                      setBaselineValues((v) => ({
                        ...v,
                        [name]: parseFloat(e.target.value),
                      }))
                    }
                    style={{ width: "100%" }}
                  />
                </td>
                <td
                  style={{
                    padding: "6px 8px",
                    fontFamily: "monospace",
                    width: 60,
                  }}
                >
                  {(baselineValues[name] ?? def.current) != null
                    ? (baselineValues[name] ?? def.current).toFixed(3)
                    : "—"}
                </td>
                <td style={{ padding: "6px 8px", width: "20%" }}>
                  <input
                    type="range"
                    min={def.min}
                    max={def.max}
                    step={def.step}
                    value={candidateValues[name] ?? def.current}
                    onChange={(e) =>
                      setCandidateValues((v) => ({
                        ...v,
                        [name]: parseFloat(e.target.value),
                      }))
                    }
                    style={{ width: "100%" }}
                  />
                </td>
                <td
                  style={{
                    padding: "6px 8px",
                    fontFamily: "monospace",
                    width: 60,
                  }}
                >
                  {(candidateValues[name] ?? def.current) != null
                    ? (candidateValues[name] ?? def.current).toFixed(3)
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <button
        onClick={run}
        disabled={running || !schema}
        style={{
          marginTop: 12,
          padding: "8px 16px",
          background: running || !schema ? "#444" : "#3aa757",
          color: "#fff",
          border: "none",
          borderRadius: 4,
          cursor: running || !schema ? "default" : "pointer",
          fontSize: 13,
        }}
      >
        {running ? "Running…" : "Run walk-forward"}
      </button>

      {running && !doneMsg && !errorMsg && (
        <div style={{ marginTop: 12, padding: 12, background: "#1a1a1a",
                      borderRadius: 4, color: "#aaa", fontSize: 13 }}>
          Running walk-forward… {foldsCompleted}/{totalSteps} fold-runs completed
          {ackJobId && (
            <span style={{ color: "#888", fontFamily: "monospace", marginLeft: 8 }}>
              (job {ackJobId})
            </span>
          )}
        </div>
      )}

      {errorMsg && (
        <div style={{ marginTop: 12, padding: 12, background: "#3a0000",
                      border: "1px solid #6a0000", color: "#ffb4b4",
                      borderRadius: 4, fontSize: 13 }}>
          <strong>Error:</strong> {errorMsg.error}
        </div>
      )}

      {doneMsg && (
        <div style={{ marginTop: 16, padding: 12, background: "#1a1a1a",
                      borderRadius: 4 }}>
          <h4 style={{ marginTop: 0 }}>Verdict (paired Wilcoxon, α=0.05)</h4>
          {Object.entries(doneMsg.wilcoxon || {}).map(([metric, v]) => {
            const color = v.verdict === "better" ? "#3aa757" :
                          v.verdict === "worse"  ? "#d04545" : "#888";
            return (
              <div key={metric} style={{ fontFamily: "monospace", fontSize: 12 }}>
                <span style={{ color, fontWeight: 600 }}>{(v.verdict || "?").toUpperCase()}</span>
                {" — "}{metric}: {v.candidate_wins}/{v.n} wins, p={Number(v.p_value).toFixed(4)},
                median Δ={Number(v.median_delta).toFixed(3)}
              </div>
            );
          })}
          <div style={{ marginTop: 8, fontSize: 11, color: "#888" }}>
            {doneMsg.n_folds_completed} folds completed
            ({doneMsg.skipped_folds} skipped due to insufficient trades)
          </div>
        </div>
      )}
    </div>
  );
}
