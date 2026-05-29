"""Tests for clone coordinator inheritance + the set-coordinator command.

Verified shapes (live, 2026-05-29):
- meld coordinator detail-GET shape is an object {id, ...}; PATCH accepts a
  bare int. Meld top-level PATCH requires a FULL payload echo (delta 400s on
  brief_description/work_location/work_category/work_type/priority).
- clone_meld must inherit the source coordinator (it previously did not).
"""
import pytest

from cli_anything.propertymeld import http_backend as hb


def _patch_creds_csrf(monkeypatch):
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    monkeypatch.setattr(hb, "_cookie_header", lambda creds: "sessionid=fake")
    monkeypatch.setattr(hb, "_get_csrf_token", lambda cookie_hdr: "csrf-fake")


SOURCE_MELD = {
    "id": 111,
    "brief_description": "Leaky faucet",
    "work_category": "PLUMBING",
    "work_location": "kitchen",
    "work_type": "REPAIR",
    "priority": "MEDIUM",
    "coordinator": {"id": 57163, "user": {"id": 1, "first_name": "David"}},
    "unit": {"id": 222},
}


class TestSetCoordinator:
    def test_full_echo_payload_with_int_coordinator(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        monkeypatch.setattr(hb, "_http_get", lambda path, ck: dict(SOURCE_MELD))
        captured = {}

        def fake_patch(path, payload, ck, csrf):
            captured["path"] = path
            captured["payload"] = payload
            return {"id": 111, "coordinator": {"id": 57163}}

        monkeypatch.setattr(hb, "_http_patch", fake_patch)

        result = hb.set_coordinator(111, 57163)

        assert result["ok"] is True
        assert result["coordinator_id"] == 57163
        assert captured["path"] == "melds/111/"
        # FULL ECHO: all PM-required fields present + coordinator as a bare int.
        p = captured["payload"]
        assert p["coordinator"] == 57163
        assert isinstance(p["coordinator"], int)
        for required in ("brief_description", "work_location", "work_category",
                         "work_type", "priority"):
            assert required in p, f"full-echo payload missing {required}"
        assert p["priority"] == "MEDIUM"

    def test_fetch_failure_surfaces_error_not_silent(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        monkeypatch.setattr(hb, "_http_get",
                            lambda path, ck: {"error": "HTTP 404", "status_code": 404})
        # _http_patch must NOT be called if the fetch failed.
        monkeypatch.setattr(hb, "_http_patch",
                            lambda *a, **k: pytest.fail("PATCH attempted despite fetch failure"))
        result = hb.set_coordinator(111, 57163)
        assert result["ok"] is False
        assert "could not fetch" in result["error"]

    def test_patch_400_surfaces_error(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        monkeypatch.setattr(hb, "_http_get", lambda path, ck: dict(SOURCE_MELD))
        monkeypatch.setattr(hb, "_http_patch",
                            lambda *a, **k: {"error": "HTTP 400", "status_code": 400})
        result = hb.set_coordinator(111, 57163)
        assert result["ok"] is False
        assert "PATCH failed" in result["error"]


class TestCloneCoordinatorInheritance:
    def test_clone_inherits_source_coordinator(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        monkeypatch.setattr(hb, "_http_get", lambda path, ck: dict(SOURCE_MELD))
        monkeypatch.setattr(hb, "_http_post",
                            lambda path, payload, ck, csrf: {"id": 999, "reference_id": "TNEW"})
        calls = {}

        def fake_set_coord(meld_id, user_id):
            calls["meld_id"] = meld_id
            calls["user_id"] = user_id
            return {"ok": True, "meld_id": meld_id, "coordinator_id": user_id}

        monkeypatch.setattr(hb, "set_coordinator", fake_set_coord)

        result = hb.clone_meld(111)

        assert result["ok"] is True
        assert result["new_meld_id"] == 999
        # Source coordinator object {id:57163} -> extracted int 57163, set on new meld.
        assert calls["meld_id"] == 999
        assert calls["user_id"] == 57163
        assert result["coordinator_id"] == 57163

    def test_explicit_override_wins_over_source(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        monkeypatch.setattr(hb, "_http_get", lambda path, ck: dict(SOURCE_MELD))
        monkeypatch.setattr(hb, "_http_post",
                            lambda path, payload, ck, csrf: {"id": 999, "reference_id": "TNEW"})
        calls = {}
        monkeypatch.setattr(hb, "set_coordinator",
                            lambda meld_id, user_id: calls.update(user_id=user_id)
                            or {"ok": True, "coordinator_id": user_id})

        result = hb.clone_meld(111, coordinator_id=88888)
        assert calls["user_id"] == 88888
        assert result["coordinator_id"] == 88888

    def test_no_coordinator_when_source_has_none_and_no_override(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        src = dict(SOURCE_MELD, coordinator=None)
        monkeypatch.setattr(hb, "_http_get", lambda path, ck: src)
        monkeypatch.setattr(hb, "_http_post",
                            lambda path, payload, ck, csrf: {"id": 999, "reference_id": "TNEW"})
        monkeypatch.setattr(hb, "set_coordinator",
                            lambda *a, **k: pytest.fail("set_coordinator called with no coordinator to set"))
        result = hb.clone_meld(111)
        assert result["ok"] is True
        assert "coordinator_id" not in result
        assert "coordinator_warning" not in result

    def test_coordinator_failure_warns_but_clone_succeeds(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        monkeypatch.setattr(hb, "_http_get", lambda path, ck: dict(SOURCE_MELD))
        monkeypatch.setattr(hb, "_http_post",
                            lambda path, payload, ck, csrf: {"id": 999, "reference_id": "TNEW"})
        monkeypatch.setattr(hb, "set_coordinator",
                            lambda meld_id, user_id: {"ok": False, "error": "coordinator PATCH failed"})
        result = hb.clone_meld(111)
        # Clone still succeeds; the coordinator failure is LOUD, not silent.
        assert result["ok"] is True
        assert result["new_meld_id"] == 999
        assert "coordinator_warning" in result
        assert "57163" in result["coordinator_warning"]
