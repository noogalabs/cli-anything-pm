"""
Property Meld plain-HTTP backend — cookie-based session auth, no Playwright.

Auth flow:
  1. Load sessionid cookie from PM_CREDS_PATH JSON file.
  2. Fetch CSRF token from page HTML (window.PM.csrf_token) — cached per process.
  3. GET requests need only the sessionid cookie.
  4. POST/PUT/PATCH also need X-CSRFToken header.

Two API contexts:
  Management: https://app.propertymeld.com/{MULTITENANT}/m/{MULTITENANT}/api/
  Nexus Partner: https://app.propertymeld.com/{NEXUS_ACCOUNT_ID}/n/{NEXUS_ACCOUNT_ID}/api/
"""
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

from .utils import normalize_http_error

CREDS_PATH = os.environ.get(
    "PM_CREDS_PATH", os.path.expanduser("~/.claude/credentials/property-meld.json")
)
MULTITENANT = os.environ.get("PM_MULTITENANT_ID", "3287")
NEXUS_ACCOUNT_ID = os.environ.get("PM_NEXUS_ACCOUNT_ID", "338")
BASE = f"https://app.propertymeld.com/{MULTITENANT}/m/{MULTITENANT}"
NEXUS_BASE = f"https://app.propertymeld.com/{NEXUS_ACCOUNT_ID}/n/{NEXUS_ACCOUNT_ID}"
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
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
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
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


def _get_nexus_csrf(cookie_hdr: str) -> str:
    """Fetch and cache CSRF token from the Nexus Partner API keys page."""
    if _csrf_cache.get("nexus_token"):
        return _csrf_cache["nexus_token"]

    req = urllib.request.Request(
        f"{NEXUS_BASE}/nexus/api-keys/",
        headers={"Cookie": cookie_hdr, "User-Agent": UA, "Accept": "text/html"},
    )
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    m = re.search(r"window\.PM\.csrf_token\s*=\s*[\"']([\w-]+)[\"']", html)
    if not m:
        print(json.dumps({"error": "Could not extract CSRF token from Nexus page"}), file=sys.stderr)
        sys.exit(2)

    _csrf_cache["nexus_token"] = m.group(1)
    return _csrf_cache["nexus_token"]


