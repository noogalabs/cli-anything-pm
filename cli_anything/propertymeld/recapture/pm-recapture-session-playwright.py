#!/usr/bin/env python3
"""
Cross-platform PropertyMeld session recapture using Playwright.

Works on Linux (headless), Mac, and Windows. Playwright is the locked platform
for cookie-session recapture (the macOS-only SafariDriver path is deprecated):
its auto-wait/actionability handling is the direct fix for the intermittent
SPA-render flake (element-found-then-NoSuchElement) seen on the Selenium path.

WRITE-PATH GATE (the load-bearing fix)
--------------------------------------
The old gate shelled ``pm probe``, which validates the Nexus API *token* — the
READ path. Writes go through the cookie session on the ``/m/`` manager surface,
so a write-only outage (read up, UI cookie stale) was invisible to that probe
and the script would no-op ("still valid") while every write kept 401-ing. The
gate now uses ``http_backend.session_cookie_valid()`` (a non-mutating manager
GET — the WRITE path). ``--force`` bypasses the gate entirely; the auto-recovery
caller (``http_backend._attempt_recapture``) passes it because it only runs
after a confirmed 401.

MFA
---
Default (headless/auto) mode FAILS FAST on an MFA challenge: it exits 2 with
``{"error": "mfa_required"}`` so the agent routes to the manual relay. It does
not block, because the auto caller kills the subprocess at 180s while the relay
waits up to 360s. The opt-in ``--mfa-relay`` mode runs the file-poll relay
(agent drops David's relayed SMS code into PM_MFA_CODE_FILE) for the manual,
human-in-the-loop path.

Usage:
    pip install playwright
    playwright install chromium
    PM_WEB_EMAIL=you@example.com PM_WEB_PASSWORD=secret \
        python3 pm-recapture-session-playwright.py [--force] [--mfa-relay]

Env vars:
    PM_WEB_EMAIL         PropertyMeld login email (required)
    PM_WEB_PASSWORD      PropertyMeld login password (required)
    PM_CREDS_PATH        Where to read/write cookies
    PM_RECAPTURE_HEADED  Set to 1 to show the browser window (recommended for the
                         single careful live validation hit — lower bot profile)
    PM_MFA_CODE_FILE     File the agent drops the relayed MFA code into (--mfa-relay)
    PM_MFA_WAIT_SECONDS  How long --mfa-relay polls for the code (default 360)

Exit codes:
    0  success (cookies written AND write-path session valid afterward)
    1  generic failure (missing creds, login failed, no cookies, post-write invalid)
    2  mfa_required (auto mode — distinct so the agent routes to --mfa-relay)
    3  still-not-authenticated after --mfa-relay
"""

import argparse
import json
import os
import random
import shutil
import sys
import time


CREDS_PATH = os.environ.get(
    "PM_CREDS_PATH",
    os.path.expanduser("~/.claude/credentials/property-meld.json"),
)
# Correct login URL: bare /login/ and /accounts/login/ are dead/misread pages;
# /login/?next=/ is the one that actually serves the Django login form.
LOGIN_URL = "https://app.propertymeld.com/login/?next=/"
APP_HOST = "app.propertymeld.com"
PM_DOMAIN = "propertymeld.com"

# Realistic UA so headless Chromium is not trivially fingerprinted.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Bounded transient-render resilience inside ONE invocation. This is NOT a
# hammer-the-login loop (that is what tripped bot-detection); it only retries
# transient SPA-render flake, capped hard at MAX_ATTEMPTS, and never retries a
# genuine auth failure.
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 2.0
JITTER_SECONDS = 1.0

NAV_TIMEOUT_MS = 30_000
ELEMENT_TIMEOUT_MS = 15_000
POST_SUBMIT_TIMEOUT_MS = 25_000

EMAIL_SEL = "input[name='email'], input[type='email'], #id_email"
PASSWORD_SEL = "input[type='password'], #id_password, input[name='password']"
SUBMIT_SEL = "button[type='submit'], [type='submit']"
OTP_SEL = (
    "input[name='otp'], input[name='code'], input[name='token'], "
    "input[id*='code'], input[id*='otp'], input[id*='token'], "
    "input[autocomplete='one-time-code'], input[type='tel']"
)

