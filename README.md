# hermes-provider-bedrock

Hermes providers reach Amazon Web Services (AWS) Bedrock through the native
Converse application programming interface (API) and Mantle. `bedrock-personal`
serves personal Claude and GPT through native Converse, with one account and one provider.

## Contents

Three directories register **three** providers, one per directory. Authentication
uses AWS Identity and Access Management (IAM) credentials and Signature Version 4 (SigV4).

| Path | Provider | Model family and API | Account profile | Region |
|---|---|---|---|---|
| `bedrock/` | `bedrock` | Internal Claude, native Converse | claude-code | `us-west-2` |
| `bedrock-personal/` | `bedrock-personal` | Personal Claude and GPT, native Converse | personal | `us-east-1` |
| `bedrock-mantle/` | `bedrock-mantle` | Internal GPT-5.6 Sol/Luna/Terra, Responses | codex | `us-east-2` |
| `_bedrock_accounts.py` | — | Shared profile/region/model helpers | — | — |

The live config also includes `kiro`, which comes from a different repo.
`bedrock-mantle/__init__.py` registers only `bedrock-mantle`, with a signing proxy on port `8791`.

Personal GPT now uses `bedrock-personal`, whose display name is
"Bedrock: Personal Claude + GPT (my own acct)". The migration removed
`bedrock-mantle-personal` (port `8792`) and `bedrock-mantle-west` (port `8793`).

`bedrock` and `bedrock-personal` are **separate implementations**, not one plugin
with different config. `bedrock` patches the Bedrock adapter, forces the
Converse API and filters discovery. `bedrock-personal` patches credentials by region.
Do not assume that a change to one applies to the other.

`bedrock-mantle` shares nothing with the other two beyond the region constant.
It runs its own SigV4 proxy on a loopback port.

## Layout matters

The repo root maps onto `~/.hermes/plugins/model-providers/`. `bedrock` and
`bedrock-personal` load the shared module with

```python
Path(__file__).resolve().parent.parent / "_bedrock_accounts.py"
```

so `_bedrock_accounts.py` **must stay one level above the provider dirs**. Moving
it into a package or a subdirectory breaks both providers at import time.

## Install

```sh
cp -r bedrock bedrock-mantle bedrock-personal ~/.hermes/plugins/model-providers/
cp _bedrock_accounts.py ~/.hermes/plugins/model-providers/
```

The live plugin directory is a copy, not a clone. It does not sync automatically.

Compare both copies before you copy changes into this repo. Preserve any changes
that another session copied. Copy your changes back before you commit.

### Required Hermes host changes

A fresh Hermes install needs two core changes outside this repo.

| Core file and symbol | Required value | Reason |
|---|---|---|
| `agent/bedrock_adapter.py`: `BEDROCK_OPENAI_RESPONSES_MODEL_IDS` | Keep only `openai.gpt-5.5` | GPT-5.6 and Astra use native Converse. The old list forces bare Sol/Terra/Luna ids through Mantle. |
| `agent/model_metadata.py`: `DEFAULT_CONTEXT_LENGTHS` | Add `"gpt-6-astra": 1050000` | Without this key, Astra falls back to `CONTEXT_PROBE_TIERS[0]`, or 256,000 tokens. |

The adapter allowlist uses exact-string matches. The `us.`-prefixed ids bypass it,
but bare ids need the host correction. Native GPT requires the `us.` inference-profile form.

Publish Astra's 1,050,000-token total window. Hermes subtracts the 128,000-token
output reserve itself and derives a 922,000-token effective input budget.

When you move from Mantle to native Converse, start a clean session. Mantle returns
replayable Responses reasoning items. Native Converse returns a base64
`redactedContent` blob, not replayable plaintext. A Mantle-Astra session cannot
continue on native-Astra.

## Configuration

Every account reference is a **local AWS config profile name**, resolved through
your own `~/.aws/config`. Override any of them:

