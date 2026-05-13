"""Unit tests for api_backend — all API calls mocked."""
import json
import os
import sys
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

# Set dummy env vars before importing
os.environ.setdefault("PM_CLIENT_ID", "test-client-id")
os.environ.setdefault("PM_CLIENT_SECRET", "test-client-secret")

from cli_anything.propertymeld import api_backend
from cli_anything.propertymeld.utils import clear_token_cache


@pytest.fixture(autouse=True)
def reset_token_cache():
    clear_token_cache()
    yield
    clear_token_cache()


def make_response(data, status: int = 200):
    """Create a mock urllib response."""
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = json.dumps(data).encode()
    mock.status = status
    return mock


TOKEN_RESPONSE = {"access_token": "test-token-abc123", "token_type": "Bearer"}
WO_LIST_RESPONSE = {
    "count": 2,
    "results": [
        {"id": 1001, "status": "open", "description": "Leak in unit 2B"},
        {"id": 1002, "status": "open", "description": "HVAC not working"},
    ]
}
SINGLE_WO_RESPONSE = {"id": 1001, "status": "open", "description": "Leak in unit 2B"}
PROPERTIES_RESPONSE = {"count": 1, "results": [{"id": 5, "name": "123 Main St"}]}
VENDORS_RESPONSE = {"count": 1, "results": [{"id": 10, "name": "Dyer HVAC"}]}


class TestListWorkOrders:
    def test_returns_results_list(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(WO_LIST_RESPONSE),
            ]
            results = api_backend.list_work_orders()
        assert len(results) == 2
        assert results[0]["id"] == 1001

    def test_status_filter_passed_as_param(self):
        # 'open' fans out to all 3 PENDING_* states sent as repeated status=
        # params per Nexus DRF MultipleChoiceFilter shape (901d1f4).
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response({"results": []}),
            ]
            api_backend.list_work_orders(status="open")
        call_args = mock_open.call_args_list[1]
        url = call_args[0][0].full_url
        assert "status=PENDING_ASSIGNMENT" in url
        assert "status=PENDING_VENDOR" in url
        assert "status=PENDING_MORE_MANAGEMENT_AVAILABILITY" in url

    def test_handles_flat_list_response(self):
        """Some endpoints return a flat list, not {results: [...]}."""
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response([{"id": 999}]),
            ]
            results = api_backend.list_work_orders()
        assert results == [{"id": 999}]


class TestGetWorkOrder:
    def test_returns_single_work_order(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(SINGLE_WO_RESPONSE),
            ]
            result = api_backend.get_work_order("1001")
        assert result["id"] == 1001

    def test_url_contains_meld_id(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(SINGLE_WO_RESPONSE),
            ]
            api_backend.get_work_order("1001")
        url = mock_open.call_args_list[1][0][0].full_url
        assert "/meld/1001/" in url

    def test_rejects_short_code(self):
        with pytest.raises(ValueError) as exc:
            api_backend.get_work_order("T5LKWTDB")
        assert "integer PK" in str(exc.value)


class TestListProperties:
    def test_returns_property_list(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(PROPERTIES_RESPONSE),
            ]
            results = api_backend.list_properties()
        assert results[0]["name"] == "123 Main St"


class TestListVendors:
    def test_returns_vendor_list(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response(VENDORS_RESPONSE),
            ]
            results = api_backend.list_vendors()
        assert results[0]["name"] == "Dyer HVAC"


