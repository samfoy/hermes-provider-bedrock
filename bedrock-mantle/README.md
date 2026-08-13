# bedrock-mantle (Hermes model provider)

GPT-5.6 (**Sol / Luna / Terra**, ~1M context) on **Amazon Bedrock Mantle**, authenticated with **SigV4** — no long-term API key.

## Why this plugin exists

Hermes already has *some* Mantle support (`hermes model` → Bedrock API Key), but it cannot reach GPT-5.6. Two independent blockers, both verified against the live service:

1. **GPT-5.6 is Responses-API only.**

   ```
   POST /v1/chat/completions  {"model": "openai.gpt-5.6-sol", ...}
   -> 400 "The model 'openai.gpt-5.6-sol' does not support the '/v1/chat/completions' API"
   ```

   The built-in flow configures a `custom` provider on `/v1` in `chat_completions` mode, so it cannot serve these models.

2. **Mantle needs per-request SigV4; Hermes sends one static bearer.** A SigV4 signature covers the method, path, headers and a SHA-256 of the body, so it must be recomputed per request. The built-in `hermes_cli/proxy` adapter contract (`UpstreamCredential`) is bearer-only by design.

This plugin closes both gaps: a **loopback SigV4-signing proxy** plus a provider profile in `codex_responses` mode.

```
Hermes ──POST http://127.0.0.1:8791/v1/responses──> proxy.py ──SigV4──> bedrock-mantle.us-east-2.api.aws/openai/v1/responses
```

## Setup

Nothing to configure for auth — the plugin defaults to the `codex-DO-NOT-DELETE`
profile, which is the account Amazon provisions specifically for the internal
GPT models. Its `credential_process` shells out to the codex wrapper, so it
auto-refreshes and needs no `ada credentials update`.

Register the provider so `--provider bedrock-mantle` resolves on the CLI. The
`providers/` profile registry is not consulted by the `--provider` flag chain,
so a `providers:` entry is required:

```bash
hermes config set providers.bedrock-mantle.name      "Bedrock Mantle (GPT-5.6)"
hermes config set providers.bedrock-mantle.base_url  "http://127.0.0.1:8791/v1"
hermes config set providers.bedrock-mantle.transport "codex_responses"
```

Use it:

```bash
hermes -z "hello" --provider bedrock-mantle -m openai.gpt-5.6-sol
```

## The Bedrock accounts — they are not interchangeable

Amazon vends a **separate account per tool**, and each is authorized for a
different model family. Verified 2026-08-04:

| Profile | Mantle GPT inference | Claude on Bedrock |
|---|---|---|
| codex (GPT-provisioned) | **✅** | ✅ |
| claude-code (Claude-provisioned) | ❌ `CreateInference` denied | ✅ |
| personal | ✅ | ✅ |

Consequences worth knowing:

* **The claude-code profile cannot serve GPT at all** — `bedrock-mantle:CreateInference`
  is explicitly denied. This is the same class of failure the codex docs
  describe under *"Unauthorized with explicit deny in service control policy"*.
* **The codex profile is denied `bedrock-mantle:ListModels` but allowed
  `CreateInference`.** Discovery 401s while inference succeeds, which is why
  `fetch_models()` returns a curated list — a discovery-based catalog would
  report zero models on a perfectly working account. Never conclude a profile
  lacks Mantle access from a failed `ListModels`; test a real completion.
* **The personal profile works for both but bills the user personally.**
  Prefer the purpose-built codex profile for GPT traffic.

