#!/usr/bin/env python3
"""Hermetic tests for the Bedrock Mantle signing proxy.

Runs against a stub upstream on loopback — no AWS credentials, no network, no
real Mantle calls. Covers the behaviours that actually broke during
development, so they cannot regress silently.

Run:  python3 test_proxy.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "mantle_proxy_under_test", Path(__file__).resolve().parent / "proxy.py"
)
mp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mp)

FAILURES: list[str] = []

# Stands in for the plugin's MANTLE_MODELS without importing the plugin.
STUB_MODELS = ("openai.gpt-5.6-sol", "openai.gpt-5.6-luna", "openai.gpt-5.6-terra")


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


# ── Stub upstream standing in for bedrock-mantle ────────────────────────────
class StubUpstream:
    """Records the request it received and replays a scripted response."""

    def __init__(self):
        self.last_path = None
        self.last_headers = None
        self.last_body = None
        self.mode = "json"          # json | stream | error
        self._server = None
        self.port = None

    def start(self) -> int:
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                stub.last_path = self.path
                stub.last_headers = {k.lower(): v for k, v in self.headers.items()}
                n = int(self.headers.get("Content-Length") or 0)
                stub.last_body = self.rfile.read(n) if n else b""

                if stub.mode == "error":
                    body = json.dumps({
                        "error": {"code": "validation_error",
                                  "message": "prompt tokens exceed model maximum"}
                    }).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if stub.mode == "stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for tok in ["one ", "two ", "three"]:
                        ev = f"data: {json.dumps({'type':'response.output_text.delta','delta':tok})}\n\n"
                        raw = ev.encode()
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
                        self.wfile.flush()
                    done = b"data: [DONE]\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(done), done))
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return

                body = json.dumps({
                    "status": "completed",
                    "output": [{"content": [{"type": "output_text", "text": "ok"}]}],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.port

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()


class FakeCreds:
    access_key = "AKIDEXAMPLE"
    secret_key = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    token = "FAKESESSIONTOKEN"


def build_proxy(stub_port: int) -> mp.MantleProxy:
    """A MantleProxy pointed at the stub, with signing stubbed out."""
    proxy = mp.MantleProxy(region="us-east-2", models=STUB_MODELS)
    proxy._get_frozen_credentials = lambda: FakeCreds()  # type: ignore[assignment]
    # Redirect upstream to the stub: plain HTTP on loopback.
    mp.UPSTREAM_HOST_TMPL = f"127.0.0.1:{stub_port}"

    orig = proxy._sign_and_forward

    def http_forward(method, upstream_path, body, inbound_headers):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        host = f"127.0.0.1:{stub_port}"
        url = f"http://{host}{upstream_path}"
        content_type = "application/json"
        for k, v in inbound_headers.items():
            if k.lower() == "content-type" and v:
                content_type = v
                break
        headers = {"host": host, "content-type": content_type}
        req_aws = AWSRequest(method=method, url=url, data=body, headers=headers)
        SigV4Auth(FakeCreds(), "bedrock", "us-east-2").add_auth(req_aws)
        req = urllib.request.Request(
            url, data=body or None, headers=dict(req_aws.headers), method=method
        )
        return urllib.request.urlopen(req, timeout=30)

    proxy._sign_and_forward = http_forward  # type: ignore[assignment]
    _ = orig
    return proxy


def post(base: str, payload: dict, extra_headers: dict | None = None, raw=False):
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        base + "/responses", data=json.dumps(payload).encode(), headers=headers
    )
    resp = urllib.request.urlopen(req, timeout=30)
    return resp if raw else json.load(resp)


def get(url: str):
    """GET a proxy URL, returning (status, parsed-json-or-raw-text)."""
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url), timeout=15)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    with resp:
        raw = resp.read().decode(errors="replace")
    try:
        return resp.status, json.loads(raw)
    except Exception:
        return resp.status, raw


def main() -> int:
    stub = StubUpstream()
    stub_port = stub.start()
    proxy = build_proxy(stub_port)
    base = proxy.base_url()

    try:
        print("\n[1] path rewrite /v1/responses -> /openai/v1/responses")
        post(base, {"model": "openai.gpt-5.6-sol", "input": "hi"})
        check(stub.last_path == "/openai/v1/responses",
              f"upstream path is /openai/v1/responses (got {stub.last_path})")

        print("\n[2] body forwarded byte-for-byte")
        payload = {"model": "openai.gpt-5.6-luna", "input": "exact-body-check",
                   "max_output_tokens": 123}
        post(base, payload)
        check(json.loads(stub.last_body) == payload, "upstream body matches request")

        print("\n[3] header hygiene — the SigV4 401 regression")
        post(base, {"model": "x", "input": "y"}, extra_headers={
            "Authorization": "Bearer placeholder-must-be-dropped",
            "Accept": "application/json",
            "User-Agent": "OpenAI/Python 1.99",
            "Accept-Encoding": "gzip",
        })
        h = stub.last_headers or {}
        auth = h.get("authorization", "")
        check(auth.startswith("AWS4-HMAC-SHA256"),
              "Authorization replaced by SigV4 (client bearer discarded)")
        check("placeholder-must-be-dropped" not in auth,
              "client bearer never forwarded upstream")
        signed = ""
        for part in auth.split():
            if part.startswith("SignedHeaders="):
                signed = part.split("=", 1)[1].rstrip(",")
        check(signed == "content-type;host;x-amz-date;x-amz-security-token",
              f"SignedHeaders is the minimal fixed set (got {signed!r})")
        for leaked in ("accept", "user-agent", "accept-encoding"):
            check(leaked not in signed, f"{leaked} not in SignedHeaders")
        check("openai/python" not in (h.get("user-agent") or "").lower(),
              "client User-Agent not forwarded")

        print("\n[4] streaming relayed intact")
        stub.mode = "stream"
        resp = post(base, {"model": "openai.gpt-5.6-sol", "input": "count",
                           "stream": True}, raw=True)
        deltas = []
        for line in resp:
            s = line.decode(errors="replace").strip()
            if s.startswith("data:") and "[DONE]" not in s:
                try:
                    deltas.append(json.loads(s[5:].strip()).get("delta", ""))
                except Exception:
                    pass
        check("".join(deltas) == "one two three",
              f"SSE deltas reassemble exactly (got {''.join(deltas)!r})")

        print("\n[5] upstream error relayed verbatim, not masked as 502")
        stub.mode = "error"
        code, body = None, ""
        try:
            post(base, {"model": "openai.gpt-5.6-sol", "input": "too big"})
        except urllib.error.HTTPError as e:
            code, body = e.code, e.read().decode()
        check(code == 400, f"status preserved (got {code})")
        check("exceed model maximum" in body, "upstream error body preserved")

        print("\n[6] unknown path rejected")
        stub.mode = "json"
        code = None
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{proxy.port}/bogus",
                data=b"{}", headers={"Content-Type": "application/json"}), timeout=15)
        except urllib.error.HTTPError as e:
            code = e.code
        check(code == 404, f"unsupported path returns 404 (got {code})")

        print("\n[7] loopback binding only")
        check(proxy._server.server_address[0] == "127.0.0.1",
              "proxy bound to 127.0.0.1")

        print("\n[8] start() is idempotent")
        check(proxy.start() == proxy.port, "repeated start() reuses one listener")

        print("\n[9] GET /v1/models — the WebUI Test-button 501 regression")
        # BaseHTTPRequestHandler answers an unimplemented verb with 501, so
        # before do_GET existed the WebUI Custom Endpoints probe of
        # {base_url}/models reported "Endpoint returned HTTP 501." on a fully
        # working provider. The catalog is served locally on purpose: upstream
        # ListModels is denied on the codex account.
        stub.last_path = None
        code, payload = get(f"http://127.0.0.1:{proxy.port}/v1/models")
        check(code == 200, f"GET /v1/models returns 200, not 501 (got {code})")
        ids = [m.get("id") for m in payload.get("data", [])] if isinstance(payload, dict) else []
        check(ids == list(STUB_MODELS), f"catalog matches the curated list (got {ids})")
        check(isinstance(payload, dict) and payload.get("object") == "list",
              "response uses the OpenAI /v1/models envelope")
        check(stub.last_path is None,
              "catalog served locally — never forwarded upstream (ListModels is denied)")

        print("\n[10] GET trailing slash and query string tolerated")
        code, _ = get(f"http://127.0.0.1:{proxy.port}/v1/models/")
        check(code == 200, f"trailing slash still 200 (got {code})")
        code, _ = get(f"http://127.0.0.1:{proxy.port}/v1/models?limit=5")
        check(code == 200, f"query string still 200 (got {code})")
        code, _ = get(f"http://127.0.0.1:{proxy.port}/v1")
        check(code == 200, f"GET /v1 health returns 200 (got {code})")
        code, _ = get(f"http://127.0.0.1:{proxy.port}/nope")
        check(code == 404, f"unknown GET path returns 404, not 501 (got {code})")

        print("\n[11] GET never reaches the signer")
        # A GET must not consume AWS credentials; only POST is signed.
        calls = {"n": 0}
        real = proxy._sign_and_forward

        def counting(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        proxy._sign_and_forward = counting  # type: ignore[assignment]
        get(f"http://127.0.0.1:{proxy.port}/v1/models")
        check(calls["n"] == 0, f"GET did not invoke _sign_and_forward (got {calls['n']})")
        proxy._sign_and_forward = real  # type: ignore[assignment]

        print("\n[12] two proxies keep separate credential sessions")
        # The codex and personal providers share this class but MUST NOT share a
        # botocore session, or whichever signed first would bill both accounts.
        a = mp.MantleProxy(region="us-east-2", profile="profile-a")
        b = mp.MantleProxy(region="us-east-2", profile="profile-b")
        check(a.profile != b.profile, "each instance keeps its own profile")
        check(a._creds_session is None and b._creds_session is None,
              "credential sessions are per-instance, not class-level")
        a._creds_session = object()
        check(b._creds_session is None,
              "setting one instance's session does not leak to the other")

    finally:
        proxy.stop()
        stub.stop()

    print("\n" + "=" * 56)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("All proxy tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
