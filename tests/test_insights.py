"""Recorded-shape tests for the read-only PropertyMeld Insights client."""

import ast
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as parquet
import pytest
from click.testing import CliRunner

from cli_anything.propertymeld import api_backend, insights_backend
from cli_anything.propertymeld.cli import cli


def _parquet_bytes(rows):
    sink = pa.BufferOutputStream()
    parquet.write_table(pa.Table.from_pylist(rows), sink)
    return sink.getvalue().to_pybytes()


def _response(payload):
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.geturl.return_value = (
        "https://app.propertymeld.com/3287/m/3287/api/analytics/parquet/"
        "raw_meld_data.parquet"
    )
    response.read.return_value = payload
    return response


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    path = tmp_path / "property-meld.json"
    path.write_text(json.dumps({
        "cookies": [{
            "name": "sessionid",
            "value": "recorded-test-session",
            "domain": ".propertymeld.com",
        }]
    }))
    monkeypatch.setenv("PM_CREDS_PATH", str(path))
    monkeypatch.setenv("PM_MULTITENANT_ID", "3287")
    return path


RECORDED_MELDS = [
    {
        "meld_meld_id": 101,
        "meld_meld_created": "2026-08-01T10:00:00Z",
        "meld_meld_status": "COMPLETED",
        "meld_meld_work_category": "TURNOVER",
        "meld_meld_project_id": 7001,
        "vendor_assigned_name": "  DBH   Construction ",
        "inhouse_servicer_name": None,
        "meld_meld_coordinator_id": 57163,
        "meld_meld_coordinator_name": "Coordinator",
        "meld_meld_assigned_to_accepted_for_vendor": 30.0,
        "meld_meld_accepted_to_scheduled_for_vendor": 60.0,
        "meld_meld_assigned_to_scheduled": 90.0,
        "meld_meld_assigned_to_completed": 180.0,
        "meld_meld_tenant_rating": 5,
        "invoice_amount": 250.0,
        "expenditure_amount": None,
        "total_worklog_hours": 2.5,
        "vendor_chat_response_seconds": 45,
        "meld_meld_brief_description": "resident text must never leave projection",
        "workentry_log": "private notes must never leave projection",
    },
    {
        "meld_meld_id": 102,
        "meld_meld_created": "2026-08-02T10:00:00Z",
        "meld_meld_status": "COMPLETED",
        "meld_meld_work_category": "TURNOVER",
        "meld_meld_project_id": None,
        "vendor_assigned_name": "Unknown Vendor",
        "inhouse_servicer_name": None,
        "meld_meld_coordinator_id": 57163,
        "meld_meld_coordinator_name": "Coordinator",
        "meld_meld_assigned_to_accepted_for_vendor": None,
        "meld_meld_accepted_to_scheduled_for_vendor": None,
        "meld_meld_assigned_to_scheduled": None,
        "meld_meld_assigned_to_completed": 240.0,
        "meld_meld_tenant_rating": None,
        "invoice_amount": None,
        "expenditure_amount": None,
        "total_worklog_hours": None,
        "vendor_chat_response_seconds": None,
        "meld_meld_brief_description": "another private description",
        "workentry_log": None,
    },
    {
        "meld_meld_id": 103,
        "meld_meld_created": "2026-08-03T10:00:00Z",
        "meld_meld_status": "PENDING_ASSIGNMENT",
        "meld_meld_work_category": "PLUMBING",
        "meld_meld_project_id": None,
        "vendor_assigned_name": None,
        "inhouse_servicer_name": "Technician",
        "meld_meld_coordinator_id": 57163,
        "meld_meld_coordinator_name": "Coordinator",
        "meld_meld_assigned_to_accepted_for_vendor": None,
        "meld_meld_accepted_to_scheduled_for_vendor": None,
        "meld_meld_assigned_to_scheduled": None,
        "meld_meld_assigned_to_completed": None,
        "meld_meld_tenant_rating": None,
        "invoice_amount": None,
        "expenditure_amount": 75.0,
        "total_worklog_hours": 1.0,
        "vendor_chat_response_seconds": None,
        "meld_meld_brief_description": "private",
        "workentry_log": "private",
    },
]

