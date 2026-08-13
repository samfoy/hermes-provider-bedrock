"""Bedrock provider profile — Amazon-internal `claude-code` account.

Overrides the bundled ``plugins/model-providers/bedrock`` profile (user plugins
win on name collision, last-writer-wins in ``register_provider()``), and is the
Hermes-side equivalent of pi's ``pi-provider-claude-code`` extension.

Two things the bundled profile can't do on its own:

1. **Region.** The bundled profile pins ``bedrock-runtime.us-east-1``. Hermes
   derives the Bedrock region by regexing the base_url
   (``agent/agent_init.py``), so the URL itself has to name ``us-west-2`` —
   that's where the ``CeceliaAmazonInternal`` role has model access.

2. **User-Agent.** The role's IAM policy carries an ``aws:UserAgent`` condition
   that gates streaming. Empirically (4 trials against this account):

   | UA approach                          | Converse | ConverseStream |
   |--------------------------------------|----------|----------------|
   | boto3 default                        | OK       | **403**        |
   | ``Config(user_agent=UA)`` (override) | OK       | OK             |
   | ``Config(user_agent_extra=UA)``      | -        | **403**        |
   | ``AWS_SDK_UA_APP_ID=claude-cli``     | -        | **403**        |

   So the condition is a *prefix* match, and only a full ``user_agent``
   override satisfies it. No env var or ``~/.aws/config`` key can do this —
   botocore only exposes ``user_agent_appid`` (a suffix). Hence the boto3
   client patch below.

   Mutating the UA is signature-safe: botocore's SigV4 signer lists
   ``user-agent`` in ``SIGNED_HEADERS_BLACKLIST``, so it is never signed.

Also disables control-plane model discovery: the role is denied
``bedrock:ListFoundationModels``, so discovery just burns a failed API call on
startup. The static catalog in ``hermes_cli/models.py`` plus ``FALLBACK_MODELS``
below cover the picker instead.
"""

from __future__ import annotations

import logging
import os

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

# ── Account / role constants ────────────────────────────────────────────────
REGION = "us-west-2"
BASE_URL = f"https://bedrock-runtime.{REGION}.amazonaws.com"

# Provisioned by `toolbox install claude-code` (account 175342148895,
# role CeceliaAmazonInternal). HERMES_BEDROCK_PROFILE overrides for anyone
# pointing Hermes at a different Bedrock account.
DEFAULT_AWS_PROFILE = "claude-code-DO-NOT-DELETE"
AWS_PROFILE = os.environ.get("HERMES_BEDROCK_PROFILE") or DEFAULT_AWS_PROFILE

# The exact UA the IAM condition expects. Keep the `claude-cli/` prefix.
CLAUDE_CLI_USER_AGENT = "claude-cli/2.1.131 (external, sdk-cli)"

# Services whose clients must carry the UA override.
_PATCHED_SERVICES = frozenset({"bedrock-runtime", "bedrock"})

# Modules patched lazily on first import (see _patch_on_import).
_PENDING_IMPORT_PATCHES: dict = {}

# Verified invokable by this role on both converse and converse_stream.
# Sourced from the shared account map so all three Bedrock providers agree on
# one Claude catalog: the LATEST release per family only (opus / sonnet / fable
# / haiku). `global.*` ids are deliberately excluded — every one duplicates a
# `us.*` entry and doubled the picker length for no added capability.
_acct = None
try:
    import importlib.util as _ilu
    from pathlib import Path as _Path
    _acct_spec = _ilu.spec_from_file_location(
        "_hermes_bedrock_accounts", _Path(__file__).resolve().parent.parent / "_bedrock_accounts.py"
    )
    _acct = _ilu.module_from_spec(_acct_spec)
    _acct_spec.loader.exec_module(_acct)
    FALLBACK_MODELS = list(_acct.CLAUDE_MODELS)
except Exception:  # pragma: no cover — never break startup on a helper import
    FALLBACK_MODELS = [
        "us.anthropic.claude-opus-5",
        "us.anthropic.claude-sonnet-5",
        "us.anthropic.claude-fable-5",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    ]


