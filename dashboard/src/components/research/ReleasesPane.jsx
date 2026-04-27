// dashboard/src/components/research/ReleasesPane.jsx
//
// Mode C pane of the Research tab — per-version regression snapshot inspector.
// Talks to research_releases_list and research_releases_diff WS handlers
// (see hydra_backtest_server.py:mount_backtest_routes).
//
// Mirrors the props-based ws pattern established by DatasetPane and LabPane:
// parent owns the WS, passes `sendMessage` + the relevant response slice.

import React, { useEffect, useState } from "react";

const fmtDate = (ts) =>
  ts ? new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 16) : "—";

const verdictColor = (s) => {
  if (s === "significant") return "#d04545";   // red — needs human eyes
  if (s === "equivocal") return "#888";        // grey — no signal
  if (s === "no folds") return "#aaa";         // grey — vacuous
  return "#aaa";
};

export default function ReleasesPane({ sendMessage, releasesList, releasesDiff }) {
  const [selected, setSelected] = useState([]);   // up to 2 run_ids

  useEffect(() => {
    sendMessage({ type: "research_releases_list" });
    // Intentionally only run on mount; if the user wants a refresh they'll
    // re-open the tab. Polling here would race with /release writes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = (id) => {
    setSelected((prev) =>
      prev.includes(id)
        ? prev.filter((x) => x !== id)
        : prev.length < 2
        ? [...prev, id]
        : [prev[1], id]   // keep last 2; FIFO
    );
  };

  const compare = () => {
    if (selected.length !== 2) return;
    sendMessage({
      type: "research_releases_diff",
      a_run_id: selected[0],
      b_run_id: selected[1],
    });
  };

  if (releasesList === null || releasesList === undefined) {
    return <div style={{ padding: 16, color: "#888" }}>Loading…</div>;
  }
  if (releasesList.success === false) {
    return (
      <div style={{ padding: 16, color: "#ffb4b4" }}>
        <strong>Error:</strong> {releasesList.error}
      </div>
    );
  }
  const rows = releasesList.data || [];

  return (
    <div style={{ padding: 16 }}>
      <h3 style={{ marginTop: 0 }}>Release Regression Snapshots</h3>
      <p style={{ color: "#888", fontSize: 12, marginTop: -4 }}>
        Mode C — anchored quarterly walk-forward verdicts persisted at each /release.
        Select 2 runs (any pair, any version) to diff their per-fold metrics.
      </p>

      {rows.length === 0 ? (
        <div style={{ color: "#888", padding: "12px 0" }}>
          No regression runs yet. Run{" "}
          <code style={{ color: "#fff" }}>
            python -m tools.run_regression --version &lt;X.Y.Z&gt;
          </code>{" "}
          to populate.
        </div>
      ) : (
        <>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", borderBottom: "1px solid #333" }}>
                <th style={{ padding: "6px 8px", width: 30 }}>✓</th>
                <th style={{ padding: "6px 8px" }}>Version</th>
                <th style={{ padding: "6px 8px" }}>Pair</th>
                <th style={{ padding: "6px 8px" }}>Created</th>
                <th style={{ padding: "6px 8px" }}>Verdict</th>
                <th style={{ padding: "6px 8px" }}>Override?</th>
                <th style={{ padding: "6px 8px", fontFamily: "monospace" }}>
                  Run ID
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.run_id}
                  style={{
                    borderBottom: "1px solid #222",
                    background: selected.includes(r.run_id) ? "#1a2a3a" : "transparent",
                  }}
                >
                  <td style={{ padding: "6px 8px" }}>
                    <input
                      type="checkbox"
                      checked={selected.includes(r.run_id)}
                      onChange={() => toggle(r.run_id)}
                    />
                  </td>
                  <td style={{ padding: "6px 8px" }}>{r.hydra_version}</td>
                  <td style={{ padding: "6px 8px" }}>{r.pair}</td>
                  <td style={{ padding: "6px 8px", fontSize: 12, color: "#aaa" }}>
                    {fmtDate(r.created_at)}
                  </td>
                  <td
                    style={{
                      padding: "6px 8px",
                      color: verdictColor(r.verdict_summary),
                      fontWeight: r.verdict_summary === "significant" ? 600 : 400,
                    }}
                  >
                    {r.verdict_summary}
                  </td>
                  <td style={{ padding: "6px 8px", fontSize: 12, color: "#ffd58a" }}>
                    {r.override_reason ? `⚠ ${r.override_reason}` : ""}
                  </td>
                  <td
                    style={{
                      padding: "6px 8px",
                      fontFamily: "monospace",
                      fontSize: 11,
                      color: "#888",
                    }}
                  >
                    {String(r.run_id).slice(0, 8)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <button
            onClick={compare}
            disabled={selected.length !== 2}
            style={{
              marginTop: 12,
              padding: "8px 16px",
              background: selected.length === 2 ? "#3aa757" : "#444",
              color: "#fff",
              border: "none",
              borderRadius: 4,
              cursor: selected.length === 2 ? "pointer" : "default",
              fontSize: 13,
            }}
          >
            Diff selected ({selected.length}/2)
          </button>
        </>
      )}

      {releasesDiff && releasesDiff.success === false && (
        <div
          style={{
            background: "#3a0000",
            border: "1px solid #6a0000",
            color: "#ffb4b4",
            padding: 12,
            marginTop: 16,
            borderRadius: 4,
          }}
        >
          <strong>Diff error:</strong> {releasesDiff.error}
        </div>
      )}

      {releasesDiff && releasesDiff.success === true && (
        <DiffView a={releasesDiff.a} b={releasesDiff.b} />
      )}
    </div>
  );
}

function DiffView({ a, b }) {
  // Build a unified row set keyed by (fold_idx, metric).
  // For MVP, everything is fold_idx=-1 (aggregate); we still render generally.
  const keyOf = (m) => `${m.fold_idx}|${m.metric}`;
  const aMap = new Map((a.metrics || []).map((m) => [keyOf(m), m.value]));
  const bMap = new Map((b.metrics || []).map((m) => [keyOf(m), m.value]));
  const allKeys = Array.from(new Set([...aMap.keys(), ...bMap.keys()])).sort();

  return (
    <div style={{ marginTop: 16, padding: 12, background: "#1a1a1a", borderRadius: 4 }}>
      <h4 style={{ marginTop: 0 }}>
        Diff: {a.hydra_version} ({a.pair}) vs {b.hydra_version} ({b.pair})
      </h4>
      <table
        style={{ width: "100%", borderCollapse: "collapse", fontFamily: "monospace", fontSize: 12 }}
      >
        <thead>
          <tr style={{ textAlign: "left", borderBottom: "1px solid #333" }}>
            <th style={{ padding: "4px 8px" }}>Fold</th>
            <th style={{ padding: "4px 8px" }}>Metric</th>
            <th style={{ padding: "4px 8px", textAlign: "right" }}>A</th>
            <th style={{ padding: "4px 8px", textAlign: "right" }}>B</th>
            <th style={{ padding: "4px 8px", textAlign: "right" }}>Δ (B − A)</th>
          </tr>
        </thead>
        <tbody>
          {allKeys.map((k) => {
            const [foldIdx, metric] = k.split("|");
            const av = aMap.get(k);
            const bv = bMap.get(k);
            const delta = av !== undefined && bv !== undefined ? bv - av : undefined;
            const fmt = (v) =>
              v === undefined ? "—" : Number(v).toFixed(4);
            return (
              <tr key={k} style={{ borderBottom: "1px solid #222" }}>
                <td style={{ padding: "4px 8px" }}>
                  {foldIdx === "-1" ? "agg" : foldIdx}
                </td>
                <td style={{ padding: "4px 8px" }}>{metric}</td>
                <td style={{ padding: "4px 8px", textAlign: "right" }}>{fmt(av)}</td>
                <td style={{ padding: "4px 8px", textAlign: "right" }}>{fmt(bv)}</td>
                <td
                  style={{
                    padding: "4px 8px",
                    textAlign: "right",
                    color:
                      delta === undefined
                        ? "#888"
                        : delta > 0
                        ? "#3aa757"
                        : delta < 0
                        ? "#d04545"
                        : "#aaa",
                  }}
                >
                  {fmt(delta)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
