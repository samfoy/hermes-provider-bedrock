"""Local SigV4-signing proxy for Amazon Bedrock Mantle.

Why this exists
---------------
Bedrock Mantle serves the GPT-5.6 family (Sol / Luna / Terra) *only* on the
OpenAI **Responses** API::

    POST https://bedrock-mantle.<region>.api.aws/openai/v1/responses

Two facts make a proxy necessary rather than optional:

1. **GPT-5.6 rejects Chat Completions outright.** Verified against the live
   service::

       POST /v1/chat/completions  {"model": "openai.gpt-5.6-sol", ...}
       -> HTTP 400 "The model 'openai.gpt-5.6-sol' does not support the
          '/v1/chat/completions' API"

   So Hermes' existing Mantle support (``_model_flow_bedrock_api_key``, which
   configures a ``custom`` provider on ``/v1`` in ``chat_completions`` mode)
   cannot reach these models at all.

2. **Mantle needs per-request SigV4 signing; Hermes sends a static bearer.**
   Mantle accepts either a long-lived ``AWS_BEARER_TOKEN_BEDROCK`` *or* a
   SigV4-signed request. Hermes' HTTP client attaches one fixed
   ``Authorization: Bearer <key>`` header, but a SigV4 signature covers the
   method, path, headers and a SHA-256 of the *body*, so it must be recomputed
   for every request. There is no seam in the OpenAI client for that, and the
   built-in ``hermes_cli/proxy`` adapter contract (``UpstreamCredential``) is
   explicitly bearer-only.

A tiny loopback proxy resolves both: Hermes talks plain OpenAI-Responses to
``127.0.0.1``, and this process re-signs each request with the caller's real
AWS credentials before forwarding it upstream.

Design notes
------------
* **Loopback only.** Binds ``127.0.0.1`` on an ephemeral port. The listener is
  an unauthenticated hole into the user's AWS credentials, so it must never be
  reachable off-host. Any inbound ``Authorization`` header is discarded.
* **Credentials resolve per request**, through botocore's normal chain, so a
  ``credential_process`` that refreshes on demand keeps working during a long
  session. Botocore caches internally, so this is not a per-call subprocess.
* **Streaming is preserved.** The upstream SSE body is relayed in small chunks
  without buffering the whole response, so tokens reach the UI as they arrive.
* **Thread-per-request** via ``ThreadingHTTPServer`` with daemon threads: a
  streaming turn must not block concurrent requests (title generation and
  delegation fire in parallel with the main loop).

The catalog below is verified against the live endpoint rather than assumed;
see ``KNOWN_CONTEXT`` for the probe that established each context window.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

logger = logging.getLogger(__name__)

# ── Upstream ────────────────────────────────────────────────────────────────
# GPT-5.6 and the other Responses-API models are served from us-east-2.
DEFAULT_REGION = "us-east-2"
UPSTREAM_HOST_TMPL = "bedrock-mantle.{region}.api.aws"

# Mantle's Responses API prefix. Hermes (api_mode="codex_responses") POSTs to
# "{base_url}/responses", so the proxy maps /v1/responses -> /openai/v1/responses.
UPSTREAM_RESPONSES_PREFIX = "/openai/v1"

# SigV4 signing service name. Mantle is fronted by the bedrock service.
SIGV4_SERVICE = "bedrock"

# Read timeout for a single upstream call. Generous: a max-effort GPT-5.6 turn
# with a large prompt can think for minutes before the first token.
UPSTREAM_READ_TIMEOUT = 900

# Hop-by-hop headers that must not be relayed back to the client.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})


class MantleProxy:
    """Loopback HTTP server that SigV4-signs and forwards to Bedrock Mantle."""

    def __init__(self, region: str = DEFAULT_REGION, profile: Optional[str] = None,
                 pinned_port: int = 0, models: tuple = ()):
        self.region = region
        self.profile = profile
        self.pinned_port = pinned_port
        # Curated model ids this listener advertises on ``GET /v1/models``.
        # Passed in by the plugin so the proxy stays free of a back-import.
        self.models = tuple(models)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port: Optional[int] = None
        self._lock = threading.Lock()
        self._creds_session = None

    # ── Credentials ─────────────────────────────────────────────────────
    def _get_frozen_credentials(self):
        """Resolve AWS credentials, refreshing via botocore's normal chain.

        A botocore Session is reused so its internal credential cache (and any
        ``credential_process`` refresh logic) applies; we only take a frozen
        snapshot per request.
        """
        import botocore.session

        if self._creds_session is None:
            if self.profile:
                self._creds_session = botocore.session.Session(profile=self.profile)
            else:
                self._creds_session = botocore.session.get_session()

        creds = self._creds_session.get_credentials()
        if creds is None:
            raise RuntimeError(
                "No AWS credentials found for Bedrock Mantle. Set "
                "HERMES_MANTLE_AWS_PROFILE to a profile with Mantle access."
            )
        return creds.get_frozen_credentials()

    # ── Signing + forwarding ────────────────────────────────────────────
    def _sign_and_forward(self, method: str, upstream_path: str,
                          body: bytes, inbound_headers: dict):
        """Sign the request with SigV4 and return the opened upstream response.

        Only a minimal, fully-controlled header set is signed and sent:
        ``content-type`` plus the ``x-amz-*`` headers botocore adds. Inbound
        client headers are deliberately **discarded**, not forwarded.

        This is load-bearing, not tidiness. SigV4 hashes the exact set named in
        ``SignedHeaders``, so every signed header must survive to the wire
        byte-for-byte. Two failure modes were observed live, both HTTP 401
        "The request signature we calculated does not match":

        * Forwarding client headers (``accept``, ``user-agent``, ...) put them
          in ``SignedHeaders``, but Mantle's canonical string only covers
          ``content-type;host;x-amz-date;x-amz-security-token``.
        * Passing both ``Content-Type`` and ``content-type`` (the inbound
          casing plus our own default) produced a duplicate header that urllib
          collapsed *after* signing, invalidating the digest.

        Sending a fixed set makes the signature reproducible. Nothing in the
        OpenAI client's headers is semantically required upstream — the body
        carries the request, and content negotiation is implicit (SSE when
        ``stream: true``).
        """
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        host = UPSTREAM_HOST_TMPL.format(region=self.region)
        url = f"https://{host}{upstream_path}"

        # Preserve the inbound content-type (charset can matter), else default.
        content_type = "application/json"
        for k, v in inbound_headers.items():
            if k.lower() == "content-type" and v:
                content_type = v
                break

        headers = {"host": host, "content-type": content_type}

        # SigV4 signs the body hash, so sign the exact bytes we will send.
        aws_req = AWSRequest(method=method, url=url, data=body, headers=headers)
        SigV4Auth(
            self._get_frozen_credentials(), SIGV4_SERVICE, self.region
        ).add_auth(aws_req)

        # Send exactly what was signed — no additions, no case variants.
        req = urllib.request.Request(
            url, data=body or None, headers=dict(aws_req.headers), method=method
        )
        return urllib.request.urlopen(req, timeout=UPSTREAM_READ_TIMEOUT)

    # ── Lifecycle ───────────────────────────────────────────────────────
    def start(self) -> int:
        """Start the proxy if not running; return the bound port. Idempotent.

        The port comes from ``HERMES_MANTLE_PROXY_PORT`` when set, else the OS
        picks an ephemeral one. Pinning matters when Hermes reaches the proxy
        through a ``providers:`` entry in ``config.yaml``: that base URL is
        static text, so it must name a predictable port. If the pinned port is
        already taken we fall back to ephemeral rather than refusing to start.
        """
        with self._lock:
            if self._port is not None:
                return self._port

            proxy = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, fmt, *args):  # noqa: N802 - stdlib API
                    logger.debug("mantle-proxy: " + fmt, *args)

                def _fail(self, code: int, message: str):
                    payload = json.dumps(
                        {"error": {"message": message, "type": "proxy_error"}}
                    ).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    try:
                        self.wfile.write(payload)
                    except Exception:
                        pass

                def _json(self, code: int, payload: dict):
                    body = json.dumps(payload).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except Exception:
                        pass

                def do_GET(self):  # noqa: N802 - stdlib API
                    """Serve the curated catalog on ``/v1/models`` locally.

                    This route is answered here, NOT forwarded upstream, for two
                    reasons that both showed up live:

                    * ``codex-DO-NOT-DELETE`` is denied
                      ``bedrock-mantle:ListModels`` while inference works, so a
                      forwarded probe returns 401 on a healthy account.
                    * Mantle's own list includes Chat-Completions-only models
                      this proxy cannot serve in ``codex_responses`` mode.

                    Without it, ``BaseHTTPRequestHandler`` answered every GET
                    with ``501 Unsupported method``, and the WebUI Custom
                    Endpoints "Test" button (which probes ``{base_url}/models``)
                    reported "Endpoint returned HTTP 501." on a working provider.
                    """
                    path = (self.path or "").split("?", 1)[0].rstrip("/")
                    if path in ("/v1/models", UPSTREAM_RESPONSES_PREFIX + "/models"):
                        self._json(200, {
                            "object": "list",
                            "data": [
                                {"id": mid, "object": "model", "owned_by": "bedrock-mantle"}
                                for mid in proxy.models
                            ],
                        })
                        return
                    if path in ("", "/health", "/v1"):
                        self._json(200, {"status": "ok", "service": "bedrock-mantle-proxy"})
                        return
                    self._fail(404, f"unsupported path {self.path!r}")

                def do_POST(self):  # noqa: N802 - stdlib API
                    # Hermes posts to /v1/responses; Mantle serves
                    # /openai/v1/responses. Map it, and pass through any
                    # caller that already used the upstream-style path.
                    path = self.path
                    if path.startswith("/v1/"):
                        upstream_path = UPSTREAM_RESPONSES_PREFIX + path[len("/v1"):]
                    elif path.startswith(UPSTREAM_RESPONSES_PREFIX):
                        upstream_path = path
                    else:
                        self._fail(404, f"unsupported path {path!r}")
                        return

                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        self._fail(400, "invalid Content-Length")
                        return
                    body = self.rfile.read(length) if length else b""

                    try:
                        resp = proxy._sign_and_forward(
                            "POST", upstream_path, body, dict(self.headers)
                        )
                    except urllib.error.HTTPError as e:
                        # Relay the upstream error verbatim — its body carries
                        # the actionable message (validation errors, throttling).
                        err_body = e.read()
                        self.send_response(e.code)
                        ctype = e.headers.get("Content-Type", "application/json")
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", str(len(err_body)))
                        self.end_headers()
                        try:
                            self.wfile.write(err_body)
                        except Exception:
                            pass
                        return
                    except Exception as e:
                        logger.warning("mantle-proxy: forward failed", exc_info=True)
                        self._fail(502, f"upstream request failed: {e}")
                        return

                    # Relay status + headers, then stream the body through.
                    with resp:
                        self.send_response(resp.status)
                        for k, v in resp.headers.items():
                            if k.lower() in _HOP_BY_HOP or k.lower() == "content-length":
                                continue
                            self.send_header(k, v)
                        # Length is unknown for SSE, so use chunked framing.
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        try:
                            while True:
                                chunk = resp.read(8192)
                                if not chunk:
                                    break
                                self.wfile.write(
                                    b"%x\r\n%s\r\n" % (len(chunk), chunk)
                                )
                                self.wfile.flush()
                            self.wfile.write(b"0\r\n\r\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            # Client hung up (user cancelled the turn).
                            logger.debug("mantle-proxy: client disconnected")

            # Bind loopback only. Prefer a pinned port so a static config.yaml
            # base_url stays valid across restarts; fall back to ephemeral if
            # it is occupied (e.g. a second Hermes process already holds it).
            server = None
            if self.pinned_port:
                try:
                    server = ThreadingHTTPServer(("127.0.0.1", self.pinned_port), Handler)
                except OSError:
                    logger.info(
                        "bedrock-mantle: port %s unavailable, using an ephemeral port",
                        self.pinned_port,
                    )
            if server is None:
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            self._server = server
            self._port = server.server_address[1]
            self._thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.2},
                daemon=True,
                name="bedrock-mantle-proxy",
            )
            self._thread.start()
            logger.info(
                "bedrock-mantle: SigV4 proxy on 127.0.0.1:%s -> %s",
                self._port, UPSTREAM_HOST_TMPL.format(region=self.region),
            )
            return self._port

    def stop(self) -> None:
        """Shut the proxy down. Used by tests; sessions just let it die."""
        with self._lock:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
                self._server = None
                self._port = None

    @property
    def port(self) -> Optional[int]:
        return self._port

    def base_url(self) -> str:
        """OpenAI-style base URL Hermes should point at (``/responses`` appended)."""
        return f"http://127.0.0.1:{self.start()}/v1"


__all__ = ["MantleProxy", "DEFAULT_REGION"]
