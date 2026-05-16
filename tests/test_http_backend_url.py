"""Contract tests for _build_url and side-aware HTTP helpers.

Foundation for vendor-side endpoint coverage. Asserts that the same code path
hits /m/{mgmt_id}/api/ vs /v/{vendor_id}/api/ purely by side= parameter, and
that the nested-vs-top-level path asymmetry can be exercised without code
duplication.
"""
import json
import pytest

from cli_anything.propertymeld import http_backend as hb


class TestBuildUrl:
    def test_manager_default(self):
        url = hb._build_url("melds/M123ABC/complete/")
        assert url == (
            f"https://app.propertymeld.com/{hb.MULTITENANT}"
            f"/m/{hb.MULTITENANT}/api/melds/M123ABC/complete/"
        )

    def test_manager_explicit(self):
        url = hb._build_url("melds/", side="manager")
        assert "/m/" in url
        assert "/v/" not in url

    def test_vendor_requires_vendor_id(self):
        with pytest.raises(ValueError, match="vendor_id required"):
            hb._build_url("melds/M123ABC/complete/", side="vendor")

    def test_vendor_with_id(self):
        url = hb._build_url("melds/M123ABC/complete/", side="vendor", vendor_id="91159")
        assert url == (
            f"https://app.propertymeld.com/{hb.MULTITENANT}"
            f"/v/91159/api/melds/M123ABC/complete/"
        )

    def test_unknown_side_raises(self):
        with pytest.raises(ValueError, match="unknown side"):
            hb._build_url("melds/", side="agent")


class _FakeResp:
    """Minimal context-manager stand-in for urllib.request.urlopen."""

    def __init__(self, body: bytes = b'{"ok": true}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TestHelperRouting:
    """Confirm _http_* helpers thread side+vendor_id through _build_url."""

    def test_http_patch_manager_url(self, monkeypatch):
        captured: dict = {}

        def fake_urlopen(req, **kw):
            captured["url"] = req.full_url
            return _FakeResp()

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        hb._http_patch("melds/M1/complete/", {}, "sessionid=x", "csrf")
        assert "/m/" in captured["url"]
        assert "/melds/M1/complete/" in captured["url"]

    def test_http_patch_vendor_url(self, monkeypatch):
        captured: dict = {}

        def fake_urlopen(req, **kw):
            captured["url"] = req.full_url
            return _FakeResp()

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        hb._http_patch(
            "melds/M1/complete/", {}, "sessionid=x", "csrf",
            side="vendor", vendor_id="91159",
        )
        assert "/v/91159/" in captured["url"]
        assert "/m/" not in captured["url"]

    def test_http_delete_returns_empty_on_204(self, monkeypatch):
        monkeypatch.setattr(
            hb.urllib.request, "urlopen",
            lambda req, **kw: _FakeResp(body=b""),
        )
        result = hb._http_delete("melds/work-entries/E1/", "sessionid=x", "csrf")
        assert result == {}

    def test_http_delete_top_level_path_distinct_from_nested(self, monkeypatch):
        """Guard for the asymmetry rule: DELETE work-entries hits TOP-LEVEL,
        never /melds/{meld_id}/work-entries/{entry_id}/."""
        captured: dict = {}

        def fake_urlopen(req, **kw):
            captured["url"] = req.full_url
            return _FakeResp(body=b"")

        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)
        hb._http_delete("melds/work-entries/E1/", "sessionid=x", "csrf")
        assert "/melds/work-entries/E1/" in captured["url"]
        assert "/work-entries/" in captured["url"]
        # Asymmetry guard: top-level path has NO meld_id segment before work-entries
        url_after_api = captured["url"].split("/api/", 1)[1]
        assert url_after_api == "melds/work-entries/E1/"


class TestCompleteMeldSideRouting:
    def test_complete_meld_vendor_requires_id(self):
        """Side validation fires before _validate_meld_id and before any HTTP I/O.
        Decorator @with_recapture_retry only catches SessionExpired, so
        ValueError propagates cleanly."""
        with pytest.raises(ValueError, match="vendor_id required"):
            hb.complete_meld("12345678", side="vendor")

    def test_complete_meld_vendor_requires_completion_date(self):
        """Vendor surface PATCH /melds/{id}/complete/ requires a date field
        per capture 2026-05-16 024240Z. Validate before HTTP I/O."""
        with pytest.raises(ValueError, match="completion_date required"):
            hb.complete_meld("12345678", side="vendor", vendor_id="91159")

    def test_complete_meld_manager_payload_shape(self, monkeypatch):
        """Manager side: payload is {completion_notes?: str}."""
        captured: dict = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'

        def fake_urlopen(req, **kw):
            captured["url"] = req.full_url
            captured["body"] = req.data
            return _Resp()

        monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
        monkeypatch.setattr(hb, "_cookie_header", lambda c: "sessionid=x")
        monkeypatch.setattr(hb, "_get_csrf_token", lambda c: "csrf")
        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        hb.complete_meld("12345678", completion_notes="done")
        assert "/m/" in captured["url"]
        import json as _json
        assert _json.loads(captured["body"]) == {"completion_notes": "done"}

    def test_complete_meld_vendor_payload_shape(self, monkeypatch):
        """Vendor side payload (capture-verified):
        {is_complete: true, date: <iso>, reason: <str>}."""
        captured: dict = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'

        def fake_urlopen(req, **kw):
            captured["url"] = req.full_url
            captured["body"] = req.data
            return _Resp()

        monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
        monkeypatch.setattr(hb, "_cookie_header", lambda c: "sessionid=x")
        monkeypatch.setattr(hb, "_get_csrf_token", lambda c: "csrf")
        monkeypatch.setattr(hb.urllib.request, "urlopen", fake_urlopen)

        hb.complete_meld(
            "12791157", completion_notes="completed",
            side="vendor", vendor_id="91159",
            completion_date="2026-05-17T14:00:00.000Z",
        )
        assert "/v/91159/" in captured["url"]
        assert "/m/" not in captured["url"]
        import json as _json
        body = _json.loads(captured["body"])
        assert body == {
            "is_complete": True,
            "date": "2026-05-17T14:00:00.000Z",
            "reason": "completed",
        }
