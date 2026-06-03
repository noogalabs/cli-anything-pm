"""Core unit tests for the fail-loud exit-code behavior in output_json.

output_json is the single shared output path for every CLI command. The
fix makes it exit(1) AFTER printing when the payload is a result envelope
reporting failure ({"ok": False, ...}), so shell callers and crons checking
$? no longer treat failed assigns/schedules/merges as success (exit 0).

This pins the central mechanism directly; the per-command-group CLI tests
exercise it end-to-end through real backend {ok: False} returns.
"""
import json

import pytest

from cli_anything.propertymeld.utils import output_json


def test_ok_false_exits_1_after_printing(capsys):
    with pytest.raises(SystemExit) as exc:
        output_json({"ok": False, "error": "tech not found"})
    assert exc.value.code == 1
    # The error envelope is still printed (operator sees the reason) BEFORE exit.
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is False
    assert printed["error"] == "tech not found"


def test_ok_true_does_not_exit(capsys):
    # Success envelope: print, no exit.
    output_json({"ok": True, "meld_id": 123})
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True


def test_ok_absent_does_not_exit(capsys):
    # Read commands print payloads with no "ok" key — must never exit.
    output_json({"id": 123, "status": "OPEN"})
    assert json.loads(capsys.readouterr().out)["id"] == 123


def test_list_payload_does_not_exit(capsys):
    # Read commands also print bare lists — not a dict, must never exit.
    output_json([{"id": 1}, {"id": 2}])
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_ok_none_does_not_exit(capsys):
    # An explicit ok:null is not a failure (only ok is False triggers exit).
    output_json({"ok": None, "info": "n/a"})
    assert json.loads(capsys.readouterr().out)["info"] == "n/a"
