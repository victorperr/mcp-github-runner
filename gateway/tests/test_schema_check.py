"""Argument checks run in killable worker processes, so a tenant schema can't stall the gateway."""

import time

import pytest

from app.policy import Code, _validator, validate_arguments
from app.schema_check import SchemaChecker

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
# Catastrophic backtracking: exponential time on "aaaa...!" with Python's re.
EVIL = {"type": "object", "properties": {"s": {"type": "string", "pattern": "^(a+)+$"}}}


@pytest.fixture
def checker():
    c = SchemaChecker(timeout=1.0, workers=1)
    yield c
    c.close()


async def test_valid_and_invalid_arguments(checker):
    assert await checker.check(SCHEMA, {"n": 1}) is None
    denial = await checker.check(SCHEMA, {"n": "x"})
    assert denial.code == Code.ARGUMENTS_INVALID and "schema" in denial.reason
    denial = await checker.check({"type": "not-a-type"}, {})
    assert denial.code == Code.ARGUMENTS_INVALID and "inputSchema is invalid" in denial.reason


async def test_catastrophic_regex_is_cut_off(checker):
    await checker.check(SCHEMA, {"n": 1})  # warm the worker up
    t0 = time.monotonic()
    denial = await checker.check(EVIL, {"s": "a" * 40 + "!"})
    assert time.monotonic() - t0 < 5
    assert denial.code == Code.ARGUMENTS_INVALID and "took over" in denial.reason
    # The killed worker is replaced: later checks still work.
    assert await checker.check(SCHEMA, {"n": 1}) is None


def test_validator_is_cached():
    _validator.cache_clear()
    for _ in range(3):
        assert validate_arguments(SCHEMA, {"n": 1}) is None
    assert _validator.cache_info().hits == 2
