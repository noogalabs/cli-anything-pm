"""
Property Meld Playwright browser backend.
Used for write actions the Nexus API does not yet support (tech assignment, etc.).

Requires:
  - PM_CREDS_PATH env var (default: ~/.claude/credentials/property-meld.json)
  - playwright browsers installed: playwright install chromium
"""
import json
import os
import sys
import time
from typing import Optional

CREDS_PATH = os.environ.get("PM_CREDS_PATH",
    os.path.expanduser("~/.claude/credentials/property-meld.json"))
MULTITENANT = os.environ.get("PM_MULTITENANT_ID", "3287")
LOGIN_URL = "https://app.propertymeld.com/login/"


def _load_creds() -> dict:
    """Load credentials from JSON file."""
    if not os.path.exists(CREDS_PATH):
        print(json.dumps({"error": f"Credentials file not found: {CREDS_PATH}"}), file=sys.stderr)
        sys.exit(2)
    with open(CREDS_PATH) as f:
        return json.load(f)


def _save_creds(creds: dict) -> None:
    """Persist updated credentials (fresh cookies)."""
    with open(CREDS_PATH, "w") as f:
        json.dump(creds, f, indent=2)


def _browser_session(creds: dict):
    """Return (playwright, browser, context, page) with session restored."""
    from playwright.sync_api import sync_playwright

    p = sync_playwright().start()
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1280, "height": 900})

    for c in creds.get("cookies", []):
        try:
            cookie = {k: v for k, v in c.items()
                      if v is not None and k in ["name", "value", "domain", "path", "httpOnly", "secure", "sameSite"]}
            if c.get("expires", -1) > 0:
                cookie["expires"] = int(c["expires"])
            context.add_cookies([cookie])
        except Exception:
            pass

    page = context.new_page()
    return p, browser, context, page


def _ensure_logged_in(page, context, creds: dict) -> bool:
    """Navigate to melds list; re-login if session expired. Returns True on success."""
    test_url = f"https://app.propertymeld.com/{MULTITENANT}/m/{MULTITENANT}/melds/"
    page.goto(test_url, timeout=30000)
    page.wait_for_load_state("load", timeout=15000)

    if "login" not in page.url.lower():
        return True

    username = creds.get("username", "")
    password = creds.get("password", "")
    if not username or not password:
        return False

    page.goto(LOGIN_URL, timeout=30000)
    page.wait_for_load_state("load", timeout=15000)
    time.sleep(2)

    page.fill('input[type="email"], input[name="username"], input[name="email"]', username)
    page.fill('input[type="password"]', password)
    page.click('button:has-text("LOGIN")')
    page.wait_for_load_state("load", timeout=20000)
    time.sleep(3)

    if "login" in page.url.lower():
        return False

    creds["cookies"] = context.cookies()
    _save_creds(creds)
    return True


def get_comments(meld_id: str) -> list:
    """Fetch comments for a meld using browser session (cookie-based API call)."""
    creds = _load_creds()
    p, browser, context, page = _browser_session(creds)

    try:
        if not _ensure_logged_in(page, context, creds):
            return [{"error": "Login failed"}]

        api_url = (f"https://app.propertymeld.com/{MULTITENANT}/m/{MULTITENANT}"
                   f"/api/comments/?meld={meld_id}&limit=100")
        response = page.request.get(api_url)

        if response.ok:
            data = response.json()
            return data.get("results", data) if isinstance(data, dict) else data

        melds_url = f"https://app.propertymeld.com/{MULTITENANT}/m/{MULTITENANT}/melds/"
        page.goto(melds_url, timeout=30000)
        page.wait_for_load_state("load", timeout=15000)
        time.sleep(5)

        response2 = page.request.get(api_url)
        if response2.ok:
            data = response2.json()
            return data.get("results", data) if isinstance(data, dict) else data

        return [{"error": f"API returned {response2.status}"}]

    finally:
        browser.close()
        p.stop()


NEXUS_ACCOUNT_ID = os.environ.get("PM_NEXUS_ACCOUNT_ID", "338")
NEXUS_API_KEYS_URL = f"https://app.propertymeld.com/{NEXUS_ACCOUNT_ID}/n/{NEXUS_ACCOUNT_ID}/nexus/api-keys/"


