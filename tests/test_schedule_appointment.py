"""HTTP-boundary contract tests for schedule_appointment (in-house meld).

These drive the REAL error boundary that produced the live HTTP 500:
- The OLD flow PUT to /management-appointments/{appt_id}/schedule/ with an
  `availability_segment` payload. That endpoint 500s server-side for every
  shape (diagnosed live 2026-06-03, demo fixture meld 12937555).
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
        "id": 12937555,
        "management_availability_segments": [
            {
                "id": 2878830,
                "event": {
                    "id": 42578110,
                    "dtstart": "2026-06-10T18:00:00Z",
                    "dtend": "2026-06-10T20:00:00Z",
                },
            }
        ],
        "required_appointments": None,
    }
).encode()

_MELD_WITH_APPT = json.dumps(
    {"managementappointment": [{"id": 4255991, "availability_segment": None}]}
).encode()


def _wire(monkeypatch, *, meld_body=_MELD_WITH_APPT, accept_body=_ACCEPT_200):
    """Install a urlopen that returns meld_body for the GET and accept_body for
    the accept/ PATCH, and RAISES the live 500 if anyone hits the dead
    /management-appointments/.../schedule/ endpoint (regression tripwire)."""
    calls = []

    def fake_urlopen(req, **kw):
        method = req.get_method()
        url = req.full_url
        calls.append({"method": method, "url": url, "body": req.data})
        if "/management-appointments/" in url and url.endswith("/schedule/"):
            # The endpoint that produced the original 500 — any hit is a regression.
            raise urllib.error.HTTPError(url, 500, "Server Error", {}, io.BytesIO(_PM_500_HTML))
        if url.endswith("/accept/"):
            return _FakeResp(accept_body)
        if method == "GET" and url.endswith("/melds/12937555/"):
            return _FakeResp(meld_body)
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
            "12937555", "2026-06-10T14:00:00-04:00", duration_hours=2.0
        )

        # No call ever hit the dead schedule endpoint (it would have raised 500).
        assert not any(
            c["url"].endswith("/schedule/") for c in calls
        ), "regression: schedule_appointment hit the dead /schedule/ PUT"

        # The write went to the meld-level accept action as a PATCH.
        accept_calls = [c for c in calls if c["url"].endswith("/accept/")]
        assert len(accept_calls) == 1
        assert accept_calls[0]["method"] == "PATCH"
        assert "/melds/12937555/accept/" in accept_calls[0]["url"]

        assert result["ok"] is True
        assert result["meld_id"] == 12937555
        assert result["appointment_id"] == 4255991
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
            "12937555", "2026-06-10T14:00:00-04:00", duration_hours=2.0
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
            "12937555", "2026-06-10T14:00:00-04:00", duration_hours=2.0
        )

        assert result == {
            "ok": False,
            "error": "No in-house tech assignment found on this meld",
        }
        assert not any(c["url"].endswith("/accept/") for c in calls)

    def test_dead_schedule_put_sys_exits_on_500(self, monkeypatch):
        """The dead endpoint, if ever called via _http_put, sys.exit(1)s on the
        500 — which is exactly why the old schedule_appointment killed callers.
        The fix avoids this endpoint entirely (covered above)."""
        _patch_creds_csrf(monkeypatch)
        _wire(monkeypatch)
        with pytest.raises(SystemExit):
            hb._http_put(
                "management-appointments/4255991/schedule/",
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
