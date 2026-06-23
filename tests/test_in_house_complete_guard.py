"""Guard tests for the in-house manager-complete strand fix (2026-06-23).

Root cause: manager complete/ on an IN-HOUSE meld routes it to
MAINTENANCE_COULD_NOT_COMPLETE (the completion ACTION, not meld state, is the
differentiator — tech-app checkout works, manager complete/ strands). Confirmed
live: TQY8B7DB stranded WITH completed work-entries, same state as a
tech-app-completed meld.

Fix: complete_meld (manager side) fails loud BEFORE the PATCH when the meld is
in-house; the vendor path and non-in-house manager-complete are untouched.
force-pending-completion is hard-guarded (it only ever yields in-house melds).
"""
import pytest

from cli_anything.propertymeld import http_backend as hb


def _patch_base(monkeypatch):
    monkeypatch.setattr(hb, "_load_creds", lambda: {"cookies": []})
    monkeypatch.setattr(hb, "_cookie_header", lambda creds: "sessionid=fake")
    monkeypatch.setattr(hb, "_get_csrf_token", lambda cookie_hdr: "csrf-fake")


def _meld(status="PENDING_COMPLETION", in_house=True):
    return {
        "id": 12793634,
        "status": status,
        "in_house_servicers": [{"id": 1, "agent": {"id": 2}}] if in_house else [],
        "vendorassignment": [] if in_house else [{"id": 9}],
    }


# ── the fix: in-house manager-complete fails loud, never PATCHes ────────────────
def test_in_house_manager_complete_fails_loud_without_patch(monkeypatch):
    _patch_base(monkeypatch)
    monkeypatch.setattr(hb, "_http_get", lambda path, cookie: _meld(in_house=True))
    patched = {"called": False}

    def _no_patch(*a, **k):
        patched["called"] = True
        return {}
    monkeypatch.setattr(hb, "_http_patch", _no_patch)

    with pytest.raises(SystemExit) as exc:
        hb.complete_meld("12793634")
    assert exc.value.code == 1
    assert patched["called"] is False, "complete/ PATCH must NOT be sent for in-house melds"


def test_in_house_fail_message_names_the_real_fix(monkeypatch, capsys):
    _patch_base(monkeypatch)
    monkeypatch.setattr(hb, "_http_get", lambda path, cookie: _meld(in_house=True))
    monkeypatch.setattr(hb, "_http_patch", lambda *a, **k: {})
    with pytest.raises(SystemExit):
        hb.complete_meld("12793634")
    err = capsys.readouterr().err.lower()
    assert "in-house" in err
    assert "tech-app" in err or "tech app" in err
    assert "web ui" in err or "web-ui" in err or "relabel" in err


# ── guard is in-house-SPECIFIC: non-in-house manager-complete still works ───────
def test_non_in_house_manager_complete_proceeds(monkeypatch):
    _patch_base(monkeypatch)
    # first _http_get returns the pre-complete meld (not in-house), verify returns COMPLETED
    gets = iter([_meld(in_house=False), _meld(status="COMPLETED", in_house=False)])
    monkeypatch.setattr(hb, "_http_get", lambda path, cookie: next(gets))
    sent = {"called": False, "path": None}

    def _patch(path, payload, cookie, csrf, **k):
        sent["called"] = True
        sent["path"] = path
        return {"ok": True}
    monkeypatch.setattr(hb, "_http_patch", _patch)

    out = hb.complete_meld("12793634")
    assert sent["called"] is True
    assert out["ok"] is True and out.get("status") == "COMPLETED"


# ── vendor path untouched (Dane's explicit regression) ─────────────────────────
def test_vendor_complete_still_succeeds(monkeypatch):
    _patch_base(monkeypatch)
    # vendor side does no pre-fetch and no in-house check; it must still PATCH
    monkeypatch.setattr(hb, "_http_get", lambda *a, **k: pytest.fail("vendor path must not pre-fetch the meld"))
    captured = {}

    def _patch(path, payload, cookie, csrf, *, side=None, vendor_id=None):
        captured["payload"] = payload
        captured["side"] = side
        return {"ok": True}
    monkeypatch.setattr(hb, "_http_patch", _patch)

    out = hb.complete_meld(
        "12793634", completion_notes="done",
        side="vendor", vendor_id="123", completion_date="2026-06-23T14:00:00.000Z",
    )
    assert out["ok"] is True and out["side"] == "vendor"
    assert captured["payload"]["is_complete"] is True
    assert captured["payload"]["date"] == "2026-06-23T14:00:00.000Z"


# ── force-pending-completion hard-guarded (no mutation) ─────────────────────────
def test_force_pending_completion_is_disabled(monkeypatch):
    _patch_base(monkeypatch)
    monkeypatch.setattr(hb, "_http_get", lambda *a, **k: pytest.fail("disabled command must not hit the network"))
    monkeypatch.setattr(hb, "_http_patch", lambda *a, **k: pytest.fail("disabled command must not mutate"))
    out = hb.force_pending_completion("12793634")
    assert out["ok"] is False
    assert out.get("deprecated") is True
    assert "tech-app" in out["error"] or "tech app" in out["error"]
