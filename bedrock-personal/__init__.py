"""Bedrock via a personal AWS account — the third access path.

Surfaces Claude (Converse) on a personal account rather than an Amazon-internal
one, so the model picker can express "run this on my own account" alongside the
two internal paths:

===================  ==============================  ===================  =========
provider             path                            credential           region
===================  ==============================  ===================  =========
``bedrock-mantle``   internal GPT (Responses API)    codex profile        us-east-2
``bedrock``          internal Claude (Converse)      claude-code profile  us-west-2
``bedrock-personal`` **personal Claude (Converse)**  personal profile     us-east-1
===================  ==============================  ===================  =========

Why us-east-1 and not us-west-2
-------------------------------
``agent/bedrock_adapter.py`` caches boto3 clients **keyed by region only**
(``_bedrock_runtime_client_cache[region]``) and resolves credentials from the
process-global chain. Both Claude accounts serve the *same* ``us.`` model ids,
so if this provider also used us-west-2 the two would share one cached client
and whichever warmed it first would silently bill both paths.

The accounts differ in regional reach, which supplies a natural cache key
(verified 2026-08-04):

* the claude-code profile -> us-west-2 works, us-east-1
  ``AccessDeniedException``.
* the personal profile -> us-west-2, **us-east-1** and us-east-2 all work.

Pinning personal traffic to us-east-1 therefore gives each account its own
cache slot with no core patch. The current Claude releases were each confirmed
to return a real completion there.

Credential handling
-------------------
``AWS_PROFILE`` is process-global and the sibling ``bedrock`` plugin force-sets
it to the claude-code role at import time. This module therefore does **not**
set it at import; it patches ``boto3.client`` to bind the personal profile to
clients built for its own region only. That keeps several accounts alive in one
WebUI process.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

# Shared account/region map (plain-file import: this dir is not a package).
_spec = importlib.util.spec_from_file_location(
    "_hermes_bedrock_accounts", Path(__file__).resolve().parent.parent / "_bedrock_accounts.py"
)
_acct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_acct)

PROFILE = _acct.PERSONAL_PROFILE          # local AWS config profile name
REGION = _acct.PERSONAL_REGION            # us-east-1
BASE_URL = f"https://bedrock-runtime.{REGION}.amazonaws.com"
# Seed catalog. Real ids come from the shared reducer applied to discovery; this
# only backstops a failed discovery call, so it names long-public models.
MODELS = list(_acct.CLAUDE_SEED_MODELS)


def _install_region_scoped_profile_patch() -> bool:
    """Bind the personal AWS profile to Bedrock clients for THIS region only.

    Wraps ``boto3.client`` / ``boto3.Session.client``. When a ``bedrock*``
    client is built for :data:`REGION` and the caller did not already pass
    explicit credentials or a session, the client is re-created from a
    ``boto3.Session(profile_name="claude")`` so it authenticates against the
    personal account regardless of the ambient ``AWS_PROFILE``.

    Scoped narrowly on purpose:

    * only ``bedrock`` / ``bedrock-runtime`` services,
    * only this provider's region, so the internal claude-code provider on
      us-west-2 and the Mantle proxy on us-east-2 are untouched,
    * only when no explicit credentials were supplied.

    Idempotent, and cooperates with the sibling ``bedrock`` plugin's own
    User-Agent patch (that one mutates ``config``; this one swaps the session).
    """
    try:
        import boto3
    except ImportError:
        return False

    if getattr(boto3, "_hermes_personal_bedrock_patched", False):
        return True

    _orig_client = boto3.client
    _orig_session_client = boto3.Session.client

    def _targets(args, kwargs) -> bool:
        name = kwargs.get("service_name")
        if name is None and args:
            name = args[0]
        service = (name or "").lower()
        if service not in {"bedrock", "bedrock-runtime"}:
            return False
        if kwargs.get("region_name") != REGION:
            return False
        # Respect explicit credentials — never override a deliberate caller.
        if any(kwargs.get(k) for k in
               ("aws_access_key_id", "aws_secret_access_key", "aws_session_token")):
            return False
        return True

    def client(*args, **kwargs):
        if _targets(args, kwargs):
            try:
                session = boto3.Session(profile_name=PROFILE, region_name=REGION)
                return _orig_session_client(session, *args, **kwargs)
            except Exception:
                logger.warning(
                    "bedrock-personal: could not bind profile %r; falling back "
                    "to the ambient credential chain", PROFILE, exc_info=True,
                )
        return _orig_client(*args, **kwargs)

    def session_client(self, *args, **kwargs):
        # A caller that built its own Session already chose an identity.
        return _orig_session_client(self, *args, **kwargs)

    boto3.client = client
    boto3.Session.client = session_client
    boto3._hermes_personal_bedrock_patched = True
    return True


def _install_patch_when_boto3_arrives() -> None:
    """Apply the patch now, or on boto3's first import.

    ``boto3`` is a lazy dependency — ``agent/bedrock_adapter.py`` can pip-install
    it at runtime, so it may not be importable when provider discovery runs.
    """
    if _install_region_scoped_profile_patch():
        return

    import sys
    from importlib.abc import MetaPathFinder

    class _Boto3PatchTrigger(MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "boto3":
                return None
            try:
                sys.meta_path.remove(self)
            except ValueError:
                pass
            import importlib
            try:
                importlib.import_module("boto3")
            except ImportError:
                return None
            if not _install_region_scoped_profile_patch():
                logger.warning("bedrock-personal: boto3 imported but profile patch failed")
            return None

    sys.meta_path.insert(0, _Boto3PatchTrigger())


_install_patch_when_boto3_arrives()

# Discovered catalog, cached for the process. Provider discovery can be called
# repeatedly while building the Settings payload, and each miss is a control-plane
# round-trip.
_discovered_cache: list[str] | None = None


def _discover_claude_models() -> list[str]:
    """List this account's Claude inference profiles, newest per family.

    Returns ``[]`` on any failure — no credentials yet, ``ListInferenceProfiles``
    denied, boto3 absent, wrong region. Callers fall back to the seed list.

    Never raises and never blocks startup on a slow control plane: provider
    discovery runs while the web server builds its provider list, so an exception
    here would surface as a broken Settings page rather than a short catalog.
    """
    global _discovered_cache
    if _discovered_cache is not None:
        return list(_discovered_cache)

    ids: list[str] = []
    try:
        import boto3
        from botocore.config import Config

        session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        client = session.client(
            "bedrock",
            region_name=REGION,
            config=Config(connect_timeout=3, read_timeout=6, retries={"max_attempts": 2}),
        )
        paginator = client.get_paginator("list_inference_profiles")
        for page in paginator.paginate():
            for item in page.get("inferenceProfileSummaries") or []:
                mid = item.get("inferenceProfileId") or ""
                # us.* only: global.* duplicates every entry, and non-Anthropic
                # profiles (Nova, embeddings, image models) cannot serve a turn.
                if mid.startswith("us.anthropic."):
                    ids.append(mid)
    except Exception:
        logger.debug("bedrock-personal: model discovery unavailable", exc_info=True)
        return []

    try:
        reduced = _acct.latest_per_family(ids)
    except Exception:
        logger.debug("bedrock-personal: latest-per-family reduction failed", exc_info=True)
        return []

    if reduced:
        _discovered_cache = list(reduced)
    return list(reduced)


class PersonalBedrockProfile(ProviderProfile):
    """Claude on Bedrock via a personal AWS account."""

    @property
    def fallback_models(self) -> tuple:
        """Live discovered catalog, falling back to the curated seed.

        A property rather than a static tuple because Hermes has two catalog
        paths that do not agree: chat/CLI resolution calls ``fetch_models()``,
        while the WebUI model picker reads this **attribute** directly. A static
        tuple therefore showed the seed in the picker while chat could reach every
        discovered model — a model you are entitled to, usable by ``-m <id>`` but
        absent from the UI.

        Must never raise: this is an attribute read on UI and startup paths.
        """
        try:
            discovered = _discover_claude_models()
            if discovered:
                return tuple(discovered)
        except Exception:
            logger.debug("bedrock-personal: catalog unavailable for picker", exc_info=True)
        return tuple(getattr(self, "_seed_models", ()) or MODELS)

    @fallback_models.setter
    def fallback_models(self, value) -> None:
        # The dataclass __init__ assigns the seed here.
        self._seed_models = tuple(value or ())

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Discover this account's Claude inference profiles, newest per family.

        Discovery is live rather than static because a static list is exactly
        what goes stale: the account gains a model and the picker never shows it.
        ``latest_per_family`` reduces every generation ever entitled down to the
        current release per family and orders them by capability, so no model id
        has to be named in source.

        Falls back to :data:`MODELS` when discovery cannot run (no credentials at
        startup, ``ListInferenceProfiles`` denied, wrong region). The seed names
        only long-public models, so the fallback is a usable picker rather than
        an empty one — but it is a floor, not the intended catalog.
        """
        discovered = _discover_claude_models()
        if discovered:
            return discovered
        return list(MODELS)


bedrock_personal = PersonalBedrockProfile(
    name="bedrock-personal",
    aliases=("bedrock-alpha", "claude-personal", "personal-bedrock", "my-bedrock"),
    display_name="Bedrock: Personal Claude (my own acct)",
    description=(
        f"Claude on Bedrock via my own AWS account ({REGION}, IAM auth) — "
        "billed to me; use when the internal accounts are throttled or denied"
    ),
    api_mode="bedrock_converse",
    env_vars=(),                   # AWS SDK credential chain
    base_url=BASE_URL,
    # See the matching note in ../bedrock-mantle/__init__.py: "api_key" is
    # declared only so `provider_model_ids()` reaches `fallback_models`
    # (hermes_cli/models.py:3010 gates the whole profile block on it, and 3055
    # returns the catalog from inside that block). No key exists for this
    # provider, so the `if api_key:` guard is false and no live fetch happens —
    # real auth stays SigV4/boto3 against the personal profile.
    auth_type="api_key",
    fallback_models=tuple(MODELS),
    supports_health_check=False,   # bedrock-runtime has no /models route
    supports_vision=True,
    default_aux_model="us.anthropic.claude-haiku-4-5-20251001-v1:0",
)

register_provider(bedrock_personal)
