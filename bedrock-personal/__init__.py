"""Bedrock via Sam's PERSONAL ALPHA account — the third access path.

Surfaces Claude (Converse) on account 333843746513, role
``IibsAdminAccess-DO-NOT-DELETE``, so the model picker can express "run this on
my own account" alongside the two internal ones:

===================  ==============================  =============  =========
provider             path                            account        region
===================  ==============================  =============  =========
``bedrock-mantle``   internal GPT (Responses API)    493765493388   us-east-2
``bedrock``          internal Claude (Converse)      175342148895   us-west-2
``bedrock-personal`` **personal Claude (Converse)**  333843746513   us-east-1
===================  ==============================  =============  =========

Why us-east-1 and not us-west-2
-------------------------------
``agent/bedrock_adapter.py`` caches boto3 clients **keyed by region only**
(``_bedrock_runtime_client_cache[region]``) and resolves credentials from the
process-global chain. Both Claude accounts serve the *same* ``us.`` model ids,
so if this provider also used us-west-2 the two would share one cached client
and whichever warmed it first would silently bill both paths.

The accounts differ in regional reach, which supplies a natural cache key
(verified 2026-08-04):

* ``claude-code-DO-NOT-DELETE`` -> us-west-2 works, us-east-1
  ``AccessDeniedException``.
* ``claude`` (personal) -> us-west-2, **us-east-1** and us-east-2 all work.

Pinning personal traffic to us-east-1 therefore gives each account its own
cache slot with no core patch. ``us.anthropic.claude-{opus-5,sonnet-5,fable-5}``
were each confirmed to return a real completion there.

Credential handling
-------------------
``AWS_PROFILE`` is process-global and the sibling ``bedrock`` plugin force-sets
it to the claude-code role at import time. This module therefore does **not**
set it at import; it patches ``boto3.client`` to bind the personal profile to
clients built for its own region only. That keeps three accounts alive in one
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

PROFILE = _acct.PERSONAL_PROFILE          # "claude"
ACCOUNT = _acct.PERSONAL_ACCOUNT          # 333843746513
REGION = _acct.PERSONAL_REGION            # us-east-1
BASE_URL = f"https://bedrock-runtime.{REGION}.amazonaws.com"
MODELS = list(_acct.CLAUDE_MODELS)


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


class PersonalBedrockProfile(ProviderProfile):
    """Claude on Bedrock via the personal ALPHA account."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Return the verified static Claude catalog.

        Skips ``ListInferenceProfiles`` so provider discovery never spends a
        network round-trip (and cannot fail) during startup. The list is
        identical on both Claude accounts, confirmed via the AWS CLI.
        """
        return list(MODELS)


bedrock_personal = PersonalBedrockProfile(
    name="bedrock-personal",
    aliases=("bedrock-alpha", "claude-personal", "personal-bedrock", "my-bedrock"),
    display_name="Bedrock: Personal Claude (my ALPHA acct)",
    description=(
        f"Claude on Bedrock via my own account {ACCOUNT} ({REGION}, IAM auth) — "
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