RECORDED_BENCHMARKS = [{
    "unit_count": "251-500",
    "priority": "NORMAL",
    "work_category": "TURNOVER",
    "region": "SOUTHEAST",
    "is_project": True,
    "completed_month": "2026-07-01T00:00:00Z",
    "sor_5th": 1.0,
    "sor_25th": 2.0,
    "sor_50th": 3.0,
    "sor_75th": 4.0,
    "sor_95th": 5.0,
    "res_sat_5th": 1.0,
    "res_sat_25th": 2.0,
    "res_sat_50th": 3.0,
    "res_sat_75th": 4.0,
    "res_sat_95th": 5.0,
    "invoice_spend_5th": 10.0,
    "invoice_spend_25th": 20.0,
    "invoice_spend_50th": 30.0,
    "invoice_spend_75th": 40.0,
    "invoice_spend_95th": 50.0,
    "expenditure_spend_5th": 11.0,
    "expenditure_spend_25th": 21.0,
    "expenditure_spend_50th": 31.0,
    "expenditure_spend_75th": 41.0,
    "expenditure_spend_95th": 51.0,
}]


def test_melds_uses_exact_get_and_resolves_without_leaking_free_text(credentials):
    payload = _parquet_bytes(RECORDED_MELDS)
    roster = [{"id": 91159, "name": "DBH Construction"}]
    with patch("urllib.request.urlopen", return_value=_response(payload)) as opener, patch(
        "cli_anything.propertymeld.api_backend.list_vendors", return_value=roster
    ) as list_vendors:
        result = insights_backend.get_melds(limit=10)

    request = opener.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url == (
        "https://app.propertymeld.com/3287/m/3287/api/analytics/parquet/"
        "raw_meld_data.parquet"
    )
    list_vendors.assert_called_once_with(limit=None)
    assert result["matched_count"] == 3
    assert result["returned_count"] == 3
    assert len(result["rows"]) == 3
    assert result["rows"][0]["vendor_resolution"] == {
        "status": "resolved",
        "source_name": "  DBH   Construction ",
        "vendor_id": 91159,
        "vendor_name": "DBH Construction",
    }
    assert result["rows"][1]["vendor_resolution"]["status"] == "unresolved"
    assert result["rows"][2]["vendor_resolution"]["status"] == "not_applicable"
    assert result["vendor_resolution"]["unresolved_names"] == ["Unknown Vendor"]
    for row in result["rows"]:
        assert "meld_meld_brief_description" not in row
        assert "workentry_log" not in row


def test_turnovers_filter_preserves_unresolved_rows(credentials):
    payload = _parquet_bytes(RECORDED_MELDS)
    with patch("urllib.request.urlopen", return_value=_response(payload)), patch(
        "cli_anything.propertymeld.api_backend.list_vendors", return_value=[]
    ):
        result = insights_backend.get_melds(limit=10, turnovers_only=True)

    assert result["dataset"] == "turnovers"
    assert [row["meld_meld_id"] for row in result["rows"]] == [101, 102]
    assert all(row["vendor_resolution"]["status"] == "unresolved" for row in result["rows"])


def test_meld_project_filter_treats_nan_as_non_project(credentials):
    rows = [
        {
            "meld_meld_id": 201,
            "meld_meld_work_category": "PLUMBING",
            "meld_meld_project_id": 7001.0,
            "vendor_assigned_name": None,
        },
        {
            "meld_meld_id": 202,
            "meld_meld_work_category": "PLUMBING",
            "meld_meld_project_id": float("nan"),
            "vendor_assigned_name": None,
        },
    ]
    payload = _parquet_bytes(rows)
    with patch("urllib.request.urlopen", return_value=_response(payload)), patch(
        "cli_anything.propertymeld.api_backend.list_vendors", return_value=[]
    ):
        projects = insights_backend.get_melds(limit=10, project=True)
        non_projects = insights_backend.get_melds(limit=10, project=False)

    assert [row["meld_meld_id"] for row in projects["rows"]] == [201]
    assert [row["meld_meld_id"] for row in non_projects["rows"]] == [202]
    assert non_projects["rows"][0]["meld_meld_project_id"] is None
    assert projects["project_missing_count"] == 1
    assert non_projects["project_missing_count"] == 1


