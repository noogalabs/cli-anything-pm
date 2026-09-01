"""HTTP-boundary contract tests for schedule_appointment (in-house meld).

These drive the REAL error boundary that produced the live HTTP 500:
- The OLD flow PUT to /management-appointments/{appt_id}/schedule/ with an
  `availability_segment` payload. That endpoint 500s server-side for every
  shape (diagnosed live 2026-06-03, demo fixture meld 90000018).
- The FIXED flow PATCHes /melds/{meld_id}/accept/ with
  `management_availability_segments`, which 200s and books the window.

The tests mock urllib.request.urlopen at the wire boundary (NOT a mid-level
helper), so they assert the exact endpoint, verb, and payload the function
puts on the wire, and they make the regression endpoint RAISE the live 500
to prove the new flow never touches it.
"""
import io
import json

import pytest
import urllib.error

from cli_anything.propertymeld import http_backend as hb


# A minimal stand-in for the PM Django 500 HTML error page.
_PM_500_HTML = b"<!DOCTYPE html><html><head><title>Server Error</title></head></html>"


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _patch_creds_csrf(monkeypatch):
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    monkeypatch.setattr(hb, "_cookie_header", lambda creds: "sessionid=fake")
    monkeypatch.setattr(hb, "_get_csrf_token", lambda cookie_hdr: "csrf-fake")


# The accept/ response shape verified live (trimmed to the fields the
# function reads back for its return contract).
_ACCEPT_200 = json.dumps(
    {
        "id": 90000018,
        "management_availability_segments": [
            {
                "id": 9000009,
                "event": {
                    "id": 90000022,
                    "dtstart": "2026-06-10T18:00:00Z",
                    "dtend": "2026-06-10T20:00:00Z",
                },
            }
        ],
        "required_appointments": None,
    }
).encode()

_MELD_WITH_APPT = json.dumps(
    {"managementappointment": [{"id": 9000022, "availability_segment": None}]}
).encode()

_ZOMBIE_MELD_WITH_APPT = json.dumps(
    {
        "status": "PENDING_MORE_MANAGEMENT_AVAILABILITY",
        "tenants": [],
        "managementappointment": [{"id": 9000022, "availability_segment": None}],
    }
).encode()

_PENDING_COMPLETION_MELD = json.dumps(
    {
        "status": "PENDING_COMPLETION",
        "tenants": [],
        "managementappointment": [{"id": 9000022, "availability_segment": None}],
    }
).encode()

_OCCUPIED_ZOMBIE_MELD = json.dumps(
    {
        "status": "PENDING_MORE_MANAGEMENT_AVAILABILITY",
        "tenants": [{"id": 9000023, "name": "SyntheticOne"}],
        "managementappointment": [{"id": 9000022, "availability_segment": None}],
    }
).encode()


def _wire(monkeypatch, *, meld_body=_MELD_WITH_APPT, accept_body=_ACCEPT_200):
    """Install a urlopen that returns meld_body for the GET and accept_body for
    the accept/ PATCH, and RAISES the live 500 if anyone hits the dead
    /management-appointments/.../schedule/ endpoint (regression tripwire)."""
    calls = []
    meld_bodies = list(meld_body) if isinstance(meld_body, (list, tuple)) else [meld_body]

    def fake_urlopen(req, **kw):
        method = req.get_method()
        url = req.full_url
        calls.append({"method": method, "url": url, "body": req.data})
        if "/management-appointments/" in url and url.endswith("/schedule/"):
            # The endpoint that produced the original 500 — any hit is a regression.
            raise urllib.error.HTTPError(url, 500, "Server Error", {}, io.BytesIO(_PM_500_HTML))
        if url.endswith("/accept/"):
            return _FakeResp(accept_body)
        if method == "GET" and url.endswith("/melds/90000018/work-entries/"):
            return _FakeResp(json.dumps({"results": []}).encode())
        if method == "GET" and url.endswith("/melds/90000018/"):
            if len(meld_bodies) > 1:
                return _FakeResp(meld_bodies.pop(0))
            return _FakeResp(meld_bodies[0])
        return _FakeResp(b"{}")

    monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
    return calls