MFA_CODE_FILE = os.environ.get("PM_MFA_CODE_FILE", "/tmp/pm_mfa_code.txt")
MFA_WAIT_SECONDS = int(os.environ.get("PM_MFA_WAIT_SECONDS", "360"))


class MfaRequired(Exception):
    """An MFA challenge was hit in default (no-relay) mode — caller must relay."""


class RelayAuthFailed(Exception):
    """--mfa-relay ran but the session never authenticated (bad/late code)."""


class _AuthFailed(Exception):
    """Genuine auth failure (wrong creds / logged-out) — do NOT retry."""


class _TransientRenderError(Exception):
    """Intermittent SPA-render flake — safe to retry within MAX_ATTEMPTS."""


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recapture the PropertyMeld session cookie.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the write-path validity gate and recapture unconditionally.",
    )
    parser.add_argument(
        "--mfa-relay",
        action="store_true",
        help="Manual/human path: on an MFA challenge, poll PM_MFA_CODE_FILE for a relayed code.",
    )
    return parser.parse_args(argv)


def _load_http_backend():
    """Import the cookie/WRITE backend, bootstrapping sys.path if needed.

    The script ships inside the package, so in a normal (editable or pipx)
    install ``cli_anything`` is already importable. As a belt for odd
    invocations, fall back to inserting the package parent computed from
    __file__ (recapture/ -> propertymeld/ -> cli_anything/ -> <parent>). A
    truly-missing package degrades to a clear JSON error + non-zero exit, never
    a bare ImportError traceback.
    """
    try:
        from cli_anything.propertymeld import http_backend
        return http_backend
    except ImportError:
        pkg_parent = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        if pkg_parent not in sys.path:
            sys.path.insert(0, pkg_parent)
        try:
            from cli_anything.propertymeld import http_backend
            return http_backend
        except ImportError as exc:
            print(
                json.dumps(
                    {
                        "error": "cannot import cli_anything.propertymeld.http_backend",
                        "detail": str(exc),
                    }
                ),
                file=sys.stderr,
            )
            sys.exit(1)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff + jitter. Bounded; time.sleep is monkeypatchable in tests."""
    return BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, JITTER_SECONDS)


def _normalize(cookies: list) -> list:
    result = []
    for c in cookies:
        domain = c.get("domain", "") or ""
        if PM_DOMAIN not in domain:
            continue
        result.append(
            {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": domain,
                "path": c.get("path", "/"),
                "secure": bool(c.get("secure", False)),
                "httpOnly": bool(c.get("httpOnly", False)),
                "expires": c.get("expires"),
            }
        )
    return result


def _poll_for_mfa_code() -> str:
    """Wait for the agent to drop the relayed SMS code into MFA_CODE_FILE."""
    print(
        f"Waiting for MFA code at {MFA_CODE_FILE} (up to {MFA_WAIT_SECONDS}s)...",
        flush=True,
    )
    deadline = time.time() + MFA_WAIT_SECONDS
    while time.time() < deadline:
        if os.path.exists(MFA_CODE_FILE):
            try:
                with open(MFA_CODE_FILE) as f:
                    code = f.read().strip()
            except OSError:
                code = ""
            try:
                os.remove(MFA_CODE_FILE)
            except OSError:
                pass
            if code:
                print(f"Got MFA code ({len(code)} digits).", flush=True)
                return code
        time.sleep(2)
    return ""


def _page_has_otp(page) -> bool:
    try:
        return page.query_selector(OTP_SEL) is not None
    except Exception:
        return False


def _has_login_error(page) -> bool:
    """Best-effort: an explicit error banner means wrong creds — fail fast, don't retry."""
    try:
        el = page.query_selector(".errorlist, [role='alert'], .alert-danger, .error")
        return el is not None
    except Exception:
        return False


def _login_form_present(page) -> bool:
    """A password field still present after submit => we never left the login form.

    Broadens auth-fail detection beyond the error-banner selector: wrong creds
    that leave the user on /login WITHOUT a recognized banner would otherwise be
    misclassified as a transient render flake and retried — and rapid re-login is
    exactly what trips PM bot-detection. If the password field is still here after
    a submit + post-submit timeout, treat it as a genuine auth failure (no retry).
    """
    try:
        return page.query_selector(PASSWORD_SEL) is not None
    except Exception:
        return False


