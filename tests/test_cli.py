"""CLI subprocess tests — verify commands produce valid JSON output."""
import json
import subprocess
import sys
import os
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from cli_anything.propertymeld.cli import cli
from cli_anything.propertymeld.utils import clear_token_cache

os.environ.setdefault("PM_CLIENT_ID", "test-id")
os.environ.setdefault("PM_CLIENT_SECRET", "test-secret")


@pytest.fixture(autouse=True)
def reset_cache():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def runner():
    return CliRunner()


MOCK_WO_LIST = [{"id": 1001, "status": "open", "description": "Test WO"}]
MOCK_PROPERTIES = [{"id": 5, "name": "123 Main St"}]
MOCK_VENDORS = [{"id": 10, "name": "Dyer HVAC"}]


class TestWorkOrdersCLI:
    def test_list_outputs_json(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_work_orders",
                   return_value=MOCK_WO_LIST):
            result = runner.invoke(cli, ["work-orders", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["id"] == 1001

    def test_list_with_status_flag(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_work_orders",
                   return_value=MOCK_WO_LIST) as mock_fn:
            runner.invoke(cli, ["work-orders", "list", "--status", "open"])
        mock_fn.assert_called_once_with(status="open", limit=25)

    def test_get_outputs_single_json(self, runner):
        with patch("cli_anything.propertymeld.api_backend.get_work_order",
                   return_value=MOCK_WO_LIST[0]):
            result = runner.invoke(cli, ["work-orders", "get", "1001"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 1001


class TestPropertiesCLI:
    def test_list_outputs_json(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_properties",
                   return_value=MOCK_PROPERTIES):
            result = runner.invoke(cli, ["properties", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["name"] == "123 Main St"


class TestVendorsCLI:
    def test_list_outputs_json(self, runner):
        with patch("cli_anything.propertymeld.api_backend.list_vendors",
                   return_value=MOCK_VENDORS):
            result = runner.invoke(cli, ["vendors", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["name"] == "Dyer HVAC"


class TestProbeCLI:
    def test_probe_outputs_ok(self, runner):
        with patch("cli_anything.propertymeld.api_backend.probe",
                   return_value={"ok": True, "token_prefix": "test-tok..."}):
            result = runner.invoke(cli, ["probe"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True


# ──────────────────────────────────────────────────────────────────────────────
# Coverage for unpushed feats: work-orders files / assign-vendor /
# work-orders schedule / work-orders merge|complete|cancel / tenants list|get
# ──────────────────────────────────────────────────────────────────────────────


MOCK_FILES = [
    {"id": 9001, "filename": "before.jpg", "signed_url": "https://example/before.jpg"},
    {"id": 9002, "filename": "after.jpg", "signed_url": "https://example/after.jpg"},
]
MOCK_TENANTS = [
    {"id": 1, "first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
    {"id": 2, "first_name": "John", "last_name": "Smith", "email": "john@example.com"},
]


class TestWorkOrdersFilesCLI:
    def test_files_outputs_list(self, runner):
        with patch("cli_anything.propertymeld.http_backend.list_files",
                   return_value=MOCK_FILES) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "files", "T5LKWTDB"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["filename"] == "before.jpg"
        mock_fn.assert_called_once_with("T5LKWTDB")


class TestListFilesMergesAllSources:
    """list_files() hits manager + tenant + vendor endpoints and tags uploader_role."""

    def _stub_http_get(self, manager_items, tenant_items, vendor_items):
        def _side(path, cookie_hdr):
            if "tenant-files" in path:
                return {"results": tenant_items}
            if "vendor-files" in path:
                return {"results": vendor_items}
            return {"results": manager_items}
        return _side

    def test_merges_three_sources_with_uploader_role(self):
        from cli_anything.propertymeld import http_backend
        side = self._stub_http_get(
            [{"id": 1, "filename": "mgr.pdf"}],
            [{"id": 2, "filename": "tenant.jpg"}],
            [{"id": 3, "filename": "vendor.png"}],
        )
        with patch("cli_anything.propertymeld.http_backend._load_creds", return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header", return_value=""), \
             patch("cli_anything.propertymeld.http_backend._http_get", side_effect=side):
            result = http_backend.list_files("12701108")
        assert len(result) == 3
        roles = {f["uploader_role"] for f in result}
        assert roles == {"manager", "tenant", "vendor"}
        mgr = next(f for f in result if f["uploader_role"] == "manager")
        assert mgr["filename"] == "mgr.pdf"

    def test_merges_when_some_endpoints_empty(self):
        from cli_anything.propertymeld import http_backend
        side = self._stub_http_get(
            [{"id": 1, "filename": "mgr-only.pdf"}],
            [],
            [],
        )
        with patch("cli_anything.propertymeld.http_backend._load_creds", return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header", return_value=""), \
             patch("cli_anything.propertymeld.http_backend._http_get", side_effect=side):
            result = http_backend.list_files("12701108")
        assert len(result) == 1
        assert result[0]["uploader_role"] == "manager"

    def test_handles_flat_list_response(self):
        from cli_anything.propertymeld import http_backend
        def side(path, cookie_hdr):
            if "tenant-files" in path:
                return [{"id": 9, "filename": "flat.jpg"}]
            if "vendor-files" in path:
                return []
            return {"results": []}
        with patch("cli_anything.propertymeld.http_backend._load_creds", return_value={}), \
             patch("cli_anything.propertymeld.http_backend._cookie_header", return_value=""), \
             patch("cli_anything.propertymeld.http_backend._http_get", side_effect=side):
            result = http_backend.list_files("12701108")
        assert len(result) == 1
        assert result[0]["uploader_role"] == "tenant"


class TestWorkOrdersWorkEntriesCLI:
    def test_work_entries_outputs_list(self, runner):
        mock_entries = [
            {"id": 1, "checkin": "2026-05-01T08:00:00Z", "checkout": "2026-05-01T10:30:00Z",
             "hours": 2.5, "agent_name": "Carlos", "description": "AC tune-up",
             "long_description": "Replaced filter, cleaned coils."},
        ]
        with patch("cli_anything.propertymeld.http_backend.list_work_entries",
                   return_value=mock_entries) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "work-entries", "12701108"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["agent_name"] == "Carlos"
        mock_fn.assert_called_once_with("12701108")


class TestAssignVendorCLI:
    def test_assign_vendor_passes_partial_name(self, runner):
        with patch("cli_anything.propertymeld.http_backend.assign_vendor_by_name",
                   return_value={"ok": True, "vendor_id": 10, "matched_name": "Dyer HVAC"}) as mock_fn:
            result = runner.invoke(cli, ["assign-vendor",
                                         "--work-order-id", "T5LKWTDB",
                                         "--vendor", "dyer"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        mock_fn.assert_called_once_with("T5LKWTDB", "dyer", account_prefix="1")


class TestWorkOrdersScheduleCLI:
    def test_schedule_passes_dtstart_and_hours(self, runner):
        with patch("cli_anything.propertymeld.http_backend.schedule_appointment",
                   return_value={"id": 4242, "scheduled_dtstart": "2026-04-27T14:00:00-04:00"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "schedule",
                                         "--meld-id", "T5LKWTDB",
                                         "--dtstart", "2026-04-27T14:00:00-04:00",
                                         "--hours", "3"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 4242
        mock_fn.assert_called_once_with("T5LKWTDB",
                                        "2026-04-27T14:00:00-04:00",
                                        duration_hours=3.0)


class TestWorkOrdersLifecycleCLI:
    def test_merge_into_destination(self, runner):
        with patch("cli_anything.propertymeld.http_backend.merge_meld",
                   return_value={"ok": True, "source": "TSRC", "destination": "TDEST"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "merge",
                                         "--meld-id", "TSRC",
                                         "--into", "TDEST"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["destination"] == "TDEST"
        mock_fn.assert_called_once_with("TSRC", "TDEST")

    def test_complete_with_notes(self, runner):
        with patch("cli_anything.propertymeld.http_backend.complete_meld",
                   return_value={"id": 1001, "status": "COMPLETE"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "complete",
                                         "--meld-id", "T5LKWTDB",
                                         "--notes", "Replaced filter."])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "COMPLETE"
        mock_fn.assert_called_once_with("T5LKWTDB", completion_notes="Replaced filter.")

    def test_cancel_with_reason(self, runner):
        with patch("cli_anything.propertymeld.http_backend.cancel_meld",
                   return_value={"id": 1002, "status": "CANCELLED"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "cancel",
                                         "--meld-id", "TXYZ",
                                         "--reason", "Duplicate"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "CANCELLED"
        mock_fn.assert_called_once_with("TXYZ", reason="Duplicate")


class TestTenantsCLI:
    def test_list_with_search(self, runner):
        with patch("cli_anything.propertymeld.http_backend.list_tenants",
                   return_value=MOCK_TENANTS) as mock_fn:
            result = runner.invoke(cli, ["tenants", "list", "--search", "doe"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        mock_fn.assert_called_once_with(search="doe", limit=100)

    def test_get_single_tenant(self, runner):
        with patch("cli_anything.propertymeld.http_backend.get_tenant",
                   return_value=MOCK_TENANTS[0]):
            result = runner.invoke(cli, ["tenants", "get", "1"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["first_name"] == "Jane"


# ──────────────────────────────────────────────────────────────────────────────
# Phase 2 backport coverage: projects / estimates / receipts / vendor-invites /
# work-orders schedule-vendor
# ──────────────────────────────────────────────────────────────────────────────


MOCK_PROJECT = {"id": 7001, "name": "Q2 Renovations", "description": "Bldg-A common-area refresh"}
MOCK_ESTIMATE = {"id": 8001, "estimate_number": "INV-2026-001", "amount": "1250.00", "status": "draft"}
MOCK_RECEIPT = {"id": 9001, "filename": "home-depot-2026-04-29.pdf", "linked_estimate_id": 8001}


class TestProjectsCLI:
    def test_create_project_passes_args(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_project",
                   return_value=MOCK_PROJECT) as mock_fn:
            result = runner.invoke(cli, ["projects", "create",
                                         "--name", "Q2 Renovations",
                                         "--description", "Bldg-A refresh"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 7001
        mock_fn.assert_called_once_with("Q2 Renovations",
                                        description="Bldg-A refresh",
                                        meld_id=None)


class TestEstimatesCLI:
    def test_create_estimate_passes_args(self, runner):
        with patch("cli_anything.propertymeld.http_backend.create_estimate",
                   return_value=MOCK_ESTIMATE) as mock_fn:
            result = runner.invoke(cli, ["estimates", "create",
                                         "--meld-id", "T5LKWTDB",
                                         "--estimate-number", "INV-2026-001",
                                         "--amount", "1250.00"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["estimate_number"] == "INV-2026-001"
        # Positional + kwarg shape per cli wiring
        call = mock_fn.call_args
        assert call.args[0] == "T5LKWTDB"
        assert call.args[1] == "INV-2026-001"
        assert call.args[2] == "1250.00"


class TestReceiptsCLI:
    def test_upload_receipt_passes_file_path(self, runner, tmp_path):
        receipt_file = tmp_path / "rcpt.pdf"
        receipt_file.write_bytes(b"%PDF-1.4 fake\n")
        with patch("cli_anything.propertymeld.http_backend.upload_receipt",
                   return_value=MOCK_RECEIPT) as mock_fn:
            result = runner.invoke(cli, ["receipts", "upload",
                                         "--meld-id", "T5LKWTDB",
                                         "--file", str(receipt_file)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 9001
        mock_fn.assert_called_once()
        call = mock_fn.call_args
        assert call.args[0] == "T5LKWTDB"
        assert call.args[1] == str(receipt_file)


class TestWorkOrdersScheduleVendorCLI:
    def test_schedule_vendor_passes_args(self, runner):
        with patch("cli_anything.propertymeld.http_backend.schedule_vendor_appointment",
                   return_value={"id": 5555, "scheduled_dtstart": "2026-05-06T14:00:00-04:00"}) as mock_fn:
            result = runner.invoke(cli, ["work-orders", "schedule-vendor",
                                         "--meld-id", "T5LKWTDB",
                                         "--vendor-id", "10",
                                         "--dtstart", "2026-05-06T14:00:00-04:00",
                                         "--hours", "3"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 5555
        mock_fn.assert_called_once_with("T5LKWTDB", "10",
                                        "2026-05-06T14:00:00-04:00",
                                        duration_hours=3.0)