| Variable | Applies to | Default | Purpose |
|---|---|---|---|
| `HERMES_BEDROCK_PROFILE` | `bedrock` | `claude-code-DO-NOT-DELETE` | Profile for internal Claude |
| `HERMES_MANTLE_AWS_PROFILE` | `bedrock-mantle` | `codex-DO-NOT-DELETE` | Profile that signs Mantle requests |
| `HERMES_MANTLE_REGION` | `bedrock-mantle` | `us-east-2` | Mantle region. This region serves Sol, Luna, and Terra. |
| `HERMES_MANTLE_PROXY_PORT` | `bedrock-mantle` | `8791` | Loopback port for the signing proxy. If the port is taken, the proxy binds an ephemeral port. |

For diagnostics, `account_id_for_profile()` resolves an AWS account number at runtime. This repo contains no account numbers.

Register `bedrock-mantle` for the command-line interface:

```bash
hermes config set providers.bedrock-mantle.name      "Bedrock Mantle (GPT-5.6)"
hermes config set providers.bedrock-mantle.base_url  "http://127.0.0.1:8791/v1"
hermes config set providers.bedrock-mantle.transport "codex_responses"
```

```bash
hermes -z "hello" --provider bedrock-mantle -m openai.gpt-5.6-sol
```

The `--provider` flag reads the `providers:` config rather than the `providers/`
profile registry. The `base_url` port must match `HERMES_MANTLE_PROXY_PORT`.

## Why `bedrock-mantle` needs a proxy

Mantle signs every request with SigV4, and Hermes sends one static bearer token.
A SigV4 signature covers the method, path, headers, and a SHA-256 body digest, so
the signature must be recomputed per request. The `hermes_cli/proxy` adapter
contract (`UpstreamCredential`) is bearer-only by design. GPT-5.6 on Mantle also
requires the Responses API: `POST /v1/chat/completions` returns HTTP 400
`"The model 'openai.gpt-5.6-sol' does not support the '/v1/chat/completions' API"`.

```
Hermes ──POST http://127.0.0.1:8791/v1/responses──> proxy.py ──SigV4──> bedrock-mantle.us-east-2.api.aws/openai/v1/responses
```

The native personal path reaches `bedrock-runtime` directly and needs no proxy.

Three proxy properties are load-bearing. `proxy.py` documents why:

* It signs and sends only `host`, `content-type`, and botocore's `x-amz-*`.
  Forwarding client headers breaks the signature with
  `401 "The request signature we calculated does not match"`.
* It binds `127.0.0.1` only and discards any inbound `Authorization`, because an
  exposed port is an unauthenticated hole into your AWS credentials.
* It answers `GET /v1/models` locally from a curated catalog. Upstream discovery
  needs `bedrock-mantle:ListModels`, which the codex account denies, and
  `BaseHTTPRequestHandler` returns HTTP 501 for a verb with no `do_<VERB>`
  method. That 501 broke the WebUI *Test* button while inference worked, so
  `bedrock-mantle` declares `supports_health_check=True` on the strength of this
  route.

## Native GPT request fields

Native Converse takes reasoning effort through `additionalModelRequestFields`.
Only one shape works:

| Request | Result |
|---|---|
| `additionalModelRequestFields={"reasoning":{"effort":"high"}}` | Works. `low`, `medium`, `high`, and `xhigh` are all accepted. |
| Top-level `reasoning_effort` | `"Unknown parameter: 'reasoning_effort'"` |
| `{"reasoning":{"effort":{...}}}` as an object | `"Invalid type for 'reasoning.effort'"` |
| Tool use | Returns a `toolUse` block with `stopReason: "tool_use"` |
| Image input | Works. A degenerate 1x1 PNG returns `"Invalid or unsupported image format"`, which the image causes. |
| Reasoning content | Returns a base64 `redactedContent` blob, not replayable plaintext |

## Why one account per tool

The internal provider split reflects an entitlement boundary. The claude-code
profile cannot serve GPT: `bedrock:InvokeModel` returns `AccessDeniedException`
for every `openai.*` id, and discovery lists none.