class TestProbe:
    def test_returns_ok_with_token(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = make_response(TOKEN_RESPONSE)
            result = api_backend.probe()
        assert result["ok"] is True
        assert "test-tok" in result["token_prefix"]


# ──────────────────────────────────────────────────────────────────────────────
# int-PK guard — Phase A backport
# ──────────────────────────────────────────────────────────────────────────────


from cli_anything.propertymeld import http_backend


class TestValidateMeldIdGuard:
    def test_int_passthrough(self):
        assert http_backend._validate_meld_id(12701108) == 12701108
        assert http_backend._validate_meld_id("12701108") == 12701108

    def test_rejects_short_code(self):
        with pytest.raises(ValueError) as exc:
            http_backend._validate_meld_id("T5LKWTDB")
        assert "T5LKWTDB" in str(exc.value)
        assert "integer PK" in str(exc.value)


class TestRecaptureRetry:
    def _session_expired(self):
        return http_backend.SessionExpired(
            urllib.error.HTTPError(
                url="https://app.propertymeld.com/test",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=BytesIO(b""),
            )
        )

    def test_retries_once_after_recapture(self):
        calls = {"count": 0}

        @http_backend.with_recapture_retry
        def flaky():
            calls["count"] += 1
            if calls["count"] == 1:
                raise self._session_expired()
            return {"ok": True}

        with patch("cli_anything.propertymeld.http_backend._attempt_recapture", return_value=True) as recapture:
            assert flaky() == {"ok": True}
        recapture.assert_called_once_with()
        assert calls["count"] == 2

    def test_raises_exit_when_recapture_fails(self):
        @http_backend.with_recapture_retry
        def flaky():
            raise self._session_expired()

        with patch("cli_anything.propertymeld.http_backend._attempt_recapture", return_value=False):
            with pytest.raises(SystemExit) as exc:
                flaky()
        assert exc.value.code == 1


class TestScheduleVendorAppointment:
    def test_schedules_vendor_appointment_happy_path(self):
        """Happy path: vendor found by vendor_id, assignment created."""
        meld_response = {
            "id": 12701108,
            "status": "open",
            "vendorassignment": [
                {"id": 201, "vendor_id": 42, "name": "Dyer HVAC"},
            ]
        }
        schedule_response = {
            "availability_segment": {
                "event": {
                    "dtstart": "2026-05-20T14:00:00-04:00",
                    "duration": 7200,
                }
            }
        }

        with patch("cli_anything.propertymeld.http_backend._load_creds") as mock_creds, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mock_cookie, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mock_csrf, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mock_get, \
             patch("cli_anything.propertymeld.http_backend._http_put") as mock_put, \
             patch.object(http_backend, "_emit_meld_state_change", create=True) as mock_emit:

            mock_creds.return_value = {"cookie": "test"}
            mock_cookie.return_value = "Cookie: session=xyz"
            mock_csrf.return_value = "csrf123"
            mock_get.return_value = meld_response
            mock_put.return_value = schedule_response

            result = http_backend.schedule_vendor_appointment(
                "12701108", "42", "2026-05-20T14:00:00-04:00", duration_hours=2.0
            )

            assert result["ok"] is True
            assert result["meld_id"] == 12701108
            assert result["vendor_id"] == "42"
            assert result["assignment_id"] == 201
            assert result["dtstart"] == "2026-05-20T14:00:00-04:00"
            assert result["duration_hours"] == 2.0
            mock_put.assert_called_once()
            mock_emit.assert_called_once()

    def test_returns_error_when_no_vendor_assignment(self):
        """Missing vendor assignment: vendor_assignments is empty."""
        meld_response = {
            "id": 12701108,
            "status": "open",
            "vendorassignment": []
        }

        with patch("cli_anything.propertymeld.http_backend._load_creds") as mock_creds, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mock_cookie, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mock_csrf, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mock_get:

            mock_creds.return_value = {"cookie": "test"}
            mock_cookie.return_value = "Cookie: session=xyz"
            mock_csrf.return_value = "csrf123"
            mock_get.return_value = meld_response

            result = http_backend.schedule_vendor_appointment(
                "12701108", "42", "2026-05-20T14:00:00-04:00"
            )

            assert result["ok"] is False
            assert "No vendor assignment" in result["error"]

    def test_uses_first_vendor_assignment_fallback(self):
        """Multi-vendor fallback: use first assignment when vendor_id doesn't match."""
        meld_response = {
            "id": 12701108,
            "status": "open",
            "vendorassignment": [
                {"id": 201, "vendor_id": 10, "name": "First HVAC"},
                {"id": 202, "vendor_id": 42, "name": "Dyer HVAC"},
            ]
        }
        schedule_response = {
            "availability_segment": {
                "event": {
                    "dtstart": "2026-05-20T14:00:00-04:00",
                    "duration": 7200,
                }
            }
        }

        with patch("cli_anything.propertymeld.http_backend._load_creds") as mock_creds, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mock_cookie, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mock_csrf, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mock_get, \
             patch("cli_anything.propertymeld.http_backend._http_put") as mock_put, \
             patch.object(http_backend, "_emit_meld_state_change", create=True) as mock_emit:

            mock_creds.return_value = {"cookie": "test"}
            mock_cookie.return_value = "Cookie: session=xyz"
            mock_csrf.return_value = "csrf123"
            mock_get.return_value = meld_response
            mock_put.return_value = schedule_response

            # Pass non-matching vendor_id to trigger fallback
            result = http_backend.schedule_vendor_appointment(
                "12701108", "999", "2026-05-20T14:00:00-04:00"
            )

            assert result["ok"] is True
            # Should use first assignment (id=201) as fallback
            assert result["assignment_id"] == 201
            mock_put.assert_called_once()
            # Verify the PUT was called with the first assignment ID
            call_args = mock_put.call_args
            assert "vendor-assignments/201/" in call_args[0][0]


# ──────────────────────────────────────────────────────────────────────────────
# projects create / update — PM-Blue queue #3
# ──────────────────────────────────────────────────────────────────────────────


class TestCreateProject:
    """create_project posts the full required payload to /api/projects/."""

    def _patched(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_post"),
        )

    def test_happy_path_returns_project_id_and_posts_full_payload(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_post_p = self._patched()
        with mock_creds_p as mock_creds, mock_cookie_p as mock_cookie, \
             mock_csrf_p as mock_csrf, mock_post_p as mock_post:
            mock_creds.return_value = {"cookie": "test"}
            mock_cookie.return_value = "Cookie: session=xyz"
            mock_csrf.return_value = "csrf123"
            mock_post.return_value = {"id": 219852, "name": "Test reno"}

            result = http_backend.create_project(
                name="Test reno",
                description="kitchen redo",
                start_date="2026-06-01",
                due_date="2026-06-30",
                coordinators=["7", "11"],
                project_type="construction",
                unit={"id": 1754499},
            )

            assert result["ok"] is True
            assert result["project_id"] == 219852
            assert result["result"]["name"] == "Test reno"
            mock_post.assert_called_once()
            posted_path, posted_payload, _, _ = mock_post.call_args[0]
            assert posted_path == "projects/"
            assert posted_payload["name"] == "Test reno"
            assert posted_payload["description"] == "kitchen redo"
            assert posted_payload["start_date"] == "2026-06-01"
            assert posted_payload["due_date"] == "2026-06-30"
            assert posted_payload["coordinators"] == [7, 11]
            assert posted_payload["project_type"] == "construction"
            assert posted_payload["unit"] == {"id": 1754499}

    def test_http_error_propagates(self):
        """A 400 from PM should bubble up (caller decides retry/UX)."""
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_post_p = self._patched()
        with mock_creds_p as mock_creds, mock_cookie_p as mock_cookie, \
             mock_csrf_p as mock_csrf, mock_post_p as mock_post:
            mock_creds.return_value = {"cookie": "test"}
            mock_cookie.return_value = "Cookie: session=xyz"
            mock_csrf.return_value = "csrf123"
            mock_post.side_effect = urllib.error.HTTPError(
                url="https://app.propertymeld.com/3287/m/3287/api/projects/",
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=BytesIO(b'{"unit": ["Please select an Active Unit"]}'),
            )

            with pytest.raises(urllib.error.HTTPError) as exc:
                http_backend.create_project(
                    name="x",
                    description="",
                    start_date="2026-06-01",
                    due_date="2026-06-30",
                    coordinators=["7"],
                    project_type="construction",
                    unit=1754499,
                )
            assert exc.value.code == 400


class TestUpdateProject:
    """update_project PATCHes only the fields the caller explicitly set."""

    def _patched(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_patch"),
        )

    def test_happy_path_sends_only_set_fields(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_patch_p = self._patched()
        with mock_creds_p as mock_creds, mock_cookie_p as mock_cookie, \
             mock_csrf_p as mock_csrf, mock_patch_p as mock_patch_http:
            mock_creds.return_value = {"cookie": "test"}
            mock_cookie.return_value = "Cookie: session=xyz"
            mock_csrf.return_value = "csrf123"
            mock_patch_http.return_value = {"id": 219852, "name": "Renamed"}

            result = http_backend.update_project(
                project_id="219852",
                name="Renamed",
                status="archived",
            )

            assert result["ok"] is True
            assert result["project_id"] == "219852"
            mock_patch_http.assert_called_once()
            patched_path, patched_payload, _, _ = mock_patch_http.call_args[0]
            assert patched_path == "projects/219852/"
            assert patched_payload == {"name": "Renamed", "status": "archived"}

    def test_no_fields_returns_error_without_patch_call(self):
        """Empty patch: short-circuit, do not hit PM."""
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_patch_p = self._patched()
        with mock_creds_p as mock_creds, mock_cookie_p as mock_cookie, \
             mock_csrf_p as mock_csrf, mock_patch_p as mock_patch_http:
            mock_creds.return_value = {"cookie": "test"}
            mock_cookie.return_value = "Cookie: session=xyz"
            mock_csrf.return_value = "csrf123"

            result = http_backend.update_project(project_id="219852")

            assert result["ok"] is False
            assert "no fields" in result["error"]
            mock_patch_http.assert_not_called()
