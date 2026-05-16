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