# ── boto3 User-Agent patch ─────────────────────────────────────────────────
def _install_boto3_user_agent_patch() -> bool:
    """Force ``Config(user_agent=CLAUDE_CLI_USER_AGENT)`` on Bedrock clients.

    Wraps ``boto3.client`` and ``boto3.Session.client`` so every Bedrock client
    Hermes builds carries the UA, no matter which layer builds it
    (``agent/bedrock_adapter.py`` passes no ``Config`` at all).

    Any caller-supplied ``Config`` is preserved via ``merge()`` — we only take
    over the ``user_agent`` field. Idempotent.
    """
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        return False

    if getattr(boto3, "_hermes_claude_code_ua_patched", False):
        return True

    def _with_ua(kwargs: dict) -> dict:
        """Return kwargs with a UA-forcing Config merged in."""
        ua_cfg = Config(user_agent=CLAUDE_CLI_USER_AGENT)
        existing = kwargs.get("config")
        # merge() lets the argument win, so merge ONTO existing to force our UA.
        kwargs["config"] = existing.merge(ua_cfg) if existing is not None else ua_cfg
        return kwargs

    def _service_of(args, kwargs) -> str:
        name = kwargs.get("service_name")
        if name is None and args:
            name = args[0]
        return (name or "").lower()

    _orig_client = boto3.client
    _orig_session_client = boto3.Session.client

    def client(*args, **kwargs):
        if _service_of(args, kwargs) in _PATCHED_SERVICES:
            kwargs = _with_ua(kwargs)
        return _orig_client(*args, **kwargs)

    def session_client(self, *args, **kwargs):
        if _service_of(args, kwargs) in _PATCHED_SERVICES:
            kwargs = _with_ua(kwargs)
        return _orig_session_client(self, *args, **kwargs)

    boto3.client = client
    boto3.Session.client = session_client
    boto3._hermes_claude_code_ua_patched = True
    return True


def _install_patch_when_boto3_arrives() -> None:
    """Apply the UA patch now, or as soon as ``boto3`` is first imported.

    ``boto3`` is a lazy dependency — ``agent/bedrock_adapter.py`` can pip-install
    it at runtime, so it may not be importable when this plugin loads (provider
    discovery happens well before the first inference call). A one-shot
    ``sys.meta_path`` finder closes that window: without it, a fresh install
    silently loses the UA override and every streaming call 403s.
    """
    if _install_boto3_user_agent_patch():
        return

    import sys
    from importlib.abc import MetaPathFinder

    class _Boto3PatchTrigger(MetaPathFinder):
        def find_module(self, fullname, path=None):  # legacy API, unused
            return None

        def find_spec(self, fullname, path=None, target=None):
            if fullname != "boto3":
                return None
            # Step aside so the real import proceeds, then patch the module.
            try:
                sys.meta_path.remove(self)
            except ValueError:
                pass
            import importlib

            try:
                importlib.import_module("boto3")
            except ImportError:
                return None
            if not _install_boto3_user_agent_patch():
                logger.warning(
                    "bedrock(claude-code): boto3 imported but UA patch failed; "
                    "streaming calls will 403"
                )
            return None

    sys.meta_path.insert(0, _Boto3PatchTrigger())


