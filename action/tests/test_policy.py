"""Policy file parsing: strict, fail closed, safe defaults (modelled on gh-aw's validation)."""

from pathlib import Path

import pytest

from mcp_runner.loader import load_server
from mcp_runner.manifest import build_manifest
from mcp_runner.policy import DEFAULT_LIMITS, PolicyError, load_policy, parse_policy
from mcp_runner.security import Redactor, unverified_claims

TEMPLATE = Path(__file__).resolve().parents[2] / "template"


def test_missing_file_gives_safe_defaults(tmp_path):
    policy = load_policy(tmp_path / "nope.toml")
    assert policy == {"allow": None, "require_approval": [], "limits": DEFAULT_LIMITS}


def test_partial_limits_keep_other_defaults():
    policy = parse_policy({"limits": {"max_calls_per_hour": 10}})
    assert policy["limits"]["max_calls_per_hour"] == 10
    assert policy["limits"]["max_concurrent_calls"] == DEFAULT_LIMITS["max_concurrent_calls"]


@pytest.mark.parametrize("raw, match", [
    ({"tool": {}}, "Unknown section"),                                   # typo'd section
    ({"tools": {"alow": ["a"]}}, "Unknown key"),                          # typo'd key
    ({"limits": {"max_calls": 1}}, "Unknown key"),
    ({"limits": {"max_calls_per_hour": 0}}, "positive integer"),
    ({"limits": {"max_calls_per_hour": -5}}, "positive integer"),
    ({"limits": {"max_calls_per_hour": True}}, "positive integer"),        # bool is not an int here
    ({"limits": {"max_calls_per_hour": "10"}}, "positive integer"),
    ({"limits": {"call_timeout_seconds": 10**9}}, "at most"),
    ({"tools": {"allow": "add"}}, "list of tool names"),
    ({"tools": {"allow": ["add", "add"]}}, "duplicate"),
    ({"tools": []}, "must be a table"),
])
def test_invalid_policies_are_rejected(raw, match):
    with pytest.raises(PolicyError, match=match):
        parse_policy(raw)


def test_invalid_toml(tmp_path):
    (tmp_path / "p.toml").write_text("[tools\n")
    with pytest.raises(PolicyError, match="not valid TOML"):
        load_policy(tmp_path / "p.toml")


@pytest.fixture
def server():
    return load_server("server.py:mcp", TEMPLATE)


async def test_allow_list_hides_tools(server):
    manifest = await build_manifest(server, policy=parse_policy({"tools": {"allow": ["add"]}}))
    assert [t["name"] for t in manifest["tools"]] == ["add"]
    assert manifest["approval_required"] == []


async def test_empty_allow_list_publishes_nothing(server):
    # gh-aw semantics: omitted = no restriction, empty = deny all.
    manifest = await build_manifest(server, policy=parse_policy({"tools": {"allow": []}}))
    assert manifest["tools"] == []


async def test_require_approval_adds_to_tags(server):
    manifest = await build_manifest(server, policy=parse_policy({"tools": {"require_approval": ["internal_api_get"]}}))
    assert manifest["approval_required"] == ["internal_api_get", "restart_service"]


async def test_unknown_tool_names_are_errors(server):
    with pytest.raises(PolicyError, match="doesn't have"):
        await build_manifest(server, policy=parse_policy({"tools": {"require_approval": ["restrat_service"]}}))
    with pytest.raises(PolicyError, match="doesn't have"):
        await build_manifest(server, policy=parse_policy({"tools": {"allow": ["ad"]}}))


async def test_gated_tool_hidden_by_allow_is_an_error(server):
    raw = {"tools": {"allow": ["add"], "require_approval": ["internal_api_get"]}}
    with pytest.raises(PolicyError, match="hides"):
        await build_manifest(server, policy=parse_policy(raw))


def test_redactor_matches_secret_names_and_explicit_list():
    env = {"DB_PASSWORD": "hunter2-hunter2", "PLAIN": "not-secret-value", "CUSTOM": "custom-secret-1",
           "MCP_REDACT_ENV": "CUSTOM", "SHORT_TOKEN": "abc"}
    r = Redactor(env)
    out, n = r.redact({"a": ["pw hunter2-hunter2", "custom-secret-1"], "b": "not-secret-value", "c": "abc"})
    assert out == {"a": ["pw ***", "***"], "b": "not-secret-value", "c": "abc"}  # short values are left alone
    assert n == 2


def test_unverified_claims_rejects_garbage():
    from mcp_runner.security import SecurityError

    with pytest.raises(SecurityError):
        unverified_claims("not-a-jwt")
