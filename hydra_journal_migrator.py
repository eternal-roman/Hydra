#!/usr/bin/env python3
"""
HYDRA Order Journal Migrator

One-shot migration of the legacy `trade_log` shape (hydra_trades_live.json
and the `trade_log` field inside hydra_session_snapshot.json) to the new
`order_journal` shape introduced on branch feat/ws-execution-stream.

New shape — one entry per placed order:

    {
      "placed_at":       ISO-8601 with microseconds,
      "pair":            "SOL/USDC",
      "side":            "BUY" | "SELL",
      "intent":          {amount, limit_price, post_only, order_type, paper},
      "decision":        {strategy, regime, reason, confidence, params_at_entry,
                          cross_pair_override, book_confidence_modifier,
                          brain_verdict, swap_id},
      "order_ref":       {order_userref, order_id},
      "lifecycle":       {state, vol_exec, avg_fill_price, fee_quote,
                          final_at, terminal_reason, exec_ids}
    }

The local journal captures only what Kraken does NOT — decision context and
lifecycle pointers. Material fill details (individual exec_id costs, fees,
timestamps) live in `kraken trades-history`.

Usage:
    python hydra_journal_migrator.py                   # migrate files in cwd
    python hydra_journal_migrator.py --dry-run         # report only
    python hydra_journal_migrator.py --dir /some/path  # migrate elsewhere

Programmatic:
    from hydra_journal_migrator import (
        migrate_legacy_trade_log_file,
        migrate_trade_log_entries,
    )
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ── Canonical lifecycle states (must stay in sync with hydra_agent.py) ───
LIFECYCLE_STATES = (
    "PLACED",
    "FILLED",
    "PARTIALLY_FILLED",
    "CANCELLED_UNFILLED",
    "REJECTED",
    "PLACEMENT_FAILED",
)


def _is_new_shape(entry: Dict[str, Any]) -> bool:
    """An entry is in the new shape if it has the top-level sections."""
    return (
        isinstance(entry, dict)
        and "intent" in entry
        and "decision" in entry
        and "lifecycle" in entry
    )


def _extract_txid(legacy: Dict[str, Any]) -> Optional[str]:
    """Pull the Kraken order_id out of a legacy entry's result blob."""
    result = legacy.get("result") or {}
    if not isinstance(result, dict):
        return None
    txids = result.get("txid")
    if isinstance(txids, list) and txids:
        return str(txids[0])
    if isinstance(txids, str) and txids:
        return txids
    return None


def _infer_terminal_reason(legacy: Dict[str, Any]) -> Optional[str]:
    """Derive a stable terminal_reason string from legacy status + error text.

    PR #40 left reconciliation notes on two known phantom entries that we map
    to specific canonical reasons (dms_timeout, post_only). Anything else
    uses a tagged error string that carries the original field.
    """
    status = legacy.get("status")
    error = legacy.get("error")
    note = legacy.get("reconciliation_note") or ""
    note_l = note.lower()

    if status == "PLACED_NOT_FILLED":
        if "dead-man" in note_l or "cancelallordersafter" in note_l or "dms" in note_l:
            return "dms_timeout"
        if "post-only" in note_l or "post only" in note_l:
            return "post_only"
        return "cancelled_unfilled"
    if status == "FAILED":
        return f"placement_error:{error}" if error else "placement_error"
    if status == "TICKER_FAILED":
        return f"ticker_failed:{error}" if error else "ticker_failed"
    if status == "VALIDATION_FAILED":
        return f"validation_failed:{error}" if error else "validation_failed"
    if status == "PAPER_FAILED":
        return f"paper_failed:{error}" if error else "paper_failed"
    return None


def _classify_order_type(legacy: Dict[str, Any]) -> Tuple[str, bool, bool]:
    """Return (order_type, post_only, paper) from the legacy `order_type` tag."""
    ot = (legacy.get("order_type") or "").strip().lower()
    if ot.startswith("paper"):
        # "paper market" / "paper limit"
        rest = ot.split(None, 1)[1] if " " in ot else "market"
        return (rest, False, True)
    if ot == "limit post-only":
        return ("limit", True, False)
    if ot == "limit":
        return ("limit", False, False)
    if ot == "market":
        return ("market", False, False)
    # Unknown legacy tag — preserve literal but assume post-only live limit
    # since that is the only live path Hydra has ever used.
    return ("limit", True, False)