The personal account serves Claude and all four GPT models through one
`bedrock-personal` provider. Native Converse in either `us-east-1` or `us-west-2`
serves Astra, Sol, Terra, and Luna together. Mantle availability splits by region:
`us-east-2` serves Sol but not Astra, while `us-west-2` serves Astra but not Sol.
That split is why the remaining Mantle listener stays pinned to `us-east-2`.
`bedrock-mantle/__init__.py` records the live probe results per region.

Three traps, all verified against the live service:

* **The claude-code profile cannot serve GPT at all.** `bedrock:InvokeModel` returns
  `AccessDeniedException` for every `openai.*` id, and discovery lists none. Mantle
  denies `bedrock-mantle:CreateInference` for the same role. The codex
  documentation files this failure class under *"Unauthorized with explicit deny in
  service control policy"*.
* **A denied `ListModels` does not mean no access.** The codex account denies discovery but permits `CreateInference`.
  A discovery-based catalog reports zero models on a working account. Test a real completion.
* **Client-cache collision.** The core Bedrock adapter caches boto3 clients by region only.
  Two accounts in one region share whichever client enters the cache first, so one account pays for both.
  Internal Claude uses `us-west-2`. Personal Claude and GPT share the `us-east-1`
  client with the same account credentials. These regions keep the accounts in distinct cache slots.

The sibling `bedrock` plugin sets `AWS_PROFILE` at import time, and Mantle denies
that role, so `bedrock-mantle` passes its profile to botocore explicitly rather
than using the default credential chain.

## Model catalog policy

Claude model ids are **discovered, never hardcoded**. `latest_per_family()`
reduces a discovered list to the newest release per family and orders families by capability tier.
Newly entitled Claude models reach the picker through discovery. The repo does
not need unreleased Claude model names.

If discovery cannot run, `CLAUDE_SEED_MODELS` supplies a last-resort seed. It names only
long-public models. An unrecognised family sorts between opus and sonnet rather
than last, so the picker keeps newly entitled families visible.

Native GPT is a deliberate exception. `_discover_claude_models` filters to
`us.anthropic.*`, so discovery cannot supply GPT ids. `GPT_MODELS` in
`bedrock-personal/__init__.py` must name every GPT id in source:

| Native Converse model id |
|---|
| `us.openai.gpt-6-astra` |
| `us.openai.gpt-5.6-sol` |
| `us.openai.gpt-5.6-terra` |
| `us.openai.gpt-5.6-luna` |

`bedrock-mantle` serves Sol, Luna, and Terra at 1,050,000 context and 128,000 max
output. `fetch_models()` returns that curated list rather than querying
`/v1/models`, because the upstream route also lists Chat-Completions-only models
that a `codex_responses` provider cannot serve. There is no plain `openai.gpt-5.6`
id: it ships as the three named variants only.

## Where the evidence lives

Measurements, live probe results, and the reasoning behind each constant live in
the module docstrings next to the code they constrain, not in this file:

| Question | Read |
|---|---|
| Per-region model availability, Astra context math, encrypted-reasoning scoping, capability probes | `bedrock-mantle/__init__.py` |
| SigV4 header hygiene, the 501 route history, loopback binding | `bedrock-mantle/proxy.py` |
| Region-scoped credential patching | `bedrock-personal/__init__.py` |
| Adapter patching, User-Agent IAM condition, discovery filtering | `bedrock/__init__.py` |
| Profile and region map, capability matrix | `_bedrock_accounts.py` |

Commit messages carry the dated provenance for each change.

## Tests

```sh
python -m pytest -q        # 64 tests, hermetic — no credentials, no network
```

`bedrock-mantle/test_proxy.py` covers the signing proxy against a stub upstream
(path rewrite, SigV4 header hygiene, streaming relay, error passthrough, loopback
binding, local `/v1/models`). `test_accounts.py` covers the shared helpers (id
parsing, capability ordering, profile/region wiring).

Both are real pytest modules. Previously, `test_proxy.py` collected **zero** tests
under pytest but passed as a standalone script. The continuous integration (CI)
job reported success against untested code.
