# bedrock-mantle (Hermes model provider)

This provider serves internal GPT-5.6 (**Sol / Luna / Terra**, 1,050,000 context) through **Amazon Web Services (AWS) Bedrock Mantle**.
Its single listener uses **AWS Signature Version 4 (SigV4)** authentication.
Personal Claude and GPT, including **Astra**, use `bedrock-personal` through the native Bedrock Converse application programming interface (API).

## Why this plugin exists

The internal GPT path needs a SigV4 proxy and the Responses API. The built-in
Mantle flow (`hermes model` → Bedrock API Key) encounters two verified blockers:

1. **GPT-5.6 on Mantle requires the Responses API.**

   ```
   POST /v1/chat/completions  {"model": "openai.gpt-5.6-sol", ...}
   -> 400 "The model 'openai.gpt-5.6-sol' does not support the '/v1/chat/completions' API"
   ```

   The built-in flow configures a `custom` provider on `/v1` in `chat_completions` mode, so it cannot serve these models.

2. **Mantle needs per-request SigV4. Hermes sends one static bearer.** A SigV4 signature covers the method, path, headers, and a SHA-256 body digest. The proxy must recompute the signature for each request. The built-in `hermes_cli/proxy` adapter contract (`UpstreamCredential`) is bearer-only by design.

This plugin uses one **loopback SigV4-signing proxy** and one provider in
`codex_responses` mode. The native personal path reaches `bedrock-runtime`
directly and needs neither a proxy nor a separate listener.

```
Hermes ──POST http://127.0.0.1:8791/v1/responses──> proxy.py ──SigV4──> bedrock-mantle.us-east-2.api.aws/openai/v1/responses
```

## Setup

The internal listener defaults to the `codex-DO-NOT-DELETE` profile in `us-east-2`.
Amazon provisions this account for the internal GPT models.

Its `credential_process` uses the codex wrapper. It refreshes automatically and needs no `ada credentials update`.

The `--provider` flag chain reads the `providers:` config rather than the
`providers/` profile registry.

Register the provider for the command-line interface (CLI):

```bash
hermes config set providers.bedrock-mantle.name      "Bedrock Mantle (GPT-5.6)"
hermes config set providers.bedrock-mantle.base_url  "http://127.0.0.1:8791/v1"
hermes config set providers.bedrock-mantle.transport "codex_responses"
```

Use it:

```bash
hermes -z "hello" --provider bedrock-mantle -m openai.gpt-5.6-sol
```

## Why one listener remains

Only the internal Mantle listener remains. Personal GPT moved to native Converse
through `bedrock-personal` on `us-east-1`.

| Provider | Port | Region | Profile | Models |
|---|---:|---|---|---|
| `bedrock-mantle` | `8791` | `us-east-2` | `codex-DO-NOT-DELETE` | `openai.gpt-5.6-sol`, `openai.gpt-5.6-luna`, `openai.gpt-5.6-terra` |

The migration removed `bedrock-mantle-personal` (port `8792`) and
`bedrock-mantle-west` (port `8793`). Native Converse serves Astra, Sol, Terra, and
Luna together in either `us-east-1` or `us-west-2`. The personal provider uses
`us-east-1` to keep its client cache separate from internal Claude on `us-west-2`.

Mantle availability depends on the region. The 2026-09-09 results below use Hypertext Transfer Protocol (HTTP) status codes:

| Region | Models served | Models rejected |
|---|---|---|
| `us-east-2` | `openai.gpt-5.6-sol`, `openai.gpt-5.6-luna`, `openai.gpt-5.6-terra` | `openai.gpt-6-astra`: HTTP 404 `"The model 'openai.gpt-6-astra' does not exist"` |
| `us-west-2` | `openai.gpt-6-astra`, `openai.gpt-5.6-luna`, `openai.gpt-5.6-terra` | `openai.gpt-5.6-sol`: HTTP 404 |

The remaining Mantle listener stays on `us-east-2` for Sol. Native Converse removes
this regional model split for personal GPT, so the personal account needs only one provider.

## The Bedrock accounts are not interchangeable

The internal accounts have different entitlements. Amazon vends a **separate
account per tool**. The personal account serves both Claude and GPT.
The 2026-08-04 tests established the account boundary. Tests on 2026-09-09/10
verified the native personal path and the internal Astra denial.

