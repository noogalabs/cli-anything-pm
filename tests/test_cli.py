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
