"""Bedrock Mantle provider profile — GPT-5.6 (Sol / Luna / Terra) on Hermes.

Registers a ``bedrock-mantle`` provider that reaches Amazon Bedrock Mantle's
OpenAI **Responses** API using SigV4 auth, via the loopback signing proxy in
``proxy.py`` (see that module for why a proxy is required).

Why a separate provider from ``bedrock``
---------------------------------------
The existing user plugin ``plugins/model-providers/bedrock`` targets a
different account, endpoint and wire protocol: the internal ``claude-code``
role on ``bedrock-runtime.us-west-2`` in ``bedrock_converse`` mode, and it
force-disables the Anthropic SDK path for IAM reasons. Mantle is a distinct
service (``bedrock-mantle.us-east-2.api.aws``) speaking the OpenAI Responses
protocol. Overloading one profile would break Claude-on-Bedrock, so this
registers under its own name and leaves ``bedrock`` untouched.

Verified state of the world (2026-09-09, live probes against both regions)
-------------------------------------------------------------------------
Region availability is per MODEL. Both catalogs were read live, and every
claim below is a real ``/openai/v1/responses`` call, not a catalog reading:

    us-east-2   gpt-5.6-sol, -luna, -terra          gpt-6-astra ABSENT (404)
    us-west-2   gpt-6-astra, gpt-5.6-luna, -terra   gpt-5.6-sol ABSENT (404)

* ``openai.gpt-6-astra`` returns HTTP 200 ``status=completed`` on us-west-2
  under the PERSONAL profile only. The internal builder/codex account is
  denied it by an explicit service control policy on
  ``bedrock-mantle:CreateInference`` for ``project/default`` in us-west-2 —
  an org policy, not a role gap, so it cannot be fixed from here.
* Astra context: publish **1,050,000**, the TOTAL window. Do not publish the
  input limit here. Hermes derives ``effective_window = context_length -
  max_tokens`` itself (``agent/context_compressor._compute_threshold_tokens``),
  so publishing the input limit subtracts the output reserve a second time.
  Measured live, and corroborated by models.dev, which lists
  ``context: 1050000 / input: 922000 / output: 128000``:

      total window   1,050,000
      input limit      922,000   <- binary-searched to 921,758..921,882
      max output       128,000   (922,000 + 128,000 = 1,050,000)

  The input limit does NOT move with requested ``max_output_tokens``, and the
  cap is on the TOTAL. Proof: at ``max_output_tokens=60000``, two requests with
  inputs 6,000 tokens apart both stopped at ``total_tokens=921,858`` exactly,
  with ``status=incomplete`` / ``reason=max_output_tokens`` — output shrank from
  836 to 6,836 tokens to absorb the difference. With ``context_length=1,050,000``
  Hermes computes an effective input budget of exactly 922,000, matching the
  measured boundary.
* Astra accepts ``reasoning.effort`` low / medium / high / xhigh, and emits
  ``reasoning`` items carrying ``encrypted_content``. ``effort: "none"`` is
  rejected (HTTP 400).
* Tool calling, SSE streaming, and image input all verified working on astra.
  Vision needs the ``image_url`` data-URL form; the ``source`` block form is
  accepted but silently ignored, so the model cannot see the image.
* Encrypted reasoning is sealed **per region**, not per model. Measured:
  astra(us-west-2) history replayed to gpt-5.6-luna(us-east-2) returns HTTP
  400 "encrypted reasoning is scoped to the region that produced it and
  cannot be replayed in a different region", while astra -> luna within
  us-west-2 succeeds. Tampering with the blob returns "invalid encrypted
  reasoning", which proves the service really validates it.

Earlier verified facts (2026-08-04) that still hold
---------------------------------------------------
* Context ceilings from the service's own oversize validation error:
  ``openai.gpt-5.6-*`` -> 1,050,000; ``gpt-5.5`` / ``gpt-5.4`` -> 278,528.
* ``/v1/chat/completions`` returns HTTP 400 for 5.6 — Responses API only.
"""

from __future__ import annotations

import logging
import os

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