@pytest.mark.parametrize(("flag", "expected_ids"), [
    ("--project", [201]),
    ("--non-project", [202]),
])
def test_cli_project_flags_handle_real_nan(credentials, flag, expected_ids):
    rows = [
        {
            "meld_meld_id": 201,
            "meld_meld_work_category": "PLUMBING",
            "meld_meld_project_id": 7001.0,
            "vendor_assigned_name": None,
        },
        {
            "meld_meld_id": 202,
            "meld_meld_work_category": "PLUMBING",
            "meld_meld_project_id": float("nan"),
            "vendor_assigned_name": None,
        },
    ]
    payload = _parquet_bytes(rows)
    with patch("urllib.request.urlopen", return_value=_response(payload)), patch(
        "cli_anything.propertymeld.api_backend.list_vendors", return_value=[]
    ):
        result = CliRunner().invoke(cli, ["insights", "melds", flag, "--limit", "10"])

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert [row["meld_meld_id"] for row in output["rows"]] == expected_ids
    assert output["project_missing_count"] == 1


def test_vendor_name_collision_is_ambiguous_and_row_is_not_dropped():
    rows = [{"meld_meld_id": 1, "vendor_assigned_name": "Same Vendor"}]
    vendors = [
        {"id": 1, "name": "Same Vendor"},
        {"id": 2, "name": " same   vendor "},
    ]
    enriched, summary = insights_backend.resolve_vendor_names(rows, vendors)
    assert len(enriched) == 1
    resolution = enriched[0]["vendor_resolution"]
    assert resolution["status"] == "ambiguous"
    assert [match["vendor_id"] for match in resolution["matches"]] == [1, 2]
    assert summary["ambiguous_names"] == ["Same Vendor"]


def test_duplicate_roster_rows_with_same_id_are_not_false_ambiguous():
    rows = [{"meld_meld_id": 1, "vendor_assigned_name": "Same Vendor"}]
    vendors = [
        {"id": 1, "name": "Same Vendor"},
        {"id": 1, "name": " same   vendor "},
    ]
    enriched, summary = insights_backend.resolve_vendor_names(rows, vendors)
    assert enriched[0]["vendor_resolution"]["status"] == "resolved"
    assert enriched[0]["vendor_resolution"]["vendor_id"] == 1
    assert summary["ambiguous_names"] == []


def test_vendor_roster_unbounded_mode_exhausts_nexus_pagination():
    first_page = {
        "next": "https://app.propertymeld.com/api/v2/vendor/?cursor=second",
        "results": [{"id": 1, "name": "First"}],
    }
    second_page = {
        "next": None,
        "results": [{"id": 2, "name": "Second"}],
    }
    with patch.object(api_backend, "_api_get", side_effect=[first_page, second_page]) as getter:
        rows = api_backend.list_vendors(limit=None)
    assert rows == [{"id": 1, "name": "First"}, {"id": 2, "name": "Second"}]
    assert getter.call_count == 2


def test_benchmarks_exact_endpoint_and_recorded_shape(credentials):
    payload = _parquet_bytes(RECORDED_BENCHMARKS)
    response = _response(payload)
    response.geturl.return_value = (
        "https://app.propertymeld.com/3287/m/3287/api/analytics/parquet/"
        "benchmarks.parquet"
    )
    with patch("urllib.request.urlopen", return_value=response) as opener:
        result = insights_backend.get_benchmarks(
            limit=10,
            work_category="turnover",
            priority="normal",
            region="southeast",
            project=True,
        )
    request = opener.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.full_url.endswith("/api/analytics/parquet/benchmarks.parquet")
    assert result["returned_count"] == 1
    assert result["rows"][0]["sor_50th"] == 3.0


