"""Shared helpers for the three Amazon Bedrock access paths.

Amazon vends a **separate AWS account per tool**, and they are not
interchangeable. Verified 2026-08-04:

===========================  ============  =====================  ==========  ======
profile                      account       role                   Mantle GPT  Claude
===========================  ============  =====================  ==========  ======
``codex-DO-NOT-DELETE``      493765493388  CaminusBedrockAccess   yes         yes
``claude-code-DO-NOT-DELETE``175342148895  CeceliaAmazonInternal  **denied**  yes
``claude`` (personal ALPHA)  333843746513  IibsAdminAccess        yes         yes
===========================  ============  =====================  ==========  ======

Hermes surfaces these as four providers so the model picker can express which
account a request bills to:

==========================  ==================================  ===================
provider                    path                                credential
==========================  ==================================  ===================
``bedrock-mantle``          internal GPT (Responses API)        codex account
``bedrock``                 internal Claude (Converse)          claude-code account
``bedrock-personal``        personal Claude (Converse)          personal account
``bedrock-mantle-personal`` personal GPT (Responses API)        personal account
==========================  ==================================  ===================

The region-collision problem
----------------------------
``agent/bedrock_adapter.py`` caches boto3 clients **keyed by region only**, and
resolves credentials from the process-global chain. Two Claude accounts serving
the *same* ``us.`` model ids in the *same* region would therefore share one
cached client, and whichever provider warmed the cache first would silently bill
both.

The accounts differ in regional reach, which supplies a natural key:

* ``claude-code-DO-NOT-DELETE`` works in **us-west-2 only** — us-east-1 returns
  ``AccessDeniedException``.
* the personal ``claude`` account works in us-west-2, **us-east-1** and us-east-2.

So internal Claude stays on us-west-2 and personal Claude is pinned to
us-east-1. Different region -> different cache slot -> no credential bleed,
without patching core.

``AWS_PROFILE`` is still process-global, so it is applied per request rather
than at import time; see :func:`profile_env_for_provider`.
"""

from __future__ import annotations

import re

# ── Account map ─────────────────────────────────────────────────────────────
CODEX_PROFILE = "codex-DO-NOT-DELETE"
CLAUDE_CODE_PROFILE = "claude-code-DO-NOT-DELETE"
PERSONAL_PROFILE = "claude"

CODEX_ACCOUNT = "493765493388"
CLAUDE_CODE_ACCOUNT = "175342148895"
PERSONAL_ACCOUNT = "333843746513"

# ── Regions ─────────────────────────────────────────────────────────────────
# Internal Claude is only entitled in us-west-2.
CLAUDE_CODE_REGION = "us-west-2"
# Personal Claude pinned to us-east-1 to keep a distinct client-cache slot.
PERSONAL_REGION = "us-east-1"
# Mantle GPT is served from us-east-2.
MANTLE_REGION = "us-east-2"

# User-Agent required by the CeceliaAmazonInternal IAM condition on the
# claude-code account. Streaming 403s without the `claude-cli/` prefix.
CLAUDE_CLI_USER_AGENT = "claude-cli/2.1.131 (external, sdk-cli)"

# ── Claude model catalog (Bedrock inference profiles) ───────────────────────
# Both Claude accounts expose an identical 25-entry list; verified via
# `aws bedrock list-inference-profiles` on each.
#
# Reduced to the LATEST of each family (see latest_per_family). Discovery still
# runs and is filtered through the same reducer, so a newly released
# `us.anthropic.claude-opus-6` appears without editing this file.
CLAUDE_MODELS = [
    "us.anthropic.claude-opus-5",
    "us.anthropic.claude-sonnet-5",
    "us.anthropic.claude-fable-5",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
]

# Family display order in the picker (most capable first).
CLAUDE_FAMILY_ORDER = ["opus", "sonnet", "fable", "haiku"]

# Max output tokens per Claude model, verified empirically against Converse.
CLAUDE_MAX_OUTPUT = {
    "us.anthropic.claude-opus-5": 128000,
    "us.anthropic.claude-sonnet-5": 128000,
    "us.anthropic.claude-fable-5": 64000,
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": 64000,
}

_CLAUDE_ID_RE = re.compile(
    r"^(?P<prefix>us|global|eu|ap)\.anthropic\.claude-"
    r"(?:(?P<legacy>3(?:-5|-7)?)-(?P<legacyfam>opus|sonnet|haiku)"
    r"|(?P<family>opus|sonnet|haiku|fable)-(?P<ver>[0-9]+(?:-[0-9]+)?))"
)


def claude_family_and_version(model_id: str):
    """Return ``(family, version_tuple)`` for a Claude id, or ``None``.

    Handles both modern ids (``claude-opus-4-8`` -> ``("opus", (4, 8))``,
    ``claude-sonnet-5`` -> ``("sonnet", (5,))``) and the legacy
    generation-first form (``claude-3-5-sonnet`` -> ``("sonnet", (3, 5))``).
    A trailing date/revision (``-20251001-v1:0``) is deliberately ignored: it
    is a build stamp, not a version, and two ids differing only by stamp are
    the same model release.
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
    """Keep only the highest-versioned id per Claude family, ordered sensibly.

    Why this exists: ``list_inference_profiles`` returns every generation ever
    entitled to the account — 13 ``us.anthropic.*`` ids covering opus 4.1
    through 5, three sonnet generations, and Claude 3 haiku/sonnet from 2024.
    A picker that long buries the model you actually want.

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

    def sort_key(family: str) -> int:
        try:
            return CLAUDE_FAMILY_ORDER.index(family)
        except ValueError:
            return len(CLAUDE_FAMILY_ORDER)

    return [chosen[f] for f in sorted(chosen, key=sort_key)]


def profile_env_for_provider(provider: str) -> dict:
    """Return the ``AWS_*`` env overrides a provider's requests must run under.

    Returned as a dict rather than applied at import time on purpose: three
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


__all__ = [
    "CODEX_PROFILE", "CLAUDE_CODE_PROFILE", "PERSONAL_PROFILE",
    "CODEX_ACCOUNT", "CLAUDE_CODE_ACCOUNT", "PERSONAL_ACCOUNT",
    "CLAUDE_CODE_REGION", "PERSONAL_REGION", "MANTLE_REGION",
    "CLAUDE_CLI_USER_AGENT", "CLAUDE_MODELS", "CLAUDE_MAX_OUTPUT",
    "CLAUDE_FAMILY_ORDER", "claude_family_and_version", "latest_per_family",
    "profile_env_for_provider",
]