def rotate_api_key(key_name: Optional[str] = None) -> dict:
    """Create a new Nexus partner API key via the PM dashboard.

    Flow:
      1. Switch to Nexus Partner account via /choose-account/
      2. Navigate to Nexus API Keys page
      3. Click "Create API Key"
      4. Capture client_id and client_secret from the success modal
      5. Close the modal

    Returns dict with keys: ok, client_id, client_secret, error (on failure).
    The client_secret is shown ONLY ONCE — capture it immediately.
    """
    creds = _load_creds()
    p, browser, context, page = _browser_session(creds)

    try:
        if not _ensure_logged_in(page, context, creds):
            return {"ok": False, "error": "Login failed"}

        # Step 1: Navigate to account chooser and switch to Nexus Partner
        page.goto("https://app.propertymeld.com/choose-account/", timeout=30000)
        page.wait_for_load_state("load", timeout=15000)
        time.sleep(3)

        # Find and click the Nexus Partner card
        nexus_clicked = False
        for card in page.locator("button, a").all():
            try:
                txt = card.inner_text().strip()
                if "Nexus Partner" in txt and "Ascend" in txt and len(txt) < 60:
                    card.click()
                    time.sleep(5)
                    nexus_clicked = True
                    break
            except Exception:
                pass

        if not nexus_clicked:
            return {"ok": False, "error": "Could not find Nexus Partner account option"}

        if "/n/" not in page.url:
            return {"ok": False, "error": f"Did not land on Nexus page, got: {page.url}"}

        # Step 2: Navigate to API Keys page
        api_keys_link = page.locator("a:has-text('API Keys')").first
        if api_keys_link.count() > 0:
            api_keys_link.click()
            time.sleep(4)
        else:
            page.goto(NEXUS_API_KEYS_URL, timeout=30000)
            page.wait_for_load_state("load", timeout=15000)
            time.sleep(4)

        if "api-keys" not in page.url:
            return {"ok": False, "error": f"Could not reach API Keys page, got: {page.url}"}

        # Step 3: Click "Create API Key"
        create_btn = page.locator("button:has-text('Create API Key')").first
        if create_btn.count() == 0:
            return {"ok": False, "error": "Create API Key button not found"}

        create_btn.click()
        time.sleep(4)

        # Step 4: Extract credentials from the success modal
        # Modal text: "Client ID: <id>" and "Client Secret: <secret>"
        page_text = page.inner_text("body")

        import re
        client_id_match = re.search(r"Client ID:\s*([A-Za-z0-9]{20,60})", page_text)
        client_secret_match = re.search(r"Client Secret:\s*([A-Za-z0-9]{40,200})", page_text)

        if not client_id_match or not client_secret_match:
            return {"ok": False, "error": "Could not extract credentials from modal"}

        client_id = client_id_match.group(1).strip()
        client_secret = client_secret_match.group(1).strip()

        # Step 5: Close the modal
        close_btn = page.locator("button:has-text('Close')").first
        if close_btn.count() > 0:
            close_btn.click()
            time.sleep(2)

        return {
            "ok": True,
            "client_id": client_id,
            "client_secret": client_secret,
            "note": "client_secret shown once — store it immediately",
        }

    finally:
        browser.close()
        p.stop()


def list_api_keys() -> dict:
    """List existing Nexus partner API keys (name, date, client_id only — secrets not shown)."""
    creds = _load_creds()
    p, browser, context, page = _browser_session(creds)

    try:
        if not _ensure_logged_in(page, context, creds):
            return {"ok": False, "error": "Login failed"}

        page.goto("https://app.propertymeld.com/choose-account/", timeout=30000)
        page.wait_for_load_state("load", timeout=15000)
        time.sleep(3)

        for card in page.locator("button, a").all():
            try:
                txt = card.inner_text().strip()
                if "Nexus Partner" in txt and "Ascend" in txt and len(txt) < 60:
                    card.click()
                    time.sleep(5)
                    break
            except Exception:
                pass

        page.goto(NEXUS_API_KEYS_URL, timeout=30000)
        page.wait_for_load_state("load", timeout=15000)
        time.sleep(4)

        # Parse the keys table
        import re
        text = page.inner_text("body")
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        keys = []
        # Table rows follow pattern: Name, Date, Client ID, Actions
        i = 0
        in_table = False
        while i < len(lines):
            if "Showing" in lines[i] and "API Keys" in lines[i]:
                in_table = True
                i += 1
                continue
            if in_table and i + 2 < len(lines):
                name = lines[i]
                date = lines[i + 1] if re.match(r"\d+/\d+/\d+", lines[i + 1]) else None
                if date:
                    client_id = lines[i + 2] if re.match(r"[A-Za-z0-9]{20,}", lines[i + 2]) else None
                    if client_id:
                        keys.append({"name": name, "created": date, "client_id": client_id})
                        i += 4  # skip Revoke button line too
                        continue
            i += 1

        return {"ok": True, "count": len(keys), "keys": keys}

    finally:
        browser.close()
        p.stop()


def assign_tech(meld_id: str, tech_name: str) -> dict:
    """Assign an in-house tech to a meld via browser automation."""
    creds = _load_creds()
    p, browser, context, page = _browser_session(creds)

    try:
        if not _ensure_logged_in(page, context, creds):
            return {"ok": False, "error": "Login failed"}

        url = (f"https://app.propertymeld.com/{MULTITENANT}/m/{MULTITENANT}"
               f"/meld/{meld_id}/summary/")
        page.goto(url, timeout=30000)
        page.wait_for_load_state("load", timeout=30000)
        time.sleep(5)

        if "Meld Summary" not in page.title():
            return {"ok": False, "error": f"Could not load meld page: {page.title()}"}

        page.click("button:has-text('Assign')")
        time.sleep(3)

        panel_buttons = page.locator("button:has-text('Assign')").all()
        if len(panel_buttons) < 2:
            return {"ok": False, "error": "Assign panel did not open"}

        panel_buttons[1].click()
        time.sleep(3)

        page.reload()
        time.sleep(4)

        if tech_name.lower() in page.content().lower():
            return {"ok": True, "meld_id": meld_id, "assigned_to": tech_name}

        return {"ok": False, "error": f"{tech_name} not found in page after assignment"}

    finally:
        browser.close()
        p.stop()
