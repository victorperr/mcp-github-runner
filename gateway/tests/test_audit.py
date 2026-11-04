"""Audit events must reach stdout by default: before, nothing configured the logger and INFO lines were lost."""

import json
import logging

from app import audit as audit_mod
from app.main import create_app

from .conftest import make_settings


def _reset():
    for h in list(audit_mod.logger.handlers):
        audit_mod.logger.removeHandler(h)
    audit_mod.logger.propagate = True
    audit_mod.logger.setLevel(logging.NOTSET)


def test_audit_lines_go_to_stdout_by_default(tmp_path, dispatcher, capsys):
    _reset()
    create_app(make_settings(tmp_path), dispatcher=dispatcher)
    create_app(make_settings(tmp_path), dispatcher=dispatcher)  # no duplicate handler
    capsys.readouterr()
    audit_mod.audit("call.created", repo="me/demo", call_id="c1", tool="add", skipped=None)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "call.created" and record["repo"] == "me/demo" and "skipped" not in record


def test_audit_stdout_can_be_turned_off(tmp_path, dispatcher, capsys):
    _reset()
    create_app(make_settings(tmp_path, audit_log="off"), dispatcher=dispatcher)
    capsys.readouterr()
    audit_mod.audit("call.created", repo="me/demo")
    assert capsys.readouterr().out == ""
    assert audit_mod.logger.handlers == []


def test_operator_handler_is_kept(tmp_path, dispatcher):
    _reset()
    mine = logging.NullHandler()
    audit_mod.logger.addHandler(mine)
    try:
        create_app(make_settings(tmp_path), dispatcher=dispatcher)
        assert audit_mod.logger.handlers == [mine]
    finally:
        _reset()
