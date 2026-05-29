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
    "coordinator": {"id": 90025, "user": {"id": 1, "first_name": "Person031"}},
    "unit": {"id": 222},
}


class TestSetCoordinator:
    def test_full_echo_payload_with_int_coordinator(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        # set_coordinator fetch-first uses the NON-EXITING get variant.
        monkeypatch.setattr(hb, "_http_get_no_exit", lambda path, ck: dict(SOURCE_MELD))
        captured = {}

        def fake_patch(path, payload, ck, csrf):
            captured["path"] = path
            captured["payload"] = payload
            return {"id": 111, "coordinator": {"id": 90025}}

        monkeypatch.setattr(hb, "_http_patch_no_exit", fake_patch)

        result = hb.set_coordinator(111, 90025)

        assert result["ok"] is True
        assert result["coordinator_id"] == 90025
        assert captured["path"] == "melds/111/"
        # FULL ECHO: all PM-required fields present + coordinator as a bare int.
        p = captured["payload"]
        assert p["coordinator"] == 90025
        assert isinstance(p["coordinator"], int)
        for required in ("brief_description", "work_location", "work_category",
                         "work_type", "priority"):
            assert required in p, f"full-echo payload missing {required}"
        assert p["priority"] == "MEDIUM"

    def test_fetch_failure_surfaces_error_not_silent(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        # Fetch returns an error dict (no exit) — the NO-EXIT get variant.
        monkeypatch.setattr(hb, "_http_get_no_exit",
                            lambda path, ck: {"error": "HTTP 404", "status_code": 404})
        # PATCH must NOT be attempted if the fetch failed.
        monkeypatch.setattr(hb, "_http_patch_no_exit",
                            lambda *a, **k: pytest.fail("PATCH attempted despite fetch failure"))
        result = hb.set_coordinator(111, 90025)
        assert result["ok"] is False
        assert "could not fetch" in result["error"]

    def test_patch_400_surfaces_error(self, monkeypatch):
        _patch_creds_csrf(monkeypatch)
        monkeypatch.setattr(hb, "_http_get_no_exit", lambda path, ck: dict(SOURCE_MELD))
        # set_coordinator must use the NON-EXITING patch variant.
        monkeypatch.setattr(hb, "_http_patch_no_exit",
                            lambda *a, **k: {"error": "HTTP 400", "status_code": 400})
        result = hb.set_coordinator(111, 90025)
        assert result["ok"] is False
        assert "PATCH failed" in result["error"]


class TestHttpPatchNoExit:
    """The helper that makes the loud-warning path reachable: it must NOT
    sys.exit on a non-401 HTTPError, but MUST still raise SessionExpired on 401
    so @with_recapture_retry re-auths unchanged."""

    def _raise_httperror(self, code, body=b'{"detail":"bad"}'):
        import urllib.error
        import io
        return urllib.error.HTTPError(
            url="https://x/api/melds/1/", code=code, msg="err",
            hdrs={}, fp=io.BytesIO(body),
        )

    def test_returns_error_dict_on_4xx_not_systemexit(self, monkeypatch):
        err = self._raise_httperror(400)

        def boom(req, **kw):
            raise err

        monkeypatch.setattr(hb.urllib.request, "urlopen", boom)
        # Must return a dict carrying status_code, NOT raise SystemExit.
        result = hb._http_patch_no_exit("melds/1/", {"x": 1}, "ck", "csrf")
        assert isinstance(result, dict)
        assert result["status_code"] == 400

    def test_http_patch_no_exit_preserves_session_expired_on_401(self, monkeypatch):
        err = self._raise_httperror(401)

        def boom(req, **kw):
            raise err

        monkeypatch.setattr(hb.urllib.request, "urlopen", boom)
        # 401 must still raise SessionExpired (recapture-retry path unchanged).
        with pytest.raises(hb.SessionExpired):
            hb._http_patch_no_exit("melds/1/", {"x": 1}, "ck", "csrf")

    def test_http_get_no_exit_returns_error_dict_on_4xx_not_systemexit(self, monkeypatch):
        err = self._raise_httperror(404)

        def boom(req, **kw):
            raise err

        monkeypatch.setattr(hb.urllib.request, "urlopen", boom)
        result = hb._http_get_no_exit("melds/1/", "ck")
        assert isinstance(result, dict)
        assert result["status_code"] == 404

    def test_http_get_no_exit_preserves_session_expired_on_401(self, monkeypatch):
        err = self._raise_httperror(401)

        def boom(req, **kw):
            raise err

        monkeypatch.setattr(hb.urllib.request, "urlopen", boom)
        with pytest.raises(hb.SessionExpired):
            hb._http_get_no_exit("melds/1/", "ck")


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
        # Source coordinator object {id:90025} -> extracted int 90025, set on new meld.
        assert calls["meld_id"] == 999
        assert calls["user_id"] == 90025
        assert result["coordinator_id"] == 90025

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
        assert "90025" in result["coordinator_warning"]

    def test_clone_coordinator_4xx_returns_id_and_warning_not_exit(self, monkeypatch):
        """The actual P2 (Codex + Aussie): a real coordinator PATCH 4xx AFTER a
        successful clone POST must NOT sys.exit and orphan the clone. Exercises
        the REAL set_coordinator -> _http_patch_no_exit path (set_coordinator is
        NOT mocked here) — the earlier test mocked it too high to catch this."""
        _patch_creds_csrf(monkeypatch)
        monkeypatch.setattr(hb, "_http_get", lambda path, ck: dict(SOURCE_MELD))  # clone source read
        monkeypatch.setattr(hb, "_http_get_no_exit", lambda path, ck: dict(SOURCE_MELD))  # set_coordinator echo read
        monkeypatch.setattr(hb, "_http_post",
                            lambda path, payload, ck, csrf: {"id": 999, "reference_id": "TNEW"})
        # The follow-up coordinator PATCH returns a 4xx error dict (no exit).
        monkeypatch.setattr(hb, "_http_patch_no_exit",
                            lambda *a, **k: {"error": "HTTP 400", "status_code": 400,
                                             "detail": "invalid coordinator"})
        # Guard: the EXITING _http_patch must NOT be used on this path.
        monkeypatch.setattr(hb, "_http_patch",
                            lambda *a, **k: pytest.fail("clone path used the sys.exit _http_patch"))

        result = hb.clone_meld(111)  # must NOT raise SystemExit

        assert result["ok"] is True
        assert result["new_meld_id"] == 999  # clone id preserved
        assert "coordinator_warning" in result
        assert "90025" in result["coordinator_warning"]

    def test_clone_coordinator_FETCH_fails_returns_id_and_warning_not_exit(self, monkeypatch):
        """Second-boundary P2 (Codex re-review of the fix): set_coordinator does
        fetch-FIRST for the full-echo payload, and that GET also used to
        sys.exit on non-401. A post-clone read-after-write 404/500 on the echo
        fetch must NOT exit — clone id is preserved + warning emitted. Exercises
        the REAL set_coordinator path with the FETCH leg failing (not the PATCH)."""
        _patch_creds_csrf(monkeypatch)
        monkeypatch.setattr(hb, "_http_get", lambda path, ck: dict(SOURCE_MELD))  # clone source read
        monkeypatch.setattr(hb, "_http_post",
                            lambda path, payload, ck, csrf: {"id": 999, "reference_id": "TNEW"})
        # set_coordinator's fetch-first GET returns a 404 error dict (no exit).
        monkeypatch.setattr(hb, "_http_get_no_exit",
                            lambda path, ck: {"error": "HTTP 404", "status_code": 404})
        # Guards: neither EXITING helper may run on this recovery path.
        monkeypatch.setattr(hb, "_http_patch",
                            lambda *a, **k: pytest.fail("clone path used the sys.exit _http_patch"))
        # PATCH must never be reached because the fetch failed first.
        monkeypatch.setattr(hb, "_http_patch_no_exit",
                            lambda *a, **k: pytest.fail("PATCH attempted despite failed echo fetch"))

        result = hb.clone_meld(111)  # must NOT raise SystemExit

        assert result["ok"] is True
        assert result["new_meld_id"] == 999  # clone id preserved despite fetch failure
        assert "coordinator_warning" in result
        assert "90025" in result["coordinator_warning"]