Profile names are whatever the local AWS config calls them. Set
`HERMES_MANTLE_AWS_PROFILE` to point at your own.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_MANTLE_AWS_PROFILE` | `codex-DO-NOT-DELETE` | Profile that signs requests. Override to use `claude` (personal) if the codex account loses access. |
| `HERMES_MANTLE_REGION` | `us-east-2` | Mantle region. GPT-5.6 is served from us-east-2, matching the codex wrapper's own default. |
| `HERMES_MANTLE_PROXY_PORT` | `8791` | Loopback port. Must match the `base_url` above. Falls back to an ephemeral port if taken. |

## Models

| Model | Context | Max output |
|---|---|---|
| `openai.gpt-5.6-sol` | 1,000,000 | 128,000 |
| `openai.gpt-5.6-luna` | 1,000,000 | 128,000 |
| `openai.gpt-5.6-terra` | 1,000,000 | 128,000 |
| `openai.gpt-5.5` | 272,000 | 128,000 |
| `openai.gpt-5.4` | 272,000 | 128,000 |

`fetch_models()` deliberately returns this curated list instead of querying `/v1/models`: that route also lists Chat-Completions-only models (DeepSeek, Qwen, Kimi, ...) which this profile cannot serve in `codex_responses` mode, and it requires `bedrock-mantle:ListModels`, which some roles lack.

### The proxy serves `GET /v1/models` itself

The proxy answers `GET /v1/models` locally with the same curated catalog. It does
not forward that route upstream, for the two reasons above: upstream discovery
needs `bedrock-mantle:ListModels` (denied on the codex account), and the upstream
list includes models this profile cannot serve.

This route is load-bearing for the GUI. `BaseHTTPRequestHandler` answers any verb
without a `do_<VERB>` method with **HTTP 501**, so before the handler existed, the
WebUI *Custom Endpoints -> Test* button — which probes `{base_url}/models` — showed
`Endpoint returned HTTP 501.` on a provider whose inference worked perfectly. The
same 501 came back for `GET /` and `GET /v1`.

Properties to preserve:

* `GET` never reaches the signer, so a probe costs no AWS credentials.
* Trailing slashes and query strings resolve to the same route.
* An unknown `GET` path returns 404, not 501.
* Both providers declare `supports_health_check=True` because of this route.

## Verified findings (2026-08-04, live us-east-2)

* **There is no plain `openai.gpt-5.6`.** It ships as three named variants: `-sol`, `-luna`, `-terra`. Any config naming `openai.gpt-5.6` will fail.
* **The ~1M context is real and specific to 5.6.** From the service's own validation error on an oversized prompt:

  | Model | Reported maximum |
  |---|---|
  | `gpt-5.6-sol` / `-luna` / `-terra` | **1,050,000** |
  | `gpt-5.5` / `gpt-5.4` | 278,528 |

  The catalog publishes `1,000,000` — under the true ceiling, so token-estimate drift cannot push a request over. This mirrors the existing convention of listing 5.5 as 272,000 against a real 278,528.
* Internal docs lag reality here. A July 16 note said 1M was "in the works"; a July 28 guide and a benchmark writeup both still list 272K for 5.6. The live endpoint is authoritative.
* `reasoning.effort: "xhigh"` is accepted and echoed back.
* Image input works via the `source` block form, so `supports_vision=True`.

## Pitfalls

* **Sign a fixed header set — never forward client headers.** SigV4 hashes exactly the headers named in `SignedHeaders`, and every one must reach the wire unchanged. Two live failure modes, both `401 "The request signature we calculated does not match"`:
  * Forwarding `accept` / `user-agent` put them in `SignedHeaders`, but Mantle's canonical string only covers `content-type;host;x-amz-date;x-amz-security-token`.
  * Setting both `Content-Type` and `content-type` created a duplicate that urllib collapsed *after* signing, invalidating the digest.

  The proxy therefore signs and sends only `host` + `content-type` + botocore's `x-amz-*`.
* **The proxy is an unauthenticated hole into your AWS credentials.** It binds `127.0.0.1` only and discards any inbound `Authorization`. Do not add a bind-address option.
* **The sibling `bedrock` plugin hijacks `AWS_PROFILE`** at import time, forcing `claude-code-DO-NOT-DELETE` — a role that Mantle denies. This plugin therefore passes its profile explicitly to botocore rather than relying on the ambient environment. Do not "simplify" that to the default credential chain.
* **A denied `ListModels` does not mean no access.** The codex account 401s on discovery while inference works fine. Test `CreateInference` (an actual completion) before concluding an account lacks Mantle access.

## Testing

`test_proxy.py` covers path rewriting, header hygiene, streaming relay and error pass-through against a stub upstream (no credentials or network needed):

```bash
python3 test_proxy.py
```
