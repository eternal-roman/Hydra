// dashboard/src/components/research/DatasetPane.jsx
//
// Mode A pane of the Research tab — read-only inspector for the canonical
// hydra_history.sqlite store. Talks to the research_dataset_coverage WS handler
// (see hydra_backtest_server.py:mount_backtest_routes).
//
// Mirrors the ws-client pattern used by DocumentLibraryPanel and ThesisPanel in
// App.jsx: sendMessage is passed as a prop; the response is received via
// coverageData prop (App.jsx stores the response in state via the central
// ws.onmessage switch and passes it down — T23 wires that up). The component
// sends the initial request on mount when coverageData is null.

import React, { useEffect } from "react";

// ─── Formatters ──────────────────────────────────────────────────────────────

const fmtDate = (ts) =>
  ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "—";

const fmtGrain = (grainSec) => {
  if (!grainSec) return "—";
  const mins = grainSec / 60;
  if (mins < 60) return `${mins} min`;
  const hrs = grainSec / 3600;
  if (hrs < 24) return `${hrs} hr`;
  return `${grainSec / 86400} day`;
};

const fmtGap = (s) => {
  if (!s && s !== 0) return "—";
  if (s < 3600) return `${s}s`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
};

// ─── Constants ───────────────────────────────────────────────────────────────

const STALE_THRESHOLD_SEC = 2 * 86400; // 2 days

// ─── Styles ──────────────────────────────────────────────────────────────────

const styles = {
  container: { padding: 16 },
  heading: { marginTop: 0, marginBottom: 4, fontSize: 16, fontWeight: 600 },
  note: { color: "#888", fontSize: 12, marginTop: 0, marginBottom: 16 },
  code: { color: "#fff" },
  table: { width: "100%", borderCollapse: "collapse", background: "transparent" },
  theadTr: { textAlign: "left", borderBottom: "1px solid #333" },
  th: { padding: "6px 8px", fontWeight: 600, fontSize: 12 },
  thRight: { padding: "6px 8px", fontWeight: 600, fontSize: 12, textAlign: "right" },
  tdLeft: { padding: "6px 8px", fontSize: 12 },
  tdRight: { padding: "6px 8px", fontSize: 12, textAlign: "right" },
  bodyRowNormal: { borderBottom: "1px solid #222", background: "transparent" },
  bodyRowStale: { borderBottom: "1px solid #222", background: "#3a2a00" },
  loading: { padding: 16, color: "#888" },
  empty: { padding: 16, color: "#888" },
  errorBanner: { padding: 16, color: "#ffb4b4" },
};

// ─── Component ───────────────────────────────────────────────────────────────

/**
 * DatasetPane
 *
 * Props:
 *   sendMessage  {Function}        — App.jsx WS send helper (see line ~3869)
 *   coverageData {object|null}     — response from research_dataset_coverage,
 *                                    shape: {success, data?, error?}
 *                                    null = not yet fetched (triggers send on mount)
 */
export default function DatasetPane({ sendMessage, coverageData }) {
  // Send the coverage request on mount (or when sendMessage becomes available).
  // coverageData starts as null in App.jsx; T23 populates it via the WS switch.
  useEffect(() => {
    if (typeof sendMessage === "function") {
      sendMessage({ type: "research_dataset_coverage" });
    }
  }, [sendMessage]);

  // ── Loading state (request sent, no response yet) ─────────────────────────
  if (coverageData === null || coverageData === undefined) {
    return <div style={styles.loading}>Loading…</div>;
  }

  // ── Error from server ─────────────────────────────────────────────────────
  if (!coverageData.success) {
    return (
      <div style={styles.errorBanner}>
        <strong>Error:</strong>{" "}
        {coverageData.error || "Unknown error from research_dataset_coverage"}
      </div>
    );
  }

  const rows = Array.isArray(coverageData.data) ? coverageData.data : [];

  // ── Empty store ───────────────────────────────────────────────────────────
  if (rows.length === 0) {
    return (
      <div style={styles.empty}>
        No coverage data — bootstrap with{" "}
        <code style={styles.code}>python -m tools.bootstrap_history</code> first.
      </div>
    );
  }

  // ── Table ─────────────────────────────────────────────────────────────────
  const now = Date.now() / 1000;
  const isStale = (r) => r.last_ts && now - r.last_ts > STALE_THRESHOLD_SEC;

  return (
    <div style={styles.container}>
      <h3 style={styles.heading}>Canonical Historical Store</h3>
      <p style={styles.note}>
        Read-only inspector. Refresh via{" "}
        <code style={styles.code}>tools/refresh_history.py</code>.
      </p>
      <div
        style={{
          background: "#1a2a3a",
          border: "1px solid #2a4a6a",
          borderRadius: 4,
          padding: "8px 12px",
          marginBottom: 12,
          fontSize: 11,
          color: "#a8c8e8",
          lineHeight: 1.5,
        }}
      >
        <strong style={{ color: "#fff" }}>About gaps:</strong> Gap counts
        below reflect <em>real exchange-side activity</em> — hours where
        Kraken recorded zero trades on that pair. Verified: zero trades in
        these windows in the source archive. Common causes: (a) Kraken's
        early low-volume era for BTC/USD (~1300 gaps in 2014, drops to
        &lt;10/year by 2017); (b) exchange maintenance / outages affecting
        SOL trading (e.g. <code style={styles.code}>2024-01-20</code>,
        <code style={styles.code}>2024-04-14</code>,
        <code style={styles.code}>2025-11-01</code> appear identically in
        SOL/USD and SOL/BTC). The data ingestion is correct; gaps are
        ground truth from the exchange.
      </div>
      <table style={styles.table}>
        <thead>
          <tr style={styles.theadTr}>
            <th style={styles.th}>Pair</th>
            <th style={styles.th}>Grain</th>
            <th style={styles.th}>First</th>
            <th style={styles.th}>Last</th>
            <th style={styles.thRight}>Candles</th>
            <th style={styles.thRight}>Gaps</th>
            <th style={styles.thRight}>Max gap</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const stale = isStale(r);
            return (
              <tr
                key={`${r.pair}-${r.grain_sec}`}
                style={stale ? styles.bodyRowStale : styles.bodyRowNormal}
              >
                <td style={styles.tdLeft}>{r.pair}</td>
                <td style={styles.tdLeft}>{fmtGrain(r.grain_sec)}</td>
                <td style={styles.tdLeft}>{fmtDate(r.first_ts)}</td>
                <td style={styles.tdLeft}>
                  {fmtDate(r.last_ts)}
                  {stale ? " ⚠" : ""}
                </td>
                <td style={styles.tdRight}>
                  {typeof r.candle_count === "number"
                    ? r.candle_count.toLocaleString()
                    : r.candle_count ?? "—"}
                </td>
                <td style={styles.tdRight}>
                  {r.gap_count ?? "—"}
                </td>
                <td style={styles.tdRight}>{fmtGap(r.max_gap_sec)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