def test_benchmark_project_filter_treats_nan_as_non_project(credentials):
    project_row = dict(RECORDED_BENCHMARKS[0], is_project=1.0, priority="NORMAL")
    non_project_row = dict(RECORDED_BENCHMARKS[0], is_project=float("nan"), priority="HIGH")
    payload = _parquet_bytes([project_row, non_project_row])
    response = _response(payload)
    response.geturl.return_value = (
        "https://app.propertymeld.com/3287/m/3287/api/analytics/parquet/"
        "benchmarks.parquet"
    )
    with patch("urllib.request.urlopen", return_value=response):
        projects = insights_backend.get_benchmarks(limit=10, project=True)
        non_projects = insights_backend.get_benchmarks(limit=10, project=False)

    assert [row["priority"] for row in projects["rows"]] == ["NORMAL"]
    assert [row["priority"] for row in non_projects["rows"]] == ["HIGH"]
    assert non_projects["rows"][0]["is_project"] is None
    assert projects["project_missing_count"] == 1
    assert non_projects["project_missing_count"] == 1


def test_unknown_dataset_refuses_before_credentials_or_network():
    with patch("urllib.request.urlopen") as opener:
        with pytest.raises(insights_backend.InsightsError, match="Unsupported"):
            insights_backend._fetch_parquet_bytes("arbitrary-path")
    opener.assert_not_called()


def test_non_parquet_login_or_html_response_fails_closed(credentials):
    with patch("urllib.request.urlopen", return_value=_response(b"<html>login</html>")):
        with pytest.raises(insights_backend.InsightsError, match="valid parquet"):
            insights_backend._fetch_parquet_bytes("melds")


def test_http_401_is_clean_failure_without_recapture(credentials):
    error = __import__("urllib.error").error.HTTPError(
        "https://app.propertymeld.com/insights",
        401,
        "Unauthorized",
        {},
        io.BytesIO(b"unauthorized"),
    )
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(insights_backend.InsightsError, match="HTTP 401"):
            insights_backend._fetch_parquet_bytes("melds")


def test_missing_required_recorded_column_fails_closed():
    payload = _parquet_bytes([{"meld_meld_id": 1}])
    with pytest.raises(insights_backend.InsightsError, match="missing required columns"):
        insights_backend._read_parquet_rows(
            payload,
            allowed_columns=insights_backend._MELD_COLUMNS,
            required_columns=insights_backend._MELD_REQUIRED,
        )


def test_source_has_only_literal_get_request_construction():
    source = Path(insights_backend.__file__).read_text()
    tree = ast.parse(source)
    request_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Request"
    ]
    assert len(request_calls) == 1
    methods = [
        keyword.value
        for keyword in request_calls[0].keywords
        if keyword.arg == "method"
    ]
    assert len(methods) == 1
    assert isinstance(methods[0], ast.Constant)
    assert methods[0].value == "GET"
    assert "with_recapture_retry" not in source
    assert "_attempt_recapture" not in source
    assert set(cli.commands["insights"].commands) == {
        "melds",
        "turnovers",
        "benchmarks",
    }


@pytest.mark.parametrize("command,dataset", [
    (["insights", "melds", "--limit", "2"], "melds"),
    (["insights", "turnovers", "--limit", "2"], "turnovers"),
    (["insights", "benchmarks", "--limit", "2"], "benchmarks"),
])
def test_cli_subcommands_emit_clean_json(command, dataset):
    expected = {"dataset": dataset, "returned_count": 0, "rows": []}
    target = (
        "cli_anything.propertymeld.insights_backend.get_benchmarks"
        if dataset == "benchmarks"
        else "cli_anything.propertymeld.insights_backend.get_melds"
    )
    with patch(target, return_value=expected):
        result = CliRunner().invoke(cli, command)
    assert result.exit_code == 0
    assert json.loads(result.output) == expected


def test_cli_backend_failure_is_json_and_nonzero():
    with patch(
        "cli_anything.propertymeld.insights_backend.get_melds",
        side_effect=insights_backend.InsightsError("recorded schema drift"),
    ):
        result = CliRunner().invoke(cli, ["insights", "melds"])
    assert result.exit_code == 1
    assert json.loads(result.output) == {"error": "recorded schema drift"}