# Import the proxy by file path: this plugin dir is not an importable package
# under a stable module name, so a relative import is not guaranteed to work.
try:
    from .proxy import DEFAULT_REGION, MantleProxy  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - loaded as a standalone module
    import importlib.util
    from pathlib import Path

    _spec = importlib.util.spec_from_file_location(
        "hermes_bedrock_mantle_proxy", Path(__file__).resolve().parent / "proxy.py"
    )
    _mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    MantleProxy = _mod.MantleProxy
    DEFAULT_REGION = _mod.DEFAULT_REGION

# ── Configuration ───────────────────────────────────────────────────────────
REGION = os.environ.get("HERMES_MANTLE_REGION") or DEFAULT_REGION

# Which AWS profile signs the requests.
#
# Amazon vends separate Bedrock accounts, one per tool, and they are not
# interchangeable (verified 2026-08-04, see README for the capability matrix):
#
#   codex profile        GPT + Claude   <- the GPT-provisioned account
#   claude-code profile  Claude only    <- denied CreateInference on Mantle
#   personal profile     GPT + Claude   <- bills the user personally
#
# The codex profile is the account Amazon provisions specifically for the
# internal GPT models, so it is the correct default here — it keeps GPT traffic
# on the purpose-built account instead of billing the user's personal one.
# It is auto-refreshing (credential_process shells out to the codex wrapper),
# so it needs no manual `ada credentials update`.
#
# Not simply the botocore default chain: the sibling ``bedrock`` plugin FORCES
# its own profile at import time, and that role is denied on Mantle
# (bedrock-mantle:CreateInference -> access_denied). Deferring to the ambient
# value would therefore break whenever both plugins load.
DEFAULT_AWS_PROFILE = "codex-DO-NOT-DELETE"
AWS_PROFILE = os.environ.get("HERMES_MANTLE_AWS_PROFILE") or DEFAULT_AWS_PROFILE

# Fixed loopback port for the signing proxy. A stable port lets the
# ``providers.bedrock-mantle`` entry in config.yaml name a static base_url
# (see README) — with an ephemeral port that URL would go stale each restart.
try:
    PROXY_PORT = int(os.environ.get("HERMES_MANTLE_PROXY_PORT") or 8791)
except ValueError:
    PROXY_PORT = 8791

# ── Model catalog ───────────────────────────────────────────────────────────
# Context windows verified against the live endpoint; see module docstring.
GPT_5_6_CONTEXT = 1_050_000     # total window; input limit is 922,000 + 128,000 output
GPT_6_ASTRA_CONTEXT = 1_050_000 # total window; input limit is 922,000 + 128,000 output
GPT_5_X_CONTEXT = 272_000       # true ceiling 278,528
MAX_OUTPUT_TOKENS = 128_000

# Responses-API models, most capable first (drives picker order).
#
# Only the internal codex listener remains, and it serves us-east-2. Personal
# GPT moved to `bedrock-personal` over native Converse.
EAST_MODELS = [
    "openai.gpt-5.6-sol",
    "openai.gpt-5.6-luna",
    "openai.gpt-5.6-terra",
]

# Kept as the historical name for the us-east-2 set: existing config, tests and
# docs refer to MANTLE_MODELS, and that listener's catalog is unchanged.
MANTLE_MODELS = EAST_MODELS

MODEL_CONTEXT = {
    "openai.gpt-6-astra": GPT_6_ASTRA_CONTEXT,
    "openai.gpt-5.6-sol": GPT_5_6_CONTEXT,
    "openai.gpt-5.6-luna": GPT_5_6_CONTEXT,
    "openai.gpt-5.6-terra": GPT_5_6_CONTEXT,
    "openai.gpt-5.5": GPT_5_X_CONTEXT,
    "openai.gpt-5.4": GPT_5_X_CONTEXT,
}

# Single shared proxy per process — the port goes into base_url, so all
# sessions in this interpreter reuse one listener.
_PROXY = MantleProxy(
    region=REGION,
    profile=AWS_PROFILE,
    pinned_port=PROXY_PORT,
    # The proxy answers GET /v1/models itself with this catalog. Upstream
    # ListModels is denied on the codex account, so a forwarded probe 401s on
    # a working provider; see fetch_models below and proxy.do_GET.
    models=tuple(EAST_MODELS),
    model_context=MODEL_CONTEXT,
)