| Profile | Mantle GPT inference | Claude on Bedrock |
|---|---|---|
| codex (GPT-provisioned) | GPT-5.6 works. An organization policy denies Astra. | ✅ |
| claude-code (Claude-provisioned) | ❌ `CreateInference` denied | ✅ |
| personal | GPT-5.6 and Astra work. This setup now uses native Converse. | ✅ |

* **The claude-code profile cannot serve GPT at all.** Mantle denies `bedrock-mantle:CreateInference`.
  Native `bedrock:InvokeModel` returns `AccessDeniedException` for every `openai.*` id, and discovery lists none.
  The codex docs describe this failure class under *"Unauthorized with explicit deny in service control policy"*.
* **The codex profile is denied `bedrock-mantle:ListModels` but allowed `CreateInference`.** Discovery returns 403 while inference succeeds.
  `fetch_models()` therefore returns a curated list. A discovery-based catalog reports zero models on a working account.
  A failed `ListModels` request does not prove that a profile lacks Mantle access. Test a real completion.
* **The personal profile serves GPT-5.6 and Astra, but it bills the user personally.**
  Use `bedrock-mantle` for internal GPT-5.6 traffic. Use `bedrock-personal` for personal GPT, including Astra.

Profile names match the local AWS config. Set
`HERMES_MANTLE_AWS_PROFILE` to point at your own.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_MANTLE_AWS_PROFILE` | `codex-DO-NOT-DELETE` | Profile that signs requests. If the codex account loses access, override this value with `claude` (personal). |
| `HERMES_MANTLE_REGION` | `us-east-2` | Mantle region. This region serves Sol/Luna/Terra and matches the codex wrapper's default. |
| `HERMES_MANTLE_PROXY_PORT` | `8791` | Loopback port. Match the `base_url` above. If the port is taken, the proxy uses an ephemeral port. |

## Models

`fetch_models()` returns Sol, Luna, and Terra. The code retains metadata for 5.5
and 5.4, but the current listener does not list them.

| Model | Context | Max output | In listener catalog |
|---|---|---|---|
| `openai.gpt-5.6-sol` | 1,050,000 | 128,000 | Yes |
| `openai.gpt-5.6-luna` | 1,050,000 | 128,000 | Yes |
| `openai.gpt-5.6-terra` | 1,050,000 | 128,000 | Yes |
| `openai.gpt-5.5` | 272,000 | 128,000 | No |
| `openai.gpt-5.4` | 272,000 | 128,000 | No |

`fetch_models()` returns the curated catalog instead of a query to `/v1/models`. That route also lists Chat-Completions-only models, including DeepSeek, Qwen, and Kimi. This profile cannot serve those models in `codex_responses` mode. The route also requires `bedrock-mantle:ListModels`, which some roles lack.

### Model context metadata

The proxy advertises each context window through the `context_length` field on `GET /v1/models`. This field controls the Hermes context limit.

Native Astra needs the host entry `"gpt-6-astra": 1050000` in
`agent/model_metadata.py`, under `DEFAULT_CONTEXT_LENGTHS`.
Hermes uses the longest substring match. Without that entry, Astra matches no
key and falls back to `CONTEXT_PROBE_TIERS[0]`, which is 256,000.

A fresh host also needs `agent/bedrock_adapter.py` to keep only `openai.gpt-5.5`
in `BEDROCK_OPENAI_RESPONSES_MODEL_IDS`. The old four-entry list forces bare
Sol/Terra/Luna ids through Mantle. The `us.`-prefixed native ids bypass this
exact-string match. See [required host changes](../README.md#required-hermes-host-changes).

The field name must remain `context_length`. Hermes reads `max_tokens` as the maximum output token count.

### The proxy serves `GET /v1/models` itself

The proxy answers `GET /v1/models` locally with the same curated catalog.
Upstream discovery needs `bedrock-mantle:ListModels`, which the codex account denies.
The upstream list also includes models that this profile cannot serve.

This route is load-bearing for the graphical user interface (GUI). `BaseHTTPRequestHandler` returns **HTTP 501** for verbs without a `do_<VERB>` method.

Before the handler existed, the WebUI *Custom Endpoints -> Test* button probed `{base_url}/models`. It showed `Endpoint returned HTTP 501.` while inference worked.

The same 501 came back for `GET /` and `GET /v1`.

Properties to preserve:

* `GET` never reaches the signer, so a probe costs no AWS credentials.
* Trailing slashes and query strings resolve to the same route.
* An unknown `GET` path returns 404, not 501.
* `bedrock-mantle` declares `supports_health_check=True` because of this route.

## Verified findings (2026-08-04, live us-east-2)

* **There is no plain `openai.gpt-5.6`.** It ships as three named variants: `-sol`, `-luna`, `-terra`. Any config naming `openai.gpt-5.6` will fail.
* **The ~1M context is real and specific to 5.6.** From the service's own validation error on an oversized prompt:

  | Model | Reported maximum |
  |---|---|
  | `gpt-5.6-sol` / `-luna` / `-terra` | **1,050,000** |
  | `gpt-5.5` / `gpt-5.4` | 278,528 |

  The proxy publishes the full 1,050,000-token total window for GPT-5.6.
  Its Astra metadata specifies the same total. The input limit is 922,000 tokens,
  with 128,000 reserved for output.

  An earlier version published `1,000,000` to leave headroom against token-estimate drift.
  The code dropped that margin.

  The 5.5 metadata still uses 272,000 against a measured 278,528.
* Internal docs lag reality here. A July 16 note said 1M was "in the works". A July 28 guide and a benchmark writeup still list 272K for 5.6. The live endpoint is authoritative.
* Mantle accepts `reasoning.effort: "xhigh"` and echoes it back.
* Image input works through the `image_url` data-URL form, so `supports_vision=True`. The service accepts the `source` block form but silently ignores it. The model cannot see the image.

## Native GPT on the personal account (2026-09-09/10)

`bedrock-personal` serves Astra, Sol, Terra, and Luna through `bedrock-runtime`
Converse. Live tests verified all four in both `us-east-1` and `us-west-2`.
The provider uses `us-east-1` and shares one account credential and client with personal Claude.

The native path requires the `us.` inference-profile prefix. `GPT_MODELS` names
`us.openai.gpt-6-astra`, `us.openai.gpt-5.6-sol`, `us.openai.gpt-5.6-terra`, and
`us.openai.gpt-5.6-luna`. This source list is deliberate: `_discover_claude_models`
filters to `us.anthropic.*` and cannot supply GPT ids. Claude still uses discovery.

A bare `openai.gpt-6-astra` returns:

```
"Invocation of model ID openai.gpt-6-astra with on-demand throughput isn't supported. Retry your request with the ID or ARN of an inference profile that contains this model."
```

### Native capabilities and request fields

Native Converse supports text, tool use, and image input on the personal account.
The request field for reasoning effort is `additionalModelRequestFields`:

```json
{"reasoning":{"effort":"high"}}
```

| Capability or request | Live result |
|---|---|
| Text | Works |
| Tool use | Returns a real `toolUse` block with `stopReason: "tool_use"` |
| Image input | A 32x32 Portable Network Graphics (PNG) image succeeds. A degenerate 1x1 PNG returns `"Invalid or unsupported image format"`. The image causes the failure. |
| `additionalModelRequestFields={"reasoning":{"effort":"high"}}` | Works |
| Top-level `reasoning_effort` | Returns `"Unknown parameter: 'reasoning_effort'"` |
| `{"reasoning":{"effort":{...}}}` | Returns `"Invalid type for 'reasoning.effort'"` |
| Reasoning content | Returns a base64 `redactedContent` blob, not replayable plaintext |

### Latency

Warm latency is comparable across both paths. Each path ran three times against
the same trivial prompt. Run 1 is a cold start. Runs 2 and 3 are warm.

| Path | Run 1 (cold) | Run 2 (warm) | Run 3 (warm) |
|---|---|---|---|
| Native Converse | 42.32s | 2.01s | 2.19s |
| Mantle proxy | 20.13s | 3.53s | 1.24s |

Keep cold-start measurements separate from steady-state latency. An earlier pair
of one-off probes on the same two paths returned 23s for native Converse and 82s
for the Mantle proxy, both cold.

## GPT-6 Astra findings (2026-09-09)

This setup now reaches Astra through `bedrock-personal` over native Converse.
The findings below describe the earlier Mantle tests, not a current Astra listener.

The internal `codex-DO-NOT-DELETE` account returns HTTP 403 in `us-west-2` for the Mantle Astra path.
The response cites an explicit deny in an organization-level service control policy (SCP). It names action `bedrock-mantle:CreateInference` and resource `project/default`.

The plugin cannot change this organization policy. The same codex account serves `openai.gpt-5.6-luna` in `us-west-2`, so the deny is specific to Astra.

Astra's total context window is 1,050,000 tokens. Hermes uses this total for both paths.

Publish the total in host metadata and any `GET /v1/models` catalog, never the input limit.

| Quantity | Tokens |
|---|---:|
| Total context window | 1,050,000 |
| Input limit | 922,000 |
| Maximum output | 128,000 |

These values satisfy the service equation: 922,000 input tokens + 128,000 output tokens = 1,050,000 total tokens.

Hermes computes `effective_window = context_length - max_tokens` in `agent/context_compressor._compute_threshold_tokens`. The previous configuration published 900,000, which was Astra's measured input limit rather than the total window.

Hermes then reserved 128,000 output tokens a second time. That gave 900,000 - 128,000 = a 772,000-token input budget instead of 922,000.

The old value discarded about 150,000 usable input tokens. With `context_length` set to 1,050,000, Hermes derives exactly 922,000 input tokens.

A live request verified the corrected input budget. Live tests also show that the service caps the total window, not input alone:

* The service accepted a 917,007-token input with `max_output_tokens` set to 16 and 128,000. The input limit did not move.
* With `max_output_tokens` set to 60,000, two requests reached exactly 921,858 `total_tokens`. Their inputs differed by 6,000 tokens.
* Their outputs were 836 and 6,836 tokens. The output difference absorbed the input difference.
* A binary search placed the input boundary between 921,758 and 921,882 tokens. This boundary matches the 922,000 input limit.

The earlier Mantle tests verified these Astra capabilities:

| Capability | Live result |
|---|---|
| Tool calls | Works |
| Server-Sent Events (SSE) streaming | Works |
| Image input | Use the `image_url` data-URL form. The service accepts the `source` block form but silently ignores it. The model cannot see the image. |
| Reasoning effort | `low`, `medium`, `high`, and `xhigh` work. `none` returns HTTP 400. |

### Region-scoped encrypted reasoning

Mantle seals encrypted reasoning per region. A reasoning block from `us-west-2`
cannot replay in `us-east-2`, or the reverse direction. Native Converse instead
returns base64 `redactedContent`, not replayable plaintext. The move requires a clean session.

Astra history replayed to `openai.gpt-5.6-luna` in `us-east-2` returned HTTP 400. The service reports `"encrypted reasoning is scoped to the region that produced it and cannot be replayed in a different region"`.

An `us-east-2` reasoning block replayed to Astra in `us-west-2` returned an opaque HTTP 500 `internal_server_error`. A tampered blob returns `"invalid encrypted reasoning"`.

The former Mantle listeners used separate ports and base URLs for distinct
issuer identities. Hermes stamps the reasoning issuer as `other:{base_url}` in
`agent/codex_responses_adapter._classify_responses_issuer` and drops foreign items during replay.

A live session switched from an `us-east-2` model to Astra. Hermes dropped the
foreign reasoning item and returned the correct answer.
If both regions share one base URL, the issuer check cannot detect the region
change. The request then reaches the HTTP 500 path above.

That Mantle-to-Mantle behavior does not preserve reasoning across the native
migration. A session that starts on Mantle-Astra cannot continue on native-Astra.

## Pitfalls

* **Sign a fixed header set. Never forward client headers.** SigV4 hashes the headers named in `SignedHeaders`. Each header must reach the wire unchanged. Two live failure modes returned `401 "The request signature we calculated does not match"`:
  * The proxy put `accept` / `user-agent` in `SignedHeaders`, but Mantle's canonical string only covers `content-type;host;x-amz-date;x-amz-security-token`.
  * The proxy set both `Content-Type` and `content-type`. urllib collapsed the duplicate *after* signing. This invalidated the digest.

  The proxy therefore signs and sends only `host` + `content-type` + botocore's `x-amz-*`.
* **The proxy is an unauthenticated hole into your AWS credentials.** It binds `127.0.0.1` only and discards any inbound `Authorization`. Do not add a bind-address option.
* **The sibling `bedrock` plugin sets `AWS_PROFILE`** to `claude-code-DO-NOT-DELETE` at import time. Mantle denies that role. This plugin therefore passes its profile explicitly to botocore. Do not use the default credential chain.
* **A denied `ListModels` does not mean no access.** The codex account returns 403 on discovery while inference works. Test `CreateInference` (an actual completion) before concluding an account lacks Mantle access.

## Testing

`test_proxy.py` tests the proxy against a stub upstream. It covers path rewrite,
header hygiene, streaming relay, and error pass-through. The full suite has 64
hermetic tests and needs no credentials or network.

Run the suite from the repo root:

```bash
python -m pytest -q
```
