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

    def test_new_filter_flags_are_forwarded_to_query(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = [
                make_response(TOKEN_RESPONSE),
                make_response({"results": []}),
            ]
            api_backend.list_work_orders(
                assigned_to_tech=90025,
                assigned_to_vendor=99,
                stuck_hours=48,
                created_since="2026-05-18T00:00:00Z",
                status_not="COMPLETED",
                no_tenant_linked=True,
            )
        url = mock_open.call_args_list[1][0][0].full_url
        assert "assigned_to_tech=90025" in url
        assert "assigned_to_vendor=99" in url
        assert "stuck_hours=48" in url
        assert "created_since=2026-05-18T00%3A00%3A00Z" in url
        assert "status_not=COMPLETED" in url
        assert "no_tenant_linked=true" in url


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
        assert http_backend._validate_meld_id(90000014) == 90000014
        assert http_backend._validate_meld_id("90000014") == 90000014

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
    """Fixtures mirror the LIVE PM payload captured 2026-05-13 (2nd session).

    The captured manager-UI vendor-schedule request is:
      PATCH /api/assignments/{assignment_request_id}/segments/
      {
        "mark_scheduled": true,
        "segments_to_keep": [],
        "new_segments": [],
        "multiple_segments_to_book": [{"event": {"dtstart": ..., "dtend": ...}}]
      }

    The id targeted is the vendor_assignment_request.id (NOT
    vendorappointment.id). The earlier PR-#1 mocks used a fake field
    `vendorassignment` which never appears in real responses.
    """

    HAPPY_MELD = {
        "id": 90000014,
        "status": "PENDING_TENANT_AVAILABILITY",
        "vendor_assignment_requests": [
            {
                "id": 8000,
                "vendor": {"id": 42, "name": "Dyer HVAC"},
                "accepted": "2026-05-13T12:59:15.615119Z",
                "rejected": None,
                "canceled": None,
                "meld": 90000014,
            },
        ],
        "vendorappointment": [
            {"id": 7000, "meld": 90000014, "assignment_request": 8000, "availability_segment": None},
        ],
    }

    def test_happy_path_patches_segments_endpoint(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp, \
             patch.object(http_backend, "_emit_meld_state_change", create=True) as me:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = self.HAPPY_MELD
            mp.return_value = {"appointments_required": None}

            result = http_backend.schedule_vendor_appointment(
                "90000014", "42", "2026-05-20T14:00:00-04:00", duration_hours=2.0
            )

            assert result["ok"] is True
            assert result["meld_id"] == 90000014
            assert result["assignment_request_id"] == 8000
            assert result["appointment_id"] == 7000
            assert result["dtstart"] == "2026-05-20T14:00:00-04:00"
            # dtend is computed dtstart + duration_hours.
            assert result["dtend"].startswith("2026-05-20T16:00:00")
            mp.assert_called_once()
            path, payload, _, _ = mp.call_args[0]
            assert path == "assignments/8000/segments/"
            assert payload["mark_scheduled"] is True
            assert payload["segments_to_keep"] == []
            assert payload["new_segments"] == []
            assert len(payload["multiple_segments_to_book"]) == 1
            ev = payload["multiple_segments_to_book"][0]["event"]
            assert ev["dtstart"] == "2026-05-20T14:00:00-04:00"
            assert ev["dtend"].startswith("2026-05-20T16:00:00")
            me.assert_called_once()

    def test_returns_error_when_no_vendor_appointment(self):
        meld = {
            "id": 90000014,
            "status": "PENDING_ASSIGNMENT",
            "vendor_assignment_requests": [],
            "vendorappointment": [],
        }
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = meld

            result = http_backend.schedule_vendor_appointment(
                "90000014", "42", "2026-05-20T14:00:00-04:00"
            )
            assert result["ok"] is False
            assert "No vendor appointment" in result["error"]

    def test_uses_first_appointment_fallback(self):
        meld = {
            "id": 90000014,
            "status": "PENDING_TENANT_AVAILABILITY",
            "vendor_assignment_requests": [
                {"id": 8000, "vendor": {"id": 10, "name": "First HVAC"}, "accepted": "2026-05-13T10:00:00Z", "rejected": None, "canceled": None},
                {"id": 8001, "vendor": {"id": 42, "name": "Dyer HVAC"}, "accepted": "2026-05-13T11:00:00Z", "rejected": None, "canceled": None},
            ],
            "vendorappointment": [
                {"id": 7000, "meld": 90000014, "assignment_request": 8000},
                {"id": 7001, "meld": 90000014, "assignment_request": 8001},
            ],
        }
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp, \
             patch.object(http_backend, "_emit_meld_state_change", create=True):
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = meld
            mp.return_value = {}

            # vendor_id 999 doesn't match — falls back to first appointment.
            result = http_backend.schedule_vendor_appointment(
                "90000014", "999", "2026-05-20T14:00:00-04:00"
            )
            assert result["ok"] is True
            assert result["assignment_request_id"] == 8000
            assert result["appointment_id"] == 7000
            assert "assignments/8000/segments/" in mp.call_args[0][0]

    def test_skips_rejected_request(self):
        meld = {
            "id": 90000014,
            "status": "PENDING_ASSIGNMENT",
            "vendor_assignment_requests": [
                {"id": 8000, "vendor": {"id": 10, "name": "First HVAC"}, "accepted": "2026-05-13T10:00:00Z", "rejected": None, "canceled": None},
                {"id": 8001, "vendor": {"id": 42, "name": "Dyer HVAC"}, "accepted": None, "rejected": "2026-05-13T11:00:00Z", "canceled": None},
            ],
            "vendorappointment": [
                {"id": 7000, "meld": 90000014, "assignment_request": 8000},
                {"id": 7001, "meld": 90000014, "assignment_request": 8001},
            ],
        }
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp, \
             patch.object(http_backend, "_emit_meld_state_change", create=True):
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = meld
            mp.return_value = {}

            # vendor 42's request is rejected; falls back to first appointment.
            result = http_backend.schedule_vendor_appointment(
                "90000014", "42", "2026-05-20T14:00:00-04:00"
            )
            assert result["ok"] is True
            assert result["assignment_request_id"] == 8000
            assert "assignments/8000/segments/" in mp.call_args[0][0]


# ──────────────────────────────────────────────────────────────────────────────
# Project↔meld operations (pm-capture 2026-05-13 — PR #3)
# ──────────────────────────────────────────────────────────────────────────────


class TestAddMeldsToProject:
    """PUT /api/projects/{id}/add-melds/ — verified shape from pm-capture."""

    def _patched(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_put"),
        )

    def test_happy_path_single_meld(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_put_p = self._patched()
        with mock_creds_p as mc, mock_cookie_p as mch, mock_csrf_p as mcs, mock_put_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 222959, "melds": [{"id": 12772756, "project": 222959}]}

            result = http_backend.add_melds_to_project("222959", [12772756])

            assert result["ok"] is True
            assert result["project_id"] == "222959"
            path, payload, _, _ = mp.call_args[0]
            assert path == "projects/222959/add-melds/"
            assert payload == {"melds": [{"project": "222959", "id": 12772756}]}

    def test_multi_meld(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_put_p = self._patched()
        with mock_creds_p as mc, mock_cookie_p as mch, mock_csrf_p as mcs, mock_put_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 222959, "melds": []}

            http_backend.add_melds_to_project("222959", [12772756, 12772757])

            _, payload, _, _ = mp.call_args[0]
            assert payload["melds"] == [
                {"project": "222959", "id": 12772756},
                {"project": "222959", "id": 12772757},
            ]

    def test_empty_meld_list_short_circuits(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_put_p = self._patched()
        with mock_creds_p as mc, mock_cookie_p as mch, mock_csrf_p as mcs, mock_put_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"

            result = http_backend.add_melds_to_project("222959", [])

            assert result["ok"] is False
            assert "no meld_ids" in result["error"]
            mp.assert_not_called()


_FULL_UNIT_FIXTURE = {
    "id": 9000005,
    "display_address": {"id": 1718387, "line_1": "123 Main St", "city": "Chattanooga"},
    "building": None,
    "floor": None,
    "prop": {"id": 1718387, "line_1": "123 Main St"},
    "current_tenants": [{"id": 9000008, "first_name": "Demo", "last_name": "Resident"}],
}

_FULL_AGENT_FIXTURE = {
    "id": 90025,
    "type": "ManagementAgent",
    "composite_id": "1-90011",
    "first_name": "Person031",
    "last_name": "Person020",
    "title": "COORDINATOR",
    "department": "MAINTENANCE",
    "selected_property_groups": [29374],
    "denormalized_property_groups": [29374],
    "property_groups": [29374],
}


class TestCreateMeldInProject:
    """POST /api/projects/{id}/list-create-meld/ — PM requires fully hydrated objects.

    Stripped {"id": N} inputs auto-hydrate via GET /units/{id}/ + GET /agents/{id}/
    before the POST. Full objects pass through. Partial objects raise pre-wire.
    """

    def _patch_io(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_post"),
            patch("cli_anything.propertymeld.http_backend.get_unit"),
            patch("cli_anything.propertymeld.http_backend.get_management_agent"),
        )

    def test_stripped_inputs_auto_hydrate_via_get(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 12772803, "brief_description": "test"}
            mgu.return_value = _FULL_UNIT_FIXTURE
            mga.return_value = _FULL_AGENT_FIXTURE

            result = http_backend.create_meld_in_project(
                project_id="222959",
                brief_description="test",
                description="test",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T02:52:41.393Z",
                unit={"id": 9000005},
                maintenance=[{"id": 90025, "type": "ManagementAgent"}],
                work_location="ffff",
                notify_owner=False,
                notify_tenants=True,
            )

            assert result["ok"] is True
            assert result["meld_id"] == 12772803
            assert result["project_id"] == "222959"
            mgu.assert_called_once_with(9000005)
            mga.assert_called_once_with(90025)
            path, payload, _, _ = mp.call_args[0]
            assert path == "projects/222959/list-create-meld/"
            assert payload["project"] == "222959"
            assert payload["notify_owners_string"] == "false"
            assert payload["notify_tenants_string"] == "true"
            assert payload["unit"] == _FULL_UNIT_FIXTURE
            assert payload["maintenance"] == [_FULL_AGENT_FIXTURE]

    def test_full_objects_pass_through_unchanged(self):
        """Power-user path: pre-hydrated objects skip the GET round-trip."""
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 99}

            http_backend.create_meld_in_project(
                project_id="222959",
                brief_description="b",
                description="d",
                work_category="INTERIOR",
                work_type="PREVENTIVE_MAINTENANCE",
                due_date="2026-05-16T00:00:00.000Z",
                unit=_FULL_UNIT_FIXTURE,
                maintenance=[_FULL_AGENT_FIXTURE],
            )

            mgu.assert_not_called()
            mga.assert_not_called()
            _, payload, _, _ = mp.call_args[0]
            assert payload["unit"] == _FULL_UNIT_FIXTURE
            assert payload["maintenance"] == [_FULL_AGENT_FIXTURE]

    def test_maintenance_as_single_dict_gets_wrapped_and_hydrated(self):
        mc_p, mch_p, mcs_p, mp_p, mgu_p, mga_p = self._patch_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mp_p as mp, mgu_p as mgu, mga_p as mga:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 99}
            mgu.return_value = _FULL_UNIT_FIXTURE
            mga.return_value = _FULL_AGENT_FIXTURE

            http_backend.create_meld_in_project(
                project_id="222959",
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit={"id": 1},
                maintenance={"id": 90025},
            )

            _, payload, _, _ = mp.call_args[0]
            assert payload["maintenance"] == [_FULL_AGENT_FIXTURE]

    def test_partial_unit_raises_with_missing_keys(self):
        with pytest.raises(ValueError, match="display_address"):
            http_backend.create_meld_in_project(
                project_id="222959",
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit={"id": 1, "some_other_key": "x"},
                maintenance=[_FULL_AGENT_FIXTURE],
            )

    def test_partial_maintenance_raises_with_missing_keys(self):
        with pytest.raises(ValueError, match="selected_property_groups"):
            http_backend.create_meld_in_project(
                project_id="222959",
                brief_description="b",
                description="d",
                work_category="APPLIANCES",
                work_type="TURN",
                due_date="2026-05-16T00:00:00.000Z",
                unit=_FULL_UNIT_FIXTURE,
                maintenance=[{"id": 90025, "first_name": "Person031"}],
            )


class TestUnitAndAgentHydration:
    """GET /api/units/{id}/ and GET /api/agents/{id}/ — hydration helpers."""

    def test_get_unit_hits_correct_path(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mg.return_value = _FULL_UNIT_FIXTURE
            result = http_backend.get_unit(9000005)
            assert result == _FULL_UNIT_FIXTURE
            path, _ = mg.call_args[0]
            assert path == "units/9000005/"

    def test_get_management_agent_hits_correct_path(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mg.return_value = _FULL_AGENT_FIXTURE
            result = http_backend.get_management_agent(90025)
            assert result == _FULL_AGENT_FIXTURE
            path, _ = mg.call_args[0]
            assert path == "agents/90025/"


class TestPatchMeldProjectLink:
    """PATCH /api/melds/{id}/ with {"id":<m>,"project":<pid|None>} — pm-capture verified."""

    def _patched(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_patch"),
        )

    def test_attach(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_patch_p = self._patched()
        with mock_creds_p as mc, mock_cookie_p as mch, mock_csrf_p as mcs, mock_patch_p as mpt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mpt.return_value = {"id": 12772756, "project": 222959}

            result = http_backend.patch_meld_project_link("12772756", 222959)

            assert result["ok"] is True
            assert result["meld_id"] == 12772756
            assert result["project_id"] == 222959
            path, payload, _, _ = mpt.call_args[0]
            assert path == "melds/12772756/"
            assert payload == {"id": 12772756, "project": 222959}

    def test_detach_sends_null(self):
        mock_creds_p, mock_cookie_p, mock_csrf_p, mock_patch_p = self._patched()
        with mock_creds_p as mc, mock_cookie_p as mch, mock_csrf_p as mcs, mock_patch_p as mpt:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mpt.return_value = {"id": 12772756, "project": None}

            result = http_backend.patch_meld_project_link("12772756", None)

            assert result["ok"] is True
            assert result["project_id"] is None
            _, payload, _, _ = mpt.call_args[0]
            assert payload == {"id": 12772756, "project": None}


# ──────────────────────────────────────────────────────────────────────────────
# Top-level project create/edit + meld notes (pm-capture 2nd session 2026-05-13)
# ──────────────────────────────────────────────────────────────────────────────


class TestCreateProjectLiveShape:
    """POST /api/projects/ — verified from 2nd pm-capture (2026-05-13 02:58Z).

    Captured payload was:
      {name, project_type, description, due_date, start_date,
       coordinators:[int], meld_location:"Unit", prop:null,
       unit:{id:int, label:str}}
    """

    def test_happy_path_mirrors_capture(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_post") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 222962, "name": "vendor assigning"}

            result = http_backend.create_project(
                name="vendor assigning",
                project_type="TURN",
                due_date="2026-05-22T04:00:00.000Z",
                start_date="2026-05-13T23:59:59-04:00",
                coordinators=[90025],
                unit={"id": 9000005, "label": "123 Main St, Chattanooga, TN, 37421"},
            )

            assert result["ok"] is True
            assert result["project_id"] == 222962
            path, payload, _, _ = mp.call_args[0]
            assert path == "projects/"
            assert payload["name"] == "vendor assigning"
            assert payload["project_type"] == "TURN"
            assert payload["coordinators"] == [90025]
            assert payload["meld_location"] == "Unit"
            assert payload["prop"] is None
            assert payload["unit"] == {"id": 9000005, "label": "123 Main St, Chattanooga, TN, 37421"}
            assert payload["description"] == ""


class TestUpdateProjectLiveShape:
    """PATCH /api/projects/{id}/ — full-payload-echo verified live 2026-05-14.

    PM rejects partial PATCH on projects (HTTP 400 "field is required" for
    every omitted required field). update_project fetches the current
    project first, overlays caller-set fields, and sends the FULL merged
    payload. The mocked _http_get returns the current state.
    """

    CURRENT_PROJECT = {
        "id": 222959,
        "name": "original",
        "project_type": "TURN",
        "description": "old description",
        "due_date": "2026-05-30T04:00:00Z",
        "start_date": "2026-05-14T03:00:00Z",
        "coordinators": [{"id": 90025, "first_name": "Person031"}],
        "meld_location": "Unit",
        "prop": None,
        "unit": {"id": 9000005, "label": "123 Main St"},
    }

    def test_happy_path_merges_caller_fields_with_full_echo(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = dict(self.CURRENT_PROJECT)
            mp.return_value = {"id": 222959, "name": "renamed"}

            result = http_backend.update_project(
                project_id="222959",
                name="renamed",
                description="new description",
            )

            assert result["ok"] is True
            assert result["project_id"] == "222959"
            mg.assert_called_once()
            mp.assert_called_once()
            path, payload, _, _ = mp.call_args[0]
            assert path == "projects/222959/"
            # Full payload echo — required fields all present, caller fields override.
            assert payload["name"] == "renamed"
            assert payload["description"] == "new description"
            assert payload["project_type"] == "TURN"
            assert payload["coordinators"] == [90025]  # coordinator dict flattened to id
            assert payload["unit"] == {"id": 9000005, "label": "123 Main St"}
            assert payload["meld_location"] == "Unit"
            assert payload["due_date"] == "2026-05-30T04:00:00Z"
            assert payload["start_date"] == "2026-05-14T03:00:00Z"

    def test_passing_no_overrides_still_sends_full_echo(self):
        """Calling update_project with no overrides is a "ping with echo" — PM accepts."""
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = dict(self.CURRENT_PROJECT)
            mp.return_value = {"id": 222959}

            result = http_backend.update_project(project_id="222959")

            assert result["ok"] is True
            mp.assert_called_once()
            _, payload, _, _ = mp.call_args[0]
            # No overrides — echo back the current state verbatim.
            assert payload["name"] == "original"
            assert payload["project_type"] == "TURN"


class TestUpdateMeldNotes:
    """PATCH /api/v2/melds/{id}/notes/ — verified from 2nd pm-capture."""

    def test_happy_path(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 12772720, "maintenance_notes": "test"}

            result = http_backend.update_meld_notes("12772720", "test")

            assert result["ok"] is True
            assert result["meld_id"] == 12772720
            path, payload, _, _ = mp.call_args[0]
            assert path == "v2/melds/12772720/notes/"
            assert payload == {"maintenance_notes": "test"}


class TestCloneMeldOverrides:
    def _patch_clone_io(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_get"),
            patch("cli_anything.propertymeld.http_backend._http_post"),
        )

    def _source(self):
        return {
            "brief_description": "Old title",
            "description": "old long-form",
            "work_category": "PLUMBING",
            "work_location": "Bathroom",
            "priority": "EMERGENCY",
            "tenant_presence_required": True,
            "unit": {"id": 999999},
        }

    def test_long_description_override(self):
        mc_p, mch_p, mcs_p, mget_p, mp_p = self._patch_clone_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mget_p as mget, mp_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mget.return_value = self._source()
            mp.return_value = {"id": 555, "reference_id": "TABCDE"}

            http_backend.clone_meld("90000002", description="new long-form")

            _, payload, _, _ = mp.call_args[0]
            assert payload["description"] == "new long-form"

    def test_no_tenant_presence_required_override(self):
        mc_p, mch_p, mcs_p, mget_p, mp_p = self._patch_clone_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mget_p as mget, mp_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mget.return_value = self._source()
            mp.return_value = {"id": 555, "reference_id": "TABCDE"}

            http_backend.clone_meld("90000002", tenant_presence_required=False)

            _, payload, _, _ = mp.call_args[0]
            assert payload["tenant_presence_required"] is False

    def test_unit_id_override(self):
        mc_p, mch_p, mcs_p, mget_p, mp_p = self._patch_clone_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mget_p as mget, mp_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mget.return_value = self._source()
            mp.return_value = {"id": 555, "reference_id": "TABCDE"}

            http_backend.clone_meld("90000002", unit_id=9000005)

            _, payload, _, _ = mp.call_args[0]
            assert payload["unit"] == {"id": 9000005}

    def test_priority_override(self):
        mc_p, mch_p, mcs_p, mget_p, mp_p = self._patch_clone_io()
        with mc_p as mc, mch_p as mch, mcs_p as mcs, mget_p as mget, mp_p as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mget.return_value = self._source()
            mp.return_value = {"id": 555, "reference_id": "TABCDE"}

            http_backend.clone_meld("90000002", priority="LOW")

            _, payload, _, _ = mp.call_args[0]
            assert payload["priority"] == "LOW"


_TENANT_FIXTURE = {
    "id": 9000009,
    "user": {"id": 9001, "first_name": "Resident", "last_name": "Person032", "email": "resident.epsilon@example.com"},
    "contact": {"id": 4001, "home_phone": "+12025550114"},
    "is_active": True,
    "management": 3287,
}


class TestLinkTenantToMeld:
    """PATCH /api/melds/{id}/ with merged tenants array — closes P1 #14."""

    def _patches(self):
        return (
            patch("cli_anything.propertymeld.http_backend._load_creds"),
            patch("cli_anything.propertymeld.http_backend._cookie_header"),
            patch("cli_anything.propertymeld.http_backend._get_csrf_token"),
            patch("cli_anything.propertymeld.http_backend._http_get"),
            patch("cli_anything.propertymeld.http_backend._http_patch"),
        )

    def _stub_creds(self, mc, mch, mcs):
        mc.return_value = {"cookie": "x"}
        mch.return_value = "Cookie: session=xyz"
        mcs.return_value = "csrf"

    def test_appends_to_existing_tenants_array(self):
        with self._patches()[0] as mc, self._patches()[1] as mch, \
             self._patches()[2] as mcs, self._patches()[3] as mg, \
             self._patches()[4] as mp:
            self._stub_creds(mc, mch, mcs)
            existing = {"id": 9999, "first_name": "Other", "last_name": "Tenant"}
            mg.side_effect = [
                {"id": "90000003", "tenants": [existing]},  # GET meld
                _TENANT_FIXTURE,                              # GET tenant for hydration
            ]
            mp.return_value = {"id": "90000003", "tenants": [existing, _TENANT_FIXTURE]}

            result = http_backend.link_tenant_to_meld("90000003", 9000009)

            assert result["ok"] is True
            assert result["linked"] is True
            assert result["tenant_id"] == 9000009
            assert result["tenant_count"] == 2
            path, payload, _, _ = mp.call_args[0]
            assert path == "melds/90000003/"
            assert payload["id"] == 90000003
            assert len(payload["tenants"]) == 2
            assert _TENANT_FIXTURE in payload["tenants"]
            assert existing in payload["tenants"]

    def test_idempotent_already_linked_short_circuits_patch(self):
        with self._patches()[0] as mc, self._patches()[1] as mch, \
             self._patches()[2] as mcs, self._patches()[3] as mg, \
             self._patches()[4] as mp:
            self._stub_creds(mc, mch, mcs)
            mg.return_value = {"id": "90000003", "tenants": [_TENANT_FIXTURE]}

            result = http_backend.link_tenant_to_meld("90000003", 9000009)

            assert result["ok"] is True
            assert result.get("already_linked") is True
            assert result.get("linked") is None or result.get("linked") is False
            assert result["tenant_count"] == 1
            mp.assert_not_called()

    def test_hits_correct_paths_get_meld_then_get_tenant_then_patch(self):
        with self._patches()[0] as mc, self._patches()[1] as mch, \
             self._patches()[2] as mcs, self._patches()[3] as mg, \
             self._patches()[4] as mp:
            self._stub_creds(mc, mch, mcs)
            mg.side_effect = [
                {"id": "90000003", "tenants": []},
                _TENANT_FIXTURE,
            ]
            mp.return_value = {"id": "90000003", "tenants": [_TENANT_FIXTURE]}

            http_backend.link_tenant_to_meld("90000003", 9000009)

            get_paths = [c.args[0] for c in mg.call_args_list]
            assert get_paths == ["melds/90000003/", "tenants/9000009/"]
            patch_path = mp.call_args[0][0]
            assert patch_path == "melds/90000003/"

    def test_handles_missing_tenants_field_on_meld(self):
        with self._patches()[0] as mc, self._patches()[1] as mch, \
             self._patches()[2] as mcs, self._patches()[3] as mg, \
             self._patches()[4] as mp:
            self._stub_creds(mc, mch, mcs)
            mg.side_effect = [
                {"id": "90000003"},  # no tenants key at all
                _TENANT_FIXTURE,
            ]
            mp.return_value = {"id": "90000003", "tenants": [_TENANT_FIXTURE]}

            result = http_backend.link_tenant_to_meld("90000003", 9000009)

            assert result["linked"] is True
            assert result["tenant_count"] == 1
            _, payload, _, _ = mp.call_args[0]
            assert payload["tenants"] == [_TENANT_FIXTURE]


class TestGetTenant:
    """GET /api/tenants/{id}/ — tenant hydration helper."""

    def test_get_tenant_hits_correct_path(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mg.return_value = _TENANT_FIXTURE
            result = http_backend.get_tenant(9000009)
            assert result == _TENANT_FIXTURE
            assert mg.call_args[0][0] == "tenants/9000009/"


class TestUpdateUnitNotes:
    """PATCH /api/units/{id}/ with {maintenance_notes: text} — closes P3 #8 (unit-level)."""

    def test_patches_correct_path_with_notes_body(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"id": 1754419, "maintenance_notes": "Water shut-off in basement"}

            result = http_backend.update_unit_notes(1754419, "Water shut-off in basement")

            assert result["ok"] is True
            assert result["unit_id"] == 1754419
            assert result["maintenance_notes"] == "Water shut-off in basement"
            path, payload, _, _ = mp.call_args[0]
            assert path == "units/1754419/"
            assert payload == {"maintenance_notes": "Water shut-off in basement"}

    def test_coerces_string_unit_id_to_int(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"maintenance_notes": "x"}

            result = http_backend.update_unit_notes("1754419", "x")
            assert result["unit_id"] == 1754419  # int, not str
            path, _, _, _ = mp.call_args[0]
            assert path == "units/1754419/"

    def test_empty_notes_clears_field(self):
        """Per pm-capture 2026-05-14T03:07:13, PATCH with empty string clears the field."""
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mp.return_value = {"maintenance_notes": ""}

            http_backend.update_unit_notes(1754419, "")
            _, payload, _, _ = mp.call_args[0]
            assert payload == {"maintenance_notes": ""}


_TENANT_NOTES_FIXTURE = {
    "id": 9000014,
    "first_name": "Resident",
    "last_name": "Person016",
    "middle_name": "",
    "notes": "",
    "is_active": True,
    "management": 3287,
    "user": {"id": 1934544, "email": "a@example.com"},
    "contact": {"id": 5033775, "cell_phone": "(202) 555-0106"},
    "leases": [{"unit_id": 1754357}],
}


class TestUpdateTenantNotes:
    """PATCH /api/tenants/{id}/ with full body, mutating only `notes` — closes
    P3 #8 (resident-level). Verified shape from pm-tenant-notes-endpoint-capture-2026-05-18.
    """

    def _wire(self, mc, mch, mcs, mg, mp, *, get_returns, patch_returns):
        mc.return_value = {"cookie": "x"}
        mch.return_value = "Cookie: session=xyz"
        mcs.return_value = "csrf"
        mg.return_value = get_returns
        mp.return_value = patch_returns

    def test_get_then_patch_full_body_with_notes_mutated(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            self._wire(
                mc, mch, mcs, mg, mp,
                get_returns=dict(_TENANT_NOTES_FIXTURE),
                patch_returns={**_TENANT_NOTES_FIXTURE, "notes": "Access after 3pm"},
            )

            result = http_backend.update_tenant_notes(9000014, "Access after 3pm")

            assert result["ok"] is True
            assert result["tenant_id"] == 9000014
            assert result["notes"] == "Access after 3pm"
            # GET path verified
            assert mg.call_args[0][0] == "tenants/9000014/"
            # PATCH path + full-body echo verified
            patch_path, patch_payload, _, _ = mp.call_args[0]
            assert patch_path == "tenants/9000014/"
            # Full body sent — not a thin patch
            assert patch_payload["first_name"] == "Resident"
            assert patch_payload["last_name"] == "Person016"
            assert patch_payload["id"] == 9000014
            assert patch_payload["contact"]["cell_phone"] == "(202) 555-0106"
            # Notes was mutated
            assert patch_payload["notes"] == "Access after 3pm"

    def test_coerces_string_tenant_id_to_int(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            self._wire(
                mc, mch, mcs, mg, mp,
                get_returns=dict(_TENANT_NOTES_FIXTURE),
                patch_returns={**_TENANT_NOTES_FIXTURE, "notes": "x"},
            )
            result = http_backend.update_tenant_notes("9000014", "x")
            assert result["tenant_id"] == 9000014  # int, not str
            assert mg.call_args[0][0] == "tenants/9000014/"
            assert mp.call_args[0][0] == "tenants/9000014/"

    def test_empty_notes_clears_field(self):
        """Empty string is a valid value — represents a deliberate clear."""
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            self._wire(
                mc, mch, mcs, mg, mp,
                get_returns={**_TENANT_NOTES_FIXTURE, "notes": "old value"},
                patch_returns={**_TENANT_NOTES_FIXTURE, "notes": ""},
            )
            http_backend.update_tenant_notes(9000014, "")
            _, payload, _, _ = mp.call_args[0]
            assert payload["notes"] == ""

    def test_preserves_other_fields_from_get(self):
        """The whole point of GET-mutate-PATCH is that fields we didn't touch
        survive — validators run on the full payload, so dropping fields is
        what made thin-patch return 400 in the live capture."""
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            self._wire(
                mc, mch, mcs, mg, mp,
                get_returns=dict(_TENANT_NOTES_FIXTURE),
                patch_returns={**_TENANT_NOTES_FIXTURE, "notes": "n"},
            )
            http_backend.update_tenant_notes(9000014, "n")
            _, payload, _, _ = mp.call_args[0]
            # Every non-notes key from the GET response must be in the PATCH body
            for key in _TENANT_NOTES_FIXTURE:
                if key == "notes":
                    continue
                assert key in payload, f"PATCH body dropped {key!r} — thin-patch will 400"

    def test_raises_when_get_returns_non_dict(self):
        with patch("cli_anything.propertymeld.http_backend._load_creds") as mc, \
             patch("cli_anything.propertymeld.http_backend._cookie_header") as mch, \
             patch("cli_anything.propertymeld.http_backend._get_csrf_token") as mcs, \
             patch("cli_anything.propertymeld.http_backend._http_get") as mg, \
             patch("cli_anything.propertymeld.http_backend._http_patch") as mp:
            mc.return_value = {"cookie": "x"}
            mch.return_value = "Cookie: session=xyz"
            mcs.return_value = "csrf"
            mg.return_value = "not a dict — unexpected api response"
            import pytest
            with pytest.raises(RuntimeError, match="non-dict"):
                http_backend.update_tenant_notes(9000014, "x")
            # We must not have attempted a PATCH if GET was malformed
            assert mp.call_count == 0