def _http_get_nexus(path: str, cookie_hdr: str) -> Any:
    """GET from the Nexus Partner context (/338/n/338/api/...)."""
    req = urllib.request.Request(
        f"{NEXUS_BASE}/api/{path}",
        headers={
            "Cookie": cookie_hdr,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
            "Referer": f"{NEXUS_BASE}/nexus/api-keys/",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


def _http_post_nexus(path: str, payload: dict, cookie_hdr: str, csrf_token: str) -> Any:
    """POST to the Nexus Partner context (/338/n/338/api/...)."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{NEXUS_BASE}/api/{path}",
        data=data,
        method="POST",
        headers={
            "Cookie": cookie_hdr,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CSRFToken": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
            "Referer": f"{NEXUS_BASE}/nexus/api-keys/",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


def _http_put(path: str, payload: dict, cookie_hdr: str, csrf_token: str) -> Any:
    """PUT to a browser-session API path, return parsed JSON."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}/api/{path}",
        data=data,
        method="PUT",
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
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


def _http_patch(path: str, payload: dict, cookie_hdr: str, csrf_token: str) -> Any:
    """PATCH a browser-session API path, return parsed JSON."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}/api/{path}",
        data=data,
        method="PATCH",
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
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
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


def assign_tech(meld_id: str, tech_name: str) -> dict:
    """Assign an in-house tech to a meld by name (plain HTTP, no Playwright).

    Args:
        meld_id: Meld ID to assign the tech to.
        tech_name: Partial name match (case-insensitive). e.g. "Carlos" or "Carlos Calel".
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    agents = _http_get("agents/?limit=100", cookie_hdr)
    if isinstance(agents, dict):
        agents = agents.get("results", [])

    tech_lower = tech_name.lower()
    match = None
    for agent in agents:
        full_name = f"{agent.get('first_name', '')} {agent.get('last_name', '')}".lower().strip()
        if tech_lower in full_name or full_name.startswith(tech_lower):
            match = agent
            break

    if not match:
        available = ", ".join(
            f"{a.get('first_name', '')} {a.get('last_name', '')}".strip()
            for a in agents
        )
        return {"ok": False, "error": f"Tech '{tech_name}' not found.", "available": available}

    agent_obj = dict(match)
    agent_obj["type"] = "ManagementAgent"
    agent_obj["composite_id"] = f"2-{match['id']}"

    _http_patch(
        f"melds/{meld_id}/assign-maintenance/",
        {"maintenance": [agent_obj]},
        cookie_hdr,
        csrf_token,
    )
    return {
        "ok": True,
        "meld_id": meld_id,
        "assigned_to": f"{match.get('first_name', '')} {match.get('last_name', '')}".strip(),
        "agent_id": match["id"],
    }


def list_api_keys() -> dict:
    """List existing Nexus partner API keys (client IDs only — secrets not shown)."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    data = _http_get_nexus("nexus/api-keys/", cookie_hdr)
    keys = [
        {
            "id": k["id"],
            "friendly_name": k.get("friendly_name", ""),
            "created": k.get("created", ""),
            "client_id": k.get("oauth_app", {}).get("client_id", ""),
            "is_active": k.get("is_active", True),
        }
        for k in (data if isinstance(data, list) else [])
    ]
    return {"ok": True, "count": len(keys), "keys": keys}


def rotate_api_key(key_name: Optional[str] = None) -> dict:
    """Create a new Nexus partner API key. Returns client_id and client_secret (shown once)."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_nexus_csrf(cookie_hdr)
    payload = {"friendly_name": key_name or "Ascend Property Management (via API)"}
    result = _http_post_nexus("nexus/api-keys/", payload, cookie_hdr, csrf_token)
    oauth = result.get("oauth_app", {})
    return {
        "ok": True,
        "key_id": result.get("id"),
        "friendly_name": result.get("friendly_name"),
        "client_id": oauth.get("client_id"),
        "client_secret": oauth.get("client_secret"),
        "note": "client_secret shown once — store it immediately",
    }


def merge_meld(meld_id: str, into_meld_id: str) -> dict:
    """Merge source meld into destination meld. Both melds must be at the same unit/property.

    The source meld will be marked MANAGER_CANCELED with "(Merged)" in PM.

    Args:
        meld_id: Source meld ID to merge (will be cancelled).
        into_meld_id: Destination meld ID to merge into (absorbs the source).
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_post(f"melds/{meld_id}/merge/", {"meld": int(into_meld_id)}, cookie_hdr, csrf_token)
    return {"ok": True, "merged_meld_id": meld_id, "into_meld_id": into_meld_id, "result": result}


def complete_meld(meld_id: str, completion_notes: Optional[str] = None) -> dict:
    """Mark a meld complete from the manager side.

    Meld must be in PENDING_COMPLETION status. Raises HTTP 403 otherwise.

    Args:
        meld_id: Meld ID to mark complete.
        completion_notes: Optional completion notes.
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    payload: dict = {}
    if completion_notes:
        payload["completion_notes"] = completion_notes
    result = _http_patch(f"melds/{meld_id}/complete/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "meld_id": meld_id, "completion_notes": completion_notes, "result": result}


def cancel_meld(meld_id: str, reason: Optional[str] = None) -> dict:
    """Cancel a meld from the manager side.

    Args:
        meld_id: Meld ID to cancel.
        reason: Cancellation reason (recommended for audit trail).
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    payload: dict = {}
    if reason:
        payload["manager_cancellation_reason"] = reason
    result = _http_patch(f"melds/{meld_id}/cancel/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "meld_id": meld_id, "reason": reason, "result": result}


def schedule_appointment(meld_id: str, dtstart: str, duration_hours: float = 2.0) -> dict:
    """Schedule an in-house tech appointment window on a meld.

    Args:
        meld_id: Meld ID.
        dtstart: ISO 8601 datetime string, e.g. '2026-04-27T14:00:00-04:00'.
        duration_hours: Appointment duration in hours (default 2).

    The meld must have an in-house tech assigned — PM creates the managementappointment
    object at assignment time. This sets the availability_segment (the actual time window).
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    # Get the management appointment ID from the meld
    meld = _http_get(f"melds/{meld_id}/", cookie_hdr)
    appts = meld.get("managementappointment", [])
    if not appts:
        return {"ok": False, "error": "No in-house tech assignment found on this meld"}
    appt_id = appts[0]["id"]

    duration_seconds = int(duration_hours * 3600)
    payload = {
        "availability_segment": {
            "event": {
                "dtstart": dtstart,
                "duration": duration_seconds,
            },
            "meld": int(meld_id),
        }
    }
    result = _http_put(f"management-appointments/{appt_id}/schedule/", payload, cookie_hdr, csrf_token)
    appt_seg = result.get("availability_segment") or {}
    event = (appt_seg.get("event") or {}) if isinstance(appt_seg, dict) else {}
    return {
        "ok": True,
        "meld_id": meld_id,
        "appointment_id": appt_id,
        "dtstart": event.get("dtstart", dtstart),
        "duration_hours": duration_hours,
        "result": result,
    }


def list_tenants(search: Optional[str] = None, limit: int = 100) -> list:
    """List tenants, optionally filtered client-side by name or email.

    Args:
        search: Case-insensitive substring matched against first_name, last_name, or email.
        limit: Maximum number of results to return (after client-side filter).
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    # Fetch all tenants (API does not support server-side name/email filtering)
    results: list = []
    page_size = 200
    page = _http_get(f"tenants/?limit={page_size}", cookie_hdr)
    results.extend(page.get("results", []))
    # Paginate if needed and we haven't hit the requested limit
    while page.get("next") and len(results) < limit * 3:
        next_url = page["next"].split("/api/")[-1]
        page = _http_get(next_url, cookie_hdr)
        results.extend(page.get("results", []))

    if search:
        needle = search.lower()
        results = [
            t for t in results
            if needle in (t.get("first_name") or "").lower()
            or needle in (t.get("last_name") or "").lower()
            or needle in ((t.get("user") or {}).get("email") or "").lower()
            or needle in ((t.get("contact") or {}).get("cell_phone") or "")
            or needle in ((t.get("contact") or {}).get("home_phone") or "")
        ]

    return results[:limit]


def get_tenant(tenant_id: str) -> dict:
    """Get a single tenant by ID."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _http_get(f"tenants/{tenant_id}/", cookie_hdr)


def list_files(meld_id: str) -> list:
    """List files (photos, attachments) on a meld via cookie HTTP.

    Fetches all 3 photo endpoints in parallel and merges into a single list:
      GET /api/melds/{id}/files/         — manager uploads
      GET /api/melds/{id}/tenant-files/  — tenant uploads
      GET /api/melds/{id}/vendor-files/  — vendor uploads

    Each merged file gains an "uploader_role" field set to one of
    "manager", "tenant", or "vendor" so downstream consumers can filter
    by source. Other fields (filename, signed_url, full_compressed, id,
    meld, uploader, created) come straight from the source endpoint.

    Used by the pre-complete audit hook gate (photo presence check)
    and by pm-photos download tooling.
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)

    sources = [
        ("manager", f"melds/{meld_id}/files/?limit=100"),
        ("tenant",  f"melds/{meld_id}/tenant-files/?limit=100"),
        ("vendor",  f"melds/{meld_id}/vendor-files/?limit=100"),
    ]

    merged: list = []
    for role, path in sources:
        data = _http_get(path, cookie_hdr)
        items = data.get("results", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                item["uploader_role"] = role
            merged.append(item)
    return merged


def list_work_entries(meld_id: str) -> list:
    """List per-visit work-entries on a meld via cookie HTTP.

    GET /api/melds/{id}/work-entries/ — endpoint verified by Blue 5/04.
    Returns chronological per-visit logs with checkin/checkout/hours/agent
    /description/long_description fields. snapcli (api_backend) has zero
    coverage of this endpoint — work-entries was the THIRD notes location
    flagged in feedback_pm_work_entries_endpoint per agent memory.

    Used to inspect tech work logs for completion-quality auditing and
    when comparing against completion_notes / maintenance_notes.
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    data = _http_get(f"melds/{meld_id}/work-entries/", cookie_hdr)
    return data.get("results", data) if isinstance(data, dict) else data


def assign_vendor(meld_id: str, vendor_id: str, account_prefix: str = "1") -> dict:
    """Assign an external vendor to a meld by numeric ID.

    Args:
        meld_id: Meld ID.
        vendor_id: Vendor ID.
        account_prefix: Account prefix for composite_id (default "1").
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    vendor_obj = {
        "type": "Vendor",
        "id": int(vendor_id),
        "composite_id": f"{account_prefix}-{vendor_id}",
    }

    result = _http_patch(
        f"melds/{int(meld_id)}/assign-maintenance/",
        {"maintenance": [vendor_obj]},
        cookie_hdr,
        csrf_token,
    )
    return {
        "ok": True,
        "meld_id": meld_id,
        "vendor_id": vendor_id,
        "account_prefix": account_prefix,
        "result": result,
    }


def assign_vendor_by_name(meld_id: str, vendor_name: str, account_prefix: str = "1") -> dict:
    """Assign an external vendor to a meld by name (partial match).

    Mirrors assign_tech() but for vendors. Looks up the vendor by partial
    name match (case-insensitive), then delegates to assign_vendor() with
    the matched id.

    Args:
        meld_id: Meld ID to assign the vendor to.
        vendor_name: Partial name match. e.g. "Rogers" or "Rogers Electric".
        account_prefix: Account prefix for composite_id (default "1").
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)

    vendors = _http_get("vendors/?limit=100", cookie_hdr)
    if isinstance(vendors, dict):
        vendors = vendors.get("results", [])

    vendor_lower = vendor_name.lower()
    match = None
    for v in vendors:
        full = (v.get("name") or "").lower()
        if vendor_lower in full or full.startswith(vendor_lower):
            match = v
            break

    if not match:
        available = ", ".join((v.get("name") or "") for v in vendors)
        return {"ok": False, "error": f"Vendor '{vendor_name}' not found.", "available": available}

    result = assign_vendor(meld_id, str(match["id"]), account_prefix=account_prefix)
    if isinstance(result, dict) and result.get("ok"):
        result["assigned_to"] = match.get("name", "")
    return result


# ── int-PK guard ──────────────────────────────────────────────────────────────
# PM API endpoints expect the integer PK (e.g. 12701108), not the human-facing
# short code (e.g. 'T5LKWTDB'). Passing a short code returns HTTP 404 because the
# Django URL pattern is <int:pk>. This guard catches the mismatch at the SDK
# boundary instead of at the API boundary, so the error is actionable. Applied
# to Phase-2-backport functions only in this commit; existing functions
# (assign_tech, complete_meld, etc.) keep their current shape — separate
# follow-up if/when we want fleet-wide enforcement.
def _validate_meld_id(meld_id) -> int:
    """Coerce meld_id to int PK. Reject PM short codes.

    Raises ValueError on short-code input (e.g. 'T5LKWTDB') with a message
    pointing at `pm work-orders list` for resolution.
    """
    try:
        return int(meld_id)
    except (TypeError, ValueError):
        raise ValueError(
            f"meld_id must be the integer PK (e.g. 12701108), got {meld_id!r}. "
            f"PM short codes (e.g. 'T5LKWTDB') are not accepted; use 'pm work-orders list' to find the int PK."
        )


def schedule_vendor_appointment(meld_id: str, vendor_id: str, dtstart: str, duration_hours: float = 2.0) -> dict:
    """Schedule an external vendor appointment window on a meld.

    Args:
        meld_id: Meld ID.
        vendor_id: Vendor ID.
        dtstart: ISO 8601 datetime string, e.g. '2026-04-27T14:00:00-04:00'.
        duration_hours: Appointment duration in hours (default 2).

    The meld must have the vendor assigned — PM creates the vendorassignment
    object at assignment time. This sets the availability_segment (the actual time window).
    """
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    # Get the vendor assignment ID from the meld
    meld = _http_get(f"melds/{meld_id}/", cookie_hdr)
    # added 2026-04-29 by collie via dane dispatch — Wave 1.5 meld_state_change instrumentation per UU TODO
    # Reuse the existing meld fetch above to capture prior_state — no extra GET needed.
    prior_state = "unknown"
    if isinstance(meld, dict):
        prior_state = str(meld.get("status") or meld.get("state") or "unknown")
    vendor_assignments = meld.get("vendorassignment", [])
    if not vendor_assignments:
        return {"ok": False, "error": "No vendor assignment found on this meld"}

    # Find the assignment for the specific vendor
    vendor_assign_id = None
    for va in vendor_assignments:
        if str(va.get("vendor_id")) == str(vendor_id) or str(va.get("id")) == str(vendor_id):
            vendor_assign_id = va.get("id")
            break

    if not vendor_assign_id and vendor_assignments:
        # If no match found, use the first one
        vendor_assign_id = vendor_assignments[0]["id"]

    if not vendor_assign_id:
        return {"ok": False, "error": f"Vendor {vendor_id} not assigned to this meld"}

    duration_seconds = int(duration_hours * 3600)
    payload = {
        "availability_segment": {
            "event": {
                "dtstart": dtstart,
                "duration": duration_seconds,
            },
            "meld": int(meld_id),
        }
    }
    result = _http_put(f"vendor-assignments/{vendor_assign_id}/schedule/", payload, cookie_hdr, csrf_token)
    # added 2026-04-29 by collie via dane dispatch — Wave 1.5 meld_state_change instrumentation per UU TODO
    _emit_meld_state_change(
        meld_id, prior_state, "scheduled", "scheduled_vendor_appointment",
        vendor_id=int(vendor_id), assignment_id=vendor_assign_id,
        dtstart=dtstart, triggered_by="manager",
    )
    appt_seg = result.get("availability_segment") or {}
    event = (appt_seg.get("event") or {}) if isinstance(appt_seg, dict) else {}
    return {
        "ok": True,
        "meld_id": meld_id,
        "vendor_id": vendor_id,
        "assignment_id": vendor_assign_id,
        "dtstart": event.get("dtstart", dtstart),
        "duration_hours": duration_hours,
        "result": result,
    }


def list_projects(meld_id: Optional[str] = None, limit: int = 100) -> list:
    """List projects associated with a meld or account."""
    if meld_id is not None:
        meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    path = f"melds/{meld_id}/projects/" if meld_id else f"projects/?limit={limit}"
    data = _http_get(path, cookie_hdr)
    return data.get("results", data) if isinstance(data, dict) else data


def get_project(project_id: str) -> dict:
    """Get a single project by ID."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _http_get(f"projects/{project_id}/", cookie_hdr)


# create/update/delete project — DROPPED per Item 3 spike (5/05).
# Endpoint POST /api/projects/ is reachable but the create payload schema is
# incomplete in the Haiku-coauthored snapcli stub. Verified required fields:
# name + description + start_date + due_date + coordinators[] + project_type
# + unit (active unit reference). The "unit" field shape is unknown — passing
# the unit.id int (e.g. 1754499) returns HTTP 500. Needs Safari Web Inspector
# capture of the actual /api/projects/ POST payload from the manager UI to
# discover the correct shape (likely needs unit_pk, address-tied lookup, or
# a search-string indirection). update + delete commands remained untested
# because of the create-cycle dependency for safe verification.
#
# list_projects + get_project are verified working and remain available.
# See tracking task task_<TBD> for endpoint-discovery follow-up.


# ── Estimates ─────────────────────────────────────────────────────────────────

def list_estimates(meld_id: Optional[str] = None, limit: int = 100, status: Optional[str] = None) -> list:
    """List estimates for a meld or account."""
    if meld_id is not None:
        meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    if meld_id:
        path = f"estimates/meld/{meld_id}/"
    else:
        path = f"estimates/?limit={limit}"
        if status:
            path += f"&status={status}"
    data = _http_get(path, cookie_hdr)
    return data.get("results", data) if isinstance(data, dict) else data


def get_estimate(estimate_id: str) -> dict:
    """Get a single estimate by ID."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _http_get(f"estimates/{estimate_id}/", cookie_hdr)


def create_estimate(meld_id: str, estimate_number: str, amount: str, description: str = "", due_date: Optional[str] = None, project_id: Optional[str] = None) -> dict:
    """Create a new estimate linked to a meld."""
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    payload = {
        "meld_id": int(meld_id),
        "estimate_number": estimate_number,
        "description": description,
        "amount": float(amount),
    }
    if due_date:
        payload["due_date"] = due_date
    if project_id:
        payload["project_id"] = int(project_id)
    result = _http_post("estimates/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "estimate_id": result.get("id"), "result": result}


def update_estimate(estimate_id: str, estimate_number: Optional[str] = None, amount: Optional[str] = None, description: Optional[str] = None, status: Optional[str] = None) -> dict:
    """Update an invoice."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    payload = {}
    if estimate_number:
        payload["estimate_number"] = estimate_number
    if amount:
        payload["amount"] = float(amount)
    if description:
        payload["description"] = description
    if status:
        payload["status"] = status
    result = _http_patch(f"estimates/{estimate_id}/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "estimate_id": estimate_id, "result": result}


def link_estimate_to_meld(estimate_id: str, meld_id: str) -> dict:
    """Link an estimate to a meld."""
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_patch(f"estimates/{estimate_id}/", {"meld_id": int(meld_id)}, cookie_hdr, csrf_token)
    return {"ok": True, "estimate_id": estimate_id, "meld_id": meld_id, "result": result}


# ── Receipts ─────────────────────────────────────────────────────────────────

def list_receipts(meld_id: Optional[str] = None, limit: int = 100) -> list:
    """List receipts for a meld or account."""
    if meld_id is not None:
        meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    if meld_id:
        path = f"melds/{meld_id}/receipts/"
    else:
        path = f"receipts/?limit={limit}"
    data = _http_get(path, cookie_hdr)
    return data.get("results", data) if isinstance(data, dict) else data


def get_receipt(receipt_id: str) -> dict:
    """Get a single receipt by ID."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _http_get(f"receipts/{receipt_id}/", cookie_hdr)


def upload_receipt(meld_id: str, file_path: str, description: str = "", linked_estimate_id: Optional[str] = None) -> dict:
    """Upload a receipt file for a meld."""
    meld_id = _validate_meld_id(meld_id)
    import os as _os
    from pathlib import Path as _Path

    if not _os.path.exists(file_path):
        return {"ok": False, "error": f"File not found: {file_path}"}

    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    # Build multipart form data
    file_name = _Path(file_path).name
    with open(file_path, "rb") as f:
        file_data = f.read()

    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body_parts = []
    body_parts.append(f"--{boundary}".encode())
    body_parts.append(b'Content-Disposition: form-data; name="meld_id"')
    body_parts.append(b"")
    body_parts.append(str(meld_id).encode())

    if description:
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(b'Content-Disposition: form-data; name="description"')
        body_parts.append(b"")
        body_parts.append(description.encode())

    if linked_estimate_id:
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(b'Content-Disposition: form-data; name="linked_estimate_id"')
        body_parts.append(b"")
        body_parts.append(str(linked_estimate_id).encode())

    body_parts.append(f"--{boundary}".encode())
    body_parts.append(f'Content-Disposition: form-data; name="file"; filename="{file_name}"'.encode())
    body_parts.append(b"Content-Type: application/octet-stream")
    body_parts.append(b"")
    body_parts.append(file_data)
    body_parts.append(f"--{boundary}--".encode())
    body_parts.append(b"")

    body = b"\r\n".join(body_parts)

    req = urllib.request.Request(
        f"{BASE}/api/melds/{int(meld_id)}/receipts/",
        data=body,
        method="POST",
        headers={
            "Cookie": cookie_hdr,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "X-CSRFToken": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
            "Referer": f"{BASE}/melds/",
        },
    )

    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=30) as resp:
            result = json.loads(resp.read())
            return {"ok": True, "receipt_id": result.get("id"), "result": result}
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="ignore")
        error = normalize_http_error(e.code, body_err)
        error["ok"] = False
        return error


def upload_meld_file(meld_id: str, file_path: str, uploader_role: str = "manager", description: str = "") -> dict:
    """Upload a file attachment to a meld via cookie HTTP.

    Routes by uploader_role:
      manager → POST /api/melds/{id}/files/         (manager session)
      tenant  → POST /api/melds/{id}/tenant-files/  (manager-side backfill)
      vendor  → POST /api/melds/{id}/vendor-files/  (manager-side backfill)

    Closes the file-upload coverage gap flagged 5/07: previously only
    receipts had upload coverage; manager + tenant + vendor file uploads
    now work via this single function with --as flag.
    """
    meld_id = _validate_meld_id(meld_id)
    import os as _os
    from pathlib import Path as _Path

    if not _os.path.exists(file_path):
        return {"ok": False, "error": f"File not found: {file_path}"}

    role_to_endpoint = {
        "manager": "files",
        "tenant": "tenant-files",
        "vendor": "vendor-files",
    }
    if uploader_role not in role_to_endpoint:
        return {"ok": False, "error": f"Unknown uploader_role '{uploader_role}'. Use manager|tenant|vendor."}
    endpoint = role_to_endpoint[uploader_role]

    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    file_name = _Path(file_path).name
    with open(file_path, "rb") as f:
        file_data = f.read()

    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body_parts = []
    body_parts.append(f"--{boundary}".encode())
    body_parts.append(b'Content-Disposition: form-data; name="meld_id"')
    body_parts.append(b"")
    body_parts.append(str(meld_id).encode())

    if description:
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(b'Content-Disposition: form-data; name="description"')
        body_parts.append(b"")
        body_parts.append(description.encode())

    body_parts.append(f"--{boundary}".encode())
    body_parts.append(f'Content-Disposition: form-data; name="file"; filename="{file_name}"'.encode())
    body_parts.append(b"Content-Type: application/octet-stream")
    body_parts.append(b"")
    body_parts.append(file_data)
    body_parts.append(f"--{boundary}--".encode())
    body_parts.append(b"")

    body = b"\r\n".join(body_parts)

    req = urllib.request.Request(
        f"{BASE}/api/melds/{int(meld_id)}/{endpoint}/",
        data=body,
        method="POST",
        headers={
            "Cookie": cookie_hdr,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "X-CSRFToken": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
            "Referer": f"{BASE}/melds/",
        },
    )

    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=30) as resp:
            result = json.loads(resp.read())
            return {"ok": True, "uploader_role": uploader_role, "file_id": result.get("id"), "result": result}
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="ignore")
        error = normalize_http_error(e.code, body_err)
        error["ok"] = False
        error["uploader_role"] = uploader_role
        return error


def link_receipt_to_invoice(receipt_id: str, estimate_id: str) -> dict:
    """Link a receipt to an invoice."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_patch(f"receipts/{receipt_id}/", {"linked_estimate_id": int(estimate_id)}, cookie_hdr, csrf_token)
    return {"ok": True, "receipt_id": receipt_id, "estimate_id": estimate_id, "result": result}


# ── Vendor Invitations ───────────────────────────────────────────────────────