# ── Personal-account path ───────────────────────────────────────────────────
# ── Personal-account GPT moved to `bedrock-personal` ────────────────────────
# The personal account reaches every GPT model over native bedrock-runtime
# Converse, so it needs no proxy and no separate listener. Ports 8792 and 8793
# are retired. `bedrock-personal` serves Claude and GPT from one provider.
#
# Native Converse also removes the region split: us-east-1 serves astra, sol,
# terra, and luna together, which a Mantle region cannot do.


class BedrockMantleProfile(ProviderProfile):
    """Bedrock Mantle over the OpenAI Responses API with SigV4 auth."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Return this provider's curated Responses-API model list.

        Reads ``fallback_models`` so each registered provider reports its OWN
        region's catalog. A hardcoded list here would advertise gpt-5.6-sol on
        the us-west-2 provider, where it does not exist and 404s on first use.

        Deliberately does NOT hit ``/v1/models`` upstream: that route lists
        every Mantle model including Chat-Completions-only ones (DeepSeek, Qwen,
        Kimi, ...), which this profile cannot serve in ``codex_responses`` mode.
        Listing them would put models in the picker that 400 on first use.

        It also would not work on the default account. ``codex-DO-NOT-DELETE``
        is granted ``bedrock-mantle:CreateInference`` but DENIED
        ``bedrock-mantle:ListModels`` — inference succeeds while discovery
        returns 403 ``access_denied``. A discovery-based catalog would
        therefore report zero models on a fully working account.
        """
        return list(self.fallback_models)


def _resolve_base_url(proxy=None) -> str:
    """Start the signing proxy and return its OpenAI-style base URL.

    Failure here must not break Hermes startup — provider discovery runs in the
    web server process too. On failure we register the upstream URL, which
    fails later with a clear auth error rather than a confusing import crash.
    """
    try:
        return (proxy or _PROXY).base_url()
    except Exception:
        logger.warning(
            "bedrock-mantle: could not start SigV4 proxy; falling back to the "
            "direct upstream URL (requires AWS_BEARER_TOKEN_BEDROCK)",
            exc_info=True,
        )
        return f"https://bedrock-mantle.{REGION}.api.aws/openai/v1"


mantle = BedrockMantleProfile(
    name="bedrock-mantle",
    aliases=("mantle", "bedrock-openai", "gpt5-bedrock"),
    display_name="Bedrock: Internal GPT (codex acct)",
    description=(
        f"GPT-5.6 Sol/Luna/Terra (~1M context) on Bedrock Mantle {REGION} "
        "via the OpenAI Responses API — SigV4 auth, no API key"
    ),
    # Responses API, not chat_completions: GPT-5.6 rejects chat/completions.
    api_mode="codex_responses",
    env_vars=(),          # SigV4 through the AWS credential chain
    base_url=_resolve_base_url(),
    # NOTE: declared "api_key" purely to make the model catalog visible.
    # Real auth is SigV4 via boto3 inside the local proxy — no key is ever read.
    #
    # `hermes_cli.models.provider_model_ids()` only consults a provider profile
    # when `auth_type == "api_key" and base_url` (models.py:3010), and
    # `fallback_models` is returned from *inside* that branch (models.py:3055).
    # With auth_type="aws_sdk" the whole block is skipped, so the WebUI's
    # `/api/providers` card and the `/model` picker both showed this provider
    # with ZERO models. Because no API key exists for it, the `if api_key:`
    # guard (models.py:3019) is false and control falls straight through to
    # `fallback_models` — catalog visible, live fetch never attempted, auth
    # untouched. config.yaml cannot carry the list instead: `hermes config set`
    # coerces JSON arrays to strings and the WebUI requires a real list.
    auth_type="api_key",
    fallback_models=tuple(MANTLE_MODELS),
    # The proxy now serves GET /v1/models locally, so a health probe works.
    supports_health_check=True,
    supports_vision=True,          # verified: image input accepted
    default_aux_model="openai.gpt-5.6-luna",   # cheapest 5.6 for titles/vision
)

register_provider(mantle)
