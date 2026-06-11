"""Offline mock tests for the pm-recapture WRITE-path gate + creds-preserve fix.

Root cause (task_1781181614288): the recapture script's validity gate shelled
``pm probe``, which validates the Nexus API TOKEN (READ path). Writes go through
the cookie session on the ``/m/`` manager surface, so a write-only outage (read
up, UI cookie stale) was invisible to the gate and the script no-op'd while every
write kept 401-ing. The fix routes the gate through
``http_backend.session_cookie_valid()`` (a non-mutating manager GET — the WRITE
path), adds ``--force``, preserves top-level ``username``/``password`` on write,
and fast-fails on MFA.

Everything here is OFFLINE: playwright, the write-path probe, subprocess, and the
network are all mocked. No live PropertyMeld calls.
"""
import importlib.util
import json
import os
import urllib.error
from unittest import mock

import pytest

import cli_anything.propertymeld.http_backend as hb


SCRIPT_PATH = hb._RECAPTURE_SCRIPT


def _load_script():
    spec = importlib.util.spec_from_file_location("pm_recapture_under_test", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_script()


@pytest.fixture
def creds(tmp_path, mod, monkeypatch):
    """Point the script's CREDS_PATH at a throwaway file."""
    path = str(tmp_path / "property-meld.json")
    monkeypatch.setattr(mod, "CREDS_PATH", path)
    return path


@pytest.fixture(autouse=True)
def _login_env(monkeypatch):
    monkeypatch.setenv("PM_WEB_EMAIL", "ops@example.com")
    monkeypatch.setenv("PM_WEB_PASSWORD", "secret")


# ── Keystone: the write-only-outage gate ─────────────────────────────────────

def test_keystone_read_gate_removed(mod):
    """The read-path gate must be gone — the bug was shelling `pm probe`.

    Proof is code-level, not prose: the old ``probe_session`` helper no longer
    exists, the script no longer imports ``subprocess`` (nothing shells out
    anymore), no ``["pm", "probe"]`` invocation remains, and the write-path
    helper IS referenced.
    """
    src = open(SCRIPT_PATH).read()
    assert not hasattr(mod, "probe_session"), "old read-path probe_session still defined"
    assert "import subprocess" not in src, "script still shells out (subprocess import)"
    assert '"pm", "probe"' not in src, "old [pm, probe] read-path invocation still present"
    assert "session_cookie_valid" in src, "write-path gate not wired in"


def test_write_only_outage_proceeds_to_recapture(mod, creds, monkeypatch):
    """Write stale (session_cookie_valid False) => MUST recapture, not skip.

    This is the bug. Against the OLD read-path gate (`pm probe` returns ok on a
    write-only outage) the script would print "still valid" and skip. With the
    write-path gate it proceeds.
    """
    # gate -> False (write stale); post-write verify -> True (success)
    monkeypatch.setattr(
        hb, "session_cookie_valid", mock.Mock(side_effect=[False, True])
    )
    recapture = mock.Mock(return_value=[{"name": "sessionid", "value": "new", "domain": "app.propertymeld.com"}])
    monkeypatch.setattr(mod, "recapture", recapture)
    monkeypatch.setattr(mod, "write_creds", mock.Mock())

    with pytest.raises(SystemExit) as exc:
        mod.main([])
    assert exc.value.code == 0
    recapture.assert_called_once()


# ── --force bypass ───────────────────────────────────────────────────────────

def test_force_bypasses_valid_gate(mod, creds, monkeypatch):
    """--force recaptures even when the write path reports valid."""
    monkeypatch.setattr(hb, "session_cookie_valid", mock.Mock(return_value=True))
    recapture = mock.Mock(return_value=[{"name": "s", "value": "v", "domain": "app.propertymeld.com"}])
    monkeypatch.setattr(mod, "recapture", recapture)
    monkeypatch.setattr(mod, "write_creds", mock.Mock())

    with pytest.raises(SystemExit) as exc:
        mod.main(["--force"])
    assert exc.value.code == 0
    recapture.assert_called_once()


def test_valid_session_skips_recapture(mod, creds, monkeypatch):
    """No --force + write path valid => exit 0, never recapture."""
    monkeypatch.setattr(hb, "session_cookie_valid", mock.Mock(return_value=True))
    recapture = mock.Mock()
    monkeypatch.setattr(mod, "recapture", recapture)

    with pytest.raises(SystemExit) as exc:
        mod.main([])
    assert exc.value.code == 0
    recapture.assert_not_called()


# ── Creds preservation ───────────────────────────────────────────────────────

def test_write_creds_preserves_username_password(mod, creds):
    """A successful recapture must NOT destroy the stored login."""
    with open(creds, "w") as f:
        json.dump(
            {
                "cookies": [{"name": "old", "value": "1", "domain": "app.propertymeld.com"}],
                "username": "ops@example.com",
                "password": "hunter2",
                "extra_key": "keep-me",
            },
            f,
        )
    new_cookies = [{"name": "sessionid", "value": "fresh", "domain": "app.propertymeld.com"}]
    mod.write_creds(new_cookies)

    written = json.load(open(creds))
    assert written["username"] == "ops@example.com"
    assert written["password"] == "hunter2"
    assert written["extra_key"] == "keep-me"
    assert written["cookies"] == new_cookies


def test_write_creds_no_old_file(mod, creds):
    """No prior file => write cookies-only without crashing."""
    new_cookies = [{"name": "s", "value": "v", "domain": "app.propertymeld.com"}]
    mod.write_creds(new_cookies)
    assert json.load(open(creds))["cookies"] == new_cookies


def test_write_creds_is_atomic_no_tmp_left(mod, creds):
    """Atomic write: final file present, no leftover .tmp sibling."""
    with open(creds, "w") as f:
        json.dump({"cookies": [], "username": "u", "password": "p"}, f)
    mod.write_creds([{"name": "s", "value": "v", "domain": "app.propertymeld.com"}])
    assert os.path.exists(creds)
    assert not os.path.exists(creds + ".tmp"), "atomic write left a .tmp behind"
    written = json.load(open(creds))
    assert written["username"] == "u" and written["password"] == "p"


# ── Retry loop / classification (bot-lockout-prevention invariant) ────────────

class _FakeCtx:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def _attempt_factory(calls):
    """Return a new_attempt() that records each fresh context it hands out."""
    def _new_attempt():
        ctx = _FakeCtx()
        calls.append(ctx)
        return ctx, object()  # (context, page) — page unused (we mock _do_login)
    return _new_attempt


def test_auth_failure_is_not_retried(mod, monkeypatch):
    """A genuine _AuthFailed must fire login exactly ONCE — never rapid re-login.

    Rapid re-login on wrong creds is exactly what trips PM bot-detection, so this
    invariant is load-bearing. A regression that mis-classified auth-fail as
    transient would slip past every gate/creds test — this is the guard.
    """
    contexts = []
    login = mock.Mock(side_effect=mod._AuthFailed("bad creds"))
    monkeypatch.setattr(mod, "_do_login", login)
    sleep = mock.Mock()
    monkeypatch.setattr(mod.time, "sleep", sleep)

    with pytest.raises(mod._AuthFailed):
        mod._login_with_retries(_attempt_factory(contexts), "e", "p", False)

    assert login.call_count == 1, "auth failure was retried"
    assert len(contexts) == 1
    assert contexts[0].closed == 1, "context not closed on auth failure"
    sleep.assert_not_called()


def test_mfa_required_is_not_retried(mod, monkeypatch):
    """MfaRequired propagates immediately (single attempt), no backoff."""
    contexts = []
    monkeypatch.setattr(mod, "_do_login", mock.Mock(side_effect=mod.MfaRequired("x")))
    sleep = mock.Mock()
    monkeypatch.setattr(mod.time, "sleep", sleep)

    with pytest.raises(mod.MfaRequired):
        mod._login_with_retries(_attempt_factory(contexts), "e", "p", False)
    assert len(contexts) == 1
    sleep.assert_not_called()


def test_transient_render_retries_to_cap_then_raises(mod, monkeypatch):
    """Transient flake retries up to MAX_ATTEMPTS, then raises RuntimeError."""
    contexts = []
    monkeypatch.setattr(mod, "_do_login", mock.Mock(side_effect=mod._TransientRenderError("flake")))
    sleep = mock.Mock()
    monkeypatch.setattr(mod.time, "sleep", sleep)

    with pytest.raises(RuntimeError):
        mod._login_with_retries(_attempt_factory(contexts), "e", "p", False)

    assert len(contexts) == mod.MAX_ATTEMPTS, "did not use full retry budget"
    assert sleep.call_count == mod.MAX_ATTEMPTS - 1, "wrong number of backoffs"
    for c in contexts:
        assert c.closed == 1, "a retry context was not closed"


def test_transient_then_success(mod, monkeypatch):
    """A transient flake followed by success returns the cookies (2 attempts)."""
    contexts = []
    good = [{"name": "s", "value": "v", "domain": "app.propertymeld.com"}]
    monkeypatch.setattr(
        mod, "_do_login", mock.Mock(side_effect=[mod._TransientRenderError("flake"), good])
    )
    monkeypatch.setattr(mod.time, "sleep", mock.Mock())

    result = mod._login_with_retries(_attempt_factory(contexts), "e", "p", False)
    assert result == good
    assert len(contexts) == 2


# ── Correct login URL ────────────────────────────────────────────────────────

def test_login_url_is_corrected(mod):
    assert mod.LOGIN_URL.endswith("/login/?next=/"), mod.LOGIN_URL


# ── MFA fast-fail in auto mode ───────────────────────────────────────────────

def test_mfa_required_fast_fails_exit_2(mod, creds, monkeypatch, capsys):
    """An MFA challenge in default mode => exit 2 + mfa_required, never hang."""
    monkeypatch.setattr(hb, "session_cookie_valid", mock.Mock(return_value=False))
    monkeypatch.setattr(mod, "recapture", mock.Mock(side_effect=mod.MfaRequired("challenge")))

    with pytest.raises(SystemExit) as exc:
        mod.main([])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "mfa_required" in err


# ── Post-write verification uses the WRITE path ──────────────────────────────

def test_post_write_verify_uses_write_path(mod, creds, monkeypatch):
    """If the write path is still invalid AFTER writing, honestly fail (exit 1)."""
    probe = mock.Mock(side_effect=[False, False])  # gate False -> proceed; post False -> fail
    monkeypatch.setattr(hb, "session_cookie_valid", probe)
    monkeypatch.setattr(
        mod, "recapture", mock.Mock(return_value=[{"name": "s", "value": "v", "domain": "app.propertymeld.com"}])
    )
    monkeypatch.setattr(mod, "write_creds", mock.Mock())

    with pytest.raises(SystemExit) as exc:
        mod.main([])
    assert exc.value.code == 1
    assert probe.call_count == 2  # gate + post-write, both via the write path


# ── http_backend passes --force from the auto path ───────────────────────────

def test_attempt_recapture_passes_force(monkeypatch):
    """_attempt_recapture (runs only after a real 401) must force the script."""
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    fake = mock.Mock(returncode=0, stderr="", stdout="")
    run = mock.Mock(return_value=fake)
    monkeypatch.setattr(hb.subprocess, "run", run)

    assert hb._attempt_recapture() is True
    argv = run.call_args[0][0]
    assert "--force" in argv, argv
    assert argv[1] == hb._RECAPTURE_SCRIPT


# ── session_cookie_valid is fail-closed (incl. the 200-body false-valid hole) ──

# A real authenticated manager page carries the window.PM.csrf_token marker.
_MANAGER_BODY = '<html><head><script>window.PM.csrf_token = "abc123DEF-456";</script></head><body>melds</body></html>'
# A login page served as a 200 with NO redirect — the false-valid trap.
_LOGIN_200_BODY = '<html><body><form><input type="email" name="email"><input type="password" name="password"><button type="submit">Log in</button></form></body></html>'
# An MFA challenge served as a 200.
_MFA_200_BODY = '<html><body><input autocomplete="one-time-code" name="otp"></body></html>'
MANAGER_URL = "https://app.propertymeld.com/3287/m/3287/melds/"


def _seed_creds(path):
    with open(path, "w") as f:
        json.dump(
            {"cookies": [{"name": "sessionid", "value": "x", "domain": "app.propertymeld.com"}]},
            f,
        )


def _fake_resp(status=200, url=MANAGER_URL, body=""):
    class _Resp:
        def __init__(self):
            self.status = status
        def geturl(self):
            return url
        def read(self):
            return body.encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return _Resp()


def _probe(monkeypatch, tmp_path, resp_or_exc):
    path = str(tmp_path / "c.json")
    _seed_creds(path)
    monkeypatch.setattr(hb, "CREDS_PATH", path)
    if isinstance(resp_or_exc, Exception):
        monkeypatch.setattr(hb.urllib.request, "urlopen", mock.Mock(side_effect=resp_or_exc))
    else:
        monkeypatch.setattr(hb.urllib.request, "urlopen", mock.Mock(return_value=resp_or_exc))


def test_session_cookie_valid_false_on_401(tmp_path, monkeypatch):
    _probe(monkeypatch, tmp_path, urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
    assert hb.session_cookie_valid() is False


def test_session_cookie_valid_false_on_network_error(tmp_path, monkeypatch):
    _probe(monkeypatch, tmp_path, OSError("boom"))
    assert hb.session_cookie_valid() is False


def test_session_cookie_valid_false_on_login_redirect(tmp_path, monkeypatch):
    _probe(monkeypatch, tmp_path, _fake_resp(url="https://app.propertymeld.com/login/?next=/"))
    assert hb.session_cookie_valid() is False


def test_session_cookie_valid_false_on_200_login_html(tmp_path, monkeypatch):
    """THE false-valid hole (Codie's P1): a login page served as 200-no-redirect.

    Status 200, manager URL (no redirect), but the BODY is the login form. Must be
    False — a status-only check would FALSE-VALID here and skip recapture.
    """
    _probe(monkeypatch, tmp_path, _fake_resp(status=200, url=MANAGER_URL, body=_LOGIN_200_BODY))
    assert hb.session_cookie_valid() is False


def test_session_cookie_valid_false_on_200_mfa_html(tmp_path, monkeypatch):
    """An MFA challenge served as a 200 at the manager URL must also be False."""
    _probe(monkeypatch, tmp_path, _fake_resp(status=200, url=MANAGER_URL, body=_MFA_200_BODY))
    assert hb.session_cookie_valid() is False


def test_session_cookie_valid_false_on_200_without_write_marker(tmp_path, monkeypatch):
    """200 + no login form but ALSO no csrf write-marker => still False (require positive proof)."""
    _probe(monkeypatch, tmp_path, _fake_resp(status=200, url=MANAGER_URL, body="<html><body>nothing useful</body></html>"))
    assert hb.session_cookie_valid() is False


def test_session_cookie_valid_true_on_authed_200_with_csrf(tmp_path, monkeypatch):
    """200 at the manager URL WITH the window.PM.csrf_token write marker => True."""
    _probe(monkeypatch, tmp_path, _fake_resp(status=200, url=MANAGER_URL, body=_MANAGER_BODY))
    assert hb.session_cookie_valid() is True


def test_session_cookie_valid_false_when_no_creds_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "CREDS_PATH", str(tmp_path / "missing.json"))
    assert hb.session_cookie_valid() is False


# ── _do_login classification: MFA URL-shape hedge + auth-fail no-retry (P2/P3) ─

class _LoginFakePage:
    """Minimal page double for _do_login. `transition` controls whether the
    post-submit wait_for_url succeeds (lands off /login) or times out."""

    def __init__(self, transition):
        self._transition = transition
        self.url = "https://app.propertymeld.com/login/?next=/"

    def goto(self, url, **kw):
        self.url = url

    def wait_for_selector(self, sel, timeout=None):
        return True

    def fill(self, sel, val):
        pass

    def click(self, sel):
        pass

    def wait_for_url(self, predicate, timeout=None):
        from playwright.sync_api import TimeoutError as PWTimeout
        if self._transition:
            # Transitions OFF /login but to a verify page (the URL-shape hedge case).
            self.url = "https://app.propertymeld.com/verify/2fa"
            return
        raise PWTimeout("still on /login")


def test_mfa_url_shape_hedge_routes_to_mfa(mod, monkeypatch):
    """Challenge on a NON-/login URL (wait_for_url 'succeeded') is still caught
    by the post-transition OTP re-check, before cookie extraction."""
    page = _LoginFakePage(transition=True)
    ctx = mock.Mock()
    monkeypatch.setattr(mod, "_page_has_otp", mock.Mock(return_value=True))

    with pytest.raises(mod.MfaRequired):
        mod._do_login(page, ctx, "e", "p", mfa_relay=False)
    ctx.cookies.assert_not_called()  # routed to MFA before extracting cookies


def test_login_form_present_is_auth_fail_not_transient(mod, monkeypatch):
    """Wrong creds left on /login WITHOUT a banner => _AuthFailed (no retry),
    NOT _TransientRenderError. This is the P2 bot-guard broadening."""
    page = _LoginFakePage(transition=False)
    monkeypatch.setattr(mod, "_page_has_otp", mock.Mock(return_value=False))
    monkeypatch.setattr(mod, "_has_login_error", mock.Mock(return_value=False))
    monkeypatch.setattr(mod, "_login_form_present", mock.Mock(return_value=True))

    with pytest.raises(mod._AuthFailed):
        mod._do_login(page, mock.Mock(), "e", "p", mfa_relay=False)


def test_no_signals_after_submit_is_transient(mod, monkeypatch):
    """Still on /login with NO otp / no banner / no login form => transient (retry)."""
    page = _LoginFakePage(transition=False)
    monkeypatch.setattr(mod, "_page_has_otp", mock.Mock(return_value=False))
    monkeypatch.setattr(mod, "_has_login_error", mock.Mock(return_value=False))
    monkeypatch.setattr(mod, "_login_form_present", mock.Mock(return_value=False))

    with pytest.raises(mod._TransientRenderError):
        mod._do_login(page, mock.Mock(), "e", "p", mfa_relay=False)
