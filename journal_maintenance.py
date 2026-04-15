#!/usr/bin/env python3
"""journal_maintenance.py — Hydra order-journal maintenance tool.

Safely edits BOTH hydra_order_journal.json and hydra_session_snapshot.json
in lockstep so they stay consistent.  Run while the agent is STOPPED.

Usage:
    python journal_maintenance.py status                  # audit current state
    python journal_maintenance.py purge-failed             # remove PLACEMENT_FAILED entries
    python journal_maintenance.py purge-failed --dry-run   # preview without writing
    python journal_maintenance.py purge <index> [index...] # remove entries by index
    python journal_maintenance.py purge <index> --dry-run  # preview specific purge

Indexes are 0-based and correspond to the 'status' output.  After any write,
both files are rewritten atomically (.tmp → os.replace) so a crash mid-write
cannot corrupt either file.
"""

import argparse
import json
import os
from collections import Counter

# ---------------------------------------------------------------------------
# Paths — same convention as HydraAgent._snapshot_dir
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
JOURNAL_PATH = os.path.join(REPO_ROOT, "hydra_order_journal.json")
SNAPSHOT_PATH = os.path.join(REPO_ROOT, "hydra_session_snapshot.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  ERROR: {os.path.basename(path)} is corrupt: {e}")
        return None