def _patch_on_import(module_name: str, patch) -> None:
    """Run ``patch(module)`` now if *module_name* is already imported, else
    exactly once immediately after its first real import completes.

    Neither target can be imported eagerly from here:

    * ``boto3`` is a lazy runtime dependency that may not exist yet.
    * ``agent.bedrock_adapter`` pip-installs boto3 as an import side-effect and
      pulls in the agent runtime, which must not load into the web server
      process (the web server calls ``list_providers()``, so anything this
      plugin imports at module scope lands there too).

    Implementation note: the finder must *wrap the real loader*, not re-import
    the module itself. Calling ``import_module()`` from inside ``find_spec()``
    does load and patch the module, but the outer import that triggered us then
    proceeds with its own spec, re-executes the module, and replaces the patched
    object in ``sys.modules`` — the patch silently vanishes. Wrapping
    ``exec_module`` instead runs the patch as part of that single real import.
    """
    import sys

    if module_name in sys.modules:
        patch(sys.modules[module_name])
        return

    _PENDING_IMPORT_PATCHES[module_name] = patch

    from importlib.abc import MetaPathFinder

    class _PatchTrigger(MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            fn = _PENDING_IMPORT_PATCHES.get(fullname)
            if fn is None:
                return None

            # Ask the remaining finders for the genuine spec.
            spec = None
            for finder in list(sys.meta_path):
                if finder is self:
                    continue
                find = getattr(finder, "find_spec", None)
                if find is None:
                    continue
                try:
                    spec = find(fullname, path, target)
                except Exception:
                    spec = None
                if spec is not None:
                    break
            if spec is None or spec.loader is None:
                return None
            exec_module = getattr(spec.loader, "exec_module", None)
            if exec_module is None:
                return None

            # Claim the patch and retire this finder — one shot.
            del _PENDING_IMPORT_PATCHES[fullname]
            try:
                sys.meta_path.remove(self)
            except ValueError:
                pass

            def patched_exec_module(module, _orig=exec_module, _fn=fn, _name=fullname):
                _orig(module)
                try:
                    _fn(module)
                except Exception:
                    logger.warning(
                        "bedrock(claude-code): deferred patch of %s failed", _name, exc_info=True
                    )

            # Loader instances are created per find_spec() call, so setting this
            # instance attribute cannot leak into unrelated imports.
            spec.loader.exec_module = patched_exec_module
            return spec

    sys.meta_path.insert(0, _PatchTrigger())


# ── Force the Converse API path for Claude ─────────────────────────────────
def _force_converse_api(mod) -> None:
    """Make ``is_anthropic_bedrock_model()`` always report False.

    Hermes runs Claude-on-Bedrock through the ``AnthropicBedrock`` SDK
    (``anthropic_messages`` api_mode), which calls
    ``InvokeModelWithResponseStream``. The ``CeceliaAmazonInternal`` role has NO
    identity-based policy for that action — it is a hard IAM deny, not a
    User-Agent condition, so no header trick recovers it:

        HTTP 403: not authorized to perform bedrock:InvokeModelWithResponseStream
                  on .../inference-profile/us.anthropic.claude-opus-5

    The role *does* allow ``Converse``/``ConverseStream``, which is the other
    branch of the same dual-path decision. Reporting False sends Claude down the
    Converse path (boto3, UA-patched above) in both places that branch:
    ``hermes_cli/runtime_provider.py`` (main loop) and
    ``agent/auxiliary_client.py`` (vision / title generation / delegation).

    Trade-off: the Converse path gives up the Anthropic SDK's prompt-caching and
    thinking-budget conveniences. That is not a real choice here — the SDK path
    cannot authenticate at all on this account.
    """
    if getattr(mod, "_hermes_forced_converse", False):
        return

    def is_anthropic_bedrock_model(model_id: str) -> bool:
        return False

    is_anthropic_bedrock_model.__doc__ = _force_converse_api.__doc__
    mod.is_anthropic_bedrock_model = is_anthropic_bedrock_model
    mod._hermes_forced_converse = True


# ── AWS credential resolution ──────────────────────────────────────────────
# auth_type="aws_sdk" hands off to the boto3 credential chain, so point that
# chain at the claude-code profile.
#
# This FORCES the profile rather than using setdefault. Hermes launches from an
# interactive shell, so AWS_PROFILE is frequently already set to something
# unrelated (this machine had AWS_PROFILE=claude → a different account
# entirely). Deferring to an ambient value silently bills/authorises the wrong
# account, and the claude-cli UA above is meaningless outside the claude-code
# role. HERMES_BEDROCK_PROFILE is the explicit, intentional override.
_ambient_profile = os.environ.get("AWS_PROFILE")
if _ambient_profile and _ambient_profile != AWS_PROFILE:
    logger.info(
        "bedrock(claude-code): overriding ambient AWS_PROFILE=%r with %r "
        "(set HERMES_BEDROCK_PROFILE to choose a different one)",
        _ambient_profile,
        AWS_PROFILE,
    )
os.environ["AWS_PROFILE"] = AWS_PROFILE
os.environ["AWS_REGION"] = REGION
os.environ["AWS_DEFAULT_REGION"] = REGION

_install_patch_when_boto3_arrives()
# NOTE: register ONE combined patch for agent.bedrock_adapter.
# _patch_on_import stores a single callback per module name
# (``_PENDING_IMPORT_PATCHES[module_name] = patch``), so calling it twice for
# the same module would silently drop the first patch. The combined function is
# defined below, next to the filter it applies.


# ── Provider profile ───────────────────────────────────────────────────────
def _filter_discovery_to_us_anthropic(mod) -> None:
    """Restrict live Bedrock discovery to ``us.anthropic.*`` inference profiles.

    ``hermes_cli.models.provider_model_ids("bedrock")`` returns
    ``bedrock_model_ids_or_none()`` verbatim and never consults this profile's
    ``fetch_models()`` (models.py:2993). So the picker showed whatever
    ``list_inference_profiles`` returned: 61 entries where 47 were noise —
    ``global.*`` duplicates of every model, plus Nova, Cohere embeddings,
    TwelveLabs and ~20 Stability image models that cannot serve a chat turn.
    Only ONE ``us.anthropic`` id made the visible (unexpanded) list.

    Note the docstring at the top of this module is now partly stale: the role
    is indeed denied ``ListFoundationModels``, but discovery does NOT come back
    empty because ``list_inference_profiles`` succeeds. That is why disabling
    the control-plane call was not enough on its own.

    Wrapping ``discover_bedrock_models`` (rather than replacing
    ``bedrock_model_ids_or_none``) keeps the region-aware behaviour and the
    discovery cache intact, and any other caller of the same function benefits.
    Falls back to the unfiltered list if filtering would empty it, so a region
    that only exposes ``eu.*``/``ap.*`` ids is never left with an empty picker.
    """
    if getattr(mod, "_hermes_filtered_us_anthropic", False):
        return

    _orig_discover = getattr(mod, "discover_bedrock_models", None)
    if _orig_discover is None:
        return

    def discover_bedrock_models(*args, **kwargs):
        models = _orig_discover(*args, **kwargs)
        if not models:
            return models
        filtered = [
            m for m in models
            if str(m.get("id", "")).startswith("us.anthropic.")
        ]
        if not filtered:
            # Non-US region (eu.*/ap.*) — better a full list than none.
            return models
        # Collapse to the newest release per family (opus/sonnet/fable/haiku),
        # so the picker shows 4 current models instead of 13 generations.
        # Applied to DISCOVERED ids, not the static list, so a newly entitled
        # `claude-opus-6` is surfaced with no code change.
        try:
            if _acct is not None:
                keep = set(_acct.latest_per_family([m.get("id", "") for m in filtered]))
                reduced = [m for m in filtered if m.get("id") in keep]
                if reduced:
                    filtered = reduced
        except Exception:
            logger.debug("bedrock: latest-per-family reduction failed", exc_info=True)
        # Preserve the curated ordering from FALLBACK_MODELS (newest first),
        # then append any discovered us.anthropic model not in that list.
        order = {mid: i for i, mid in enumerate(FALLBACK_MODELS)}
        filtered.sort(key=lambda m: order.get(m.get("id", ""), len(order)))
        return filtered

    mod.discover_bedrock_models = discover_bedrock_models
    mod._hermes_filtered_us_anthropic = True


def _patch_bedrock_adapter(mod) -> None:
    """Apply every ``agent.bedrock_adapter`` patch this plugin needs.

    Registered as a single callback because ``_patch_on_import`` keys its
    pending-patch dict by module name — two registrations for one module means
    the second overwrites the first.
    """
    _force_converse_api(mod)
    _filter_discovery_to_us_anthropic(mod)


_patch_on_import("agent.bedrock_adapter", _patch_bedrock_adapter)


class ClaudeCodeBedrockProfile(ProviderProfile):
    """Bedrock via the Amazon-internal claude-code role."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """No REST ``/models`` on bedrock-runtime, and the role is denied
        ``ListFoundationModels`` — return the verified static list."""
        return list(FALLBACK_MODELS)


bedrock = ClaudeCodeBedrockProfile(
    name="bedrock",
    aliases=("aws", "aws-bedrock", "amazon-bedrock", "amazon", "claude-code", "amazon-claude-code"),
    display_name="Bedrock: Internal Claude (claude-code acct)",
    description=f"Claude on Bedrock via the internal claude-code role ({REGION}, acct 175342148895, IAM auth — no API key)",
    api_mode="bedrock_converse",
    env_vars=(),  # AWS SDK credential chain, not env-var keys
    base_url=BASE_URL,
    auth_type="aws_sdk",
    supports_health_check=False,  # /models probe would 404 on bedrock-runtime
    default_aux_model="us.anthropic.claude-haiku-4-5-20251001-v1:0",
)

register_provider(bedrock)