def _convert_entry(legacy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert one legacy trade_log entry to the new order_journal shape.

    Returns None to signal "drop this entry" (e.g. COORDINATED_SWAP markers,
    which are redundant in the new shape because each leg is already a
    separate order entry with its own swap_id tag).
    """
    if not isinstance(legacy, dict):
        return None

    # Drop legacy COORDINATED_SWAP marker rows — each leg is already an
    # ordinary order entry; the swap_id lives on the legs' decision block
    # in the new shape.
    if legacy.get("type") == "COORDINATED_SWAP":
        return None

    # Already migrated? Return as-is so the migrator is idempotent.
    if _is_new_shape(legacy):
        return legacy

    placed_at = legacy.get("time")
    pair = legacy.get("pair")
    side_raw = (legacy.get("action") or "").upper()
    if side_raw not in ("BUY", "SELL"):
        # Not a trade entry — skip defensively.
        return None

    amount = float(legacy.get("amount") or 0)
    price = float(legacy.get("price") or 0)
    order_type, post_only, paper = _classify_order_type(legacy)

    intent = {
        "amount": amount,
        "limit_price": price if order_type == "limit" else None,
        "post_only": post_only,
        "order_type": order_type,
        "paper": paper,
    }

    # Historical decision context beyond the free-form `reason` / `confidence`
    # was never persisted — those fields stay null post-migration. The
    # runtime place_order helper populates them on fresh placements.
    conf = legacy.get("confidence")
    decision = {
        "strategy": None,
        "regime": None,
        "reason": legacy.get("reason"),
        "confidence": float(conf) if isinstance(conf, (int, float)) else None,
        "params_at_entry": None,
        "cross_pair_override": None,
        "book_confidence_modifier": None,
        "brain_verdict": None,
        "swap_id": None,
    }

    order_ref = {
        "order_userref": None,  # historical orders didn't use --userref
        "order_id": _extract_txid(legacy),
    }

    status = legacy.get("status")
    terminal_reason = _infer_terminal_reason(legacy)

    # Lifecycle mapping — one branch per legacy status.
    if status in ("EXECUTED", "PAPER_EXECUTED"):
        # Legacy EXECUTED means "Kraken accepted placement" (plus historical
        # truth that most of these did fill — the phantom entries were
        # already downgraded to PLACED_NOT_FILLED in PR #40). Treat as
        # fully filled at the limit price. vol_exec is the bot's requested
        # amount and avg_fill_price is the limit price we sent; these are
        # the best approximations available from the local log. Real fill
        # prices are authoritative on Kraken.
        lifecycle = {
            "state": "FILLED",
            "vol_exec": amount,
            "avg_fill_price": price,
            "fee_quote": None,
            "final_at": placed_at,
            "terminal_reason": None,
            "exec_ids": [],
        }
    elif status == "PLACED_NOT_FILLED":
        lifecycle = {
            "state": "CANCELLED_UNFILLED",
            "vol_exec": 0.0,
            "avg_fill_price": None,
            "fee_quote": None,
            "final_at": placed_at,
            "terminal_reason": terminal_reason or "cancelled_unfilled",
            "exec_ids": [],
        }
    elif status in ("FAILED", "TICKER_FAILED", "VALIDATION_FAILED", "PAPER_FAILED"):
        lifecycle = {
            "state": "PLACEMENT_FAILED",
            "vol_exec": 0.0,
            "avg_fill_price": None,
            "fee_quote": None,
            "final_at": placed_at,
            "terminal_reason": terminal_reason or "placement_failed",
            "exec_ids": [],
        }
    else:
        # Unknown status — preserve as PLACED (non-terminal) with a tag so
        # the human can audit. This never fires on the PR #40 data repair
        # set but keeps the migrator robust against future legacy shapes.
        lifecycle = {
            "state": "PLACED",
            "vol_exec": 0.0,
            "avg_fill_price": None,
            "fee_quote": None,
            "final_at": None,
            "terminal_reason": f"unknown_legacy_status:{status}",
            "exec_ids": [],
        }

    entry = {
        "placed_at": placed_at,
        "pair": pair,
        "side": side_raw,
        "intent": intent,
        "decision": decision,
        "order_ref": order_ref,
        "lifecycle": lifecycle,
    }

    # Preserve the `source` tag if the legacy entry was a kraken_backfill
    # insertion from PR #40 — keeps the audit trail intact.
    if legacy.get("source"):
        entry["source"] = legacy["source"]

    return entry


def migrate_trade_log_entries(legacy_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply _convert_entry to every legacy entry, drop Nones, sort by placed_at.

    Idempotent: entries already in the new shape pass through unchanged.
    """
    out: List[Dict[str, Any]] = []
    for e in legacy_entries:
        converted = _convert_entry(e)
        if converted is not None:
            out.append(converted)
    out.sort(key=lambda e: e.get("placed_at") or "")
    return out


def _atomic_write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def migrate_legacy_trade_log_file(
    base_dir: str,
    *,
    dry_run: bool = False,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Migrate any legacy trade_log artifacts found in `base_dir`.

    Affects up to three files:
      - hydra_trades_live.json  -> hydra_order_journal.json (new file),
                                   legacy file renamed to .migrated
      - hydra_session_snapshot.json — in-place key rename `trade_log` ->
                                   `order_journal` with entry conversion
      - hydra_order_journal.json — if it already exists, no-op (idempotent)

    Returns a report dict: {converted_rolling, converted_snapshot, ...}.
    """
    report: Dict[str, Any] = {
        "converted_rolling": 0,
        "converted_snapshot": 0,
        "actions": [],
    }

    legacy_path = os.path.join(base_dir, "hydra_trades_live.json")
    new_path = os.path.join(base_dir, "hydra_order_journal.json")
    snap_path = os.path.join(base_dir, "hydra_session_snapshot.json")

    # ── Rolling file ────────────────────────────────────────────────
    if os.path.exists(new_path):
        report["actions"].append(
            f"new journal already present at {os.path.basename(new_path)} — skipping rolling migration"
        )
    elif os.path.exists(legacy_path):
        with open(legacy_path, "r", encoding="utf-8") as f:
            legacy = json.load(f)
        if not isinstance(legacy, list):
            raise ValueError(f"{legacy_path} is not a JSON list (got {type(legacy).__name__})")
        converted = migrate_trade_log_entries(legacy)
        report["converted_rolling"] = len(converted)
        if dry_run:
            report["actions"].append(
                f"[dry-run] would write {len(converted)} entries to "
                f"{os.path.basename(new_path)} and rename legacy to .migrated"
            )
        else:
            _atomic_write_json(new_path, converted)
            migrated_backup = legacy_path + ".migrated"
            # Clean up any stale backup from a prior aborted run
            if os.path.exists(migrated_backup):
                os.remove(migrated_backup)
            os.rename(legacy_path, migrated_backup)
            report["actions"].append(
                f"wrote {len(converted)} entries to {os.path.basename(new_path)}; "
                f"legacy preserved as {os.path.basename(migrated_backup)}"
            )
    else:
        report["actions"].append("no hydra_trades_live.json to migrate")

    # ── Session snapshot ────────────────────────────────────────────
    if os.path.exists(snap_path):
        with open(snap_path, "r", encoding="utf-8") as f:
            snap = json.load(f)
        if not isinstance(snap, dict):
            report["actions"].append(f"{os.path.basename(snap_path)} is not a JSON object — skipping")
        elif "order_journal" in snap:
            report["actions"].append(
                f"{os.path.basename(snap_path)} already uses order_journal key — idempotent skip"
            )
        elif "trade_log" in snap:
            legacy_list = snap.get("trade_log") or []
            converted = migrate_trade_log_entries(legacy_list)
            snap.pop("trade_log", None)
            snap["order_journal"] = converted
            report["converted_snapshot"] = len(converted)
            if dry_run:
                report["actions"].append(
                    f"[dry-run] would convert {len(converted)} entries in "
                    f"{os.path.basename(snap_path)} and rename key trade_log->order_journal"
                )
            else:
                _atomic_write_json(snap_path, snap)
                report["actions"].append(
                    f"converted {len(converted)} entries in {os.path.basename(snap_path)} "
                    f"and renamed key trade_log->order_journal"
                )
        else:
            report["actions"].append(
                f"{os.path.basename(snap_path)} has neither trade_log nor order_journal key — skipping"
            )
    else:
        report["actions"].append("no hydra_session_snapshot.json to migrate")

    if verbose:
        for line in report["actions"]:
            print(f"  [MIGRATE] {line}")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir", default=None,
        help="Directory containing the legacy files (default: dir of this script)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()

    base_dir = args.dir or os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(base_dir):
        print(f"ERROR: {base_dir} is not a directory", file=sys.stderr)
        return 2

    try:
        report = migrate_legacy_trade_log_file(
            base_dir, dry_run=args.dry_run, verbose=not args.quiet,
        )
    except Exception as e:
        print(f"ERROR: migration failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"\nconverted_rolling  = {report['converted_rolling']}")
        print(f"converted_snapshot = {report['converted_snapshot']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
