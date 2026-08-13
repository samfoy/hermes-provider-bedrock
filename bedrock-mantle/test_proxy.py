#!/usr/bin/env python3
"""Hermetic tests for the Bedrock Mantle signing proxy.

Runs against a stub upstream on loopback — no AWS credentials, no network, no
real Mantle calls. Covers the behaviours that actually broke during
development, so they cannot regress silently.

Run:  python -m pytest test_proxy.py -q

This is a real pytest module. It previously used a hand-rolled ``check()``
helper driven from ``main()``, which meant ``pytest`` collected **zero** tests
and reported success while running nothing — a CI job wired to pytest was green
against untested code. Every assertion below is a collected test.
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

import pytest

_spec = importlib.util.spec_from_file_location(
    "mantle_proxy_under_test", Path(__file__).resolve().parent / "proxy.py"
)
mp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mp)

# Stands in for the plugin's MANTLE_MODELS without importing the plugin.
STUB_MODELS = ("openai.gpt-5.6-sol", "openai.gpt-5.6-luna", "openai.gpt-5.6-terra")

# The exact canonical header set Mantle's SigV4 signature covers. Forwarding any
# extra client header (accept, user-agent) breaks the signature with a 401.
EXPECTED_SIGNED_HEADERS = "content-type;host;x-amz-date;x-amz-security-token"


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


def build_proxy(stub_port: int) -> "mp.MantleProxy":
    """A MantleProxy pointed at the stub, with signing stubbed out."""
    proxy = mp.MantleProxy(region="us-east-2", models=STUB_MODELS)
    proxy._get_frozen_credentials = lambda: FakeCreds()  # type: ignore[assignment]
    # Redirect upstream to the stub: plain HTTP on loopback.
    mp.UPSTREAM_HOST_TMPL = f"127.0.0.1:{stub_port}"

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
    return proxy


@pytest.fixture
def stub():
    up = StubUpstream()
    up.start()
    try:
        yield up
    finally:
        up.stop()


@pytest.fixture
def proxy(stub):
    p = build_proxy(stub.port)
    # start() explicitly: `port` is None until the listener exists, and several
    # tests address the proxy by port rather than through base_url().
    p.start()
    try:
        yield p
    finally:
        p.stop()


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


def signed_headers_of(auth_header: str) -> str:
    for part in auth_header.split():
        if part.startswith("SignedHeaders="):
            return part.split("=", 1)[1].rstrip(",")
    return ""


# ── Request translation ─────────────────────────────────────────────────────
def test_path_rewritten_to_openai_prefix(proxy, stub):
    """Mantle serves the Responses API under /openai/v1, not /v1."""
    post(proxy.base_url(), {"model": "openai.gpt-5.6-sol", "input": "hi"})
    assert stub.last_path == "/openai/v1/responses"


def test_body_forwarded_byte_for_byte(proxy, stub):
    payload = {"model": "openai.gpt-5.6-luna", "input": "exact-body-check",
               "max_output_tokens": 123}
    post(proxy.base_url(), payload)
    assert json.loads(stub.last_body) == payload


# ── Header hygiene: the SigV4 401 regression ───────────────────────────────
# SigV4 signs a canonical header list. Forwarding extra client headers, or
# sending both `Content-Type` and `content-type`, makes the signature cover
# headers the service did not expect and every call 401s.
NOISY_CLIENT_HEADERS = {
    "Authorization": "Bearer placeholder-must-be-dropped",
    "Accept": "application/json",
    "User-Agent": "OpenAI/Python 1.99",
    "Accept-Encoding": "gzip",
}


def test_client_bearer_replaced_by_sigv4(proxy, stub):
    post(proxy.base_url(), {"model": "x", "input": "y"},
         extra_headers=NOISY_CLIENT_HEADERS)
    auth = (stub.last_headers or {}).get("authorization", "")
    assert auth.startswith("AWS4-HMAC-SHA256")
    assert "placeholder-must-be-dropped" not in auth


def test_signed_headers_are_the_minimal_fixed_set(proxy, stub):
    post(proxy.base_url(), {"model": "x", "input": "y"},
         extra_headers=NOISY_CLIENT_HEADERS)
    auth = (stub.last_headers or {}).get("authorization", "")
    assert signed_headers_of(auth) == EXPECTED_SIGNED_HEADERS


@pytest.mark.parametrize("leaked", ["accept", "user-agent", "accept-encoding"])
def test_client_headers_excluded_from_signature(proxy, stub, leaked):
    post(proxy.base_url(), {"model": "x", "input": "y"},
         extra_headers=NOISY_CLIENT_HEADERS)
    auth = (stub.last_headers or {}).get("authorization", "")
    assert leaked not in signed_headers_of(auth)


def test_client_user_agent_not_forwarded(proxy, stub):
    post(proxy.base_url(), {"model": "x", "input": "y"},
         extra_headers=NOISY_CLIENT_HEADERS)
    ua = ((stub.last_headers or {}).get("user-agent") or "").lower()
    assert "openai/python" not in ua


# ── Response relay ─────────────────────────────────────────────────────────
def test_streaming_deltas_relayed_intact(proxy, stub):
    stub.mode = "stream"
    resp = post(proxy.base_url(),
                {"model": "openai.gpt-5.6-sol", "input": "count", "stream": True},
                raw=True)
    deltas = []
    for line in resp:
        s = line.decode(errors="replace").strip()
        if s.startswith("data:") and "[DONE]" not in s:
            try:
                deltas.append(json.loads(s[5:].strip()).get("delta", ""))
            except Exception:
                pass
    assert "".join(deltas) == "one two three"


def test_upstream_error_relayed_verbatim(proxy, stub):
    """A 400 must not be masked as a 502: the message names the real problem."""
    stub.mode = "error"
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(proxy.base_url(), {"model": "openai.gpt-5.6-sol", "input": "too big"})
    assert exc.value.code == 400
    assert "exceed model maximum" in exc.value.read().decode()


def test_unknown_post_path_returns_404(proxy):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{proxy.port}/bogus",
            data=b"{}", headers={"Content-Type": "application/json"}), timeout=15)
    assert exc.value.code == 404


# ── Listener ───────────────────────────────────────────────────────────────
def test_proxy_binds_loopback_only(proxy):
    """A bearer-carrying signer must never be reachable off-host."""
    assert proxy._server.server_address[0] == "127.0.0.1"


def test_start_is_idempotent(proxy):
    assert proxy.start() == proxy.port


# ── GET /v1/models: the WebUI Test-button 501 regression ───────────────────
# BaseHTTPRequestHandler answers an unimplemented verb with 501, so before
# do_GET existed the WebUI Custom Endpoints probe of {base_url}/models reported
# "Endpoint returned HTTP 501." on a fully working provider. The catalog is
# served locally on purpose: upstream ListModels is denied on the codex profile.
def test_get_models_returns_200_not_501(proxy):
    code, _ = get(f"http://127.0.0.1:{proxy.port}/v1/models")
    assert code == 200


def test_get_models_returns_the_curated_catalog(proxy):
    _, payload = get(f"http://127.0.0.1:{proxy.port}/v1/models")
    assert isinstance(payload, dict)
    assert payload.get("object") == "list"
    assert [m.get("id") for m in payload.get("data", [])] == list(STUB_MODELS)


def test_catalog_never_forwarded_upstream(proxy, stub):
    """ListModels is denied, so a forwarded probe would 401 on a working account."""
    stub.last_path = None
    get(f"http://127.0.0.1:{proxy.port}/v1/models")
    assert stub.last_path is None


@pytest.mark.parametrize("path,expected", [
    ("/v1/models", 200),
    ("/v1/models/", 200),          # trailing slash
    ("/v1/models?limit=5", 200),   # query string
    ("/v1", 200),                  # health
    ("/nope", 404),                # unknown GET path, not 501
])
def test_get_routing(proxy, path, expected):
    code, _ = get(f"http://127.0.0.1:{proxy.port}{path}")
    assert code == expected


def test_get_never_reaches_the_signer(proxy):
    """A GET must not consume AWS credentials; only POST is signed."""
    calls = {"n": 0}
    real = proxy._sign_and_forward

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    proxy._sign_and_forward = counting  # type: ignore[assignment]
    try:
        get(f"http://127.0.0.1:{proxy.port}/v1/models")
    finally:
        proxy._sign_and_forward = real  # type: ignore[assignment]
    assert calls["n"] == 0


# ── Per-instance credential isolation ──────────────────────────────────────
def test_each_proxy_keeps_its_own_credential_session():
    """The codex and personal providers share this class but MUST NOT share a
    botocore session, or whichever signed first would bill both accounts."""
    a = mp.MantleProxy(region="us-east-2", profile="profile-a")
    b = mp.MantleProxy(region="us-east-2", profile="profile-b")
    assert a.profile != b.profile
    assert a._creds_session is None and b._creds_session is None

    a._creds_session = object()
    assert b._creds_session is None, "setting one session must not leak to the other"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