def _atomic_write(path, data):
    """Write JSON atomically via .tmp → os.replace."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _dedup_key(entry):
    """Same dedup key logic as HydraAgent._merge_order_journal."""
    t = entry.get("placed_at", "")
    ref = entry.get("order_ref") or {}
    order_id = ref.get("order_id") if isinstance(ref, dict) else None
    if order_id:
        return (t, order_id)
    intent = entry.get("intent") or {}
    return (t, entry.get("pair", ""), entry.get("side", ""),
            intent.get("amount", 0) if isinstance(intent, dict) else 0)


def _entry_summary(i, e):
    """One-line summary of a journal entry."""
    lc = e.get("lifecycle", {})
    state = lc.get("state", "?")
    reason = lc.get("terminal_reason", "")
    pair = e.get("pair", "?")
    side = e.get("side", "?")
    amt = (e.get("intent") or {}).get("amount", "?")
    ts = e.get("placed_at", "?")[:19]
    src = e.get("source", "agent")
    reason_str = f"  reason={reason}" if reason else ""
    return f"[{i:3d}] {ts}  {pair:10s} {side:4s}  {state:20s}  amt={amt}  src={src}{reason_str}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_status(args):
    """Print audit of current journal + snapshot state."""
    journal = _load_json(JOURNAL_PATH)
    snapshot = _load_json(SNAPSHOT_PATH)

    if journal is None:
        print(f"  Journal not available: {JOURNAL_PATH}")
        return
    print(f"Journal: {len(journal)} entries  ({JOURNAL_PATH})")
    j_states = Counter(e.get("lifecycle", {}).get("state", "?") for e in journal)
    for s, c in j_states.most_common():
        print(f"  {s}: {c}")

    print()
    if snapshot is None:
        print(f"  Snapshot not available: {SNAPSHOT_PATH}")
    else:
        snap_j = snapshot.get("order_journal", [])
        print(f"Snapshot journal: {len(snap_j)} entries")
        s_states = Counter(e.get("lifecycle", {}).get("state", "?") for e in snap_j)
        for s, c in s_states.most_common():
            print(f"  {s}: {c}")

        # Engine positions
        print()
        print("Engine positions (from snapshot):")
        for pair, eng in snapshot.get("engines", {}).items():
            pos = eng.get("position", {})
            sz = pos.get("size", 0)
            ae = pos.get("avg_entry", 0)
            bal = eng.get("balance", 0)
            marker = "  <-- HAS POSITION" if sz > 0 else ""
            print(f"  {pair}: size={sz}, avg_entry={ae}, balance={bal:.4f}{marker}")

    # List non-FILLED entries for quick triage
    non_filled = [(i, e) for i, e in enumerate(journal)
                  if e.get("lifecycle", {}).get("state") not in ("FILLED", "PARTIALLY_FILLED")]
    if non_filled:
        print()
        print(f"Non-fill entries ({len(non_filled)}):")
        for i, e in non_filled:
            print(f"  {_entry_summary(i, e)}")

    # Consistency check: snapshot journal should be subset of rolling journal
    if snapshot:
        j_keys = {_dedup_key(e) for e in journal}
        snap_j = snapshot.get("order_journal", [])
        orphans = [e for e in snap_j if _dedup_key(e) not in j_keys]
        if orphans:
            print()
            print(f"WARNING: {len(orphans)} snapshot entries NOT in rolling journal:")
            for e in orphans:
                lc = e.get("lifecycle", {})
                print(f"  {e.get('placed_at','?')[:19]}  {e.get('pair','?')}  "
                      f"{e.get('side','?')}  {lc.get('state','?')}")


def _do_purge(indexes, dry_run):
    """Remove entries at given indexes from both files."""
    journal = _load_json(JOURNAL_PATH)
    snapshot = _load_json(SNAPSHOT_PATH)

    if journal is None:
        print("Journal not available.")
        return

    # Validate indexes
    bad = [i for i in indexes if i < 0 or i >= len(journal)]
    if bad:
        print(f"Invalid indexes (journal has {len(journal)} entries): {bad}")
        return

    # Collect entries to remove and their dedup keys
    to_remove = {i: journal[i] for i in indexes}
    remove_keys = {_dedup_key(e) for e in to_remove.values()}

    print(f"Purging {len(to_remove)} entries:")
    for i in sorted(to_remove):
        print(f"  {_entry_summary(i, to_remove[i])}")

    if dry_run:
        print()
        print("(dry run — no files modified)")
        return

    # Filter journal
    new_journal = [e for i, e in enumerate(journal) if i not in to_remove]
    removed_from_journal = len(journal) - len(new_journal)

    # Filter snapshot journal
    removed_from_snapshot = 0
    if snapshot:
        snap_j = snapshot.get("order_journal", [])
        new_snap_j = [e for e in snap_j if _dedup_key(e) not in remove_keys]
        removed_from_snapshot = len(snap_j) - len(new_snap_j)
        snapshot["order_journal"] = new_snap_j

    # Write both atomically
    _atomic_write(JOURNAL_PATH, new_journal)
    print(f"  -> Journal: {removed_from_journal} removed, {len(new_journal)} remaining")

    if snapshot:
        _atomic_write(SNAPSHOT_PATH, snapshot)
        print(f"  -> Snapshot: {removed_from_snapshot} removed, {len(snapshot['order_journal'])} remaining")
    else:
        print("  -> Snapshot: not available, skipped")

    print("Done.")


def cmd_purge_failed(args):
    """Remove all PLACEMENT_FAILED entries from both files."""
    journal = _load_json(JOURNAL_PATH)
    if journal is None:
        print("Journal not available.")
        return

    indexes = [i for i, e in enumerate(journal)
               if e.get("lifecycle", {}).get("state") == "PLACEMENT_FAILED"]

    if not indexes:
        print("No PLACEMENT_FAILED entries found.")
        return

    _do_purge(indexes, args.dry_run)


def cmd_purge(args):
    """Remove entries at specific indexes from both files."""
    indexes = sorted(set(args.indexes))
    _do_purge(indexes, args.dry_run)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Hydra order-journal maintenance tool. Run while agent is STOPPED.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Audit journal + snapshot state")

    pf = sub.add_parser("purge-failed", help="Remove all PLACEMENT_FAILED entries")
    pf.add_argument("--dry-run", action="store_true", help="Preview without writing")

    p = sub.add_parser("purge", help="Remove entries by index (0-based from 'status' output)")
    p.add_argument("indexes", type=int, nargs="+", help="Entry indexes to remove")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    is_write = args.command in ("purge", "purge-failed") and not getattr(args, "dry_run", False)

    # Safety check: warn if agent might be running (write ops only)
    if is_write:
        import subprocess
        try:
            # PowerShell Get-CimInstance checks actual command lines, not
            # just window titles (which tasklist /V is limited to).
            # Filter to python.exe to avoid self-match (the PowerShell
            # query itself contains "hydra_agent") and IDE/shell noise.
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter "
                 "\"Name = 'python.exe' AND CommandLine like '%hydra_agent%'\" "
                 "| Select-Object -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=10,
            )
            pids = [line.strip() for line in result.stdout.splitlines()
                    if line.strip()]
            if pids:
                print("WARNING: hydra_agent.py appears to be running "
                      "(PIDs: {}).".format(", ".join(pids)))
                print("The agent overwrites these files from memory every tick.")
                print("Any changes made now will likely be clobbered.")
                resp = input("Continue anyway? [y/N] ").strip().lower()
                if resp != "y":
                    print("Aborted.")
                    return
        except Exception:
            pass  # Best-effort check; proceed if detection fails

    {"status": cmd_status, "purge-failed": cmd_purge_failed, "purge": cmd_purge}[args.command](args)


if __name__ == "__main__":
    main()
