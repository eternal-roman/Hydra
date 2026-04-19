"""HID (Hydra Information Density) measurement.

Scores any CLAUDE.md variant on:
  CV  = F + alpha*E + beta*I*F                  (Connected Value)
  HID = CV / C * 1000

  F = atomic facts    (sentences, JSON leaf scalars, list items)
  E = relational facts (JSON edges + lineage pointers + cross-refs in prose)
  N = nodes            (JSON objects with an `i`/`id` field, or markdown bullets)
  I = inheritance leverage (1 - declared / total_logical)
  C = UTF-8 byte count

  alpha = 1.0   (each edge is one relational proposition)
  beta  = 0.5   (each inherited field substitutes ~half a re-declaration)

Counting rules are intentionally simple, deterministic, and version-agnostic
so the comparison is reproducible.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ALPHA = 1.0
BETA = 0.5


def _split_sentences(text: str) -> int:
    text = text.strip()
    if not text:
        return 0
    pieces = re.split(r"(?<=[.!?;])\s+(?=[A-Z\(\[`*0-9])", text)
    return sum(1 for p in pieces if len(p.strip()) > 3)


def _count_leaves_and_edges(obj, in_edges: bool = False, parent_key: str = ""):
    """Returns (facts, edges, nodes, declared_fields, lineage_refs)."""
    facts = edges = nodes = declared = lin = 0
    if isinstance(obj, dict):
        if any(k in obj for k in ("i", "id")):
            nodes += 1
        if "l" in obj or "lineage" in obj:
            lin += 1
        for k, v in obj.items():
            declared += 1
            if isinstance(v, (dict, list)):
                f, e, n, d, li = _count_leaves_and_edges(
                    v, in_edges=(k in ("e", "edges")), parent_key=k
                )
                facts += f
                edges += e
                nodes += n
                declared += d
                lin += li
            else:
                if isinstance(v, str):
                    facts += max(1, _split_sentences(v))
                else:
                    facts += 1
    elif isinstance(obj, list):
        for item in obj:
            if in_edges:
                edges += 1
                if isinstance(item, (list, dict)):
                    f, _e, n, d, li = _count_leaves_and_edges(item)
                    facts += f
                    nodes += n
                    declared += d
                    lin += li
            elif isinstance(item, (dict, list)):
                f, e, n, d, li = _count_leaves_and_edges(item, parent_key=parent_key)
                facts += f
                edges += e
                nodes += n
                declared += d
                lin += li
            else:
                if isinstance(item, str):
                    facts += max(1, _split_sentences(item))
                else:
                    facts += 1
    return facts, edges, nodes, declared, lin


def _measure_prose(text: str):
    """Measure markdown/prose: sentences, list items, headings, edges (cross-refs)."""
    facts = edges = nodes = 0
    lines = text.splitlines()
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("```"):
            continue
        if s.startswith("#"):
            nodes += 1
            facts += 1
            continue
        if re.match(r"^[-*]\s+", s) or re.match(r"^\d+\.\s+", s):
            nodes += 1
            body = re.sub(r"^[-*\d.]+\s+", "", s)
            facts += max(1, _split_sentences(body))
            edges += len(re.findall(r"`[a-zA-Z_][\w./]+`|\[[^\]]+\]\([^)]+\)", body))
            continue
        facts += _split_sentences(s)
        edges += len(re.findall(r"`[a-zA-Z_][\w./]+`|\[[^\]]+\]\([^)]+\)", s))
    return facts, edges, nodes


def measure(path: str):
    p = Path(path)
    raw = p.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8")
    chars = len(raw)

    json_facts = json_edges = json_nodes = declared = lin = 0
    json_blocks = re.findall(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
    json_chars = 0
    for blk in json_blocks:
        json_chars += len(blk.encode("utf-8"))
        try:
            data = json.loads(blk)
        except json.JSONDecodeError as e:
            print(f"  WARN: JSON parse failed in {p.name}: {e}", file=sys.stderr)
            continue
        f, e, n, d, li = _count_leaves_and_edges(data)
        json_facts += f
        json_edges += e
        json_nodes += n
        declared += d
        lin += li

    prose_text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    p_facts, p_edges, p_nodes = _measure_prose(prose_text)

    F = json_facts + p_facts
    E = json_edges + p_edges + lin
    N = max(1, json_nodes + p_nodes)

    total_logical = declared + lin * 3
    I = (lin * 3) / total_logical if total_logical else 0.0

    CV = F + ALPHA * E + BETA * I * F
    HID = CV / chars * 1000 if chars else 0.0

    return {
        "file": p.name,
        "chars": chars,
        "F": F,
        "E": E,
        "N": N,
        "I": round(I, 3),
        "CV": round(CV, 1),
        "HID": round(HID, 3),
        "facts_per_kb": round(F / chars * 1000, 2),
        "edges_per_node": round(E / N, 2),
    }


def main():
    paths = sys.argv[1:] or [
        "_v0_original.md",
        "CLAUDE.md",
        "CLAUDE.draft.md",
    ]
    results = [measure(p) for p in paths]
    keys = ["file", "chars", "F", "E", "N", "I", "CV", "HID", "facts_per_kb", "edges_per_node"]
    widths = {k: max(len(k), max(len(str(r[k])) for r in results)) + 2 for k in keys}
    header = "".join(k.ljust(widths[k]) for k in keys)
    print(header)
    print("-" * len(header))
    for r in results:
        print("".join(str(r[k]).ljust(widths[k]) for k in keys))

    if len(results) >= 2:
        base = results[0]
        print()
        print("Improvement vs", base["file"])
        for r in results[1:]:
            d_chars = (r["chars"] - base["chars"]) / base["chars"] * 100
            d_hid = (r["HID"] - base["HID"]) / base["HID"] * 100
            d_cv = (r["CV"] - base["CV"]) / base["CV"] * 100
            print(
                f"  {r['file']:25s}  chars {d_chars:+6.1f}%   "
                f"CV {d_cv:+6.1f}%   HID {d_hid:+6.1f}%"
            )


if __name__ == "__main__":
    main()
