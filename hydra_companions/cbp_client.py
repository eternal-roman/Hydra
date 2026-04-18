"""Thin client for the cbp-runner sidecar.

Reads the sidecar's handshake file (`<cbp-runner>/state/ready.json`)
fresh on every call so we pick up token rotations across server
restarts. Every network call is guarded by a short timeout and any
failure degrades silently — the sidecar invariant is that clients
MUST NOT block on its availability, so Hydra keeps running on its
local JSONL store whenever CBP is unreachable.

Environment:
    CBP_RUNNER_DIR   absolute path to the cbp-runner checkout.
                     Defaults to C:/Users/elamj/Dev/cbp-runner.
    CBP_CLIENT_TIMEOUT_S  per-call HTTP timeout (default 1.5).

Usage:
    from hydra_companions.cbp_client import CbpClient
    cbp = CbpClient()
    cbp.remember(label="user.prefers_terse", summary="...", tags=["companion:apex"])
    nodes = cbp.recall(tag="companion:apex")
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_RUNNER_DIR = Path(os.environ.get(
    "CBP_RUNNER_DIR", "C:/Users/elamj/Dev/cbp-runner"
))
DEFAULT_TIMEOUT_S = float(os.environ.get("CBP_CLIENT_TIMEOUT_S", "1.5"))


@dataclass(frozen=True)
class CbpNode:
    id: str
    val: dict
    w: float
    tags: tuple[str, ...]


class CbpClient:
    """Best-effort client. Never raises on network/config failure — every
    public method returns either the success payload or `None`."""

    def __init__(
        self,
        runner_dir: Optional[Path] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self._runner_dir = Path(runner_dir or DEFAULT_RUNNER_DIR)
        self._timeout_s = timeout_s or DEFAULT_TIMEOUT_S

    # ----- handshake -----

    def _ready(self) -> Optional[dict]:
        ready_path = self._runner_dir / "state" / "ready.json"
        if not ready_path.exists():
            return None
        try:
            return json.loads(ready_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _frame_root_id(self) -> Optional[str]:
        """Derive the frame-root node id the same way seed-hydra.py and
        memory-write.py do (`sha256('node:<root_label>')[:8]`). Needed so
        `remember()` can set a valid lineage without another HTTP round
        trip."""
        cfg_path = self._runner_dir / "config.json"
        if not cfg_path.exists():
            return None
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            label = cfg["clients"]["hydra_session"]["frame_root_label"]
        except (OSError, ValueError, KeyError):
            return None
        return _derive_id(f"node:{label}")

    def is_available(self) -> bool:
        return self._ready() is not None

    # ----- writes -----

    def remember(
        self,
        *,
        label: str,
        summary: str,
        tags: Iterable[str] = (),
        weight: float = 0.9,
        decay: str = "epoch",
        node_type: str = "entity",
        refs: Iterable[str] = (),
    ) -> Optional[str]:
        """Idempotent upsert via PUT /v1/node/:id. Returns the node id on
        success, None on any failure (sidecar down, network, HTTP error)."""
        ready = self._ready()
        if not ready:
            return None
        lineage = self._frame_root_id()
        if not lineage:
            return None

        node_id = _derive_id(f"node:{label}")
        tag_list = list(tags)
        # `label:<slug>` tag is the join key used by memory-read --label /
        # hydra-banner's CBQ. Dedup-safe.
        if f"label:{label}" not in tag_list:
            tag_list.append(f"label:{label}")

        body = {
            "id": node_id,
            "type": node_type,
            "val": {
                "label": label,
                "summary": summary,
                "refs": list(refs),
            },
            "w": weight,
            "decay": decay,
            "ttl": None,
            "lineage": lineage,
            "tags": tag_list,
        }
        status, _ = self._request(
            ready, "PUT", f"/v1/node/{node_id}", body=body
        )
        return node_id if status in (200, 201) else None

    # ----- reads -----

    def recall(
        self,
        *,
        label: Optional[str] = None,
        tag: Optional[str] = None,
        weight_min: Optional[float] = None,
    ) -> Optional[list[CbpNode]]:
        """Server-side CBQ query against the hydra_session frame. Returns
        a list of CbpNode (possibly empty) or None on any failure. Never
        filters client-side."""
        ready = self._ready()
        if not ready:
            return None
        frame_id = ready.get("frame_id") or "hydra_session"

        parts: list[str] = []
        if label:
            parts.append(f"tag:label:{label}")
        if tag:
            parts.append(f"tag:{tag}")
        if weight_min is not None:
            parts.append(f"w>={weight_min}")
        query = {"tier": "full"}
        if parts:
            query["cbq"] = ",".join(parts)

        path = f"/v1/frame/{frame_id}?{urllib.parse.urlencode(query)}"
        status, body = self._request(ready, "GET", path)
        if status != 200 or not isinstance(body, dict):
            return None
        nodes_raw = body.get("nodes") or []
        out: list[CbpNode] = []
        for n in nodes_raw:
            if not isinstance(n, dict):
                continue
            out.append(CbpNode(
                id=str(n.get("id", "")),
                val=n.get("val") if isinstance(n.get("val"), dict) else {},
                w=float(n.get("w", 0.0)),
                tags=tuple(n.get("tags") or ()),
            ))
        return out

    # ----- low-level -----

    def _request(
        self,
        ready: dict,
        method: str,
        path: str,
        body: Optional[dict] = None,
    ) -> tuple[int, object]:
        """Returns (status_code, parsed_body_or_raw). Any exception is
        converted to (0, str(exc)) — callers inspect the code only."""
        addr = ready.get("addr", "")
        token = ready.get("token", "")
        if not addr or not token:
            return 0, "missing addr/token"
        url = addr.rstrip("/") + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"Bearer {token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                raw = resp.read().decode("utf-8") or ""
                try:
                    return resp.status, json.loads(raw) if raw else None
                except ValueError:
                    return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8") or ""
            try:
                return e.code, json.loads(raw) if raw else None
            except ValueError:
                return e.code, raw
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return 0, f"{type(e).__name__}: {e}"


def _derive_id(raw: str) -> str:
    """Matches bin/seed-hydra.py::derive_id and bin/memory-write.py::derive_id
    (sha256[:8]). MUST stay in lockstep — id drift breaks lineage."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