class TestScheduleAppointmentFlow:
    def test_uses_accept_endpoint_not_dead_schedule_put(self, monkeypatch):
        """The fix must route through melds/{id}/accept/, never the 500-ing
        management-appointments/{id}/schedule/ PUT."""
        _patch_creds_csrf(monkeypatch)
        calls = _wire(monkeypatch)

        result = hb.schedule_appointment(
            "90000018", "2026-06-10T14:00:00-04:00", duration_hours=2.0
        )

        # No call ever hit the dead schedule endpoint (it would have raised 500).
        assert not any(
            c["url"].endswith("/schedule/") for c in calls
        ), "regression: schedule_appointment hit the dead /schedule/ PUT"

        # The write went to the meld-level accept action as a PATCH.
        accept_calls = [c for c in calls if c["url"].endswith("/accept/")]
        assert len(accept_calls) == 1
        assert accept_calls[0]["method"] == "PATCH"
        assert "/melds/90000018/accept/" in accept_calls[0]["url"]

        assert result["ok"] is True
        assert result["meld_id"] == 90000018
        assert result["appointment_id"] == 9000022
        assert result["duration_hours"] == 2.0
        # dtstart echoed back from the booked segment.
        assert result["dtstart"] == "2026-06-10T18:00:00Z"
        assert "result" in result

    def test_payload_shape_matches_live_200(self, monkeypatch):
        """Payload must be the management_availability_segments envelope with
        event {dtstart, dtend} — dtend computed from duration_hours."""
        _patch_creds_csrf(monkeypatch)
        calls = _wire(monkeypatch)

        hb.schedule_appointment(
            "90000018", "2026-06-10T14:00:00-04:00", duration_hours=2.0
        )

        accept = [c for c in calls if c["url"].endswith("/accept/")][0]
        payload = json.loads(accept["body"])
        assert payload == {
            "mark_scheduled": True,
            "segments_to_keep": [],
            "management_availability_segments": [
                {
                    "event": {
                        "dtstart": "2026-06-10T14:00:00-04:00",
                        # 14:00 + 2h = 16:00, same offset.
                        "dtend": "2026-06-10T16:00:00-04:00",
                    }
                }
            ],
        }

    def test_no_in_house_assignment_guard_preserved(self, monkeypatch):
        """A meld with no managementappointment returns the guard error and
        never fires the accept PATCH (edge case b)."""
        _patch_creds_csrf(monkeypatch)
        empty = json.dumps({"managementappointment": []}).encode()
        calls = _wire(monkeypatch, meld_body=empty)

        result = hb.schedule_appointment(
            "90000018", "2026-06-10T14:00:00-04:00", duration_hours=2.0
        )

        assert result == {
            "ok": False,
            "error": "No in-house tech assignment found on this meld",
        }
        assert not any(c["url"].endswith("/accept/") for c in calls)

    def test_existing_segments_block_not_wipe(self, monkeypatch):
        """A meld that already has a booked availability_segment must NOT be
        silently replaced. The accept/ payload sends segments_to_keep:[], which
        would wipe existing windows, so the guard returns a clear error and
        never fires the accept PATCH (no silent data loss). First-schedule
        success is covered by test_uses_accept_endpoint_not_dead_schedule_put."""
        _patch_creds_csrf(monkeypatch)
        scheduled = json.dumps(
            {"managementappointment": [
                {"id": 9000022, "availability_segment": {"id": 9000009}}
            ]}
        ).encode()
        calls = _wire(monkeypatch, meld_body=scheduled)

        result = hb.schedule_appointment(
            "90000018", "2026-06-10T14:00:00-04:00", duration_hours=2.0
        )

        assert result["ok"] is False
        assert "reschedule" in result["error"].lower()
        # Same {ok, error} contract shape as the no-assignment guard.
        assert set(result.keys()) == {"ok", "error"}
        # Crucially: NO accept/ PATCH was sent — existing windows untouched.
        assert not any(c["url"].endswith("/accept/") for c in calls)

    def test_dead_schedule_put_sys_exits_on_500(self, monkeypatch):
        """The dead endpoint, if ever called via _http_put, sys.exit(1)s on the
        500 — which is exactly why the old schedule_appointment killed callers.
        The fix avoids this endpoint entirely (covered above)."""
        _patch_creds_csrf(monkeypatch)
        _wire(monkeypatch)
        with pytest.raises(SystemExit):
            hb._http_put(
                "management-appointments/9000022/schedule/",
                {"availability_segment": {}},
                "sessionid=fake",
                "csrf-fake",
            )

    def test_compute_dtend(self):
        assert (
            hb._compute_dtend("2026-06-10T14:00:00-04:00", 2.0)
            == "2026-06-10T16:00:00-04:00"
        )
        assert (
            hb._compute_dtend("2026-06-10T14:00:00Z", 1.5)
            == "2026-06-10T15:30:00+00:00"
        )
        # Unparseable input falls back to dtstart (PM then 400s, not us guessing).
        assert hb._compute_dtend("not-a-date", 2.0) == "not-a-date"

class TestForcePendingCompletionDisabled:
    """force-pending-completion was hard-guarded 2026-06-23: it only ever produced
    in-house PENDING_COMPLETION melds, which strand in MAINTENANCE_COULD_NOT_COMPLETE
    when closed via manager complete/. It now refuses up front without mutating PM
    state. Full in-house guard coverage: tests/test_in_house_complete_guard.py.
    """

    def test_refuses_up_front_without_network(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("disabled command must not hit the network")
        monkeypatch.setattr(hb.urllib.request, "urlopen", _boom)
        result = hb.force_pending_completion(
            "90000018", dtstart="2026-06-10T18:00:00Z", duration_hours=0.25
        )
        assert result["ok"] is False
        assert result.get("deprecated") is True
        assert "tech-app" in result["error"] or "tech app" in result["error"]
