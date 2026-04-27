// dashboard/src/components/ResearchTab.jsx
//
// v2.20.0 Research tab composer — three structured sub-panes:
//   DATASET   — read-only canonical OHLC store inspector (Mode A read)
//   LAB       — Mode B hypothesis lab (DEFERRED at MVP — surface only)
//   RELEASES  — Mode C release regression snapshots
//
// Owns only the local sub-tab state. All data + ws comes from App.jsx as props.

import React, { useState } from "react";
import DatasetPane from "./research/DatasetPane";
import LabPane from "./research/LabPane";
import ReleasesPane from "./research/ReleasesPane";

const TABS = [
  ["DATASET", "Dataset"],
  ["LAB", "Lab"],
  ["RELEASES", "Releases"],
];

export default function ResearchTab({
  sendMessage,
  coverageData,
  labResult,
  labProgress,            // T30B — streaming progress array from daemon thread
  releasesList,
  releasesDiff,
  paramsSchema,           // T30A — param schema from research_params_current
}) {
  const [pane, setPane] = useState("DATASET");

  return (
    <div>
      <nav
        style={{
          borderBottom: "1px solid #333",
          padding: "0 16px",
          display: "flex",
          gap: 4,
        }}
      >
        {TABS.map(([id, label]) => {
          const active = pane === id;
          return (
            <button
              key={id}
              onClick={() => setPane(id)}
              style={{
                background: "transparent",
                border: "none",
                color: active ? "#fff" : "#888",
                padding: "12px 16px",
                cursor: "pointer",
                borderBottom: active
                  ? "2px solid #3aa757"
                  : "2px solid transparent",
                fontFamily: "inherit",
                fontSize: 13,
                fontWeight: active ? 600 : 400,
                outline: "none",
              }}
            >
              {label}
            </button>
          );
        })}
      </nav>

      {pane === "DATASET" && (
        <DatasetPane sendMessage={sendMessage} coverageData={coverageData} />
      )}
      {pane === "LAB" && (
        <LabPane
          sendMessage={sendMessage}
          labResult={labResult}
          paramsSchema={paramsSchema}
          labProgress={labProgress}
        />
      )}
      {pane === "RELEASES" && (
        <ReleasesPane
          sendMessage={sendMessage}
          releasesList={releasesList}
          releasesDiff={releasesDiff}
        />
      )}
    </div>
  );
}
