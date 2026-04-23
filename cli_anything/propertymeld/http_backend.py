"""
Property Meld plain-HTTP backend.

Uses cookie-based session auth (no Playwright at runtime) for browser-session
API endpoints that the Nexus API does not expose.

Auth flow:
  1. Load sessionid cookie from PM_CREDS_PATH JSON file.
  2. Fetch CSRF token from page HTML (window.PM.csrf_token) — cached per process.
  3. GET requests need only the sessionid cookie.
  4. POST/PUT/PATCH also need X-CSRFToken header.

Endpoint base: https://app.propertymeld.com/{MULTITENANT}/m/{MULTITENANT}/api/
"""
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

CREDS_PATH = os.environ.get(
    "PM_CREDS_PATH", os.path.expanduser("~/.claude/credentials/property-meld.json")
)
MULTITENANT = os.environ.get("PM_MULTITENANT_ID", "3287")
BASE = f"https://app.propertymeld.com/{MULTITENANT}/m/{MULTITENANT}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

_csrf_cache: dict = {}
_ssl_ctx = ssl.create_default_context()


def _load_creds() -> dict:
    if not os.path.exists(CREDS_PATH):
        print(json.dumps({"error": f"Credentials file not found: {CREDS_PATH}"}), file=sys.stderr)
        sys.exit(2)
    with open(CREDS_PATH) as f:
        return json.load(f)


def _cookie_header(creds: dict) -> str:
    """Build Cookie header string from stored credentials."""
    parts = [
        f"{c['name']}={c['value']}"
        for c in creds.get("cookies", [])
        if "propertymeld.com" in c.get("domain", "")
    ]
    return "; ".join(parts)


def _get_csrf_token(cookie_hdr: str) -> str:
    """Fetch and cache the CSRF token from the PM page HTML."""
    if _csrf_cache.get("token"):
        return _csrf_cache["token"]

    req = urllib.request.Request(
        f"{BASE}/melds/",
        headers={"Cookie": cookie_hdr, "User-Agent": UA, "Accept": "text/html"},
    )
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    m = re.search(r"window\.PM\.csrf_token\s*=\s*[\"']([\w-]+)[\"']", html)
    if not m:
        print(json.dumps({"error": "Could not extract CSRF token from PM page"}), file=sys.stderr)
        sys.exit(2)

    _csrf_cache["token"] = m.group(1)
    return _csrf_cache["token"]


def _http_get(path: str, cookie_hdr: str) -> Any:
    """GET a browser-session API path, return parsed JSON."""
    req = urllib.request.Request(
        f"{BASE}/api/{path}",
        headers={
            "Cookie": cookie_hdr,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
            "Referer": f"{BASE}/melds/",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(json.dumps({"error": f"HTTP {e.code}", "detail": body[:300]}), file=sys.stderr)
        sys.exit(1)


def _http_post(path: str, payload: dict, cookie_hdr: str, csrf_token: str) -> Any:
    """POST to a browser-session API path, return parsed JSON."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}/api/{path}",
        data=data,
        method="POST",
        headers={
            "Cookie": cookie_hdr,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CSRFToken": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
            "Referer": f"{BASE}/melds/",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(json.dumps({"error": f"HTTP {e.code}", "detail": body[:300]}), file=sys.stderr)
        sys.exit(1)


# ── Public API ─────────────────────────────────────────────────────────────────

def get_comments(meld_id: str) -> list:
    """Fetch comments/notes for a meld via cookie-based HTTP (no Playwright)."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    data = _http_get(f"comments/?meld={meld_id}&limit=100", cookie_hdr)
    return data.get("results", data) if isinstance(data, dict) else data


def send_message(
    meld_id: str,
    text: str,
    hidden_from_tenant: bool = False,
    hidden_from_vendor: bool = False,
    hidden_from_owner: bool = False,
) -> dict:
    """Post a message/comment on a meld.

    Args:
        meld_id: Meld ID (numeric string or int).
        text: Message body.
        hidden_from_tenant: If True, tenant cannot see this message.
        hidden_from_vendor: If True, vendor cannot see this message.
        hidden_from_owner: If True, owner cannot see this message.
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    payload: dict = {
        "text": text,
        "meld": int(meld_id),
    }
    if hidden_from_tenant:
        payload["hidden_from_tenant"] = True
    if hidden_from_vendor:
        payload["hidden_from_vendor"] = True
    if hidden_from_owner:
        payload["hidden_from_owner"] = True

    result = _http_post("comments/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "comment_id": result.get("id"), "meld": meld_id, "text": text}


def clone_meld(meld_id: str, brief_description: Optional[str] = None) -> dict:
    """Clone a meld by reading the original and POSTing a copy to /api/melds/.

    Copies: brief_description, work_category, work_location, unit, description,
    priority, tenants, maintenance.

    Args:
        meld_id: Source meld ID to clone.
        brief_description: Override description for the clone (default: "Copy of <original>").
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    # Fetch original meld
    original = _http_get(f"melds/{meld_id}/", cookie_hdr)

    desc = brief_description or f"Copy of {original.get('brief_description', meld_id)}"

    payload: dict = {
        "brief_description": desc,
        "work_category": original.get("work_category"),
        "work_location": original.get("work_location") or "",
    }

    # Optional fields — copy if present
    for field in ("description", "work_type", "priority", "has_pets", "pets",
                  "permission_to_enter", "tenant_presence_required"):
        val = original.get(field)
        if val is not None:
            payload[field] = val

    # Unit: pass just the id
    unit = original.get("unit")
    if isinstance(unit, dict) and unit.get("id"):
        payload["unit"] = {"id": unit["id"]}
    elif isinstance(unit, int):
        payload["unit"] = {"id": unit}

    result = _http_post("melds/", payload, cookie_hdr, csrf_token)
    new_id = result.get("id")
    return {
        "ok": True,
        "cloned_from": meld_id,
        "new_meld_id": new_id,
        "brief_description": desc,
        "reference_id": result.get("reference_id"),
    }
