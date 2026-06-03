"""Negative-control tests: CLI commands must exit NON-ZERO (code 1) when the
backend returns an ``{"ok": False, ...}`` failure envelope.

Guards the reliability fix in ``utils.output_json`` (commit 3b2e44e): printing
an ``ok:false`` envelope now calls ``sys.exit(1)`` AFTER printing, so shell
callers and crons checking ``$?`` see the failure instead of a silent exit-0.

Each test below would FAIL against the old exit-0 behavior — that is the point.

Mirrors the mock-and-invoke idioms from ``tests/test_cli.py`` exactly: patch the
``http_backend.*`` boundary fn to return the failure envelope, invoke via
``click.testing.CliRunner``, assert ``exit_code == 1`` AND ``ok is False``.

None of these commands perform a hydration GET before the terminal backend fn —
the patched ``http_backend.*`` fn is the only network surface, so patching it
guarantees nothing touches the network.
"""
import json
import os
from unittest.mock import patch

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


MELD_ID = "90000014"


class TestAssignTechExitsNonZeroOnFailure:
    def test_assign_tech_tech_not_found_exits_1(self, runner):
        """tech-not-found returns {ok:false}; command must exit 1, not 0."""
        envelope = {
            "ok": False,
            "error": "No in-house tech matched 'ZZ Nonexistent Tech'",
            "work_order_id": MELD_ID,
        }
        with patch("cli_anything.propertymeld.http_backend.assign_tech",
                   return_value=envelope) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "assign-tech",
                "--work-order-id", MELD_ID,
                "--tech", "ZZ Nonexistent Tech",
            ])
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["ok"] is False
        mock_fn.assert_called_once_with(MELD_ID, "ZZ Nonexistent Tech")


class TestAssignVendorExitsNonZeroOnFailure:
    def test_assign_vendor_vendor_not_found_exits_1(self, runner):
        """vendor-not-found returns {ok:false}; command must exit 1, not 0."""
        envelope = {
            "ok": False,
            "error": "No vendor matched 'ZZ Nonexistent Vendor'",
            "work_order_id": MELD_ID,
        }
        with patch("cli_anything.propertymeld.http_backend.assign_vendor_by_name",
                   return_value=envelope) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "assign-vendor",
                "--work-order-id", MELD_ID,
                "--vendor", "ZZ Nonexistent Vendor",
            ])
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["ok"] is False
        mock_fn.assert_called_once_with(MELD_ID, "ZZ Nonexistent Vendor", account_prefix="1")


class TestSetCoordinatorExitsNonZeroOnFailure:
    def test_set_coordinator_patch_failed_exits_1(self, runner):
        """coordinator PATCH failed returns {ok:false}; command must exit 1, not 0."""
        envelope = {
            "ok": False,
            "error": "Coordinator PATCH failed: 404 Not Found",
            "meld_id": int(MELD_ID),
        }
        with patch("cli_anything.propertymeld.http_backend.set_coordinator",
                   return_value=envelope) as mock_fn:
            result = runner.invoke(cli, [
                "work-orders", "set-coordinator",
                "--meld-id", MELD_ID,
                "--user-id", "99999999",
            ])
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["ok"] is False
        mock_fn.assert_called_once_with(MELD_ID, 99999999)
