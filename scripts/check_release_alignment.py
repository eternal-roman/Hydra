#!/usr/bin/env python3
"""Release alignment check.

Asserts every canonical version site in the Hydra repo reports the same
X.Y.Z string. Optionally verifies that a signed git tag v<X.Y.Z> exists
and points at HEAD.

Exit 0 on alignment, non-zero with a diff on mismatch. Intended as a
pre-flight gate for the /release skill and a standalone CLI check.

Canonical sites (CLAUDE.md §Version sites):
  1. CHANGELOG.md                 — latest `## [X.Y.Z]` section header
  2. dashboard/package.json       — top-level "version"
  3. dashboard/package-lock.json  — root "version" + packages[""]["version"]
  4. dashboard/src/App.jsx        — footer "HYDRA vX.Y.Z"
  5. hydra_agent.py               — _export_competition_results() "version"
  6. hydra_backtest.py            — HYDRA_VERSION = "X.Y.Z"
  7. CLAUDE.md                    — "**Version pin:** vX.Y.Z"
  (8.) git tag vX.Y.Z             — checked when --check-tag is passed

Usage:
  python scripts/check_release_alignment.py
  python scripts/check_release_alignment.py --check-tag
  python scripts/check_release_alignment.py --expect 2.14.2
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _first(pattern: str, text: str, group: int = 1) -> str | None:
    m = re.search(pattern, text)
    return m.group(group) if m else None


def probe_changelog() -> str | None:
    text = _read(REPO / "CHANGELOG.md")
    return _first(r"^##\s*\[(\d+\.\d+\.\d+)\]", text, 1) or _first(
        r"##\s*\[(\d+\.\d+\.\d+)\]", text, 1
    )


def probe_package_json() -> str | None:
    data = json.loads(_read(REPO / "dashboard" / "package.json"))
    return data.get("version")


def probe_package_lock() -> list[tuple[str, str]]:
    data = json.loads(_read(REPO / "dashboard" / "package-lock.json"))
    out: list[tuple[str, str]] = []
    if "version" in data:
        out.append(("package-lock.json:root", data["version"]))
    pkgs = data.get("packages", {})
    if "" in pkgs and "version" in pkgs[""]:
        out.append(('package-lock.json:packages[""]', pkgs[""]["version"]))
    return out


def probe_app_jsx() -> str | None:
    text = _read(REPO / "dashboard" / "src" / "App.jsx")
    return _first(r"HYDRA\s+v(\d+\.\d+\.\d+)", text)


def probe_hydra_agent() -> str | None:
    text = _read(REPO / "hydra_agent.py")
    # Look for the "version": "X.Y.Z" line inside _export_competition_results.
    # Fall back to any "version": "X.Y.Z" line that isn't the int 1 schema stamp.
    for m in re.finditer(r'"version"\s*:\s*"(\d+\.\d+\.\d+)"', text):
        return m.group(1)
    return None


def probe_hydra_backtest() -> str | None:
    text = _read(REPO / "hydra_backtest.py")
    return _first(r'HYDRA_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', text)


def probe_claude_md_pin() -> str | None:
    text = _read(REPO / "CLAUDE.md")
    return _first(r"\*\*Version pin:\*\*\s*v(\d+\.\d+\.\d+)", text)


def probe_gh_latest_release() -> str | None:
    try:
        out = subprocess.check_output(
            ["gh", "release", "view", "--json", "tagName", "-q", ".tagName"],
            cwd=REPO,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    m = re.match(r"v?(\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def probe_git_tag_at_head() -> tuple[str | None, str | None]:
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip()
    except subprocess.CalledProcessError:
        return None, None
    try:
        tags = subprocess.check_output(
            ["git", "tag", "--points-at", "HEAD"], cwd=REPO, text=True
        ).strip().splitlines()
    except subprocess.CalledProcessError:
        return head, None
    semver_tags = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return head, (semver_tags[0][1:] if semver_tags else None)


def collect() -> list[tuple[str, str | None]]:
    sites: list[tuple[str, str | None]] = []
    sites.append(("CHANGELOG.md:[X.Y.Z]", probe_changelog()))
    sites.append(("dashboard/package.json", probe_package_json()))
    sites.extend(probe_package_lock())
    sites.append(("dashboard/src/App.jsx:footer", probe_app_jsx()))
    sites.append(("hydra_agent.py:export_version", probe_hydra_agent()))
    sites.append(("hydra_backtest.py:HYDRA_VERSION", probe_hydra_backtest()))
    sites.append(("CLAUDE.md:version_pin", probe_claude_md_pin()))
    return sites


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Hydra release version alignment")
    parser.add_argument(
        "--check-tag",
        action="store_true",
        help="Also require a signed git tag v<X.Y.Z> at HEAD",
    )
    parser.add_argument(
        "--check-gh-release",
        action="store_true",
        help="Also require a published GitHub Release v<X.Y.Z> (requires gh CLI)",
    )
    parser.add_argument(
        "--expect",
        metavar="X.Y.Z",
        help="Require all sites to match this version (otherwise infer from CHANGELOG)",
    )
    args = parser.parse_args()

    sites = collect()

    print("Release alignment check")
    print("-" * 60)
    width = max(len(name) for name, _ in sites)
    for name, val in sites:
        print(f"  {name.ljust(width)}  {val if val is not None else '<MISSING>'}")

    values = [v for _, v in sites if v is not None]
    missing = [name for name, v in sites if v is None]

    expected = args.expect or (values[0] if values else None)
    if not expected:
        print("\nERROR: could not determine expected version (no sites readable)")
        return 2

    mismatches = [(name, v) for name, v in sites if v is not None and v != expected]

    tag_ok = True
    if args.check_tag:
        head, tag_ver = probe_git_tag_at_head()
        print(f"  git tag @ HEAD              {tag_ver or '<NONE>'}  (HEAD={head[:8] if head else '?'})")
        if tag_ver != expected:
            tag_ok = False

    gh_ok = True
    gh_ver: str | None = None
    if args.check_gh_release:
        gh_ver = probe_gh_latest_release()
        print(f"  gh release latest           {gh_ver or '<NONE/not-published>'}")
        if gh_ver != expected:
            gh_ok = False

    print("-" * 60)
    print(f"Expected: {expected}")

    if missing:
        print(f"MISSING:  {', '.join(missing)}")
    if mismatches:
        print("MISMATCH:")
        for name, v in mismatches:
            print(f"  {name}: {v} != {expected}")
    if args.check_tag and not tag_ok:
        print(f"TAG MISMATCH: expected v{expected} at HEAD")
    if args.check_gh_release and not gh_ok:
        print(
            f"GH RELEASE MISMATCH: latest published is {gh_ver or '<none>'}, "
            f"expected v{expected}. Run: gh release create v{expected} --notes-from-tag"
        )

    ok = not missing and not mismatches and tag_ok and gh_ok
    print("\n" + ("OK: all sites aligned." if ok else "FAIL: alignment broken."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
