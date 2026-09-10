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
continue on native-Astra. See [native GPT findings](bedrock-mantle/README.md#native-gpt-on-the-personal-account-2026-09-0910)
for request fields, exact errors, and capability tests.

## Configuration

Every account reference is a **local AWS config profile name**, resolved through
your own `~/.aws/config`. Override any of them:

| Variable | Applies to | Purpose |
|---|---|---|
| `HERMES_BEDROCK_PROFILE` | `bedrock` | Profile for internal Claude |
| `HERMES_MANTLE_AWS_PROFILE` | `bedrock-mantle` | Profile that signs Mantle requests |
| `HERMES_MANTLE_REGION` | `bedrock-mantle` | Mantle region |
| `HERMES_MANTLE_PROXY_PORT` | `bedrock-mantle` | Loopback port for the signing proxy |

For diagnostics, `account_id_for_profile()` resolves an AWS account number at runtime. This repo contains no account numbers.

## Why one account per tool

The internal provider split reflects an entitlement boundary. The claude-code
profile cannot serve GPT: `bedrock:InvokeModel` returns `AccessDeniedException`
for every `openai.*` id, and discovery lists none.

The personal account serves Claude and all four GPT models through one
`bedrock-personal` provider. Native Converse in either `us-east-1` or `us-west-2`
serves Astra, Sol, Terra, and Luna together. Mantle availability splits by region:
`us-east-2` serves Sol but not Astra, while `us-west-2` serves Astra but not Sol.
See [the capability matrix](bedrock-mantle/README.md#the-bedrock-accounts-are-not-interchangeable).

Two traps that matrix records, both verified against the live service:

* **A denied `ListModels` does not mean no access.** The codex account denies discovery but permits `CreateInference`.
  A discovery-based catalog reports zero models on a working account. Test a real completion.
* **Client-cache collision.** The core Bedrock adapter caches boto3 clients by region only.
  Two accounts in one region share whichever client enters the cache first, so one account pays for both.
  Internal Claude uses `us-west-2`. Personal Claude and GPT share the `us-east-1`
  client with the same account credentials. These regions keep the accounts in distinct cache slots.

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
