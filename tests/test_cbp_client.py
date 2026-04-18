"""CbpClient — unit tests using a stubbed HTTP server.

Exercises the real network path (urllib talking to a localhost HTTP
server we spin up in-process) so we don't mock the transport. This
catches bearer-header shape, PUT body shape, and CBQ encoding.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.cbp_client import CbpClient, _derive_id


class _Handler(BaseHTTPRequestHandler):
    # Subclass the singleton captures per test with the in-memory store.
    store: dict = {}
    calls: list = []

    def log_message(self, *_a, **_kw):  # silence test output
        pass

    def _auth_ok(self) -> bool:
        return self.headers.get("Authorization") == "Bearer unit-test-token"

    def do_PUT(self) -> None:  # noqa: N802
        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n).decode("utf-8")) if n else None
        _Handler.calls.append(("PUT", self.path, body))
        # Mirror the real server: omit v and prev is OK — server assigns.
        node_id = self.path.rsplit("/", 1)[-1]
        existing = _Handler.store.get(node_id)
        out = dict(body or {})
        out["v"] = (existing["v"] + 1) if existing else 1
        out["prev"] = existing["id"] if existing else None
        _Handler.store[node_id] = out
        self.send_response(200 if existing else 201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(out).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            return
        u = urlparse(self.path)
        _Handler.calls.append(("GET", self.path, None))
        q = parse_qs(u.query)
        cbq = (q.get("cbq") or [""])[0]
        # Trivial filter: only support tag:<t> for the test.
        nodes = list(_Handler.store.values())
        if cbq.startswith("tag:"):
            wanted = cbq[4:]
            nodes = [n for n in nodes if wanted in (n.get("tags") or [])]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "frame_id": "hydra_session",
            "tier": "full",
            "nodes": nodes,
            "edges": [],
        }).encode("utf-8"))


def _start_server() -> tuple[HTTPServer, str, threading.Thread]:
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    thr = threading.Thread(target=srv.serve_forever, daemon=True)
    thr.start()
    addr = f"http://127.0.0.1:{srv.server_port}"
    return srv, addr, thr


def _prep_runner_dir(tmp: pathlib.Path, addr: str, frame_root: str = "hydra_root") -> pathlib.Path:
    state = tmp / "state"
    state.mkdir(parents=True)
    (state / "ready.json").write_text(json.dumps({
        "addr": addr,
        "token": "unit-test-token",
        "frame_id": "hydra_session",
        "pid": 0,
    }), encoding="utf-8")
    (tmp / "config.json").write_text(json.dumps({
        "clients": {"hydra_session": {
            "frame_id": "hydra_session",
            "frame_root_label": frame_root,
        }}
    }), encoding="utf-8")
    return tmp


def _reset() -> None:
    _Handler.store.clear()
    _Handler.calls.clear()


def test_is_available_flips_on_ready_file():
    with tempfile.TemporaryDirectory() as td:
        c = CbpClient(runner_dir=pathlib.Path(td))
        assert c.is_available() is False
        (pathlib.Path(td) / "state").mkdir()
        (pathlib.Path(td) / "state" / "ready.json").write_text(
            json.dumps({"addr": "http://x", "token": "y", "frame_id": "z"}),
            encoding="utf-8",
        )
        assert c.is_available() is True


def test_remember_omits_v_and_prev_in_put_body():
    _reset()
    srv, addr, _ = _start_server()
    try:
        with tempfile.TemporaryDirectory() as td:
            _prep_runner_dir(pathlib.Path(td), addr)
            c = CbpClient(runner_dir=pathlib.Path(td))
            nid = c.remember(label="unit.test.one", summary="hello", tags=("x",))
            assert nid == _derive_id("node:unit.test.one")
            # Assert the request body is v0.8.1-clean.
            method, path, body = _Handler.calls[-1]
            assert method == "PUT"
            assert path.endswith(f"/v1/node/{nid}")
            assert "v" not in body, f"v should be omitted in PUT body, got {body!r}"
            assert "prev" not in body, f"prev should be omitted in PUT body, got {body!r}"
            # label:<slug> auto-tag was added.
            assert "label:unit.test.one" in body["tags"]
            # lineage points at the frame-root id derived from config.
            assert body["lineage"] == _derive_id("node:hydra_root")
    finally:
        srv.shutdown()


def test_remember_idempotent_same_label_same_id():
    _reset()
    srv, addr, _ = _start_server()
    try:
        with tempfile.TemporaryDirectory() as td:
            _prep_runner_dir(pathlib.Path(td), addr)
            c = CbpClient(runner_dir=pathlib.Path(td))
            n1 = c.remember(label="twice", summary="a")
            n2 = c.remember(label="twice", summary="b")
            assert n1 == n2
            # Server-side store should have a single entry at v=2.
            assert len(_Handler.store) == 1
            assert list(_Handler.store.values())[0]["v"] == 2
    finally:
        srv.shutdown()


def test_recall_uses_cbq_tag_filter():
    _reset()
    srv, addr, _ = _start_server()
    try:
        with tempfile.TemporaryDirectory() as td:
            _prep_runner_dir(pathlib.Path(td), addr)
            c = CbpClient(runner_dir=pathlib.Path(td))
            c.remember(label="one", summary="x", tags=("companion:apex",))
            c.remember(label="two", summary="y", tags=("companion:other",))
            got = c.recall(tag="companion:apex")
            assert got is not None
            assert len(got) == 1
            assert got[0].val["summary"] == "x"
            # Confirm the URL had cbq=tag:companion:apex (server-side filter).
            get_calls = [c for c in _Handler.calls if c[0] == "GET"]
            assert any("cbq=tag%3Acompanion%3Aapex" in call[1] for call in get_calls)
    finally:
        srv.shutdown()


def test_no_ready_file_returns_none_silently():
    with tempfile.TemporaryDirectory() as td:
        c = CbpClient(runner_dir=pathlib.Path(td))
        assert c.remember(label="x", summary="y") is None
        assert c.recall(tag="x") is None


def test_network_failure_returns_none_silently():
    # Point at a bogus port — expect None, not an exception.
    with tempfile.TemporaryDirectory() as td:
        _prep_runner_dir(pathlib.Path(td), "http://127.0.0.1:1")  # port 1 refuses
        c = CbpClient(runner_dir=pathlib.Path(td), timeout_s=0.2)
        assert c.remember(label="x", summary="y") is None
        assert c.recall(tag="x") is None
