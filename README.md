# hermes-provider-bedrock

Hermes model providers for the Amazon Bedrock access paths, plus the shared
account module they import.

## Contents

| Path | Provider(s) | Model family | Auth |
|---|---|---|---|
| `bedrock/` | `bedrock` | Claude (Converse API) | claude-code profile, IAM |
| `bedrock-personal/` | `bedrock-personal` | Claude (Converse API) | personal profile, IAM |
| `bedrock-mantle/` | `bedrock-mantle`, `bedrock-mantle-personal` | GPT-5.6 Sol/Luna/Terra (Responses API) | SigV4, no API key |
| `_bedrock_accounts.py` | — | shared profile/region/model helpers | — |

Three directories register **four** providers: `bedrock-mantle/__init__.py`
declares a second profile, `bedrock-mantle-personal`, that signs with a personal
profile instead of the corp one. There is no fourth directory to look for.

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

```sh
cp -r bedrock bedrock-mantle bedrock-personal ~/.hermes/plugins/model-providers/
cp _bedrock_accounts.py ~/.hermes/plugins/model-providers/
```

The live plugin directory is a copy, not a clone — nothing syncs automatically.
Copy changes back into this repo before committing, and diff both ways first: the
repo can already hold changes another session synced.

## Configuration

Every account reference is a **local AWS config profile name**, resolved through
your own `~/.aws/config`. Override any of them:

| Variable | Applies to | Purpose |
|---|---|---|
| `HERMES_BEDROCK_PROFILE` | `bedrock` | Profile for internal Claude |
| `HERMES_MANTLE_AWS_PROFILE` | `bedrock-mantle` | Profile that signs Mantle requests |
| `HERMES_MANTLE_REGION` | `bedrock-mantle` | Mantle region |
| `HERMES_MANTLE_PROXY_PORT` | `bedrock-mantle` | Loopback port for the signing proxy |

No AWS account numbers are committed to this repo. `account_id_for_profile()`
resolves one at runtime when a diagnostic needs it.

## Why one account per tool

Amazon vends a separate AWS account per tool and they are not interchangeable.
One profile cannot serve GPT at all (`CreateInference` denied), so the provider
split is an entitlement boundary, not a preference. See
`bedrock-mantle/README.md` for the capability matrix.

Two traps that matrix records, both verified against the live service:

* **A denied `ListModels` does not mean no access.** The codex profile is denied
  discovery but permitted `CreateInference`, so a discovery-based catalog reports
  zero models on a perfectly working account. Test a real completion.
* **Client-cache collision.** The core Bedrock adapter caches boto3 clients keyed
  by region only, so two accounts serving the same model ids in the same region
  would share one client and whichever warmed the cache first would bill both.
  Internal Claude stays on us-west-2 and personal Claude is pinned to us-east-1
  to keep the cache slots distinct.

## Model catalog policy

Claude model ids are **discovered, never hardcoded**. `latest_per_family()`
reduces a discovered list to the newest release per family and orders families by
capability tier, so:

* a newly entitled model reaches the picker with no code change, and
* an unreleased model name never has to be written into this repo.

`CLAUDE_SEED_MODELS` is a last-resort seed for when discovery cannot run at all;
it names only long-public models. An unrecognised family sorts between opus and
sonnet rather than last, because burying a model you are entitled to is the worse
failure.

## Tests

```sh
python -m pytest -q        # 53 tests, hermetic — no credentials, no network
```

`bedrock-mantle/test_proxy.py` covers the signing proxy against a stub upstream
(path rewrite, SigV4 header hygiene, streaming relay, error passthrough, loopback
binding, local `/v1/models`). `test_accounts.py` covers the shared helpers (id
parsing, capability ordering, profile/region wiring).

Both are real pytest modules. `test_proxy.py` was previously a hand-rolled script
that collected **zero** tests under pytest while passing when run directly — a CI
job wired to pytest reported green against untested code.
