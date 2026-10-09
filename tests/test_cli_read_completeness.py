"""Synthetic CLI boundary and compatibility controls, with all wire I/O blocked."""
import json

import pytest
from click.testing import CliRunner

from cli_anything.propertymeld import api_backend as api, http_backend as hb
from cli_anything.propertymeld.cli import cli
from cli_anything.propertymeld.pagination import PaginationError


@pytest.fixture(autouse=True)
def no_wire(monkeypatch):
    monkeypatch.setattr(hb.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("unexpected network"))


@pytest.mark.parametrize("args,target", [
    (["comments", "900001"], "get_comments"),
    (["files", "900001"], "list_files"),
    (["work-entries", "list", "900001"], "list_work_entries"),
    (["work-entries", "900001"], "list_work_entries"),
])
def test_optin_child_envelope_and_legacy_shape(monkeypatch, args, target):
    seen = []
    def read(meld_id, **kw):
        seen.append((meld_id, kw))
        return {"schema_version": 1, "results": [{"id": 1}], "count": 1, "next": None, "complete": True} if kw else [{"id": 1}]
    monkeypatch.setattr(hb, target, read)
    legacy = CliRunner().invoke(cli, ["work-orders", *args])
    assert legacy.exit_code == 0 and json.loads(legacy.stdout) == [{"id": 1}]
    complete = CliRunner().invoke(cli, ["work-orders", *args, "--complete"])
    assert complete.exit_code == 0 and json.loads(complete.stdout)["complete"] is True
    assert seen[-1] == ("900001", {"complete": True}) and seen[0][1] == {}


@pytest.mark.parametrize("args,target,backend", [
    (["comments", "900001"], "get_comments", hb),
    (["files", "900001"], "list_files", hb),
    (["work-entries", "list", "900001"], "list_work_entries", hb),
    (["notes", "900001"], "get_notes", hb),
    (["get", "900001"], "get_work_order_complete", api),
    (["list"], "list_work_orders", api),
])
def test_requested_complete_failure_is_structured_nonzero(monkeypatch, args, target, backend):
    def fail(*a, **kw):
        raise PaginationError("truncated_count")
    monkeypatch.setattr(backend, target, fail)
    result = CliRunner().invoke(cli, ["work-orders", *args, "--complete"])
    assert result.exit_code == 1 and not result.stdout
    assert json.loads(result.stderr)["code"] == "truncated_count"
    assert "Traceback" not in result.stderr


def test_get_default_shape_and_optin_route(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "get_work_order", lambda *a, **kw: {"id": 900001})
    monkeypatch.setattr(api, "get_work_order_complete", lambda *a, **kw: calls.append((a, kw)) or {"schema_version": 1, "result": {"id": 900001}, "complete": True})
    legacy = CliRunner().invoke(cli, ["work-orders", "get", "900001"])
    assert legacy.exit_code == 0 and json.loads(legacy.stdout) == {"id": 900001} and not calls
    complete = CliRunner().invoke(cli, ["work-orders", "get", "900001", "--complete"])
    assert complete.exit_code == 0 and json.loads(complete.stdout)["complete"] and len(calls) == 1


def test_list_flag_optin_without_new_default_argument(monkeypatch):
    calls = []
    def read(**kw):
        calls.append(kw)
        return {"complete": True, "results": [{"id": 1}, {"id": 2}]} if kw.get("complete") else [{"id": 1}]
    monkeypatch.setattr(api, "list_work_orders", read)
    legacy = CliRunner().invoke(cli, ["work-orders", "list", "--limit", "1"])
    complete = CliRunner().invoke(cli, ["work-orders", "list", "--limit", "1", "--complete"])
    assert legacy.exit_code == complete.exit_code == 0
    assert "complete" not in calls[0] and calls[1]["complete"] is True
    assert len(json.loads(complete.stdout)["results"]) == 2


def test_no_updated_since_option_or_wire_claim():
    result = CliRunner().invoke(cli, ["work-orders", "list", "--updated-since", "2026-01-01", "--complete"])
    assert result.exit_code == 2 and "No such option" in result.stderr


def test_actual_complete_expiry_cli_is_structured_without_helper(monkeypatch):
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    def expired(*args):
        raise hb.SessionExpired(hb.urllib.error.HTTPError("https://synthetic.invalid/", 401, "expired", {}, None))
    monkeypatch.setattr(hb, "_http_get", expired)
    monkeypatch.setattr(hb, "_attempt_recapture", lambda: pytest.fail("no recapture"))
    result = CliRunner().invoke(cli, ["work-orders", "comments", "900001", "--complete"])
    assert result.exit_code == 1 and not result.stdout
    assert json.loads(result.stderr)["code"] == "session_expired"
