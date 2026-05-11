#!/usr/bin/env python3
"""
Cross-platform PropertyMeld session recapture using Playwright.

Works on Linux (headless), Mac, and Windows. Replaces the macOS-only
AXUIElement/osascript version for non-Mac deployments.

Usage:
    pip install playwright
    playwright install chromium
    PM_WEB_EMAIL=you@example.com PM_WEB_PASSWORD=secret python3 pm-recapture-session-playwright.py

Env vars:
    PM_WEB_EMAIL         PropertyMeld login email (required)
    PM_WEB_PASSWORD      PropertyMeld login password (required)
    PM_CREDS_PATH        Where to write cookies
    PM_RECAPTURE_HEADED  Set to 1 to show the browser window (Mac/desktop only)
"""

import json
import os
import shutil
import subprocess
import sys


CREDS_PATH = os.environ.get(
    "PM_CREDS_PATH",
    os.path.expanduser("~/.claude/credentials/property-meld.json"),
)
LOGIN_URL = "https://app.propertymeld.com/accounts/login/"
PM_DOMAIN = "propertymeld.com"


def probe_session() -> bool:
    try:
        result = subprocess.run(
            ["pm", "probe"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    output = (result.stdout or "").strip()
    if not output:
        return False
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return False
    return data.get("ok") is True


def recapture(email: str, password: str) -> list:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print(
            "playwright is not installed. Run:\n"
            "  pip install playwright\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    headed = os.environ.get("PM_RECAPTURE_HEADED", "0") == "1"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)
            page.fill("input[name='email'], input[type='email'], #id_email", email)
            page.fill("input[type='password'], #id_password", password)
            page.click("[type='submit']")
            page.wait_for_url(lambda url: "/accounts/login" not in url, timeout=25_000)
        except PWTimeout:
            browser.close()
            raise RuntimeError(
                "Timed out waiting for PropertyMeld login redirect. "
                "Check PM_WEB_EMAIL and PM_WEB_PASSWORD."
            )

        raw_cookies = context.cookies()
        browser.close()

    return _normalize(raw_cookies)


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


def write_creds(cookies: list) -> None:
    os.makedirs(os.path.dirname(CREDS_PATH) or ".", exist_ok=True)
    with open(CREDS_PATH, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies}, f)
    os.chmod(CREDS_PATH, 0o600)


def main() -> None:
    email = os.environ.get("PM_WEB_EMAIL")
    password = os.environ.get("PM_WEB_PASSWORD")
    if not email or not password:
        print("PM_WEB_EMAIL and PM_WEB_PASSWORD must be set", file=sys.stderr)
        sys.exit(1)

    if probe_session():
        print("Session still valid — no recapture needed.")
        sys.exit(0)

    backup_path = CREDS_PATH + ".bak"
    backup_created = False

    try:
        cookies = recapture(email, password)
        if not cookies:
            raise RuntimeError("No propertymeld.com cookies were extracted.")

        if os.path.exists(CREDS_PATH):
            shutil.copy2(CREDS_PATH, backup_path)
            backup_created = True

        write_creds(cookies)

        if probe_session():
            print(f"Session recaptured successfully. Cookies written to {CREDS_PATH}")
            sys.exit(0)

        raise RuntimeError("pm probe failed after writing refreshed cookies.")
    except Exception as exc:
        if backup_created and os.path.exists(backup_path):
            shutil.move(backup_path, CREDS_PATH)
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    finally:
        if backup_created and os.path.exists(backup_path) and os.path.exists(CREDS_PATH):
            try:
                os.remove(backup_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
