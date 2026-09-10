"""Shared helpers for the Bedrock access paths.

Amazon vends a **separate AWS account per tool**, and they are not
interchangeable. One account cannot serve GPT at all, so the provider split is an
entitlement boundary rather than a preference:

===========================  ====================  ==========  ======
profile                      capability            GPT         Claude
===========================  ====================  ==========  ======
codex profile                GPT + Claude          yes         yes
claude-code profile          Claude only           **denied**  yes
personal profile             GPT + Claude          yes         yes
===========================  ====================  ==========  ======

The personal profile reaches GPT over the native ``bedrock-runtime`` Converse
API, so it needs no proxy and no separate provider. Verified 2026-09-09:
``us-east-1`` and ``us-west-2`` each serve astra, sol, terra, and luna. The
``us.`` inference-profile prefix is mandatory, because a bare ``openai.*`` id
rejects with "on-demand throughput isn't supported".

Account numbers are deliberately not recorded here. Each profile resolves to its
own account through the local AWS config, and :func:`account_id_for_profile`
reports it at runtime when a diagnostic needs it.

Providers surfaced, so the model picker can express which account a request bills
to:

==========================  ==================================  ===================
provider                    path                                credential
==========================  ==================================  ===================
``bedrock-mantle``          internal GPT (Responses API)        codex profile
``bedrock``                 internal Claude (Converse)          claude-code profile
``bedrock-personal``        personal Claude + GPT (Converse)    personal profile
==========================  ==================================  ===================

The region-collision problem
----------------------------
``agent/bedrock_adapter.py`` caches boto3 clients **keyed by region only**, and
resolves credentials from the process-global chain. Two Claude accounts serving
the *same* ``us.`` model ids in the *same* region would therefore share one
cached client, and whichever provider warmed the cache first would silently bill
both.

The accounts differ in regional reach, which supplies a natural key:

* the claude-code profile works in **us-west-2 only** — us-east-1 returns
  ``AccessDeniedException``.
* the personal profile works in us-west-2, **us-east-1** and us-east-2.

So internal Claude stays on us-west-2 and personal Claude is pinned to
us-east-1. Different region -> different cache slot -> no credential bleed,
without patching core.

``AWS_PROFILE`` is still process-global, so it is applied per request rather
than at import time; see :func:`profile_env_for_provider`.

Model catalog policy
--------------------
Model ids are **discovered, never hardcoded**. ``latest_per_family`` reduces a
discovered list to the newest release per family and orders families by
capability, so a newly entitled model appears with no edit here and an
unreleased model name never has to be written down. :data:`CLAUDE_SEED_MODELS`
is a last-resort seed for the case where discovery cannot run at all; it names
only long-public models.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ── Profiles ────────────────────────────────────────────────────────────────
# Names of local AWS config profiles, not account identifiers.
CODEX_PROFILE = "codex-DO-NOT-DELETE"
CLAUDE_CODE_PROFILE = "claude-code-DO-NOT-DELETE"
PERSONAL_PROFILE = "claude"

# ── Regions ─────────────────────────────────────────────────────────────────
# Internal Claude is only entitled in us-west-2.
CLAUDE_CODE_REGION = "us-west-2"
# Personal Claude pinned to us-east-1 to keep a distinct client-cache slot.
PERSONAL_REGION = "us-east-1"
# Mantle GPT is served from us-east-2.
MANTLE_REGION = "us-east-2"

# User-Agent required by an IAM condition on the claude-code profile.
# Streaming 403s without the `claude-cli/` prefix.
CLAUDE_CLI_USER_AGENT = "claude-cli/2.1.131 (external, sdk-cli)"

# ── Claude model catalog ────────────────────────────────────────────────────
# Seed only. Discovery through `list_inference_profiles` is the real source, and
# it is filtered through `latest_per_family` below. This list exists so a failed
# discovery call still yields a usable picker, so it names only long-public
# models rather than whatever the account is currently entitled to.
CLAUDE_SEED_MODELS = [
    "us.anthropic.claude-opus-4-1-20250805-v1:0",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
]

# Backwards-compatible alias: callers historically imported CLAUDE_MODELS.
CLAUDE_MODELS = CLAUDE_SEED_MODELS

# Relative capability of each Claude family, highest first. Ordering is by tier,
# not by name, so an unlisted family sorts on its own merits instead of being
# dropped to the bottom. Anthropic's naming is stable at the tier level even
# when individual release names are not public.
_FAMILY_TIER = {
    "opus": 0,      # most capable
    "sonnet": 2,    # mid
    "haiku": 4,     # fastest / cheapest
}
# A family absent from the map lands between opus and sonnet: an unrecognised
# family is far more likely to be a new flagship than a new cheap tier, and
# burying a model the account is entitled to is the worse failure. This is what
# keeps unreleased families out of the source while still ranking them sensibly.
_UNKNOWN_FAMILY_TIER = 1

_CLAUDE_ID_RE = re.compile(
    r"^(?P<prefix>us|global|eu|ap)\.anthropic\.claude-"
    r"(?:(?P<legacy>3(?:-5|-7)?)-(?P<legacyfam>[a-z]+)"
    r"|(?P<family>[a-z]+)-(?P<ver>[0-9]+(?:-[0-9]+)?))"
)


def family_tier(family: str) -> int:
    """Capability tier for a Claude family name. Lower sorts first."""
    return _FAMILY_TIER.get(family, _UNKNOWN_FAMILY_TIER)


def claude_family_and_version(model_id: str):
    """Return ``(family, version_tuple)`` for a Claude id, or ``None``.

    Handles both modern ids (``claude-opus-4-8`` -> ``("opus", (4, 8))``,
    ``claude-sonnet-5`` -> ``("sonnet", (5,))``) and the legacy
    generation-first form (``claude-3-5-sonnet`` -> ``("sonnet", (3, 5))``).
    A trailing date/revision (``-20251001-v1:0``) is deliberately ignored: it
    is a build stamp, not a version, and two ids differing only by stamp are
    the same model release.

    The family group is an open character class rather than a fixed alternation.
    A fixed list silently drops any family it does not name, which both hides
    models the account is entitled to and forces unreleased names into this
    file.
    """
    m = _CLAUDE_ID_RE.match(str(model_id or ""))
    if not m:
        return None
    if m.group("legacy"):
        family = m.group("legacyfam")
        version = tuple(int(p) for p in m.group("legacy").split("-"))
    else:
        family = m.group("family")
        version = tuple(int(p) for p in m.group("ver").split("-"))
    return family, version


def latest_per_family(model_ids):
    """Keep only the highest-versioned id per Claude family, best first.

    Why this exists: ``list_inference_profiles`` returns every generation ever
    entitled to the account — opus 4.1 through 5, three sonnet generations, and
    Claude 3 haiku/sonnet from 2024. A picker that long buries the model you
    actually want.

    Ordering is derived from :func:`family_tier` and the version number, so no
    model id has to be named here to rank correctly.

    Unparseable ids are dropped rather than kept: everything reaching this
    function is an ``us.anthropic.claude-*`` inference profile, so a
    non-matching id is an embedding/image profile, not a chat model.
    """
    best: dict[str, tuple] = {}
    chosen: dict[str, str] = {}
    for mid in model_ids:
        parsed = claude_family_and_version(mid)
        if parsed is None:
            continue
        family, version = parsed
        if family not in best or version > best[family]:
            best[family] = version
            chosen[family] = mid

    # Sort by capability tier, then newest version first within a tier, then id
    # for a stable result.
    def sort_key(family: str):
        return (family_tier(family), tuple(-p for p in best[family]), chosen[family])

    return [chosen[f] for f in sorted(chosen, key=sort_key)]


def profile_env_for_provider(provider: str) -> dict:
    """Return the ``AWS_*`` env overrides a provider's requests must run under.

    Returned as a dict rather than applied at import time on purpose: several
    providers coexist in one WebUI process, so a module-level
    ``os.environ["AWS_PROFILE"] = ...`` would let whichever plugin imported
    last decide the account for everybody.
    """
    mapping = {
        "bedrock-mantle": (CODEX_PROFILE, MANTLE_REGION),
        "bedrock": (CLAUDE_CODE_PROFILE, CLAUDE_CODE_REGION),
        "bedrock-personal": (PERSONAL_PROFILE, PERSONAL_REGION),
    }
    profile, region = mapping.get(provider, (None, None))
    if profile is None:
        return {}
    return {
        "AWS_PROFILE": profile,
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
    }


def account_id_for_profile(profile: str) -> str | None:
    """Resolve a profile's AWS account id at runtime, or None.

    Exists so diagnostics can report which account a request bills to without
    account numbers being committed to source. Never raises, and never blocks
    startup: callers treat None as "unknown".
    """
    try:
        import boto3

        session = boto3.Session(profile_name=profile)
        return session.client("sts").get_caller_identity().get("Account")
    except Exception:
        logger.debug("could not resolve account id for profile %r", profile, exc_info=True)
        return None


__all__ = [
    "CODEX_PROFILE", "CLAUDE_CODE_PROFILE", "PERSONAL_PROFILE",
    "CLAUDE_CODE_REGION", "PERSONAL_REGION", "MANTLE_REGION",
    "CLAUDE_CLI_USER_AGENT", "CLAUDE_SEED_MODELS", "CLAUDE_MODELS",
    "family_tier", "claude_family_and_version", "latest_per_family",
    "profile_env_for_provider", "account_id_for_profile",
]
