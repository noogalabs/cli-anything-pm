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
    """Fixtures mirror the LIVE PM API shape captured against pm-dev on 2026-05-13.

    Real PM melds expose two related lists:
      - vendor_assignment_requests[]   {id, vendor: {id, name, ...}, accepted, rejected, canceled, ...}
      - vendorappointment[]            {id, meld, assignment_request, availability_segment, ...}

    schedule_vendor_appointment matches vendor_id -> assignment_request -> appointment
    and PUTs to vendor-appointments/{appointment_id}/schedule/ with
    `availability_segment.assignment_request` included (PM requires it).

    The earlier mock fixtures used `vendorassignment` (no such field on the
    live API), which let PR #1 ship a function that always returned
    "No vendor assignment found" against real PM. See the PR body for the
    full regression history.
    """

    def test_schedules_vendor_appointment_happy_path(self):
        """Vendor 42 has an accepted request linked to appointment 7000."""
        meld_response = {
            "id": 12701108,
            "status": "PENDING_TENANT_AVAILABILITY",
            "vendor_assignment_requests": [
                {
                    "id": 8000,
                    "vendor": {"id": 42, "name": "Dyer HVAC"},
                    "accepted": "2026-05-13T12:59:15.615119Z",
                    "rejected": None,
                    "canceled": None,
                    "meld": 12701108,
                },
            ],
            "vendorappointment": [
                {"id": 7000, "meld": 12701108, "assignment_request": 8000, "availability_segment": None},
            ],
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
            assert result["appointment_id"] == 7000
            assert result["dtstart"] == "2026-05-20T14:00:00-04:00"
            assert result["duration_hours"] == 2.0
            mock_put.assert_called_once()
            # The PUT URL targets vendor-appointments, not vendor-assignments.
            put_url, put_payload, *_ = mock_put.call_args[0]
            assert "vendor-appointments/7000/schedule/" in put_url
            # The payload must include the assignment_request link.
            assert put_payload["availability_segment"]["assignment_request"] == 8000
            mock_emit.assert_called_once()

    def test_returns_error_when_no_vendor_appointment(self):
        """Missing vendor: vendorappointment is empty on the meld."""
        meld_response = {
            "id": 12701108,
            "status": "PENDING_ASSIGNMENT",
            "vendor_assignment_requests": [],
            "vendorappointment": [],
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
            assert "No vendor appointment" in result["error"]

    def test_uses_first_appointment_fallback(self):
        """Multi-vendor fallback: vendor_id 999 isn't on the meld, use the first appointment."""
        meld_response = {
            "id": 12701108,
            "status": "PENDING_TENANT_AVAILABILITY",
            "vendor_assignment_requests": [
                {
                    "id": 8000,
                    "vendor": {"id": 10, "name": "First HVAC"},
                    "accepted": "2026-05-13T10:00:00Z",
                    "rejected": None,
                    "canceled": None,
                },
                {
                    "id": 8001,
                    "vendor": {"id": 42, "name": "Dyer HVAC"},
                    "accepted": "2026-05-13T11:00:00Z",
                    "rejected": None,
                    "canceled": None,
                },
            ],
            "vendorappointment": [
                {"id": 7000, "meld": 12701108, "assignment_request": 8000},
                {"id": 7001, "meld": 12701108, "assignment_request": 8001},
            ],
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

            # Pass non-matching vendor_id to trigger fallback.
            result = http_backend.schedule_vendor_appointment(
                "12701108", "999", "2026-05-20T14:00:00-04:00"
            )

            assert result["ok"] is True
            # First appointment (id=7000) wins the fallback.
            assert result["appointment_id"] == 7000
            mock_put.assert_called_once()
            assert "vendor-appointments/7000/schedule/" in mock_put.call_args[0][0]

    def test_skips_rejected_request(self):
        """A rejected request must NOT match: appointment for vendor 42 is rejected,
        so vendor 42 falls through to the multi-vendor fallback (first appointment)."""
        meld_response = {
            "id": 12701108,
            "status": "PENDING_ASSIGNMENT",
            "vendor_assignment_requests": [
                {
                    "id": 8000,
                    "vendor": {"id": 10, "name": "First HVAC"},
                    "accepted": "2026-05-13T10:00:00Z",
                    "rejected": None,
                    "canceled": None,
                },
                {
                    "id": 8001,
                    "vendor": {"id": 42, "name": "Dyer HVAC"},
                    "accepted": None,
                    "rejected": "2026-05-13T11:00:00Z",
                    "canceled": None,
                },
            ],
            "vendorappointment": [
                {"id": 7000, "meld": 12701108, "assignment_request": 8000},
                {"id": 7001, "meld": 12701108, "assignment_request": 8001},
            ],
        }
        schedule_response = {"availability_segment": {"event": {"dtstart": "2026-05-20T14:00:00-04:00", "duration": 7200}}}

        with patch("cli_anything.propertymeld.http_backend._load_creds") as mock_creds, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mock_cookie, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mock_csrf, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mock_get, \
             patch("cli_anything.propertymeld.http_backend._http_put") as mock_put, \
             patch.object(http_backend, "_emit_meld_state_change", create=True):

            mock_creds.return_value = {"cookie": "test"}
            mock_cookie.return_value = "Cookie: session=xyz"
            mock_csrf.return_value = "csrf123"
            mock_get.return_value = meld_response
            mock_put.return_value = schedule_response

            # Ask for vendor 42 — their request is rejected, so they should NOT match;
            # function falls back to the first appointment (7000, vendor 10).
            result = http_backend.schedule_vendor_appointment(
                "12701108", "42", "2026-05-20T14:00:00-04:00"
            )

            assert result["ok"] is True
            assert result["appointment_id"] == 7000
            assert "vendor-appointments/7000/schedule/" in mock_put.call_args[0][0]
