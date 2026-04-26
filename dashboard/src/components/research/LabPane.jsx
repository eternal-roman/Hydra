// dashboard/src/components/research/LabPane.jsx
//
// Mode B pane of the Research tab — hypothesis lab. Walk-forward param diff.
//
// IMPORTANT: Mode B is DEFERRED at v2.20.0 MVP. The form is wired and renders;
// the backend handler `research_lab_run` returns a structured "deferred" error.
// The UI surfaces that error clearly and points users at the Releases pane (Mode C).
//
// Mirrors the props-based ws pattern established by DatasetPane: parent owns
// the WS, passes `sendMessage` for outbound + `labResult` for inbound.

import React, { useState, useEffect } from "react";

const PAIRS = ["BTC/USD", "SOL/USD", "SOL/BTC"];

export default function LabPane({ sendMessage, labResult }) {
  const [pair, setPair] = useState("BTC/USD");
  const [baselineJson, setBaselineJson] = useState("{}");
  const [candidateJson, setCandidateJson] = useState("{}");
  const [running, setRunning] = useState(false);
  const [parseError, setParseError] = useState(null);

  // Clear running state when a result arrives.
  useEffect(() => {
    if (labResult !== null && labResult !== undefined) setRunning(false);
  }, [labResult]);

  const run = () => {
    setParseError(null);
    let baseline, candidate;
    try {
      baseline = JSON.parse(baselineJson || "{}");
      candidate = JSON.parse(candidateJson || "{}");
    } catch (e) {
      setParseError(`Invalid JSON: ${e.message}`);
      return;
    }
    setRunning(true);
    sendMessage({
      type: "research_lab_run",
      pair,
      baseline_params: baseline,
      candidate_params: candidate,
      spec: { fold_kind: "quarterly", is_lookback_quarters: 8 },
    });
  };

  const showDeferredNotice =
    labResult && labResult.success === false && /deferred/i.test(labResult.error || "");

  return (
    <div style={{ padding: 16 }}>
      <h3 style={{ marginTop: 0 }}>Hypothesis Lab</h3>
      <p style={{ color: "#888", fontSize: 12, marginTop: -4 }}>
        Mode B — paired walk-forward of candidate params vs baseline on real history.
      </p>

      {showDeferredNotice && (
        <div
          style={{
            background: "#3a2a00",
            border: "1px solid #6a4a00",
            color: "#ffd58a",
            padding: 12,
            marginBottom: 12,
            borderRadius: 4,
            fontSize: 13,
          }}
        >
          <strong>Mode B is deferred to a follow-up release.</strong> Param injection
          into the per-fold backtest engine is not yet wired. The form is here so the
          surface is in place; for working regression diffs today, see the{" "}
          <strong>Releases</strong> pane (Mode C).
        </div>
      )}

      {!showDeferredNotice && labResult && labResult.success === false && (
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

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "200px 1fr 1fr",
          gap: 12,
          alignItems: "start",
        }}
      >
        <label style={{ display: "block" }}>
          <div style={{ fontSize: 12, color: "#aaa", marginBottom: 4 }}>Pair</div>
          <select
            value={pair}
            onChange={(e) => setPair(e.target.value)}
            style={{
              width: "100%",
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
        </label>
        <label style={{ display: "block" }}>
          <div style={{ fontSize: 12, color: "#aaa", marginBottom: 4 }}>
            Baseline params (JSON)
          </div>
          <textarea
            value={baselineJson}
            onChange={(e) => setBaselineJson(e.target.value)}
            style={{
              width: "100%",
              height: 80,
              fontFamily: "monospace",
              fontSize: 12,
              padding: 6,
              background: "#1a1a1a",
              color: "#fff",
              border: "1px solid #333",
            }}
          />
        </label>
        <label style={{ display: "block" }}>
          <div style={{ fontSize: 12, color: "#aaa", marginBottom: 4 }}>
            Candidate params (JSON)
          </div>
          <textarea
            value={candidateJson}
            onChange={(e) => setCandidateJson(e.target.value)}
            style={{
              width: "100%",
              height: 80,
              fontFamily: "monospace",
              fontSize: 12,
              padding: 6,
              background: "#1a1a1a",
              color: "#fff",
              border: "1px solid #333",
            }}
          />
        </label>
      </div>

      {parseError && (
        <div style={{ color: "#ffb4b4", fontSize: 12, marginTop: 8 }}>{parseError}</div>
      )}

      <button
        onClick={run}
        disabled={running}
        style={{
          marginTop: 12,
          padding: "8px 16px",
          background: running ? "#444" : "#3aa757",
          color: "#fff",
          border: "none",
          borderRadius: 4,
          cursor: running ? "default" : "pointer",
          fontSize: 13,
        }}
      >
        {running ? "Running…" : "Run walk-forward"}
      </button>

      {labResult && labResult.success === true && labResult.wilcoxon && (
        <div
          style={{
            marginTop: 16,
            padding: 12,
            background: "#1a1a1a",
            borderRadius: 4,
          }}
        >
          <h4 style={{ marginTop: 0 }}>Verdict (paired Wilcoxon, α=0.05)</h4>
          {Object.entries(labResult.wilcoxon).map(([metric, v]) => {
            const color =
              v.verdict === "better"
                ? "#3aa757"
                : v.verdict === "worse"
                ? "#d04545"
                : "#888";
            return (
              <div key={metric} style={{ fontFamily: "monospace", fontSize: 12 }}>
                <span style={{ color, fontWeight: 600 }}>
                  {(v.verdict || "?").toUpperCase()}
                </span>
                {" — "}
                {metric}: candidate wins {v.candidate_wins}/{v.n}, p=
                {Number(v.p_value).toFixed(4)}, median Δ=
                {Number(v.median_delta).toFixed(3)}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
