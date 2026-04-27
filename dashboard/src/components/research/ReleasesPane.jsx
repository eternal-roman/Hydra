// dashboard/src/components/research/ReleasesPane.jsx
//
// Mode C pane of the Research tab — per-version regression snapshot inspector.
// Talks to research_releases_list and research_releases_diff WS handlers.
//
// v2.20.1: restyled to use shared theme tokens (was hardcoded #888/#3aa757/etc.).

import React, { useEffect, useState } from "react";
import { COLORS, mono, heading, regressionVerdictColor } from "../../theme";

const fmtDate = (ts) =>
  ts ? new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 16) : "—";

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

const th = {
  padding: "10px 12px",
  fontFamily: mono,
  fontSize: 10,
  fontWeight: 700,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  color: COLORS.textMuted,
  textAlign: "left",
};

const td = { padding: "10px 12px", fontSize: 12, color: COLORS.text };
const tdMono = { ...td, fontFamily: mono };

export default function ReleasesPane({ sendMessage, releasesList, releasesDiff }) {
  const [selected, setSelected] = useState([]);

  useEffect(() => {
    sendMessage({ type: "research_releases_list" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = (id) => {
    setSelected((prev) =>
      prev.includes(id)
        ? prev.filter((x) => x !== id)
        : prev.length < 2
        ? [...prev, id]
        : [prev[1], id]
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
    return (
      <div style={{ padding: 24 }}>
        <Card><span style={{ color: COLORS.textDim, fontFamily: mono }}>Loading…</span></Card>
      </div>
    );
  }

  if (releasesList.success === false) {
    return (
      <div style={{ padding: 24 }}>
        <Card style={{ borderColor: `${COLORS.danger}55`, background: `${COLORS.danger}14` }}>
          <div style={{ fontFamily: mono, fontSize: 10, fontWeight: 700,
                        letterSpacing: "0.1em", textTransform: "uppercase",
                        color: COLORS.danger, marginBottom: 6 }}>
            Error
          </div>
          <div style={{ color: COLORS.text, fontFamily: mono, fontSize: 12 }}>
            {releasesList.error}
          </div>
        </Card>
      </div>
    );
  }

  const rows = releasesList.data || [];
  const canCompare = selected.length === 2;

  return (
    <div style={{ padding: 24, color: COLORS.text }}>
      <div style={{ marginBottom: 20 }}>
        <h3 style={{ margin: 0, fontFamily: heading, fontSize: 18,
                     fontWeight: 700, color: COLORS.text, letterSpacing: "-0.01em" }}>
          Release Regression Snapshots
        </h3>
        <p style={{ color: COLORS.textDim, fontSize: 12, marginTop: 6,
                    marginBottom: 0, lineHeight: 1.5, maxWidth: 720 }}>
          Mode C — anchored quarterly walk-forward verdicts persisted at each
          /release. Select 2 runs (any pair, any version) to diff their per-fold metrics.
        </p>
      </div>

      {rows.length === 0 ? (
        <Card>
          <span style={{ color: COLORS.textDim, fontSize: 12 }}>
            No regression runs yet. Run{" "}
            <code style={code}>python -m tools.run_regression --version &lt;X.Y.Z&gt;</code>{" "}
            to populate.
          </span>
        </Card>
      ) : (
        <>
          <Card style={{ padding: 0, overflow: "hidden", marginBottom: 16 }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ borderBottom: `1px solid ${COLORS.panelBorder}` }}>
                  <th style={{ ...th, width: 30 }}>✓</th>
                  <th style={th}>Version</th>
                  <th style={th}>Pair</th>
                  <th style={th}>Created</th>
                  <th style={th}>Verdict</th>
                  <th style={th}>Override?</th>
                  <th style={th}>Run ID</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => {
                  const isSelected = selected.includes(r.run_id);
                  const isLast = i === rows.length - 1;
                  return (
                    <tr
                      key={r.run_id}
                      style={{
                        borderBottom: isLast ? "none" : `1px solid ${COLORS.panelBorder}`,
                        background: isSelected ? `${COLORS.blue}1A` : "transparent",
                        cursor: "pointer",
                      }}
                      onClick={() => toggle(r.run_id)}
                    >
                      <td style={td}>
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => toggle(r.run_id)}
                          onClick={(e) => e.stopPropagation()}
                          style={{ accentColor: COLORS.blue, cursor: "pointer" }}
                        />
                      </td>
                      <td style={tdMono}>{r.hydra_version}</td>
                      <td style={tdMono}>{r.pair}</td>
                      <td style={{ ...tdMono, color: COLORS.textDim }}>
                        {fmtDate(r.created_at)}
                      </td>
                      <td style={{
                        ...td,
                        color: regressionVerdictColor(r.verdict_summary),
                        fontFamily: mono,
                        fontWeight: r.verdict_summary === "significant" ? 600 : 400,
                      }}>
                        {r.verdict_summary}
                      </td>
                      <td style={{ ...td, color: COLORS.warn, fontFamily: mono }}>
                        {r.override_reason ? `⚠ ${r.override_reason}` : ""}
                      </td>
                      <td style={{ ...tdMono, fontSize: 11, color: COLORS.textMuted }}>
                        {String(r.run_id).slice(0, 8)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>

          <button
            onClick={compare}
            disabled={!canCompare}
            style={{
              padding: "10px 18px",
              background: canCompare ? `${COLORS.blue}33` : COLORS.panel,
              color: canCompare ? COLORS.blue : COLORS.textMuted,
              border: `1px solid ${canCompare ? `${COLORS.blue}55` : COLORS.panelBorder}`,
              borderRadius: 4,
              cursor: canCompare ? "pointer" : "not-allowed",
              fontFamily: mono,
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              transition: "background 160ms, border-color 160ms",
            }}
          >
            Diff selected ({selected.length}/2)
          </button>
        </>
      )}

      {releasesDiff && releasesDiff.success === false && (
        <Card style={{ marginTop: 16, borderColor: `${COLORS.danger}55`,
                       background: `${COLORS.danger}14` }}>
          <div style={{ fontFamily: mono, fontSize: 10, fontWeight: 700,
                        letterSpacing: "0.1em", textTransform: "uppercase",
                        color: COLORS.danger, marginBottom: 6 }}>
            Diff error
          </div>
          <div style={{ color: COLORS.text, fontFamily: mono, fontSize: 12 }}>
            {releasesDiff.error}
          </div>
        </Card>
      )}

      {releasesDiff && releasesDiff.success === true && (
        <DiffView a={releasesDiff.a} b={releasesDiff.b} />
      )}
    </div>
  );
}

function DiffView({ a, b }) {
  const keyOf = (m) => `${m.fold_idx}|${m.metric}`;
  const aMap = new Map((a.metrics || []).map((m) => [keyOf(m), m.value]));
  const bMap = new Map((b.metrics || []).map((m) => [keyOf(m), m.value]));
  const allKeys = Array.from(new Set([...aMap.keys(), ...bMap.keys()])).sort();

  const th2 = {
    padding: "8px 12px",
    fontFamily: mono,
    fontSize: 10,
    fontWeight: 700,
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    color: COLORS.textMuted,
    textAlign: "left",
  };
  const td2 = { padding: "8px 12px", fontFamily: mono, fontSize: 12, color: COLORS.text };

  return (
    <Card style={{ marginTop: 16 }}>
      <div style={{ marginBottom: 12 }}>
        <h4 style={{ margin: 0, fontFamily: heading, fontSize: 14,
                     fontWeight: 700, color: COLORS.text }}>
          Diff
        </h4>
        <span style={{ color: COLORS.textMuted, fontFamily: mono, fontSize: 11 }}>
          {a.hydra_version} ({a.pair}) vs {b.hydra_version} ({b.pair})
        </span>
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ borderBottom: `1px solid ${COLORS.panelBorder}` }}>
            <th style={th2}>Fold</th>
            <th style={th2}>Metric</th>
            <th style={{ ...th2, textAlign: "right" }}>A</th>
            <th style={{ ...th2, textAlign: "right" }}>B</th>
            <th style={{ ...th2, textAlign: "right" }}>Δ (B − A)</th>
          </tr>
        </thead>
        <tbody>
          {allKeys.map((k, i) => {
            const [foldIdx, metric] = k.split("|");
            const av = aMap.get(k);
            const bv = bMap.get(k);
            const delta = av !== undefined && bv !== undefined ? bv - av : undefined;
            const fmt = (v) => (v === undefined ? "—" : Number(v).toFixed(4));
            const deltaColor =
              delta === undefined
                ? COLORS.textMuted
                : delta > 0
                ? COLORS.accent
                : delta < 0
                ? COLORS.danger
                : COLORS.textDim;
            const isLast = i === allKeys.length - 1;
            return (
              <tr key={k} style={{ borderBottom: isLast ? "none"
                                   : `1px solid ${COLORS.panelBorder}` }}>
                <td style={td2}>{foldIdx === "-1" ? "agg" : foldIdx}</td>
                <td style={td2}>{metric}</td>
                <td style={{ ...td2, textAlign: "right", color: COLORS.textDim }}>
                  {fmt(av)}
                </td>
                <td style={{ ...td2, textAlign: "right", color: COLORS.textDim }}>
                  {fmt(bv)}
                </td>
                <td style={{ ...td2, textAlign: "right", color: deltaColor,
                             fontWeight: 600 }}>
                  {fmt(delta)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Card>
  );
}
