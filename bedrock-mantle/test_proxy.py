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


# ── Context window advertised on GET /v1/models ────────────────────────────
# Hermes resolves a context window by longest-substring match over
# agent/model_metadata.DEFAULT_CONTEXT_LENGTHS. Measured 2026-09-09:
# "openai.gpt-6-astra" matches NO key there and falls back to
# CONTEXT_PROBE_TIERS[0] = 256,000, against a real total window of 1,050,000.
# So the endpoint must advertise the window itself, or most of Astra's context
# goes unused and the compressor fires far too early.
#
# The advertised number is the TOTAL window, never the input limit: Hermes
# computes effective_window = context_length - max_tokens itself, so an
# input-limit value would subtract the output reserve twice.
def test_models_advertise_context_length_when_known():
    proxy = mp.MantleProxy(
        region="us-west-2",
        models=("openai.gpt-6-astra",),
        model_context={"openai.gpt-6-astra": 1_050_000},
    )
    try:
        _, payload = get(f"http://127.0.0.1:{proxy.start()}/v1/models")
        entry = payload["data"][0]
        assert entry["id"] == "openai.gpt-6-astra"
        assert entry["context_length"] == 1_050_000
    finally:
        proxy.stop()


def test_context_length_key_is_one_hermes_reads():
    """The key name is load-bearing, not cosmetic.

    ``max_tokens`` would be read as max *output* tokens, not the window
    (agent/model_metadata._MAX_COMPLETION_KEYS), so it must not be used here.
    """
    entry = mp._model_entry("m", 1_050_000)
    assert "context_length" in entry
    assert "max_tokens" not in entry


# Regression guard for a real defect: Astra was first published at 900,000,
# the measured INPUT ceiling, which cost ~150,000 tokens of usable input.
# Hermes computes effective_window = context_length - max_tokens
# (agent/context_compressor._compute_threshold_tokens), so publishing an input
# limit subtracts the 128,000 output reserve twice.
#
# Measured live 2026-09-09 and corroborated by models.dev
# (context 1050000 / input 922000 / output 128000): the cap is on the TOTAL.
# Two requests whose inputs differed by 6,000 tokens both stopped at
# total_tokens=921,858 with status=incomplete / reason=max_output_tokens.
def test_advertised_window_is_total_not_input_limit():
    total, max_output, measured_input_limit = 1_050_000, 128_000, 922_000
    assert total - max_output == measured_input_limit, (
        "advertise the TOTAL window: total minus the output reserve must equal "
        "the input limit measured against the live service"
    )
    entry = mp._model_entry("openai.gpt-6-astra", total)
    assert entry["context_length"] - max_output == measured_input_limit


def test_unknown_model_omits_context_length():
    """Absent metadata must stay absent, never become a guessed number.

    Omitting the key leaves Hermes' own resolution path intact; emitting a
    wrong value would override it with something worse than the fallback.
    """
    proxy = mp.MantleProxy(region="us-east-2", models=("some.new-model",))
    try:
        _, payload = get(f"http://127.0.0.1:{proxy.start()}/v1/models")
        assert "context_length" not in payload["data"][0]
    finally:
        proxy.stop()


def test_model_context_is_copied_not_shared():
    """Two listeners share one MODEL_CONTEXT dict at the call site."""
    shared = {"openai.gpt-6-astra": 1_050_000}
    a = mp.MantleProxy(region="us-west-2", model_context=shared)
    a.model_context["mutated"] = 1
    assert "mutated" not in shared


# ── Region isolation ───────────────────────────────────────────────────────
# Availability is per model AND per region (verified live 2026-09-09):
# gpt-5.6-sol exists only in us-east-2, gpt-6-astra only in us-west-2. A
# listener that advertises the other region's model 404s on first use.
def test_region_reaches_the_matching_upstream_host():
    """Region must reach that region's host, so a per-region listener is real.

    Uses a literal template rather than ``mp.UPSTREAM_HOST_TMPL``: ``build_proxy``
    rebinds that module global to the loopback stub, so reading it here would
    assert against whatever a previous test left behind.
    """
    east = mp.MantleProxy(region="us-east-2")
    west = mp.MantleProxy(region="us-west-2")
    assert east.region != west.region
    assert "bedrock-mantle.{region}.api.aws".format(region=west.region) == (
        "bedrock-mantle.us-west-2.api.aws"
    )


def test_distinct_ports_give_distinct_issuer_identities():
    """Encrypted reasoning is sealed per region, so the base_url must differ.

    Hermes stamps the reasoning issuer as ``other:{base_url}``
    (agent/codex_responses_adapter._classify_responses_issuer) and drops foreign
    blocks at replay. Two regions sharing one base_url would defeat that guard
    and send us-east-2 reasoning to us-west-2, which the service rejects with
    "encrypted reasoning is scoped to the region that produced it".
    """
    east = mp.MantleProxy(region="us-east-2", models=STUB_MODELS)
    west = mp.MantleProxy(region="us-west-2", models=("openai.gpt-6-astra",))
    try:
        assert east.base_url() != west.base_url()
    finally:
        east.stop()
        west.stop()


# ── /health identifies the listener, not just liveness ─────────────────────
# A pinned port can be held by another listener (a second Hermes process, or a
# stale one). config.yaml names the port as static text, so the request would
# reach the incumbent — possibly a different region or AWS account — and fail
# with a 404 "model does not exist" that looks like a service outage.
def test_health_reports_region_and_profile():
    proxy = mp.MantleProxy(
        region="us-west-2", profile="my-profile", models=("openai.gpt-6-astra",)
    )
    try:
        _, payload = get(f"http://127.0.0.1:{proxy.start()}/health")
        assert payload["region"] == "us-west-2"
        assert payload["profile"] == "my-profile"
        assert payload["models"] == ["openai.gpt-6-astra"]
    finally:
        proxy.stop()


def test_health_distinguishes_two_regions():
    """The whole point: two live listeners must be tellable apart."""
    east = mp.MantleProxy(region="us-east-2", profile="p-east")
    west = mp.MantleProxy(region="us-west-2", profile="p-west")
    try:
        _, pe = get(f"http://127.0.0.1:{east.start()}/health")
        _, pw = get(f"http://127.0.0.1:{west.start()}/health")
        assert pe["region"] != pw["region"]
    finally:
        east.stop()
        west.stop()


def test_pinned_port_collision_warns_and_still_serves():
    """An ephemeral fallback must be loud, because config.yaml pins the port.

    The incumbent keeps the pinned port, so Hermes would silently talk to the
    wrong listener. The fallback must still produce a working server.
    """
    incumbent = mp.MantleProxy(region="us-east-2", models=STUB_MODELS)
    port = incumbent.start()
    contender = mp.MantleProxy(
        region="us-west-2", pinned_port=port, models=("openai.gpt-6-astra",)
    )
    try:
        got = contender.start()
        assert got != port, "contender must not steal the incumbent's port"
        # Each listener still answers for its own region.
        _, pi = get(f"http://127.0.0.1:{port}/health")
        _, pc = get(f"http://127.0.0.1:{got}/health")
        assert pi["region"] == "us-east-2"
        assert pc["region"] == "us-west-2"
    finally:
        incumbent.stop()
        contender.stop()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
