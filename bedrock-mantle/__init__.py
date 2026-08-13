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

Verified state of the world (2026-08-04, live probes against us-east-2)
----------------------------------------------------------------------
* ``/v1/models`` lists **49** models. There is **no plain ``openai.gpt-5.6``** —
  5.6 ships as three named variants: ``-sol``, ``-luna``, ``-terra``.
* All three return HTTP 200 ``status=completed`` on ``/openai/v1/responses``.
* Context ceilings, from the service's own validation error on an oversized
  prompt (``prompt tokens (1200011) exceed model maximum (N)``):

      openai.gpt-5.6-sol / -luna / -terra ->  N = 1,050,000
      openai.gpt-5.5    / gpt-5.4         ->  N =   278,528

  So the 1M context is real for 5.6 and specific to it. We publish 1,000,000
  (under the true ceiling) so token-estimate drift cannot push a request over.
* ``reasoning.effort: "xhigh"`` is accepted and echoed back.
* Image input works via the ``source`` block form.
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
GPT_5_6_CONTEXT = 1_000_000     # true ceiling 1,050,000 — headroom on purpose
GPT_5_X_CONTEXT = 272_000       # true ceiling 278,528
MAX_OUTPUT_TOKENS = 128_000

# Responses-API models, most capable first (drives picker order).
# Latest generation only: 5.6 ships as three sibling variants (sol / luna /
# terra) which are peers, not versions of each other, so all three stay. The
# superseded 5.5 and 5.4 generations are omitted — they cap at 272K context
# versus 5.6's ~1M, so there is no reason to pick them.
MANTLE_MODELS = [
    "openai.gpt-5.6-sol",
    "openai.gpt-5.6-luna",
    "openai.gpt-5.6-terra",
]

MODEL_CONTEXT = {
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
    models=tuple(MANTLE_MODELS),
)

# ── Personal-account path ───────────────────────────────────────────────────
# The personal profile is also entitled to Mantle GPT — verified 2026-08-06, a
# real /openai/v1/responses call on openai.gpt-5.6-luna returns HTTP 200
# status=completed under that profile.
#
# It needs its OWN proxy instance rather than reusing _PROXY: MantleProxy caches
# one botocore session per instance (``_creds_session``), so a single listener
# can only ever sign for one account. A second pinned port means each provider's
# base_url names the listener holding the right credentials, and the codex path
# is untouched.
PERSONAL_AWS_PROFILE = (
    os.environ.get("HERMES_MANTLE_PERSONAL_AWS_PROFILE") or "claude"
)
try:
    PERSONAL_PROXY_PORT = int(
        os.environ.get("HERMES_MANTLE_PERSONAL_PROXY_PORT") or 8792
    )
except ValueError:
    PERSONAL_PROXY_PORT = 8792

_PERSONAL_PROXY = MantleProxy(
    region=REGION,
    profile=PERSONAL_AWS_PROFILE,
    pinned_port=PERSONAL_PROXY_PORT,
    models=tuple(MANTLE_MODELS),
)


class BedrockMantleProfile(ProviderProfile):
    """Bedrock Mantle over the OpenAI Responses API with SigV4 auth."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Return the curated Responses-API model list.

        Deliberately does NOT hit ``/v1/models``: that route lists every Mantle
        model including Chat-Completions-only ones (DeepSeek, Qwen, Kimi, ...),
        which this profile cannot serve in ``codex_responses`` mode. Listing
        them would put models in the picker that 400 on first use.

        It also would not work on the default account. ``codex-DO-NOT-DELETE``
        is granted ``bedrock-mantle:CreateInference`` but DENIED
        ``bedrock-mantle:ListModels`` — inference succeeds while discovery
        returns 401 ``access_denied``. A discovery-based catalog would
        therefore report zero models on a fully working account.
        """
        return list(MANTLE_MODELS)


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

# Same models and wire protocol, personal account's credentials. Registered as
# a sibling provider so the picker can express which account GPT traffic bills
# to, mirroring the Claude split between ``bedrock`` and ``bedrock-personal``.
mantle_personal = BedrockMantleProfile(
    name="bedrock-mantle-personal",
    aliases=("mantle-personal", "gpt-personal", "my-mantle"),
    display_name="Bedrock: Personal GPT (my own acct)",
    description=(
        f"GPT-5.6 Sol/Luna/Terra (~1M context) on Bedrock Mantle {REGION} via my "
        "own account — billed to me; use when the codex account is throttled"
    ),
    api_mode="codex_responses",
    env_vars=(),
    base_url=_resolve_base_url(_PERSONAL_PROXY),
    # See the note on `mantle` above: auth_type="api_key" only makes the catalog
    # visible; real auth is SigV4 inside the proxy and no key is ever read.
    auth_type="api_key",
    fallback_models=tuple(MANTLE_MODELS),
    supports_health_check=True,
    supports_vision=True,
    default_aux_model="openai.gpt-5.6-luna",
)

register_provider(mantle_personal)
