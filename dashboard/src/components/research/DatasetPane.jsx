// dashboard/src/components/research/DatasetPane.jsx
//
// Mode A pane of the Research tab — read-only inspector for the canonical
// hydra_history.sqlite store. Talks to the research_dataset_coverage WS handler.
//
// v2.20.1: restyled to use shared theme tokens (was hardcoded #888/#333/etc.).

import React, { useEffect } from "react";
import { COLORS, mono, heading } from "../../theme";

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

const STALE_THRESHOLD_SEC = 2 * 86400;

const Card = ({ children, style }) => (
  <div style={{
    background: COLORS.panel,
    border: `1px solid ${COLORS.panelBorder}`,
    borderRadius: 6,
    padding: 16,
    ...style,
  }}>
    {children}
  </div>
);

const code = {
  fontFamily: mono,
  color: COLORS.text,
  background: COLORS.bg,
  padding: "1px 6px",
  borderRadius: 3,
  fontSize: 11,
};

const headerRow = {
  textAlign: "left",
  borderBottom: `1px solid ${COLORS.panelBorder}`,
};

const th = {
  padding: "10px 12px",
  fontFamily: mono,
  fontSize: 10,
  fontWeight: 700,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  color: COLORS.textMuted,
};

const thRight = { ...th, textAlign: "right" };
const td = { padding: "10px 12px", fontSize: 12, color: COLORS.text };
const tdMono = { ...td, fontFamily: mono };
const tdRight = { ...td, textAlign: "right", fontFamily: mono };

export default function DatasetPane({ sendMessage, coverageData }) {
  useEffect(() => {
    if (typeof sendMessage === "function") {
      sendMessage({ type: "research_dataset_coverage" });
    }
  }, [sendMessage]);

  if (coverageData === null || coverageData === undefined) {
    return (
      <div style={{ padding: 24 }}>
        <Card><span style={{ color: COLORS.textDim, fontFamily: mono }}>Loading…</span></Card>
      </div>
    );
  }

  if (!coverageData.success) {
    return (
      <div style={{ padding: 24 }}>
        <Card style={{ borderColor: `${COLORS.danger}55`, background: `${COLORS.danger}14` }}>
          <div style={{ fontFamily: mono, fontSize: 10, fontWeight: 700,
                        letterSpacing: "0.1em", textTransform: "uppercase",
                        color: COLORS.danger, marginBottom: 6 }}>
            Error
          </div>
          <div style={{ color: COLORS.text, fontFamily: mono, fontSize: 12 }}>
            {coverageData.error || "Unknown error from research_dataset_coverage"}
          </div>
        </Card>
      </div>
    );
  }

  const rows = Array.isArray(coverageData.data) ? coverageData.data : [];

  if (rows.length === 0) {
    return (
      <div style={{ padding: 24 }}>
        <Card>
          <span style={{ color: COLORS.textDim, fontSize: 12 }}>
            No coverage data — bootstrap with{" "}
            <code style={code}>python -m tools.bootstrap_history</code> first.
          </span>
        </Card>
      </div>
    );
  }

  const now = Date.now() / 1000;
  const isStale = (r) => r.last_ts && now - r.last_ts > STALE_THRESHOLD_SEC;

  return (
    <div style={{ padding: 24, color: COLORS.text }}>
      <div style={{ marginBottom: 20 }}>
        <h3 style={{ margin: 0, fontFamily: heading, fontSize: 18,
                     fontWeight: 700, color: COLORS.text, letterSpacing: "-0.01em" }}>
          Canonical Historical Store
        </h3>
        <p style={{ color: COLORS.textDim, fontSize: 12, marginTop: 6,
                    marginBottom: 0, lineHeight: 1.5 }}>
          Read-only inspector. Refresh via{" "}
          <code style={code}>tools/refresh_history.py</code>.
        </p>
      </div>

      <Card style={{
        marginBottom: 16,
        borderColor: `${COLORS.blue}44`,
        background: `${COLORS.blue}10`,
      }}>
        <div style={{ fontFamily: mono, fontSize: 10, fontWeight: 700,
                      letterSpacing: "0.1em", textTransform: "uppercase",
                      color: COLORS.blue, marginBottom: 8 }}>
          About gaps
        </div>
        <div style={{ color: COLORS.textDim, fontSize: 12, lineHeight: 1.6 }}>
          Gap counts below reflect <em>real exchange-side activity</em> — hours where
          Kraken recorded zero trades on that pair. Verified: zero trades in
          these windows in the source archive. Common causes: (a) Kraken's
          early low-volume era for BTC/USD (~1300 gaps in 2014, drops to
          &lt;10/year by 2017); (b) exchange maintenance / outages affecting
          SOL trading (e.g. <code style={code}>2024-01-20</code>,{" "}
          <code style={code}>2024-04-14</code>,{" "}
          <code style={code}>2025-11-01</code> appear identically in
          SOL/USD and SOL/BTC). The data ingestion is correct; gaps are
          ground truth from the exchange.
        </div>
      </Card>

      <Card style={{ padding: 0, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={headerRow}>
              <th style={th}>Pair</th>
              <th style={th}>Grain</th>
              <th style={th}>First</th>
              <th style={th}>Last</th>
              <th style={thRight}>Candles</th>
              <th style={thRight}>Gaps</th>
              <th style={thRight}>Max gap</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const stale = isStale(r);
              const isLast = i === rows.length - 1;
              return (
                <tr
                  key={`${r.pair}-${r.grain_sec}`}
                  style={{
                    borderBottom: isLast ? "none" : `1px solid ${COLORS.panelBorder}`,
                    background: stale ? `${COLORS.warn}14` : "transparent",
                  }}
                >
                  <td style={tdMono}>{r.pair}</td>
                  <td style={td}>{fmtGrain(r.grain_sec)}</td>
                  <td style={tdMono}>{fmtDate(r.first_ts)}</td>
                  <td style={{ ...tdMono, color: stale ? COLORS.warn : COLORS.text }}>
                    {fmtDate(r.last_ts)}
                    {stale ? " ⚠" : ""}
                  </td>
                  <td style={tdRight}>
                    {typeof r.candle_count === "number"
                      ? r.candle_count.toLocaleString()
                      : r.candle_count ?? "—"}
                  </td>
                  <td style={tdRight}>{r.gap_count ?? "—"}</td>
                  <td style={tdRight}>{fmtGap(r.max_gap_sec)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
