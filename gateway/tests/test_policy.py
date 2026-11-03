"""Formal tests for the gateway access policy, in the style of gh-aw's mcp_access tests.

Properties checked:
- each guard denies on its own with its own code;
- when several guards fail, the FIRST in order picks the code
  (tool -> size -> schema -> rate -> concurrency);
- no spurious allow: any single failing guard means deny;
- fail safe: bad or missing limits never mean "unlimited", and repo limits
  can't exceed the gateway's ceilings.
"""

import itertools

import pytest

from app.policy import Code, Limits, evaluate

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"],
          "additionalProperties": False}
MANIFEST = {"tools": [{"name": "t", "inputSchema": SCHEMA}]}
LIMITS = Limits(max_calls_per_hour=10, max_concurrent_calls=2, max_argument_bytes=100)
OK = dict(tool="t", arguments={"n": 1}, calls_last_hour=0, active_calls=0)


def check(**over):
    req = {**OK, **over}
    return evaluate(MANIFEST, req["tool"], req["arguments"], limits=LIMITS,
                    calls_last_hour=req["calls_last_hour"], active_calls=req["active_calls"])


# One way to make each guard fail, in guard order.
FAILURES = {
    Code.TOOL_NOT_FOUND: {"tool": "missing"},
    Code.ARGUMENTS_TOO_LARGE: {"arguments": {"n": 1, "pad": "x" * 200}},
    Code.ARGUMENTS_INVALID: {"arguments": {"n": "not-an-int"}},
    Code.RATE_LIMITED: {"calls_last_hour": 10},
    Code.TOO_MANY_CONCURRENT: {"active_calls": 2},
}
ORDER = list(FAILURES)


def test_allows_valid_call():
    assert check() is None


@pytest.mark.parametrize("code", ORDER)
def test_each_guard_denies_alone(code):
    denial = check(**FAILURES[code])
    assert denial is not None and denial.code == code


@pytest.mark.parametrize("combo", [c for r in range(2, len(ORDER) + 1) for c in itertools.combinations(ORDER, r)])
def test_first_failing_guard_wins(combo):
    over = {}
    for code in combo:
        # ARGUMENTS_TOO_LARGE and ARGUMENTS_INVALID both set "arguments": merge them so both fail.
        if "arguments" in FAILURES[code] and "arguments" in over:
            over["arguments"] = {**FAILURES[code]["arguments"], **over["arguments"], "n": "not-an-int"}
        else:
            over.update(FAILURES[code])
    denial = check(**over)
    assert denial is not None
    assert denial.code == min(combo, key=ORDER.index)


def test_no_spurious_allow():
    for code, over in FAILURES.items():
        assert check(**over) is not None, code


def test_empty_or_odd_tool_names_are_denied():
    for tool in ("", None, 3, "T"):
        assert check(tool=tool).code == Code.TOOL_NOT_FOUND


def test_non_object_arguments_denied():
    assert check(arguments=["n", 1]).code == Code.ARGUMENTS_INVALID


def test_invalid_schema_fails_closed():
    manifest = {"tools": [{"name": "t", "inputSchema": {"type": "not-a-type"}}]}
    denial = evaluate(manifest, "t", {}, limits=LIMITS, calls_last_hour=0, active_calls=0)
    assert denial.code == Code.ARGUMENTS_INVALID


def test_missing_manifest_denies_everything():
    assert evaluate(None, "t", {}, limits=LIMITS, calls_last_hour=0, active_calls=0).code == Code.TOOL_NOT_FOUND


def test_denial_result_shape():
    result = check(tool="missing").to_result()
    assert result["isError"] is True
    assert result["_meta"]["mcp-github-runner/denial"] == {"code": -32001, "by": "gateway", "reason": "unknown tool 'missing'"}


# -- limits ---------------------------------------------------------------------

CEILINGS = Limits(max_calls_per_hour=100, max_concurrent_calls=3, max_argument_bytes=1000,
                  max_result_bytes=1000, call_timeout_seconds=60, approval_timeout_seconds=600)


def test_limits_default_when_missing():
    eff = Limits.effective({}, CEILINGS)
    assert eff.max_calls_per_hour == 100  # default 120 clamped to the ceiling
    assert eff.max_concurrent_calls == 3


def test_limits_can_only_tighten():
    eff = Limits.effective({"policy": {"limits": {"max_calls_per_hour": 5, "call_timeout_seconds": 9999}}}, CEILINGS)
    assert eff.max_calls_per_hour == 5
    assert eff.call_timeout_seconds == 60


@pytest.mark.parametrize("bad", [0, -1, True, "10", None, 1.5])
def test_bad_limit_values_never_mean_unlimited(bad):
    eff = Limits.effective({"policy": {"limits": {"max_calls_per_hour": bad}}}, CEILINGS)
    assert 0 < eff.max_calls_per_hour <= CEILINGS.max_calls_per_hour
