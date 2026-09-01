"""Known-positives for the confident-absence fixes (2026-08-10).

Every assertion here is two-sided where it can be: proving the boundary RAN and
produced the intended thing, not only that something bad is absent. An
absence-only assertion passes on an empty payload or an unexercised path.
"""
import pytest

from cli_anything.propertymeld import api_backend, http_backend
from cli_anything.propertymeld.markers import (
    is_marker, marker_kind, not_carried, fetch_failed, MARKER_KEY,
)


class TestMarkersResistLaundering:
    """The property the whole fix rests on."""

    def test_marker_survives_the_or_empty_idiom(self):
        # `x or []` is the idiom that turned a null field into a confident 0.
        # A dict is truthy, so the marker passes through it untouched.
        m = not_carried("work_entries", "list")
        assert (m or []) is m, "marker was laundered into an empty list"

    def test_none_and_empty_list_ARE_laundered(self):
        # The control: this is what the old behaviour did, and why it was silent.
        assert (None or []) == []
        assert ([] or []) == []

    def test_marker_is_recognisable_and_typed(self):
        assert marker_kind(not_carried("f", "w")) == "not-carried"
        assert marker_kind(fetch_failed("f", "r")) == "fetch-failed"
        assert marker_kind([]) == ""
        assert marker_kind(None) == ""
        assert not is_marker({"work_entries": []})


class TestInspectAllowlist:
    """The boundary must drop what it has never heard of."""

    def test_UNKNOWN_FIELD_IN_ABSENT_OUT(self, monkeypatch):
        # THE known-positive dane specified. A denylist of named secrets would
        # pass every other test here and fail this one.
        sentinel = "SENTINEL-a7f3-must-not-appear"
        raw = {
            "id": 123,
            "reference_id": "ABC123",
            "status": "open",
            "management_auth_token": sentinel,      # never seen before
            "some_future_upstream_field": sentinel,  # nor this
        }
        exposed = http_backend._expose_meld(raw)

        # POSITIVE half: the boundary ran and kept what consumers read.
        assert exposed["id"] == 123
        assert exposed["reference_id"] == "ABC123"
        assert exposed["status"] == "open"

        # NEGATIVE half: the sentinel is gone from every value.
        assert sentinel not in repr(exposed)
        assert "management_auth_token" not in exposed
        assert "some_future_upstream_field" not in exposed

    def test_allowlist_does_not_invent_missing_fields(self):
        # Absent upstream stays absent — the boundary selects, it does not
        # fabricate empties, which would be the same lie in a new place.
        exposed = http_backend._expose_meld({"id": 1})
        assert exposed == {"id": 1}

    def test_non_dict_passes_through_untouched(self):
        assert http_backend._expose_meld(None) is None


class TestWorkEntriesNeverEmptyOnFailure:
    """A fetch failure must not degrade into the bug being fixed."""

    def test_failure_yields_FETCH_FAILED_marker_not_empty(self, monkeypatch):
        def boom(_meld_id):
            raise RuntimeError("timeout after 20s")
        monkeypatch.setattr(http_backend, "list_work_entries", boom)
        monkeypatch.setattr(api_backend.time, "sleep", lambda _s: None)

        got = api_backend._fetch_work_entries_or_marker("900001")

        assert is_marker(got), "failure produced a launderable empty value"
        assert marker_kind(got) == "fetch-failed"
        assert "timeout after 20s" in got["detail"]
        assert (got or []) is got  # still unlaunderable

    def test_success_returns_REAL_entries_and_no_marker(self, monkeypatch):
        # Known positive: without it, a function hardwired to return a marker
        # would satisfy the failure test above.
        rows = [{"id": 1, "hours": 2.5}]
        monkeypatch.setattr(http_backend, "list_work_entries", lambda _m: rows)
        got = api_backend._fetch_work_entries_or_marker("900001")
        assert got == rows
        assert not is_marker(got)

    def test_retries_before_giving_up(self, monkeypatch):
        calls = {"n": 0}
        def flaky(_meld_id):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("timeout")
            return [{"id": 9}]
        monkeypatch.setattr(http_backend, "list_work_entries", flaky)
        monkeypatch.setattr(api_backend.time, "sleep", lambda _s: None)

        got = api_backend._fetch_work_entries_or_marker("900001")
        assert got == [{"id": 9}]
        assert calls["n"] == 3, "did not retry the measured 1-in-3 timeout rate"

    def test_unexpected_payload_type_is_a_failure_not_a_result(self, monkeypatch):
        monkeypatch.setattr(http_backend, "list_work_entries", lambda _m: {"nope": 1})
        monkeypatch.setattr(api_backend.time, "sleep", lambda _s: None)
        got = api_backend._fetch_work_entries_or_marker("900001")
        assert marker_kind(got) == "fetch-failed"
