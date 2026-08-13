#!/usr/bin/env python3
"""Hermetic tests for the shared Bedrock account/model helpers.

No AWS credentials, no network. These guard the model-catalog policy, which is
the part most likely to break silently: a bad reducer either buries a model the
account is entitled to, or forces an unreleased model name into source.

Run:  python -m pytest test_accounts.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "bedrock_accounts_under_test",
    Path(__file__).resolve().parent / "_bedrock_accounts.py",
)
acct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acct)


# ── Id parsing ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("model_id,expected", [
    ("us.anthropic.claude-opus-5", ("opus", (5,))),
    ("us.anthropic.claude-opus-4-8", ("opus", (4, 8))),
    ("us.anthropic.claude-sonnet-4-6", ("sonnet", (4, 6))),
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", ("haiku", (4, 5))),
    # Legacy generation-first form.
    ("us.anthropic.claude-3-5-sonnet-20240620-v1:0", ("sonnet", (3, 5))),
    ("us.anthropic.claude-3-haiku-20240307-v1:0", ("haiku", (3,))),
    # Non-us prefixes still parse.
    ("eu.anthropic.claude-sonnet-5", ("sonnet", (5,))),
])
def test_parses_known_id_shapes(model_id, expected):
    assert acct.claude_family_and_version(model_id) == expected


@pytest.mark.parametrize("model_id", [
    "",
    None,
    "amazon.nova-pro-v1:0",
    "cohere.embed-english-v3",
    "us.anthropic.claude-garbage",
])
def test_rejects_non_claude_chat_ids(model_id):
    assert acct.claude_family_and_version(model_id) is None


def test_build_stamp_is_not_treated_as_a_version():
    """Two ids differing only by date stamp are the same release."""
    a = acct.claude_family_and_version("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    b = acct.claude_family_and_version("us.anthropic.claude-haiku-4-5-20260101-v1:0")
    assert a == b


# ── The reason this module exists ───────────────────────────────────────────
def test_unknown_family_parses_instead_of_being_dropped():
    """A family this file does not name must still be recognised.

    The regex used a fixed alternation of family names, which silently dropped
    any family it did not list. That both hid models the account was entitled to
    and forced unreleased model names to be written into source.
    """
    parsed = acct.claude_family_and_version("us.anthropic.claude-somethingnew-5")
    assert parsed == ("somethingnew", (5,))


def test_unknown_family_ranks_above_the_cheap_tiers():
    """An unrecognised family is more likely a new flagship than a new cheap tier.

    Burying a model the account is entitled to is the worse failure, so unknown
    families sort between opus and sonnet rather than last.
    """
    assert acct.family_tier("opus") < acct.family_tier("somethingnew")
    assert acct.family_tier("somethingnew") < acct.family_tier("sonnet")
    assert acct.family_tier("sonnet") < acct.family_tier("haiku")


def test_no_unreleased_model_names_in_source():
    """The catalog must be derived, never hardcoded.

    Unreleased Anthropic codenames carry internal-only handling restrictions, so
    they must not appear in this repo. They reach the picker through discovery.
    """
    source = (Path(__file__).resolve().parent / "_bedrock_accounts.py").read_text()
    assert "fable" not in source.lower()


# ── latest_per_family ──────────────────────────────────────────────────────
def test_reduces_many_generations_to_latest_per_family():
    discovered = [
        "us.anthropic.claude-opus-5",
        "us.anthropic.claude-opus-4-8",
        "us.anthropic.claude-opus-4-1-20250805-v1:0",
        "us.anthropic.claude-sonnet-5",
        "us.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-3-haiku-20240307-v1:0",
    ]
    out = acct.latest_per_family(discovered)
    assert out == [
        "us.anthropic.claude-opus-5",
        "us.anthropic.claude-sonnet-5",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    ]


def test_orders_by_capability_tier_not_discovery_order():
    """Discovery order is arbitrary; the picker must lead with the best model."""
    out = acct.latest_per_family([
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-sonnet-5",
        "us.anthropic.claude-opus-5",
    ])
    assert out[0] == "us.anthropic.claude-opus-5"
    assert out[-1] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_undiscovered_family_is_placed_by_capability():
    """A family absent from the tier map lands just below opus, above sonnet."""
    out = acct.latest_per_family([
        "us.anthropic.claude-opus-5",
        "us.anthropic.claude-sonnet-5",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-somethingnew-5",
    ])
    assert out.index("us.anthropic.claude-somethingnew-5") == 1


def test_drops_non_chat_profiles():
    out = acct.latest_per_family([
        "us.anthropic.claude-opus-5",
        "amazon.nova-pro-v1:0",
        "cohere.embed-english-v3",
        "stability.stable-diffusion-xl",
    ])
    assert out == ["us.anthropic.claude-opus-5"]


def test_empty_input_yields_empty_output():
    assert acct.latest_per_family([]) == []


def test_result_is_deterministic():
    ids = [
        "us.anthropic.claude-opus-5",
        "us.anthropic.claude-sonnet-5",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    ]
    assert acct.latest_per_family(ids) == acct.latest_per_family(list(reversed(ids)))


# ── Seed catalog ───────────────────────────────────────────────────────────
def test_seed_models_are_parseable_and_public():
    """The seed only backstops failed discovery, so it names long-public models."""
    assert acct.CLAUDE_SEED_MODELS
    for mid in acct.CLAUDE_SEED_MODELS:
        assert acct.claude_family_and_version(mid) is not None
    assert acct.CLAUDE_MODELS is acct.CLAUDE_SEED_MODELS  # back-compat alias


def test_seed_is_a_floor_not_a_catalog():
    """The seed must never be mistaken for the real catalog.

    A provider whose ``fetch_models`` returned the seed verbatim would DOWNGRADE
    the picker after this refactor: the seed deliberately names older public
    models, so any provider using it must attempt discovery first and treat the
    seed only as a fallback. Guards that intent by asserting the seed is strictly
    weaker than what a current account discovers.
    """
    current = acct.latest_per_family([
        "us.anthropic.claude-opus-5",
        "us.anthropic.claude-sonnet-5",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    ])
    assert acct.CLAUDE_SEED_MODELS != current
    seed_opus = [m for m in acct.CLAUDE_SEED_MODELS if "opus" in m]
    assert seed_opus and seed_opus[0] < "us.anthropic.claude-opus-5"


# ── Profile / region wiring ────────────────────────────────────────────────
@pytest.mark.parametrize("provider", ["bedrock", "bedrock-personal", "bedrock-mantle"])
def test_each_provider_gets_a_complete_env_override(provider):
    env = acct.profile_env_for_provider(provider)
    assert set(env) == {"AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"}
    assert env["AWS_REGION"] == env["AWS_DEFAULT_REGION"]
    assert env["AWS_PROFILE"]


def test_unknown_provider_gets_no_override():
    """Never guess an account for a provider that was not mapped."""
    assert acct.profile_env_for_provider("nope") == {}


def test_claude_providers_use_distinct_regions():
    """Same model ids + same region = one shared client-cache slot = wrong bill.

    The core Bedrock adapter caches boto3 clients keyed by region only, so the
    two Claude accounts must not share a region.
    """
    internal = acct.profile_env_for_provider("bedrock")
    personal = acct.profile_env_for_provider("bedrock-personal")
    assert internal["AWS_REGION"] != personal["AWS_REGION"]
    assert internal["AWS_PROFILE"] != personal["AWS_PROFILE"]


def test_no_account_ids_in_source():
    """Account numbers are resolved at runtime, never committed."""
    import re

    source = (Path(__file__).resolve().parent / "_bedrock_accounts.py").read_text()
    assert not re.search(r"\b\d{12}\b", source)


def test_account_lookup_never_raises_without_credentials():
    """Diagnostics must degrade to 'unknown', not crash startup."""
    assert acct.account_id_for_profile("definitely-not-a-real-profile") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
