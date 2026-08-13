# hermes-provider-bedrock

Hermes model providers for the three Amazon Bedrock access paths, plus the shared
account module they import.

> **PRIVATE repo. Do not make it public as-is.**
> It contains AWS account IDs, internal IAM role names, a personal account
> reference, and an unreleased model codename. See [Before publishing](#before-publishing).

## Contents

| Path | Provider(s) | Model family | Auth |
|---|---|---|---|
| `bedrock/` | `bedrock` | Claude (Converse API) | corp claude-code role, IAM |
| `bedrock-personal/` | `bedrock-personal` | Claude (Converse API) | personal account, IAM |
| `bedrock-mantle/` | `bedrock-mantle`, `bedrock-mantle-personal` | GPT-5.6 Sol/Luna/Terra (Responses API) | SigV4, no API key |
| `_bedrock_accounts.py` | — | shared account/region/model map | — |

Three directories register **four** providers: `bedrock-mantle/__init__.py`
declares a second profile, `bedrock-mantle-personal`, that signs with the personal
account instead of the corp codex one. There is no fourth directory to look for.

`bedrock` and `bedrock-personal` are **separate implementations**, not one plugin
with different settings. `bedrock` patches the Bedrock adapter, forces the
Converse API and filters discovery; `bedrock-personal` does region-scoped profile
patching only. Do not assume a change to one applies to the other.

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

The live plugin directory is a copy, not a clone — nothing syncs automatically.

```sh
cp -r bedrock bedrock-mantle bedrock-personal ~/.hermes/plugins/model-providers/
cp _bedrock_accounts.py ~/.hermes/plugins/model-providers/
```

Copy changes back into this repo before committing, and diff both ways first: the
repo can already hold changes another session synced.

## Why one account per tool

Amazon vends a separate AWS account per tool and they are not interchangeable.
One account cannot serve GPT at all (`CreateInference` denied), so the provider
split is an entitlement boundary, not a preference. `_bedrock_accounts.py` holds
the verified matrix.

The providers also avoid a **client-cache collision**: the core Bedrock adapter
caches boto3 clients keyed by region only, so two accounts serving the same model
ids in the same region would share one client and whichever warmed the cache
first would bill both. The accounts differ in regional reach, which supplies a
natural key — corp Claude stays on us-west-2, personal Claude is pinned to
us-east-1.

## Tests

```sh
cd bedrock-mantle && python test_proxy.py     # runs; prints PASS lines
```

`test_proxy.py` is a **hand-rolled script**, not a pytest module. It defines
`check()` and `main()` and collects **zero tests** under `pytest -q`, so a CI job
that shells out to pytest reports success while testing nothing. `bedrock` and
`bedrock-personal` have no tests at all.

## Before publishing

Each item is a real change, not a redaction pass.

1. **Remove the unreleased model codename.** `claude-fable-5` is hardcoded in 8
   places across `bedrock/__init__.py`, `bedrock-personal/__init__.py` and
   `_bedrock_accounts.py`, including a regex family group and the max-output
   table. The catalog labels it internal, development-use-only, not for customer
   data. Derive the family list and ordering from live capability instead, the way
   `hermes-provider-kiro` does, so the source never names it.
2. **Remove the three AWS account IDs** (7 files), including the account/role
   permission matrix in `bedrock-mantle/README.md` and the reST table in
   `_bedrock_accounts.py`. Describe roles by capability, not by name and number.
3. **Remove the personal account reference.** `bedrock-personal/plugin.yaml`
   names a personal ALPHA account and its ID.
4. **Convert `test_proxy.py` to real pytest** and add coverage for the two Claude
   providers.

Reference for the shape of a publishable version: `samfoy/pi-bedrock-mantle` is
public with zero account IDs, keeping only generic `*-DO-NOT-DELETE` profile names
as troubleshooting examples.