def _handle_mfa(page, mfa_relay: bool) -> None:
    """Route a detected MFA challenge: relay-fill it (manual path), or fast-fail.

    Shared by both detection sites — the still-on-/login timeout branch and the
    non-/login URL-shape hedge — so they route identically.
    """
    if mfa_relay:
        _do_mfa_relay(page)  # fills the code, submits; raises on failure
    else:
        raise MfaRequired(f"MFA challenge at {page.url[:80]}")


def _do_login(page, context, email: str, password: str, mfa_relay: bool) -> list:
    """One login attempt. Raises MfaRequired / _AuthFailed / _TransientRenderError."""
    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        # SPA-aware: wait for each element explicitly so an intermittent render
        # surfaces as a clean retry, not a hard crash mid-fill.
        page.wait_for_selector(EMAIL_SEL, timeout=ELEMENT_TIMEOUT_MS)
        page.wait_for_selector(PASSWORD_SEL, timeout=ELEMENT_TIMEOUT_MS)
        page.wait_for_selector(SUBMIT_SEL, timeout=ELEMENT_TIMEOUT_MS)
    except PWTimeout as exc:
        raise _TransientRenderError(f"login form did not render: {exc}")

    page.fill(EMAIL_SEL, email)
    page.fill(PASSWORD_SEL, password)
    page.click(SUBMIT_SEL)

    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=POST_SUBMIT_TIMEOUT_MS)
    except PWTimeout:
        # Still on /login after submit. Possibilities, in priority order:
        if _page_has_otp(page):
            _handle_mfa(page, mfa_relay)
        elif _has_login_error(page) or _login_form_present(page):
            # Explicit error banner, OR the login form is still sitting there
            # (submit did not advance us) => genuine auth failure. NOT retried —
            # rapid re-login is what trips bot-detection.
            raise _AuthFailed(
                "login did not advance past /login (error banner or login form still present "
                "— check credentials)"
            )
        else:
            # No OTP, no error, no login form: a genuine transient render flake (retry).
            raise _TransientRenderError(f"did not advance past /login (url={page.url[:80]})")

    # URL-shape hedge: PM may serve the MFA/verify challenge on a NON-/login URL
    # (so wait_for_url above "succeeded" off /login but we are not actually in).
    # Re-check for an OTP field HERE — after the URL transition but BEFORE the
    # app-host nav / cookie extraction, because navigating away would lose the
    # challenge page. Making MFA detection robust to the challenge's URL shape
    # directly hardens the limited-shot live validation.
    if _page_has_otp(page):
        _handle_mfa(page, mfa_relay)

    # Ensure we land on the app host so all session cookies are set.
    if APP_HOST not in page.url:
        page.goto(f"https://{APP_HOST}/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    cookies = _normalize(context.cookies())
    if not cookies or "/login" in page.url:
        raise _AuthFailed(
            f"not authenticated after login (url={page.url[:80]}, cookies={len(cookies)})"
        )
    return cookies


def _do_mfa_relay(page) -> None:
    """Manual path: poll for the relayed code, fill it, submit. Raises on failure."""
    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        page.wait_for_selector(OTP_SEL, timeout=ELEMENT_TIMEOUT_MS)
    except PWTimeout as exc:
        raise RelayAuthFailed(f"OTP field never appeared: {exc}")

    code = _poll_for_mfa_code()
    if not code:
        raise RelayAuthFailed("no MFA code supplied within the relay window")

    page.fill(OTP_SEL, code)
    try:
        page.click(SUBMIT_SEL)
        page.wait_for_url(lambda url: "/login" not in url, timeout=POST_SUBMIT_TIMEOUT_MS)
    except PWTimeout as exc:
        raise RelayAuthFailed(f"session did not authenticate after MFA code: {exc}")


def _login_with_retries(new_attempt, email: str, password: str, mfa_relay: bool) -> list:
    """Run ``_do_login`` with bounded transient-retry.

    ``new_attempt()`` returns a fresh ``(context, page)`` per try. ONLY
    ``_TransientRenderError`` is retried (SPA-render flake); ``MfaRequired`` and
    ``_AuthFailed`` propagate immediately — a genuine auth failure must NOT
    trigger rapid re-login attempts (that is what trips bot-detection). Split
    out from ``recapture`` so the classification logic is unit-testable without
    a real browser.
    """
    last_err = None
    for attempt in range(MAX_ATTEMPTS):
        context, page = new_attempt()
        try:
            return _do_login(page, context, email, password, mfa_relay)
        except _TransientRenderError as exc:
            last_err = exc
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(_backoff_delay(attempt))
        finally:
            try:
                context.close()
            except Exception:
                pass
    raise RuntimeError(f"login failed after {MAX_ATTEMPTS} attempts: {last_err}")


def recapture(email: str, password: str, mfa_relay: bool = False) -> list:
    """Drive the headless (or headed) Playwright login and return normalized cookies."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            json.dumps(
                {
                    "error": "playwright not installed",
                    "detail": "pip install playwright && playwright install chromium",
                }
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    headed = os.environ.get("PM_RECAPTURE_HEADED", "0") == "1"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        try:
            def _new_attempt():
                # Fresh context per attempt — no carried-over state between tries.
                context = browser.new_context(user_agent=UA)
                return context, context.new_page()

            return _login_with_retries(_new_attempt, email, password, mfa_relay)
        finally:
            browser.close()


def write_creds(cookies: list) -> None:
    """Write refreshed cookies, PRESERVING every existing top-level key.

    The old version wrote ``{"cookies": ...}`` only, DROPPING the top-level
    ``username``/``password`` — so a successful recapture destroyed the stored
    login. We merge: read the old file, overwrite only ``cookies``, keep the rest.
    """
    old: dict = {}
    if os.path.exists(CREDS_PATH):
        try:
            with open(CREDS_PATH) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                old = loaded
        except (OSError, ValueError):
            old = {}

    merged = dict(old)
    merged["cookies"] = cookies

    # Atomic write: a SIGKILL (e.g. the auto caller's subprocess timeout) between
    # truncate and flush would otherwise leave property-meld.json zero-byte/corrupt
    # and lose the login. Write a sibling tmp, fsync, chmod, then os.replace (atomic
    # rename on the same filesystem). Mandated by the cortextos atomic-write rule.
    os.makedirs(os.path.dirname(CREDS_PATH) or ".", exist_ok=True)
    tmp_path = CREDS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, CREDS_PATH)


def main(argv=None) -> None:
    args = _parse_args(argv)

    email = os.environ.get("PM_WEB_EMAIL")
    password = os.environ.get("PM_WEB_PASSWORD")
    if not email or not password:
        print(json.dumps({"error": "PM_WEB_EMAIL and PM_WEB_PASSWORD must be set"}), file=sys.stderr)
        sys.exit(1)

    http_backend = _load_http_backend()

    # Gate on the WRITE path (not pm probe / read). --force bypasses it because
    # the auto caller already proved the session expired.
    if not args.force:
        if http_backend.session_cookie_valid():
            print("Session still valid — no recapture needed.")
            sys.exit(0)

    backup_path = CREDS_PATH + ".bak"
    backup_created = False
    try:
        cookies = recapture(email, password, mfa_relay=args.mfa_relay)
        if not cookies:
            raise RuntimeError("No propertymeld.com cookies were extracted.")

        if os.path.exists(CREDS_PATH):
            shutil.copy2(CREDS_PATH, backup_path)
            backup_created = True

        write_creds(cookies)

        # POST-write verification uses the WRITE path too — the whole point is
        # that the refreshed cookie actually works for writes, not just reads.
        if http_backend.session_cookie_valid():
            print(f"Session recaptured successfully. Cookies written to {CREDS_PATH}")
            sys.exit(0)

        raise RuntimeError("Write-path session still invalid after writing refreshed cookies.")
    except MfaRequired as exc:
        _restore(backup_path, backup_created)
        backup_created = False
        print(json.dumps({"error": "mfa_required", "detail": str(exc)}), file=sys.stderr)
        sys.exit(2)
    except RelayAuthFailed as exc:
        _restore(backup_path, backup_created)
        backup_created = False
        print(json.dumps({"error": "mfa_relay_failed", "detail": str(exc)}), file=sys.stderr)
        sys.exit(3)
    except Exception as exc:
        _restore(backup_path, backup_created)
        backup_created = False
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
    finally:
        if backup_created and os.path.exists(backup_path) and os.path.exists(CREDS_PATH):
            try:
                os.remove(backup_path)
            except OSError:
                pass


def _restore(backup_path: str, backup_created: bool) -> None:
    if backup_created and os.path.exists(backup_path):
        shutil.move(backup_path, CREDS_PATH)


if __name__ == "__main__":
    main()
