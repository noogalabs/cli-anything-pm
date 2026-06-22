"""Negative-control tests: commands must exit NON-ZERO (code 1) when their
backend returns a failure envelope ({"ok": False, ...}).

The reliability fix lives in utils.output_json(): after printing, if the
payload is a dict with ``ok`` explicitly False it sys.exit(1)s, so shell
callers / crons checking ``$?`` see the failure. Before the fix these
commands printed the {ok:false} error but still exited 0 — silently
reporting failed schedules/links as success.

Each test below patches the backend fn (at cli_anything.propertymeld.
http_backend.<fn>) to RETURN {"ok": False, ...}. Patching the backend fn
means NOTHING hits the network — mirrors the green-path tests in
test_cli.py exactly. We then assert:
  - result.exit_code == 1   (would have been 0 under the old bug)
  - json.loads(result.output)["ok"] is False   (envelope still printed)
"""
import json
import os

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


class TestExitNonZeroOnBackendFailure:
    def test_schedule_in_house_exits_1_on_failure_envelope(self, runner):
        """work-orders schedule -> schedule_appointment returns the
        no-in-house-assignment guard failure. Command must exit 1."""
        from unittest.mock import patch
        with patch("cli_anything.propertymeld.http_backend.schedule_appointment",
                   return_value={"ok": False,
                                 "error": "No in-house tech assignment found on this meld"}):
            result = runner.invoke(cli, ["work-orders", "schedule",
                                         "--meld-id", "90000014",
                                         "--dtstart", "2026-04-27T14:00:00-04:00",
                                         "--hours", "3"])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["ok"] is False

    def test_force_pending_completion_exits_1_on_failure_envelope(self, runner):
        """work-orders force-pending-completion returns a guard failure.
        Command must exit 1 so hooks/crons cannot treat it as success."""
        from unittest.mock import patch
        with patch("cli_anything.propertymeld.http_backend.force_pending_completion",
                   return_value={"ok": False,
                                 "error": "meld 90000014 is PENDING_COMPLETION"}):
            result = runner.invoke(cli, ["work-orders", "force-pending-completion",
                                         "--meld-id", "90000014"])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["ok"] is False

    def test_schedule_vendor_exits_1_on_failure_envelope(self, runner):
        """work-orders schedule-vendor -> schedule_vendor_appointment returns
        an unresolved-assignment failure. Command must exit 1."""
        from unittest.mock import patch
        with patch("cli_anything.propertymeld.http_backend.schedule_vendor_appointment",
                   return_value={"ok": False,
                                 "error": "No vendor appointment found on this meld"}):
            result = runner.invoke(cli, ["work-orders", "schedule-vendor",
                                         "--meld-id", "90000014",
                                         "--vendor-id", "10",
                                         "--dtstart", "2026-05-06T14:00:00-04:00",
                                         "--hours", "3"])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["ok"] is False

    def test_projects_add_melds_exits_1_on_failure_envelope(self, runner):
        """projects add-melds -> add_melds_to_project returns a backend
        failure envelope. Command must exit 1."""
        from unittest.mock import patch
        with patch("cli_anything.propertymeld.http_backend.add_melds_to_project",
                   return_value={"ok": False, "error": "no meld_ids provided"}):
            result = runner.invoke(cli, ["projects", "add-melds",
                                         "222959", "12772756"])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["ok"] is False

    def test_projects_edit_exits_1_on_failure_envelope(self, runner):
        """projects edit -> update_project returns a could-not-fetch failure
        envelope. Command must exit 1."""
        from unittest.mock import patch
        with patch("cli_anything.propertymeld.http_backend.update_project",
                   return_value={"ok": False,
                                 "error": "could not fetch current project state"}):
            result = runner.invoke(cli, ["projects", "edit", "222969",
                                         "--name", "Renamed"])
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["ok"] is False
