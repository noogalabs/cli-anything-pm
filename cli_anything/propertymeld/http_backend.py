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
import functools
import json
import mimetypes
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Optional

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


def _build_url(path: str, side: str = "manager", vendor_id: Optional[str] = None) -> str:
    """Build a PM cookie-API URL for either management or vendor surface.

    side='manager' -> https://app.propertymeld.com/{MULTITENANT}/m/{MULTITENANT}/api/{path}
    side='vendor'  -> https://app.propertymeld.com/{MULTITENANT}/v/{vendor_id}/api/{path}

    Same Django backend, different URL prefix. Vendor-side endpoints are
    operator-on-behalf-of-vendor flows (accept assignment, upload vendor file,
    set schedule segments, vendor-complete, vendor invoice CRUD).
    """
    host = "https://app.propertymeld.com"
    if side == "vendor":
        if not vendor_id:
            raise ValueError("vendor_id required for side='vendor'")
        return f"{host}/{MULTITENANT}/v/{vendor_id}/api/{path}"
    if side == "manager":
        return f"{host}/{MULTITENANT}/m/{MULTITENANT}/api/{path}"
    raise ValueError(f"unknown side: {side!r}")


class SessionExpired(Exception):
    """Raised by HTTP helpers on 401 so public API calls can retry once."""

    def __init__(self, http_error: urllib.error.HTTPError) -> None:
        self.http_error = http_error
        super().__init__(f"PropertyMeld session expired (HTTP {http_error.code})")


_RECAPTURE_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
    "pm-recapture-session-playwright.py",
)


def _attempt_recapture() -> bool:
    """Refresh the PM session cookie via the Playwright recapture helper."""
    if not os.path.exists(_RECAPTURE_SCRIPT):
        print(
            json.dumps({"error": "Playwright recapture script not found", "path": _RECAPTURE_SCRIPT}),
            file=sys.stderr,
        )
        return False

    print(json.dumps({"event": "auto_recapture_attempt", "script": _RECAPTURE_SCRIPT}), file=sys.stderr)
    try:
        result = subprocess.run(
            [sys.executable, _RECAPTURE_SCRIPT],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "Recapture timed out (120s)"}), file=sys.stderr)
        return False
    except OSError as exc:
        print(json.dumps({"error": "Recapture spawn failed", "detail": str(exc)}), file=sys.stderr)
        return False

    if result.returncode != 0:
        tail = (result.stderr or "")[-300:]
        print(
            json.dumps({"error": "Recapture failed", "rc": result.returncode, "stderr_tail": tail}),
            file=sys.stderr,
        )
        return False

    print(json.dumps({"event": "auto_recapture_ok"}), file=sys.stderr)
    _csrf_cache.clear()
    return True


def with_recapture_retry(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Retry one public API call after a single cookie recapture."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except SessionExpired:
            print(json.dumps({"event": "session_expired_caught", "fn": fn.__name__}), file=sys.stderr)
            if not _attempt_recapture():
                print(
                    json.dumps({"error": "Auto-recapture failed; manual intervention needed", "fn": fn.__name__}),
                    file=sys.stderr,
                )
                sys.exit(1)
            try:
                return fn(*args, **kwargs)
            except SessionExpired:
                print(
                    json.dumps({"error": "Still 401 after recapture; manual intervention needed", "fn": fn.__name__}),
                    file=sys.stderr,
                )
                sys.exit(1)

    return wrapper


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
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SessionExpired(e)
        raise

    m = re.search(r"window\.PM\.csrf_token\s*=\s*[\"']([\w-]+)[\"']", html)
    if not m:
        print(json.dumps({"error": "Could not extract CSRF token from PM page"}), file=sys.stderr)
        sys.exit(2)

    _csrf_cache["token"] = m.group(1)
    return _csrf_cache["token"]


def _http_get(path: str, cookie_hdr: str, *, side: str = "manager", vendor_id: Optional[str] = None) -> Any:
    """GET a browser-session API path, return parsed JSON."""
    req = urllib.request.Request(
        _build_url(path, side=side, vendor_id=vendor_id),
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
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


def _http_get_no_exit(path: str, cookie_hdr: str, *, side: str = "manager", vendor_id: Optional[str] = None) -> Any:
    """GET variant that RETURNS a normalized error dict on non-401 HTTPError
    instead of sys.exit(1).

    Sibling of _http_patch_no_exit, for the SAME reason: callers that have
    already created an artifact (clone_meld's post-create coordinator
    assignment does fetch-first to build the full-echo PATCH payload) must not
    hard-exit on a read-after-write 404 or a 403/500 on the detail fetch —
    that would orphan the new meld and drop its id one step BEFORE the no-exit
    PATCH path is even reached. Those callers use this variant and inspect the
    returned dict for an `error`/`status_code` to recover.

    401 still raises SessionExpired so @with_recapture_retry can re-auth —
    that path is unchanged. Only non-401 HTTPErrors are converted to a dict.
    Global _http_get is untouched; its other callers keep sys.exit semantics.
    """
    req = urllib.request.Request(
        _build_url(path, side=side, vendor_id=vendor_id),
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
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        return normalize_http_error(e.code, body)


def _paginate_all(path: str, cookie_hdr: str, max_pages: int = 50) -> list:
    """Walk the DRF `next` link chain and concatenate `results` arrays.

    Many cookie-API list endpoints return at most 100 items per page (and
    often default to 25). Helpers that read only the first page silently
    truncate when the underlying record set is larger — this masked photo
    sources past 100 items in the inspect aggregator and capped name-based
    tech/vendor matching at the first 100 records. Use this helper anywhere
    the caller's intent is "all of them, not just page 1".

    `max_pages` is a defensive cap to prevent runaway pagination on a
    misconfigured endpoint; raise it if a real list legitimately exceeds it.
    """
    results: list = []
    pages = 0
    next_path: Optional[str] = path
    while next_path and pages < max_pages:
        page = _http_get(next_path, cookie_hdr)
        pages += 1
        if isinstance(page, dict):
            page_items = page.get("results")
            if isinstance(page_items, list):
                results.extend(page_items)
            else:
                # Non-paginated dict — return whatever it gave us
                return page_items if isinstance(page_items, list) else []
            raw_next = page.get("next")
            if not raw_next:
                next_path = None
            elif "/api/" in raw_next:
                next_path = raw_next.split("/api/", 1)[1]
            else:
                next_path = None
        elif isinstance(page, list):
            # Endpoint returned a flat list (no pagination wrapper)
            results.extend(page)
            next_path = None
        else:
            next_path = None
    return results


def _http_post(path: str, payload: dict, cookie_hdr: str, csrf_token: str, *, side: str = "manager", vendor_id: Optional[str] = None) -> Any:
    """POST to a browser-session API path, return parsed JSON."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _build_url(path, side=side, vendor_id=vendor_id),
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
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


def _http_post_no_exit(path: str, payload: dict, cookie_hdr: str, csrf_token: str, *, side: str = "manager", vendor_id: Optional[str] = None) -> Any:
    """POST variant that returns normalized non-401 errors and supports empty success bodies."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _build_url(path, side=side, vendor_id=vendor_id),
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
            body = resp.read()
            if not body:
                return {}
            return json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        return normalize_http_error(e.code, body)


def _get_nexus_csrf(cookie_hdr: str) -> str:
    """Fetch and cache CSRF token from the Nexus Partner API keys page."""
    if _csrf_cache.get("nexus_token"):
        return _csrf_cache["nexus_token"]

    req = urllib.request.Request(
        f"{NEXUS_BASE}/nexus/api-keys/",
        headers={"Cookie": cookie_hdr, "User-Agent": UA, "Accept": "text/html"},
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SessionExpired(e)
        raise

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
        if e.code == 401:
            raise SessionExpired(e)
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
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


def _http_put(path: str, payload: dict, cookie_hdr: str, csrf_token: str, *, side: str = "manager", vendor_id: Optional[str] = None) -> Any:
    """PUT to a browser-session API path, return parsed JSON."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _build_url(path, side=side, vendor_id=vendor_id),
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
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


def _http_patch(path: str, payload: dict, cookie_hdr: str, csrf_token: str, *, side: str = "manager", vendor_id: Optional[str] = None) -> Any:
    """PATCH a browser-session API path, return parsed JSON."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _build_url(path, side=side, vendor_id=vendor_id),
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
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


def _http_patch_no_exit(path: str, payload: dict, cookie_hdr: str, csrf_token: str, *, side: str = "manager", vendor_id: Optional[str] = None) -> Any:
    """PATCH variant that RETURNS a normalized error dict on non-401 HTTPError
    instead of sys.exit(1).

    The default _http_patch sys.exit(1)s on error, which is correct for the
    20+ top-level CLI write paths that have no artifact to lose. But callers
    that have ALREADY created an artifact (e.g. clone_meld's post-create
    coordinator assignment) must NOT hard-exit — that would orphan the new
    meld and drop its id. Those callers use this variant and inspect the
    returned dict for an `error`/`status_code` to recover (return the artifact
    id + a loud warning) instead of dying.

    401 still raises SessionExpired so @with_recapture_retry can re-auth —
    that path is unchanged. Only non-401 HTTPErrors are converted to a dict.
    """
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _build_url(path, side=side, vendor_id=vendor_id),
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
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        return normalize_http_error(e.code, body)


def _http_delete(path: str, cookie_hdr: str, csrf_token: str, *, side: str = "manager", vendor_id: Optional[str] = None) -> Any:
    """DELETE a browser-session API path. Returns parsed JSON, or {} on 204."""
    req = urllib.request.Request(
        _build_url(path, side=side, vendor_id=vendor_id),
        method="DELETE",
        headers={
            "Cookie": cookie_hdr,
            "Accept": "application/json",
            "X-CSRFToken": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
            "Referer": f"{BASE}/melds/",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as resp:
            body = resp.read()
            if not body:
                return {}
            return json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


def _http_get_optional_results(path: str, cookie_hdr: str, note_label: str) -> tuple[list, Optional[str]]:
    """GET an optional list endpoint and downgrade 404s to an empty list + note."""
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
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        if e.code == 404:
            return [], f"{note_label} endpoint unavailable (404): /api/{path}"
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)

    items = data.get("results", data) if isinstance(data, dict) else data
    return (items if isinstance(items, list) else []), None


# ── Public API ─────────────────────────────────────────────────────────────────

@with_recapture_retry
def get_comments(meld_id: str) -> list:
    """Fetch comments/notes for a meld via cookie-based HTTP (no Playwright)."""
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    data = _http_get(f"comments/?meld={meld_id}&limit=100", cookie_hdr)
    return data.get("results", data) if isinstance(data, dict) else data


@with_recapture_retry
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
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    payload: dict = {
        "text": text,
        "meld": meld_id,
    }
    if hidden_from_tenant:
        payload["hidden_from_tenant"] = True
    if hidden_from_vendor:
        payload["hidden_from_vendor"] = True
    if hidden_from_owner:
        payload["hidden_from_owner"] = True

    result = _http_post("comments/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "comment_id": result.get("id"), "meld": meld_id, "text": text}


@with_recapture_retry
def clone_meld(
    meld_id: str,
    brief_description: Optional[str] = None,
    description: Optional[str] = None,
    tenant_presence_required: Optional[bool] = None,
    unit_id: Optional[int] = None,
    priority: Optional[str] = None,
    coordinator_id: Optional[int] = None,
) -> dict:
    """Clone a meld by reading the original and POSTing a copy to /api/melds/.

    Copies: brief_description, work_category, work_location, unit, description,
    work_type, priority, and the coordinator. The coordinator is inherited from
    the source meld via a follow-up set_coordinator() PATCH after the clone is
    created (the create POST does not have a verified coordinator-write shape),
    so a coordinator failure never blocks the clone itself.

    Args:
        meld_id: Source meld ID to clone.
        brief_description: Override short description for the clone
            (default: "Copy of <original>").
        description: Override long-form description for the clone.
        tenant_presence_required: Optional override for tenant-presence gate.
        unit_id: Optional override for target unit id.
        priority: Optional override for priority (LOW|MEDIUM|HIGH|EMERGENCY).
        coordinator_id: Optional override coordinator user id. When omitted the
            clone inherits the source meld's coordinator; when None and the
            source has no coordinator, no coordinator is set.
    """
    meld_id = _validate_meld_id(meld_id)
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

    # Optional fields — copy if present, with explicit overrides winning.
    for field in ("description", "work_type", "priority", "has_pets", "pets",
                  "permission_to_enter", "tenant_presence_required"):
        if field == "description" and description is not None:
            payload["description"] = description
            continue
        if field == "tenant_presence_required" and tenant_presence_required is not None:
            payload["tenant_presence_required"] = tenant_presence_required
            continue
        if field == "priority" and priority is not None:
            payload["priority"] = priority
            continue
        val = original.get(field)
        if val is not None:
            payload[field] = val

    if unit_id is not None:
        payload["unit"] = {"id": int(unit_id)}
    else:
        # Unit: pass just the id from source
        unit = original.get("unit")
        if isinstance(unit, dict) and unit.get("id"):
            payload["unit"] = {"id": unit["id"]}
        elif isinstance(unit, int):
            payload["unit"] = {"id": unit}

    result = _http_post("melds/", payload, cookie_hdr, csrf_token)
    new_id = result.get("id")

    # Inherit the coordinator (single-field on melds). The detail GET returns
    # coordinator as an object {id, ...}; the explicit --coordinator-id override
    # wins, otherwise we carry the source's coordinator id forward. Set it via
    # the verified full-echo PATCH (set_coordinator) AFTER the clone exists so a
    # coordinator failure surfaces as a warning rather than dropping the clone.
    src_coord = original.get("coordinator")
    src_coord_id = (
        src_coord.get("id") if isinstance(src_coord, dict)
        else (src_coord if isinstance(src_coord, int) else None)
    )
    coord_to_set = coordinator_id if coordinator_id is not None else src_coord_id

    out = {
        "ok": True,
        "cloned_from": meld_id,
        "new_meld_id": new_id,
        "brief_description": desc,
        "reference_id": result.get("reference_id"),
    }

    if coord_to_set is not None and new_id is not None:
        coord_result = set_coordinator(new_id, int(coord_to_set))
        if coord_result.get("ok"):
            out["coordinator_id"] = coord_result.get("coordinator_id")
        else:
            # Loud, not silent: the clone succeeded but coordinator assignment
            # did not — the caller must know to set it manually.
            out["coordinator_warning"] = (
                f"clone created (meld {new_id}) but coordinator assignment "
                f"to {coord_to_set} failed: {coord_result.get('error')}"
            )

    return out


@with_recapture_retry
def set_coordinator(meld_id: str, user_id: int) -> dict:
    """Set the coordinator on a meld via a full-payload-echo PATCH.

    PM requires a FULL payload echo on meld PATCH — a delta PATCH returns HTTP
    400 with field-required errors for brief_description, work_location,
    work_category, work_type, and priority (verified live 2026-05-29). So we
    fetch the current meld, overlay the coordinator, and PATCH the full required
    set. The coordinator detail-GET shape is an object {id, ...}, but PATCH
    accepts the bare int user id (verified live, same session).

    Args:
        meld_id: Meld ID to set the coordinator on.
        user_id: ManagementAgent user id to assign as coordinator.
    """
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    # NON-EXITING fetch: when set_coordinator runs from clone_meld AFTER the
    # clone POST, a read-after-write 404 or a 403/500 on this detail GET must
    # return {ok: False} (so clone_meld surfaces a warning + still returns the
    # new meld id) rather than sys.exit(1) and orphan the clone one step before
    # the no-exit PATCH leg. 401 still raises SessionExpired inside the helper.
    current = _http_get_no_exit(f"melds/{meld_id}/", cookie_hdr)
    if not isinstance(current, dict) or current.get("error"):
        return {
            "ok": False,
            "error": "could not fetch current meld state for full-payload echo",
            "detail": current,
        }

    payload: dict = {
        "brief_description": current.get("brief_description"),
        "work_location": current.get("work_location") or "",
        "work_category": current.get("work_category"),
        "work_type": current.get("work_type"),
        "priority": current.get("priority"),
        "coordinator": int(user_id),
    }

    # Use the NON-EXITING patch variant: set_coordinator is called by
    # clone_meld AFTER the clone POST already created a meld, so a coordinator
    # PATCH 4xx/5xx must return {ok: False} (letting clone_meld surface a loud
    # warning + still return the new meld id) rather than sys.exit(1) and
    # orphan the clone. 401 still raises SessionExpired inside the helper, so
    # @with_recapture_retry re-auths unchanged.
    result = _http_patch_no_exit(f"melds/{meld_id}/", payload, cookie_hdr, csrf_token)
    if isinstance(result, dict) and isinstance(result.get("status_code"), int) and result["status_code"] >= 400:
        return {"ok": False, "error": "coordinator PATCH failed", "detail": result}

    new_coord = result.get("coordinator") if isinstance(result, dict) else None
    coord_id = new_coord.get("id") if isinstance(new_coord, dict) else new_coord
    return {"ok": True, "meld_id": meld_id, "coordinator_id": coord_id}


@with_recapture_retry
def assign_tech(meld_id: str, tech_name: str) -> dict:
    """Assign an in-house tech to a meld by name (plain HTTP, no Playwright).

    Args:
        meld_id: Meld ID to assign the tech to.
        tech_name: Partial name match (case-insensitive). e.g. "Person019" or "Synthetic Person 007".
    """
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    # Paginate the full agent roster — the previous first-100-only call
    # silently false-negatived on any tech beyond the first page once the
    # team grew past that threshold.
    agents = _paginate_all("agents/?limit=100", cookie_hdr)

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


@with_recapture_retry
def merge_meld(destination_id: str, source_ids, meld_id=None, into_meld_id=None) -> dict:
    """Merge one or more source melds into a destination meld.

    Source melds are marked MANAGER_CANCELED with "(Merged)" in PM. All
    melds must be at the same unit/property.

    Args:
        destination_id: Destination meld ID — the meld that absorbs the sources.
        source_ids: List of source meld IDs (or a single id; coerced to list).
        meld_id, into_meld_id: DEPRECATED legacy kwargs from the pre-2026-05-19
            broken-shape API. If passed, treated as source=meld_id, destination=
            into_meld_id and warned in the result. Will be removed.

    Endpoint shape captured from PM web UI 2026-05-20T01:14:45Z (capture doc:
    orgs/ascendops/docs/pm-create-meld-in-and-merge-endpoint-capture-2026-05-19.md):

        POST /api/melds/{destination_id}/merge/
        body: { "destination_id": int, "source_ids": [int, ...] }
        response: 200 { "message": "Melds merged successfully" }

    Previous CLI shape (URL = source meld id, body = {"meld": dest_id}) was
    wrong from day one — URL semantic was flipped and body field name was
    wrong. Every prior CLI merge returned HTTP 400 "Destination Meld not found"
    because PM was treating our SOURCE id as the destination role.
    """
    # Legacy-arg compatibility shim — old callers passed (source, destination)
    # positionally. If destination_id looks like a source-meld id and into_meld_id
    # is provided, treat as legacy and swap.
    if meld_id is not None or into_meld_id is not None:
        # Legacy form: merge_meld(meld_id=source, into_meld_id=dest)
        legacy_source = meld_id if meld_id is not None else destination_id
        legacy_dest = into_meld_id if into_meld_id is not None else None
        if legacy_dest is None:
            raise ValueError("legacy merge_meld call missing into_meld_id")
        destination_id = legacy_dest
        source_ids = [legacy_source]

    destination_id = _validate_meld_id(destination_id)
    if not isinstance(source_ids, (list, tuple)):
        source_ids = [source_ids]
    if len(source_ids) == 0:
        raise ValueError("merge_meld requires at least one source_id")
    validated_sources = [_validate_meld_id(s) for s in source_ids]

    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    payload = json.dumps({
        "destination_id": destination_id,
        "source_ids": validated_sources,
    }).encode()
    req = urllib.request.Request(
        _build_url(f"melds/{destination_id}/merge/"),
        data=payload,
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
            raw = resp.read().decode("utf-8", errors="ignore")
            try:
                result = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                result = {"raw": raw[:500]}
        return {
            "ok": True,
            "destination_meld_id": destination_id,
            "source_meld_ids": validated_sources,
            "result": result,
        }
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        # Standard normalize-and-exit on errors. The earlier "Destination Meld
        # not found" 400 was a symptom of the wrong-shape body, not a real
        # destination-state constraint — the captured web UI payload works
        # against ANY meld state (PENDING_ASSIGNMENT, assigned, etc.).
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


@with_recapture_retry
def complete_meld(
    meld_id: str,
    completion_notes: Optional[str] = None,
    *,
    side: str = "manager",
    vendor_id: Optional[str] = None,
    completion_date: Optional[str] = None,
) -> dict:
    """Mark a meld complete. Side-aware: manager vs vendor PM uses different payload shapes.

    Manager surface (default): meld must be in PENDING_COMPLETION. Raises HTTP 403 otherwise.
        Payload: {completion_notes?: str}.

    Vendor surface: operator-on-behalf-of-vendor; pass vendor_id of the assigned vendor.
        Payload (verified capture 2026-05-16 024240Z):
            {is_complete: true, date: <ISO datetime>, reason: <str>}.
        Vendor-side requires `completion_date` (ISO 8601 datetime when the work
        was actually completed). `completion_notes` maps to `reason` in the
        PM request; it becomes the meld's `completion_notes` field in the
        response.

    Args:
        meld_id: Meld ID to mark complete.
        completion_notes: Optional completion notes. On vendor side this is
            sent as `reason` and is recommended for audit trail.
        side: "manager" (default) or "vendor".
        vendor_id: Required when side="vendor".
        completion_date: Required when side="vendor". ISO 8601 datetime, e.g.
            '2026-05-17T14:00:00.000Z'.
    """
    if side == "vendor":
        if not vendor_id:
            raise ValueError("vendor_id required when side='vendor'")
        if not completion_date:
            raise ValueError("completion_date required when side='vendor'")
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    if side == "vendor":
        payload: dict = {
            "is_complete": True,
            "date": completion_date,
            "reason": completion_notes or "",
        }
    else:
        payload = {}
        if completion_notes:
            payload["completion_notes"] = completion_notes

    result = _http_patch(
        f"melds/{meld_id}/complete/", payload, cookie_hdr, csrf_token,
        side=side, vendor_id=vendor_id,
    )
    return {"ok": True, "meld_id": meld_id, "completion_notes": completion_notes, "side": side, "result": result}


@with_recapture_retry
def create_work_entry(
    meld_id: str,
    *,
    agent: int,
    description: str,
    long_description: str = "",
    checkin: Optional[str] = None,
    checkout: Optional[str] = None,
    hours: Optional[float] = None,
) -> dict:
    """Create a new work entry on a meld (nested path).

    POST /api/melds/{meld_id}/work-entries/ — verified capture 2026-05-16
    025132Z (201 Created). Nested under meld, NOT top-level.

    Args:
        meld_id: Meld ID (int PK).
        agent: Agent persona_id (the maintenance person doing the work).
        description: Short summary of work performed (required).
        long_description: Longer notes (default empty).
        checkin: ISO 8601 datetime work started, e.g. '2026-05-16T02:52:00.000Z'.
        checkout: ISO 8601 datetime work ended.
        hours: Hours worked (float). If omitted PM may compute from checkin/checkout.
    """
    meld_id = _validate_meld_id(meld_id)
    payload: dict = {
        "agent": int(agent),
        "description": description,
        "long_description": long_description,
        "meld": meld_id,
    }
    if checkin is not None:
        payload["checkin"] = checkin
    if checkout is not None:
        payload["checkout"] = checkout
    if hours is not None:
        payload["hours"] = hours
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_post(f"melds/{meld_id}/work-entries/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "meld_id": meld_id, "entry_id": result.get("id") if isinstance(result, dict) else None, "result": result}


@with_recapture_retry
def vendor_accept_assignment(vendor_id: str, assignment_id: int) -> dict:
    """Vendor accepts an assignment request (vendor-side).

    PATCH /3287/v/{vendor_id}/api/assignments/{assignment_id}/accept/ —
    verified capture 2026-05-16 024240Z. Empty body.
    """
    if not vendor_id:
        raise ValueError("vendor_id is required")
    vendor_id = str(vendor_id)
    assignment_id = int(assignment_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_patch(
        f"assignments/{assignment_id}/accept/", {}, cookie_hdr, csrf_token,
        side="vendor", vendor_id=vendor_id,
    )
    return {"ok": True, "vendor_id": vendor_id, "assignment_id": assignment_id, "result": result}


@with_recapture_retry
def vendor_set_schedule(
    vendor_id: str,
    assignment_id: int,
    new_segments: list,
    *,
    segments_to_keep: Optional[list] = None,
    mark_scheduled: bool = False,
    appointments_required: int = 1,
) -> dict:
    """Vendor sets schedule segments on an assignment (vendor-side).

    PATCH /3287/v/{vendor_id}/api/assignments/{assignment_id}/segments/ —
    verified capture 2026-05-16 024240Z.

    Args:
        vendor_id: Vendor PK.
        assignment_id: Assignment request PK.
        new_segments: List of segments to add. Each segment is a dict with
            event.dtstart, event.dtend, event.type, event._cid. Helper
            accepts either fully-formed segments or simplified
            (dtstart, dtend) tuples/dicts and normalizes.
        segments_to_keep: Existing segment IDs to retain (default empty).
        mark_scheduled: PM flag — leave False unless echoing the web UI.
        appointments_required: Number of appointment windows needed (default 1).
    """
    if not vendor_id:
        raise ValueError("vendor_id is required")
    vendor_id = str(vendor_id)
    assignment_id = int(assignment_id)

    def _normalize_segment(seg, idx: int) -> dict:
        if isinstance(seg, dict) and "event" in seg:
            return seg
        if isinstance(seg, dict):
            event = {
                "dtstart": seg["dtstart"],
                "dtend": seg["dtend"],
                "type": seg.get("type", "default"),
                "_cid": seg.get("_cid", f"event_{idx}"),
            }
            return {"event": event}
        if isinstance(seg, (tuple, list)) and len(seg) == 2:
            return {"event": {"dtstart": seg[0], "dtend": seg[1], "type": "default", "_cid": f"event_{idx}"}}
        raise ValueError(f"Unsupported segment shape at index {idx}: {seg!r}")

    payload = {
        "segments_to_keep": segments_to_keep or [],
        "new_segments": [_normalize_segment(s, i) for i, s in enumerate(new_segments)],
        "mark_scheduled": mark_scheduled,
        "appointments_required": appointments_required,
    }
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_patch(
        f"assignments/{assignment_id}/segments/", payload, cookie_hdr, csrf_token,
        side="vendor", vendor_id=vendor_id,
    )
    return {"ok": True, "vendor_id": vendor_id, "assignment_id": assignment_id, "result": result}


@with_recapture_retry
def vendor_create_invoice(
    vendor_id: str,
    meld_id: str,
    line_items: list,
) -> dict:
    """Vendor creates a draft invoice on a meld (vendor-side).

    POST /3287/v/{vendor_id}/api/meld-invoices/ — verified capture
    2026-05-16 024240Z (201 Created). Returned in DRAFT status until
    vendor_submit_invoice is called.

    Args:
        vendor_id: Vendor PK.
        meld_id: Meld PK.
        line_items: List of dicts with quantity, unit_price (string), description.
            Optional _cid is auto-generated if omitted.
    """
    if not vendor_id:
        raise ValueError("vendor_id is required")
    vendor_id = str(vendor_id)
    meld_id = _validate_meld_id(meld_id)
    if not line_items:
        raise ValueError("line_items must contain at least one entry")

    normalized: list = []
    for i, item in enumerate(line_items):
        if not isinstance(item, dict):
            raise ValueError(f"line_item at index {i} must be a dict")
        normalized.append({
            "quantity": item.get("quantity", 1),
            "unit_price": str(item["unit_price"]),
            "description": item.get("description", ""),
            "_cid": item.get("_cid", f"line_item_{i}"),
        })

    payload = {"meld": meld_id, "invoice_line_items": normalized}
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_post(
        "meld-invoices/", payload, cookie_hdr, csrf_token,
        side="vendor", vendor_id=vendor_id,
    )
    return {
        "ok": True, "vendor_id": vendor_id, "meld_id": meld_id,
        "invoice_id": result.get("id") if isinstance(result, dict) else None,
        "result": result,
    }


@with_recapture_retry
def vendor_submit_invoice(vendor_id: str, invoice_id: int) -> dict:
    """Vendor submits a draft invoice to the manager (vendor-side).

    PATCH /3287/v/{vendor_id}/api/meld-invoices/{invoice_id}/ — verified
    capture 2026-05-16 024240Z. Sends {submit_to_manager: true}.
    """
    if not vendor_id:
        raise ValueError("vendor_id is required")
    vendor_id = str(vendor_id)
    invoice_id = int(invoice_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_patch(
        f"meld-invoices/{invoice_id}/", {"submit_to_manager": True},
        cookie_hdr, csrf_token,
        side="vendor", vendor_id=vendor_id,
    )
    return {"ok": True, "vendor_id": vendor_id, "invoice_id": invoice_id, "result": result}


@with_recapture_retry
def invite_vendor(
    *,
    email: str,
    first_name: str,
    last_name: str,
    company: str,
    line1: str,
    postcode: str,
    phone: str,
    state: str = "",
) -> dict:
    """Create a vendor and send the portal invite in one PM call.

    Captured 2026-05-31: POST /api/vendors/invite/ with an empty 201 body.
    Duplicate/in-use email returns HTTP 400 and is surfaced as ok:false,
    never as silent success.
    """
    payload = {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "name": company,
        "line_1": line1,
        "state": state or "",
        "postcode": postcode,
        "phone": phone,
    }
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_post_no_exit("vendors/invite/", payload, cookie_hdr, csrf_token)

    if isinstance(result, dict) and result.get("status_code") == 400:
        return {
            "ok": False,
            "error": "vendor email already exists or invite is already pending",
            "already_exists": True,
            "already_invited": True,
            "email": email,
            "detail": result,
        }
    if isinstance(result, dict) and isinstance(result.get("status_code"), int) and result["status_code"] >= 400:
        return {
            "ok": False,
            "error": "vendor invite failed",
            "email": email,
            "detail": result,
        }

    return {
        "ok": True,
        "email": email,
        "company": company,
        "result": result,
    }


@with_recapture_retry
def update_work_entry(
    entry_id: int,
    *,
    description: Optional[str] = None,
    long_description: Optional[str] = None,
    checkin: Optional[str] = None,
    checkout: Optional[str] = None,
    hours: Optional[float] = None,
    agent: Optional[int] = None,
) -> dict:
    """Person003 an existing work entry (top-level path, not nested).

    PATCH /api/melds/work-entries/{entry_id}/ — verified capture
    2026-05-16 25132Z. PM exposes the EDIT/DELETE paths at the top-level
    `/melds/work-entries/{id}/`, NOT under the meld (asymmetry rule —
    nested CREATE, top-level EDIT/DELETE).

    Sends partial payload of only the fields the caller passed. Capture
    shows PM also accepts the full echo shape, but partial works for the
    fields tested live. Switch to GET-then-overlay if a future smoke
    surfaces 400 on a partial PATCH.
    """
    entry_id = int(entry_id)
    payload: dict = {"id": entry_id}
    if description is not None:
        payload["description"] = description
    if long_description is not None:
        payload["long_description"] = long_description
    if checkin is not None:
        payload["checkin"] = checkin
    if checkout is not None:
        payload["checkout"] = checkout
    if hours is not None:
        payload["hours"] = hours
    if agent is not None:
        payload["agent"] = agent
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_patch(f"melds/work-entries/{entry_id}/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "entry_id": entry_id, "result": result}


@with_recapture_retry
def delete_work_entry(entry_id: int) -> dict:
    """Delete a work entry (top-level path).

    DELETE /api/melds/work-entries/{entry_id}/ — verified capture
    2026-05-16 030217Z (204 No Content). Asymmetry rule applies: this
    path is top-level, NOT /melds/{meld_id}/work-entries/{entry_id}/.
    """
    entry_id = int(entry_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    _http_delete(f"melds/work-entries/{entry_id}/", cookie_hdr, csrf_token)
    return {"ok": True, "entry_id": entry_id, "deleted": True}


@with_recapture_retry
def delete_meld_file(file_id: int) -> dict:
    """Delete a manager-uploaded meld file (top-level path).

    DELETE /api/melds/files/{file_id}/ — verified capture 2026-05-16
    030217Z (204 No Content). Top-level path, NOT
    /melds/{meld_id}/files/{file_id}/ (asymmetry rule — CREATE is nested
    at /melds/{meld_id}/files/, DELETE is top-level).
    """
    file_id = int(file_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    _http_delete(f"melds/files/{file_id}/", cookie_hdr, csrf_token)
    return {"ok": True, "file_id": file_id, "deleted": True}


@with_recapture_retry
def delete_project(project_id: int) -> dict:
    """Delete a project.

    DELETE /api/projects/{project_id}/ — verified capture 2026-05-16
    030217Z (204 No Content). Cascade behavior on linked melds is PM's
    responsibility; melds typically survive project deletion.
    """
    project_id = int(project_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    _http_delete(f"projects/{project_id}/", cookie_hdr, csrf_token)
    return {"ok": True, "project_id": project_id, "deleted": True}


@with_recapture_retry
def hold_meld_invoice(invoice_id: int, reason: str) -> dict:
    """Place a meld invoice on hold pending vendor change.

    PATCH /api/meld-invoices/{invoice_id}/hold/ — verified capture
    2026-05-16 030217Z. Payload: {reason: str}. Used when the manager
    wants the vendor to revise the invoice before approval.
    """
    if not reason:
        raise ValueError("reason is required for hold_meld_invoice")
    invoice_id = int(invoice_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_patch(
        f"meld-invoices/{invoice_id}/hold/", {"reason": reason},
        cookie_hdr, csrf_token,
    )
    return {"ok": True, "invoice_id": invoice_id, "reason": reason, "result": result}


@with_recapture_retry
def decline_meld_invoice(invoice_id: int, reason: str) -> dict:
    """Decline a meld invoice outright (vendor must resubmit).

    PATCH /api/meld-invoices/{invoice_id}/decline/ — verified capture
    2026-05-16 030217Z. Payload: {reason: str}. Stronger action than
    hold; signals the work itself is rejected.
    """
    if not reason:
        raise ValueError("reason is required for decline_meld_invoice")
    invoice_id = int(invoice_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_patch(
        f"meld-invoices/{invoice_id}/decline/", {"reason": reason},
        cookie_hdr, csrf_token,
    )
    return {"ok": True, "invoice_id": invoice_id, "reason": reason, "result": result}


@with_recapture_retry
def cancel_meld(meld_id: str, reason: Optional[str] = None) -> dict:
    """Cancel a meld from the manager side.

    Args:
        meld_id: Meld ID to cancel.
        reason: Cancellation reason (recommended for audit trail).
    """
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    payload: dict = {}
    if reason:
        payload["manager_cancellation_reason"] = reason
    result = _http_patch(f"melds/{meld_id}/cancel/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "meld_id": meld_id, "reason": reason, "result": result}


@with_recapture_retry
def schedule_appointment(meld_id: str, dtstart: str, duration_hours: float = 2.0) -> dict:
    """Schedule an in-house tech appointment window on a meld.

    Args:
        meld_id: Meld ID.
        dtstart: ISO 8601 datetime string, e.g. '2026-04-27T14:00:00-04:00'.
        duration_hours: Appointment duration in hours (default 2).

    The meld must have an in-house tech assigned — PM creates the managementappointment
    object at assignment time. This sets the availability_segment (the actual time window).
    """
    meld_id = _validate_meld_id(meld_id)
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
            "meld": meld_id,
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


# Minimum digits required in the search needle before we attempt a
# phone-number match. Below this threshold a stray digit in a name-shaped
# query would match every tenant whose phone happens to contain it, which
# is almost never what the user wants. Four digits is a sensible floor —
# enough specificity to be intentional (e.g. last-4 of a phone), short
# enough to still be ergonomic.
_PHONE_DIGIT_FLOOR = 4


def _digits_only(s: str) -> str:
    """Return only ASCII digits from ``s`` (empty string for None/non-str)."""
    if not s:
        return ""
    return "".join(ch for ch in str(s) if ch.isdigit())


def _filter_tenants(tenants: list, search: str) -> list:
    """Apply the tenant search predicate to a list of (flat) tenant dicts.

    See ``list_tenants`` docstring for the full semantics. Split out as a
    pure function so it can be unit-tested without mocking the HTTP layer.
    """
    needle = (search or "").lower().strip()
    if not needle:
        return list(tenants)
    needle_digits = _digits_only(needle)
    phone_eligible = len(needle_digits) >= _PHONE_DIGIT_FLOOR

    matches = []
    for t in tenants:
        first = (t.get("first_name") or "").lower()
        last = (t.get("last_name") or "").lower()
        # Collapse runs of whitespace — real PM data has tenants with
        # trailing-space first_names (e.g. "Resident " / "Person039") that would
        # otherwise produce "erica  mapp" and miss a "resident beta" needle.
        full_name = " ".join(f"{first} {last}".split())
        email = (t.get("email") or "").lower()
        if needle in full_name or needle in email:
            matches.append(t)
            continue
        if phone_eligible:
            stored_digits = _digits_only(t.get("phone"))
            if stored_digits and needle_digits in stored_digits:
                matches.append(t)
    return matches


def list_tenants(search: Optional[str] = None, limit: int = 100) -> list:
    """List tenants, optionally filtered client-side by name, email, or phone.

    The /api/tenants/ list response is FLAT — phone is a top-level ``phone``
    string (e.g. ``"(202) 555-0106"``) and email is top-level ``email``.
    There is NO nested ``contact`` or ``user`` object on the list shape (the
    detail endpoint ``/api/tenants/{id}/`` does return the nested objects;
    see ``get_tenant``).

    Search semantics (case-insensitive):
      * Name: matched against the combined ``"first_name last_name"`` string
        so multi-word queries like ``"Resident Beta"`` work.
      * Email: substring match against top-level ``email``.
      * Phone: BOTH the needle and the stored phone are normalized to
        digits-only before substring match. The phone branch only fires
        when the needle contains at least 4 digits, to avoid trivial
        matches from name-shaped queries that happen to contain a digit
        or two.

    Args:
        search: Case-insensitive substring matched against name / email /
            (digits-normalized) phone as described above.
        limit: Maximum number of results to return (after client-side filter).
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    # Fetch tenants. The API does not support server-side name/email
    # filtering on this endpoint, so search has to happen client-side.
    # When search is set we must paginate the full roster — the previous
    # `limit * 3` cap could miss matches that live on later pages of a
    # large tenant list. Without search, stop after the requested limit.
    page_size = 200
    if search:
        results = _paginate_all(f"tenants/?limit={page_size}", cookie_hdr)
        results = _filter_tenants(results, search)
        return results[:limit]

    results: list = []
    page = _http_get(f"tenants/?limit={page_size}", cookie_hdr)
    results.extend(page.get("results", []))
    while page.get("next") and len(results) < limit:
        next_url = page["next"].split("/api/")[-1]
        page = _http_get(next_url, cookie_hdr)
        results.extend(page.get("results", []))
    return results[:limit]


@with_recapture_retry
def get_tenant(tenant_id) -> dict:
    """GET /api/tenants/{tenant_id}/ — full tenant object with nested contact/user/address."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _http_get(f"tenants/{tenant_id}/", cookie_hdr)


@with_recapture_retry
def update_unit_notes(unit_id, maintenance_notes: str) -> dict:
    """Update the maintenance_notes on a unit.

    PATCH /api/units/{unit_id}/ with {"maintenance_notes": "<text>"} — verified
    shape from pm-capture 2026-05-14T03:07:08 (status 200).

    Unit-level notes capture per-unit quirks (water-shutoff location, breaker
    panel access, parking quirks) that surface to vendors/techs on every meld
    for that unit. Distinct from meld-level maintenance_notes (handled by
    update_meld_notes) and from any future property-level notes (not yet
    proven by capture as of P3 #8 ship).

    Closes P3 #8 (orgs/ascendops/docs/pm-cli-gap-backlog-2026-05-18.md) at
    the unit level. Property-level notes deferred pending HAR proof.
    """
    unit_id_int = int(unit_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    payload = {"maintenance_notes": maintenance_notes}
    result = _http_patch(f"units/{unit_id_int}/", payload, cookie_hdr, csrf_token)
    return {
        "ok": True,
        "unit_id": unit_id_int,
        "maintenance_notes": result.get("maintenance_notes", maintenance_notes),
        "result": result,
    }


@with_recapture_retry
def update_tenant_notes(tenant_id, notes: str) -> dict:
    """Update the notes field on a tenant.

    Endpoint: PATCH /api/tenants/{tenant_id}/ with the FULL tenant body, mutating
    only `notes`. Verified shape from pm-tenant-notes-endpoint-capture-2026-05-18
    (HAR capture against tenant 9000014, status 200, round-trip-reverted).

    The endpoint is NOT thin-patch — `{"notes": "..."}` alone returns 400 because
    validators run on `first_name` / `last_name` even when not changed. We GET
    the full tenant, mutate `notes`, and PATCH the full body back.

    Field name is `notes`, NOT `maintenance_notes` — distinct from unit-level
    (`/api/units/{id}/` with `maintenance_notes`) and meld-level
    (`/api/v2/melds/{id}/notes/`). This is the canonical surface for
    resident-level recallable context (preferences, schedule, access constraints)
    that should travel with the resident across melds.
    """
    tenant_id_int = int(tenant_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    current = _http_get(f"tenants/{tenant_id_int}/", cookie_hdr)
    if not isinstance(current, dict):
        raise RuntimeError(
            f"GET tenants/{tenant_id_int}/ returned non-dict (cannot full-body-echo)"
        )
    current["notes"] = notes
    result = _http_patch(f"tenants/{tenant_id_int}/", current, cookie_hdr, csrf_token)
    return {
        "ok": True,
        "tenant_id": tenant_id_int,
        "notes": result.get("notes", notes) if isinstance(result, dict) else notes,
        "result": result,
    }


@with_recapture_retry
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
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)

    # Paginate each photo source — capping at the first page silently
    # under-counts when a meld has >100 manager/tenant/vendor uploads. The
    # pre-complete audit hook treats this list as authoritative for the
    # photo-presence gate, so an under-count would let a meld through
    # without proof of work in edge cases.
    sources = [
        ("manager", f"melds/{meld_id}/files/?limit=100"),
        ("tenant",  f"melds/{meld_id}/tenant-files/?limit=100"),
        ("vendor",  f"melds/{meld_id}/vendor-files/?limit=100"),
    ]

    merged: list = []
    for role, path in sources:
        for item in _paginate_all(path, cookie_hdr):
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


def _list_photo_source(meld_id: str, endpoint: str, role: str, optional: bool = False) -> tuple[list, Optional[str]]:
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    path = f"melds/{meld_id}/{endpoint}/?limit=100"
    note = None
    if optional:
        # The optional path may legitimately 403 (tenant/vendor endpoints
        # without elevated session); preserve its single-page note semantics
        # but still walk pagination on success so the inspect aggregator
        # never silently truncates above 100 items.
        items_first, note = _http_get_optional_results(path, cookie_hdr, role)
        if note is not None:
            items: list = items_first if isinstance(items_first, list) else []
        else:
            items = _paginate_all(path, cookie_hdr)
    else:
        items = _paginate_all(path, cookie_hdr)

    tagged: list = []
    for item in items:
        if isinstance(item, dict):
            item = dict(item)
            item["uploader_role"] = role
        tagged.append(item)
    return tagged, note


def inspect_meld(meld_id: str) -> dict:
    """Aggregate meld detail, photos, notes, work entries, and comments."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    meld = _http_get(f"melds/{meld_id}/", cookie_hdr)

    manager_photos, _ = _list_photo_source(meld_id, "files", "manager")
    tenant_photos, tenant_note = _list_photo_source(meld_id, "tenant-files", "tenant", optional=True)
    vendor_photos, vendor_note = _list_photo_source(meld_id, "vendor-files", "vendor", optional=True)

    result = {
        "meld": meld,
        "photos": {
            "manager": manager_photos,
            "tenant": tenant_photos,
            "vendor": vendor_photos,
        },
        "notes": {
            "completion_notes": meld.get("completion_notes"),
            "maintenance_notes": meld.get("maintenance_notes"),
            "work_entries": list_work_entries(meld_id),
            "comments": get_comments(meld_id),
        },
    }
    if tenant_note:
        result["photos"]["tenant_note"] = tenant_note
    if vendor_note:
        result["photos"]["vendor_note"] = vendor_note
    return result


@with_recapture_retry
def assign_vendor(meld_id: str, vendor_id: str, account_prefix: str = "1") -> dict:
    """Assign an external vendor to a meld by numeric ID.

    Args:
        meld_id: Meld ID.
        vendor_id: Vendor ID.
        account_prefix: Account prefix for composite_id (default "1").
    """
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    vendor_obj = {
        "type": "Vendor",
        "id": int(vendor_id),
        "composite_id": f"{account_prefix}-{vendor_id}",
    }

    result = _http_patch(
        f"melds/{meld_id}/assign-maintenance/",
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


@with_recapture_retry
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
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)

    # Paginate the full vendor book; previously capped at first 100.
    vendors = _paginate_all("vendors/?limit=100", cookie_hdr)

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
# PM API endpoints expect the integer PK (e.g. 90000014), not the human-facing
# short code (e.g. 'T5LKWTDB'). Passing a short code returns HTTP 404 because the
# Django URL pattern is <int:pk>. This guard catches the mismatch at the SDK
# boundary instead of at the API boundary, so the error is actionable. Applied
# to all meld-path functions in this module so callers fail fast with an
# actionable error instead of surfacing a PM 404 from the Django <int:pk> path.
def _validate_meld_id(meld_id) -> int:
    """Coerce meld_id to int PK. Reject PM short codes.

    Raises ValueError on short-code input (e.g. 'T5LKWTDB') with a message
    pointing at `pm work-orders list` for resolution.
    """
    try:
        return int(meld_id)
    except (TypeError, ValueError):
        raise ValueError(
            f"meld_id must be the integer PK (e.g. 90000014), got {meld_id!r}. "
            f"PM short codes (e.g. 'T5LKWTDB') are not accepted; use 'pm work-orders list' to find the int PK."
        )


@with_recapture_retry
def schedule_vendor_appointment(meld_id: str, vendor_id: str, dtstart: str, duration_hours: float = 2.0) -> dict:
    """Schedule an external vendor appointment window on a meld.

    Args:
        meld_id: Meld ID.
        vendor_id: Vendor ID (the integer PK from PM, e.g. 91195 for Dyer HVAC).
        dtstart: ISO 8601 datetime string, e.g. '2026-04-27T14:00:00-04:00'.
        duration_hours: Appointment duration in hours (default 2).

    Live-PM shape (verified against pm-dev 2026-05-13):
      - meld.vendor_assignment_requests[] holds vendor identity:
          {id, vendor: {id, name, ...}, accepted, rejected, canceled, ...}
      - meld.vendorappointment[] holds the appointment objects to PUT to:
          {id, meld, assignment_request, availability_segment, ...}
      - assignment_request on an appointment links to vendor_assignment_requests.id.

    To resolve vendor_id → appointment_id:
      1. Find the request in vendor_assignment_requests whose vendor.id matches
         and which is accepted (rejected/canceled both null).
      2. Find the appointment in vendorappointment whose assignment_request
         matches that request.id.

    Falls back to the first appointment on the meld when no exact vendor match
    is found, mirroring the prior multi-vendor fallback behavior.
    """
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    # Get the vendor appointment from the meld (live API uses `vendorappointment`,
    # NOT the legacy `vendorassignment` field that earlier mocks referenced).
    meld = _http_get(f"melds/{meld_id}/", cookie_hdr)
    # added 2026-04-29 by collie via dane dispatch — Wave 1.5 meld_state_change instrumentation per UU TODO
    # Reuse the existing meld fetch above to capture prior_state — no extra GET needed.
    prior_state = "unknown"
    if isinstance(meld, dict):
        prior_state = str(meld.get("status") or meld.get("state") or "unknown")

    appointments = meld.get("vendorappointment", []) or []
    requests_list = meld.get("vendor_assignment_requests", []) or []
    if not appointments:
        return {"ok": False, "error": "No vendor appointment found on this meld"}

    # Build a map of request_id -> vendor.id from accepted (non-rejected, non-canceled) requests.
    req_to_vendor: dict = {}
    for req in requests_list:
        if not isinstance(req, dict):
            continue
        if req.get("rejected") is not None or req.get("canceled") is not None:
            continue
        rid = req.get("id")
        vendor = req.get("vendor")
        vid = vendor.get("id") if isinstance(vendor, dict) else None
        if rid is not None and vid is not None:
            req_to_vendor[rid] = vid

    # Find the appointment whose linked assignment_request belongs to vendor_id.
    appt_id = None
    request_id = None
    for appt in appointments:
        if not isinstance(appt, dict):
            continue
        linked_req = appt.get("assignment_request")
        if linked_req is not None and str(req_to_vendor.get(linked_req)) == str(vendor_id):
            appt_id = appt.get("id")
            request_id = linked_req
            break

    if appt_id is None:
        # Fallback: use the first appointment on the meld (preserves multi-vendor
        # behavior from the legacy mock-driven implementation).
        first = next((a for a in appointments if isinstance(a, dict) and a.get("id") is not None), None)
        if first:
            appt_id = first.get("id")
            request_id = first.get("assignment_request")

    if appt_id is None:
        return {"ok": False, "error": f"Vendor {vendor_id} not assigned to this meld"}

    if request_id is None:
        return {"ok": False, "error": f"Could not resolve assignment_request id for vendor {vendor_id}"}

    # Compute dtend from dtstart + duration. The captured payload uses dtstart +
    # dtend (no "duration" key) — we mirror that. Try to parse dtstart as ISO 8601;
    # fall back to a naive string-concat if parsing fails (caller can also pass
    # dtend directly via a future kwarg if needed).
    from datetime import datetime, timedelta
    try:
        # Python's fromisoformat handles "+04:00" since 3.11; pad "Z" to "+00:00".
        start_dt = datetime.fromisoformat(dtstart.replace("Z", "+00:00"))
        end_dt = start_dt + timedelta(hours=duration_hours)
        dtend = end_dt.isoformat()
    except Exception:
        dtend = dtstart  # let PM reject if the shape is wrong

    # PATCH /api/assignments/{assignment_request_id}/segments/ — verified shape
    # from 2nd pm-capture 2026-05-13. The id targeted is the
    # vendor_assignment_request.id (NOT the vendorappointment.id). Payload uses
    # multiple_segments_to_book[{event:{dtstart,dtend}}], NOT availability_segment.
    payload = {
        "mark_scheduled": True,
        "segments_to_keep": [],
        "new_segments": [],
        "multiple_segments_to_book": [
            {"event": {"dtstart": dtstart, "dtend": dtend}}
        ],
    }
    result = _http_patch(f"assignments/{request_id}/segments/", payload, cookie_hdr, csrf_token)
    # added 2026-04-29 by collie via dane dispatch — Wave 1.5 meld_state_change instrumentation per UU TODO
    _emit_meld_state_change(
        meld_id, prior_state, "scheduled", "scheduled_vendor_appointment",
        vendor_id=int(vendor_id), assignment_id=request_id,
        dtstart=dtstart, triggered_by="manager",
    )
    return {
        "ok": True,
        "meld_id": meld_id,
        "vendor_id": vendor_id,
        "assignment_request_id": request_id,
        "appointment_id": appt_id,
        "dtstart": dtstart,
        "dtend": dtend,
        "duration_hours": duration_hours,
        "result": result,
    }


@with_recapture_retry
def list_projects(meld_id: Optional[str] = None, limit: int = 100) -> list:
    """List projects associated with a meld or account."""
    if meld_id is not None:
        meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    path = f"melds/{meld_id}/projects/" if meld_id else f"projects/?limit={limit}"
    data = _http_get(path, cookie_hdr)
    return data.get("results", data) if isinstance(data, dict) else data


@with_recapture_retry
def get_project(project_id: str) -> dict:
    """Get a single project by ID."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _http_get(f"projects/{project_id}/", cookie_hdr)


@with_recapture_retry
def create_project(
    name: str,
    project_type: str,
    due_date: str,
    start_date: str,
    coordinators: list,
    unit: dict,
    description: str = "",
    meld_location: str = "Unit",
    prop: Optional[dict] = None,
) -> dict:
    """Create a new project at the top level.

    POST /api/projects/ — verified shape from pm-capture 2026-05-13 (2nd
    session). The 5/05 spike's `unit` mystery is now solved: the field
    is a {"id": int, "label": str} dict, NOT the full unit object.

    Required (per capture):
        name, project_type (e.g. "TURN"), due_date, start_date,
        coordinators (list of management-agent int ids, e.g. [90025]),
        unit ({"id": int, "label": str}).

    `meld_location` is a new field introduced in the live shape (captured
    value: "Unit"). `prop` is null when meld_location="Unit"; set when
    binding a project to a property instead of a unit.
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    payload = {
        "name": name,
        "project_type": project_type,
        "description": description,
        "due_date": due_date,
        "start_date": start_date,
        "coordinators": [int(c) for c in coordinators],
        "meld_location": meld_location,
        "prop": prop,
        "unit": unit,
    }
    result = _http_post("projects/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "project_id": result.get("id"), "result": result}


@with_recapture_retry
def update_project(
    project_id: str,
    name: Optional[str] = None,
    project_type: Optional[str] = None,
    description: Optional[str] = None,
    due_date: Optional[str] = None,
    start_date: Optional[str] = None,
    coordinators: Optional[list] = None,
    meld_location: Optional[str] = None,
    prop: Optional[dict] = None,
    unit: Optional[dict] = None,
) -> dict:
    """Person003 a top-level project.

    PATCH /api/projects/{project_id}/ — verified shape from pm-capture
    2026-05-13 (2nd session) AND live re-smoke 2026-05-14.

    PM PATCH on projects requires the FULL payload echo (project_type +
    coordinators + unit + name + start_date + due_date), not a delta.
    Sending only changed fields returns HTTP 400 with field-required
    errors for the missing keys (verified live 2026-05-14 03:33Z).

    To make caller ergonomics partial-style: fetch the project first, then
    overlay only the fields the caller explicitly set, then PATCH the
    full merged payload. Pre-existing fields come from the live record
    so we don't lose state across edits.
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    current = _http_get(f"projects/{project_id}/", cookie_hdr)
    if not isinstance(current, dict):
        return {"ok": False, "error": "could not fetch current project state for full-payload echo"}

    def _coordinator_id(c):
        if isinstance(c, dict):
            return c.get("id")
        return int(c)

    def _unit_echo(u):
        if not isinstance(u, dict):
            return u
        label = u.get("label")
        if not label:
            disp = u.get("display_address")
            if isinstance(disp, dict):
                label = disp.get("line_1")
        return {"id": u.get("id"), "label": label or ""}

    payload: dict = {
        "name": name if name is not None else current.get("name"),
        "project_type": project_type if project_type is not None else current.get("project_type"),
        "description": description if description is not None else (current.get("description") or ""),
        "due_date": due_date if due_date is not None else current.get("due_date"),
        "start_date": start_date if start_date is not None else current.get("start_date"),
        "coordinators": (
            [int(c) for c in coordinators] if coordinators is not None
            else [c_id for c_id in (_coordinator_id(c) for c in (current.get("coordinators") or [])) if c_id is not None]
        ),
        "meld_location": meld_location if meld_location is not None else (current.get("meld_location") or "Unit"),
        "prop": prop if prop is not None else current.get("prop"),
        "unit": unit if unit is not None else _unit_echo(current.get("unit")),
    }

    result = _http_patch(f"projects/{project_id}/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "project_id": project_id, "result": result}


@with_recapture_retry
def update_meld_notes(meld_id: str, maintenance_notes: str) -> dict:
    """Update the maintenance notes on a meld.

    PATCH /api/v2/melds/{meld_id}/notes/ — verified shape from pm-capture
    2026-05-13. Note the /api/v2/ prefix (NOT /api/). Body is simply
    {"maintenance_notes": "<text>"}.

    This is the maintenance-side notes field — distinct from
    completion_notes (per fleet memory feedback_pm_two_note_fields).
    """
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    # The /api/v2/ prefix is on a separate base path. The existing helper
    # uses BASE/api/ — we need to override the path explicitly here. The
    # simplest approach: pass the v2 path as a prefixed string and let
    # _http_patch use the BASE/api/ joiner. But _http_patch joins onto
    # /api/, so we just include "v2/..." as the path argument.
    result = _http_patch(f"v2/melds/{meld_id}/notes/", {"maintenance_notes": maintenance_notes}, cookie_hdr, csrf_token)
    return {"ok": True, "meld_id": meld_id, "result": result}


@with_recapture_retry
def add_melds_to_project(project_id: str, meld_ids: list) -> dict:
    """Attach one or more existing melds to a project.

    PUT /api/projects/{project_id}/add-melds/ — verified shape from
    pm-capture 2026-05-13 (manager UI multi-select on the project page).

    Request body:
        {"melds": [{"project": "<project_id>", "id": <meld_int>}, ...]}

    The "project" key inside each meld element is a STRING (PM serializes
    it that way in the manager-UI payload). The id is an int meld PK.
    """
    if not meld_ids:
        return {"ok": False, "error": "no meld_ids provided"}
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    payload = {
        "melds": [
            {"project": str(project_id), "id": _validate_meld_id(mid)}
            for mid in meld_ids
        ]
    }
    result = _http_put(f"projects/{project_id}/add-melds/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "project_id": project_id, "result": result}


@with_recapture_retry
def get_unit(unit_id) -> dict:
    """GET /api/units/{unit_id}/ — full unit object with nested prop/display_address/current_tenants."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _http_get(f"units/{unit_id}/", cookie_hdr)


@with_recapture_retry
def get_management_agent(agent_id) -> dict:
    """GET /api/agents/{agent_id}/ — full ManagementAgent object (maintenance/coordinator role)."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _http_get(f"agents/{agent_id}/", cookie_hdr)


@with_recapture_retry
def list_agents() -> list:
    """GET /api/agents/ — list all in-house technicians (management-agent roster).

    Fetches all pages via DRF `next` links (not just page 1), so larger rosters
    do not silently truncate.

    Use for: roster lookup by name when pm vendors search misses. Closes the
    silent misread class where in-house techs (Person019 / Person030 / Person017 / etc)
    get mistaken for missing vendors.
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _paginate_all("agents/?limit=100", cookie_hdr)


@with_recapture_retry
def list_work_orders_rich(limit: int = 25, status: Optional[str] = None) -> list:
    """GET /api/melds/ via cookie-auth — 68-field rich shape including tenants,
    in_house_servicers, vendor_assignment_requests, managementappointment.

    Nexus /api/v2/meld/ returns a 43-field shallow shape that OMITS the nested
    relations (tenants[], in_house_servicers[], etc). When a caller needs to
    filter on or read those relations from a list call, route through here
    instead of Nexus to avoid per-meld GET fan-out.

    Current callers:
      * api_backend.list_work_orders --no-tenant-linked (Gap #23) — Nexus
        server-side filter has wrong semantic (uses has_registered_tenant=False
        instead of len(tenants)==0). Delegating to cookie-path returns the
        tenants[] field so we can post-filter client-side. REMOVE this delegation
        once PM fixes the server-side predicate OR exposes tenants in Nexus list.

    Args:
        limit: maximum results to fetch from the cookie API (single page).
        status: optional status slug, mirrors api_backend semantics where
            "open" maps to the three PENDING_* states (OR'd via repeated params).

    Note: cookie-API page size caps at 100. Callers needing >100 results should
    paginate via _paginate_all in a future iteration; this helper does a single
    page fetch sized to the requested limit (clamped to 100).
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    page_limit = min(max(limit, 1), 100)
    params: list[tuple[str, str]] = [("limit", str(page_limit))]
    if status:
        slug_to_states = {
            "open": [
                "PENDING_ASSIGNMENT",
                "PENDING_VENDOR",
                "PENDING_MORE_MANAGEMENT_AVAILABILITY",
            ],
            "pending": ["PENDING_VENDOR"],
            "completed": ["COMPLETED"],
            "canceled": ["MANAGER_CANCELED"],
        }
        for s in slug_to_states.get(status.lower(), [status]):
            params.append(("status", s))
    path = "melds/?" + urllib.parse.urlencode(params)
    result = _http_get(path, cookie_hdr)
    if isinstance(result, list):
        return result
    return result.get("results", [])


@with_recapture_retry
def get_work_order_rich(meld_id: str) -> dict:
    """GET /api/melds/{id}/ via cookie-auth for rich assignment fields."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    result = _http_get(f"melds/{meld_id}/", cookie_hdr)
    return result if isinstance(result, dict) else {}


@with_recapture_retry
def list_all_maintenance(registered_only: bool = False) -> list:
    """GET /api/all-maintenance/ — 26-key roster shape with composite_id + type.

    This differs from GET /api/agents/{id}/ which returns a 25-key shape with
    `role` and missing `composite_id`/`type`. Standalone POST /api/melds/
    expects the all-maintenance shape.
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    flag = "true" if registered_only else "false"
    path = f"all-maintenance/?registeredOnly={flag}"
    result = _http_get(path, cookie_hdr)
    return result if isinstance(result, list) else result.get("results", [])


# Required nested keys on the fully-hydrated unit object PM expects on
# /list-create-meld/. Surface-validated pre-wire so operators get a clear
# error instead of an HTTP 500 downstream.
_UNIT_REQUIRED_KEYS = ("display_address", "prop", "current_tenants")
# Required nested keys on each ManagementAgent element. Same rationale.
_MAINT_REQUIRED_KEYS = ("selected_property_groups", "denormalized_property_groups", "department")
# Required nested keys on each Tenant element. Same rationale.
_TENANT_REQUIRED_KEYS = ("contact", "default_language", "notification_settings")
# Keys allowed on a "stripped" placeholder dict — anything beyond this set
# AND missing required keys is treated as a partially-built object (error).
_STRIPPED_KEYS = {"id", "type", "composite_id"}


def _is_stripped(obj: dict) -> bool:
    return isinstance(obj, dict) and "id" in obj and set(obj.keys()).issubset(_STRIPPED_KEYS)


def _hydrate_unit(unit) -> dict:
    """Auto-hydrate a stripped {"id": N} unit; pass full objects through; raise on partial."""
    if not isinstance(unit, dict):
        raise ValueError(f"unit must be a dict, got {type(unit).__name__}")
    if _is_stripped(unit):
        return get_unit(unit["id"])
    missing = [k for k in _UNIT_REQUIRED_KEYS if k not in unit]
    if missing:
        raise ValueError(
            f"unit object is missing required nested keys: {missing}. "
            "Pass --unit-id to auto-hydrate, or supply a full object including "
            "display_address, prop, current_tenants."
        )
    return unit


def _hydrate_maintenance_element(elem) -> dict:
    """Auto-hydrate a stripped {"id": N} ManagementAgent; full pass-through; raise on partial."""
    if not isinstance(elem, dict):
        raise ValueError(f"maintenance element must be a dict, got {type(elem).__name__}")
    if _is_stripped(elem):
        return get_management_agent(elem["id"])
    missing = [k for k in _MAINT_REQUIRED_KEYS if k not in elem]
    if missing:
        raise ValueError(
            f"maintenance element is missing required nested keys: {missing}. "
            "Pass --maintenance-id to auto-hydrate, or supply a full ManagementAgent "
            "object including selected_property_groups, denormalized_property_groups, department."
        )
    return elem


def _hydrate_tenant_element(elem) -> dict:
    """Auto-hydrate a stripped {"id": N} Tenant; full pass-through; raise on partial."""
    if not isinstance(elem, dict):
        raise ValueError(f"tenant element must be a dict, got {type(elem).__name__}")
    if _is_stripped(elem):
        return get_tenant(elem["id"])
    missing = [k for k in _TENANT_REQUIRED_KEYS if k not in elem]
    if missing:
        raise ValueError(
            f"tenant element is missing required nested keys: {missing}. "
            "Pass --tenant-id to auto-hydrate, or supply a full Tenant object "
            "including contact, default_language, notification_settings."
        )
    return elem


def _hydrate_maintenance_for_meld_create(elem, all_maintenance: Optional[list] = None) -> dict:
    """Hydrate maintenance for POST /api/melds/ standalone create.

    Distinct from _hydrate_maintenance_element: /api/melds/ expects objects from
    /api/all-maintenance/ (includes composite_id + type), not /api/agents/{id}/.
    """
    if not isinstance(elem, dict):
        raise ValueError(f"maintenance element must be a dict, got {type(elem).__name__}")
    if _is_stripped(elem):
        target_id = elem["id"]
        try:
            target_id_int: Any = int(target_id)
        except (TypeError, ValueError):
            target_id_int = target_id
        roster = all_maintenance if all_maintenance is not None else list_all_maintenance(registered_only=False)
        for m in roster:
            if not isinstance(m, dict):
                continue
            try:
                if int(m.get("id", 0)) == target_id_int:
                    return m
            except (TypeError, ValueError):
                if m.get("id") == target_id_int:
                    return m
        raise ValueError(f"maintenance id {target_id} not found in /api/all-maintenance/ roster")
    if "composite_id" not in elem or "type" not in elem:
        raise ValueError(
            "maintenance element for standalone create-meld must include composite_id + type. "
            "Pass --maintenance-id to auto-hydrate via /api/all-maintenance/, or supply a full object from that endpoint."
        )
    return elem


@with_recapture_retry
def create_meld_in_project(
    project_id: str,
    brief_description: str,
    description: str,
    work_category: str,
    work_type: str,
    due_date: str,
    unit,
    maintenance,
    work_location: str = "",
    tenants: Optional[list] = None,
    priority: str = "LOW",
    permission_to_enter: bool = True,
    tenant_presence_required: bool = False,
    notify_tenants: bool = True,
    notify_owner: bool = False,
    has_pets: bool = False,
    pets: str = "",
    tags: Optional[list] = None,
) -> dict:
    """Create a new meld INSIDE an existing project.

    POST /api/projects/{project_id}/list-create-meld/ — verified shape from
    pm-capture 2026-05-13 and 2026-05-16.

    PM requires FULLY HYDRATED unit, ManagementAgent, and tenant objects
    (30+ fields each, including nested prop/display_address/current_tenants on
    unit; selected_property_groups/agent_preferences/etc on maintenance; and
    contact/default_language/notification_settings on tenants). Stripped
    {"id": N} objects pass validation but 500 downstream.

    Callers may pass either:
      - A stripped {"id": N} dict → auto-hydrated via GET /units/{id}/ or
        GET /agents/{id}/ before the POST.
      - A fully-hydrated object → passed through unchanged.
      - A partially-built dict (id + a few keys, but missing required ones)
        for unit/maintenance/tenant →
        ValueError raised pre-wire with the missing keys named.

    The manager-UI payload uses string-typed "notify_owners_string" /
    "notify_tenants_string" alongside the boolean fields; the captured run
    sent both. We mirror that shape verbatim.
    """
    unit_obj = _hydrate_unit(unit)

    if isinstance(maintenance, list):
        maintenance_list = [_hydrate_maintenance_element(m) for m in maintenance]
    else:
        maintenance_list = [_hydrate_maintenance_element(maintenance)]
    tenants_list = [_hydrate_tenant_element(t) for t in (tenants or [])]

    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    payload = {
        "notify_owners_string": "true" if notify_owner else "false",
        "notify_tenants_string": "true" if notify_tenants else "false",
        "project": str(project_id),
        "tenant_presence_required": tenant_presence_required,
        "permission_to_enter": permission_to_enter,
        "due_date": due_date,
        "work_category": work_category,
        "work_type": work_type,
        "description": description,
        "brief_description": brief_description,
        "work_location": work_location,
        "maintenance": maintenance_list,
        "tags": tags or [],
        "has_pets": has_pets,
        "notify_tenants": notify_tenants,
        "priority": priority,
        "tenants": tenants_list,
        "pets": pets,
        "unit": unit_obj,
        "notify_owner": notify_owner,
    }
    result = _http_post(f"projects/{project_id}/list-create-meld/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "project_id": project_id, "meld_id": result.get("id"), "result": result}


@with_recapture_retry
def create_meld(
    brief_description: str,
    description: str,
    work_category: str,
    work_type: str,
    due_date: Optional[str],
    unit,
    maintenance,
    work_location: str = "",
    tenants: Optional[list] = None,
    priority: str = "LOW",
    permission_to_enter: bool = True,
    tenant_presence_required: bool = False,
    notify_tenants: bool = True,
    notify_owners: bool = False,
    has_pets: bool = False,
    pets: str = "",
    tags: Optional[list] = None,
    prop: Optional[dict] = None,
) -> dict:
    """Create a new standalone meld.

    POST /api/melds/ — verified shape from 2026-05-16 HAR capture.
    Uses the same hydration helpers as create_meld_in_project:
    - unit via _hydrate_unit
    - maintenance via _hydrate_maintenance_element
    - tenants via _hydrate_tenant_element
    """
    unit_obj = _hydrate_unit(unit)
    maintenance_input = maintenance if isinstance(maintenance, list) else [maintenance]
    needs_lookup = any(_is_stripped(m) for m in maintenance_input if isinstance(m, dict))
    all_maintenance = list_all_maintenance(registered_only=False) if needs_lookup else None
    maintenance_list = [_hydrate_maintenance_for_meld_create(m, all_maintenance) for m in maintenance_input]
    tenants_list = [_hydrate_tenant_element(t) for t in (tenants or [])]

    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    payload = {
        "brief_description": brief_description,
        "description": description,
        "due_date": due_date,
        "has_pets": has_pets,
        "maintenance": maintenance_list,
        "notify_owners": notify_owners,
        "notify_owners_string": "true" if notify_owners else "false",
        "notify_tenants": notify_tenants,
        "notify_tenants_string": "true" if notify_tenants else "false",
        "permission_to_enter": permission_to_enter,
        "pets": pets,
        "priority": priority,
        "prop": prop,
        "tags": tags or [],
        "tenant_presence_required": tenant_presence_required,
        "tenants": tenants_list,
        "unit": unit_obj,
        "work_category": work_category,
        "work_location": work_location,
        "work_type": work_type,
    }
    result = _http_post("melds/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "meld_id": result.get("id"), "result": result}



@with_recapture_retry
def link_tenant_to_meld(meld_id: str, tenant_id) -> dict:
    """Link a tenant to a meld by appending to the meld's tenants array.

    PATCH /api/melds/{meld_id}/ requires a full-payload echo: delta PATCHes
    return HTTP 400 with field-required errors for brief_description,
    work_location, work_category, work_type, and priority (verified live
    2026-05-29), mirroring the set_coordinator shape. Tenants field is replaced
    atomically — we read existing tenants first, append the new tenant as a
    fully-hydrated object, and PATCH the merged array with the required meld
    fields echoed from the current meld.

    Hydration mirrors the create_meld_in_project fix (P1 #2): PM serializers
    may walk nested fields on the tenants array, so we send full objects from
    GET /api/tenants/{id}/ rather than stripped {"id": N} placeholders.

    Idempotent: if tenant_id is already linked, returns {"already_linked": True}
    without firing the PATCH.

    Closes P1 #14 — gmail-tenant-link skill Step 5 was Playwright-only before.
    """
    meld_id = _validate_meld_id(meld_id)
    tenant_id_int = int(tenant_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)

    current = _http_get(f"melds/{meld_id}/", cookie_hdr)
    existing_tenants = current.get("tenants") or []
    existing_ids = {
        t.get("id") for t in existing_tenants
        if isinstance(t, dict) and t.get("id") is not None
    }

    if tenant_id_int in existing_ids:
        return {
            "ok": True,
            "meld_id": meld_id,
            "tenant_id": tenant_id_int,
            "already_linked": True,
            "tenant_count": len(existing_tenants),
        }

    new_tenant = get_tenant(tenant_id_int)
    merged_tenants = list(existing_tenants) + [new_tenant]

    csrf_token = _get_csrf_token(cookie_hdr)
    payload = {
        "brief_description": current.get("brief_description"),
        "work_location": current.get("work_location") or "",
        "work_category": current.get("work_category"),
        "work_type": current.get("work_type"),
        "priority": current.get("priority"),
        "tenants": merged_tenants,
    }
    result = _http_patch(f"melds/{meld_id}/", payload, cookie_hdr, csrf_token)
    return {
        "ok": True,
        "meld_id": meld_id,
        "tenant_id": tenant_id_int,
        "linked": True,
        "tenant_count": len(merged_tenants),
        "result": result,
    }


@with_recapture_retry
def patch_meld_project_link(meld_id: str, project_id) -> dict:
    """Attach or detach a meld's project link.

    PATCH /api/melds/{meld_id}/ requires a full-payload echo: delta PATCHes
    return HTTP 400 with field-required errors for brief_description,
    work_location, work_category, work_type, and priority (verified live
    2026-05-29). We fetch the current meld, echo those required fields, and
    overlay project.

    Pass project_id=None to detach. Pass an integer or string project id to
    attach. PM stores the linked project id back on the meld.
    """
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    current = _http_get(f"melds/{meld_id}/", cookie_hdr)
    csrf_token = _get_csrf_token(cookie_hdr)
    payload: dict = {
        "brief_description": current.get("brief_description"),
        "work_location": current.get("work_location") or "",
        "work_category": current.get("work_category"),
        "work_type": current.get("work_type"),
        "priority": current.get("priority"),
        "project": project_id,
    }
    result = _http_patch(f"melds/{meld_id}/", payload, cookie_hdr, csrf_token)
    return {"ok": True, "meld_id": meld_id, "project_id": project_id, "result": result}


# ── Estimates ─────────────────────────────────────────────────────────────────

@with_recapture_retry
def list_estimates(meld_id: Optional[str] = None, limit: int = 100, status: Optional[str] = None) -> list:
    """List estimates for a specific meld.

    PM does not expose an unscoped `/api/estimates/` list endpoint — the
    previous fallback to that path returned HTTP 404 silently until the
    2026-05-19 smoke matrix surfaced it. meld_id is required.
    """
    if meld_id is None:
        raise ValueError(
            "list_estimates requires meld_id — PM does not expose an unscoped /api/estimates/ list endpoint. "
            "Pass meld_id (or use `pm estimates list --meld-id <id>` on the CLI)."
        )
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    path = f"estimates/meld/{meld_id}/?limit={limit}"
    if status:
        path += f"&status={status}"
    data = _http_get(path, cookie_hdr)
    return data.get("results", data) if isinstance(data, dict) else data


@with_recapture_retry
def get_estimate(estimate_id: str) -> dict:
    """Get a single estimate by ID."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _http_get(f"estimates/{estimate_id}/", cookie_hdr)


@with_recapture_retry
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


@with_recapture_retry
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


@with_recapture_retry
def link_estimate_to_meld(estimate_id: str, meld_id: str) -> dict:
    """Link an estimate to a meld."""
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_patch(f"estimates/{estimate_id}/", {"meld_id": int(meld_id)}, cookie_hdr, csrf_token)
    return {"ok": True, "estimate_id": estimate_id, "meld_id": meld_id, "result": result}


# ── Receipts ─────────────────────────────────────────────────────────────────

@with_recapture_retry
def list_receipts(meld_id: Optional[str] = None, limit: int = 100) -> list:
    """List receipts for a specific meld.

    PM does not expose an unscoped `/api/receipts/` list endpoint — the
    previous fallback to that path returned HTTP 404 silently until the
    2026-05-19 smoke matrix surfaced it. meld_id is required. limit is
    accepted but PM's meld-scoped path returns all receipts for the meld
    (no server-side limit param honored), so the flag is informational.
    """
    if meld_id is None:
        raise ValueError(
            "list_receipts requires meld_id — PM does not expose an unscoped /api/receipts/ list endpoint. "
            "Pass meld_id (or use `pm receipts list --meld-id <id>` on the CLI)."
        )
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    path = f"melds/{meld_id}/receipts/"
    data = _http_get(path, cookie_hdr)
    return data.get("results", data) if isinstance(data, dict) else data


@with_recapture_retry
def get_receipt(receipt_id: str) -> dict:
    """Get a single receipt by ID."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _http_get(f"receipts/{receipt_id}/", cookie_hdr)


@with_recapture_retry
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
        if e.code == 401:
            raise SessionExpired(e)
        body_err = e.read().decode("utf-8", errors="ignore")
        error = normalize_http_error(e.code, body_err)
        error["ok"] = False
        return error


def _pm_presign_upload(presign_path: str, filename: str, content_type: str, cookie_hdr: str) -> dict:
    """Fetch an S3 POST policy from PM's `/files/generate-policy/` endpoint.

    Returns the parsed `{url, fields}` dict. `fields` includes `key`, `policy`,
    `signature`, `AWSAccessKeyId`, `acl`, `success_action_status`.

    Raises urllib.error.HTTPError verbatim so the caller can surface PM's
    own error body via normalize_http_error.
    """
    qs = urllib.parse.urlencode({"filename": filename, "content_type": content_type})
    url = f"{BASE}/api/{presign_path}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "Cookie": cookie_hdr,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
            "Referer": f"{BASE}/melds/",
        },
    )
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=30) as resp:
        return json.loads(resp.read())


def _s3_post_file(policy: dict, file_bytes: bytes, filename: str, content_type: str) -> None:
    """Upload file bytes to S3 via the presigned POST policy from PM.

    Raises urllib.error.HTTPError on non-2xx S3 response.
    """
    boundary = f"----CLIBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    # S3 form fields from the policy MUST come before the `file` field.
    for k, v in policy["fields"].items():
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="{k}"'.encode())
        parts.append(b"")
        parts.append(str(v).encode())
    parts.append(f"--{boundary}".encode())
    parts.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode())
    parts.append(f"Content-Type: {content_type}".encode())
    parts.append(b"")
    parts.append(file_bytes)
    parts.append(f"--{boundary}--".encode())
    parts.append(b"")
    body = b"\r\n".join(parts)

    req = urllib.request.Request(
        policy["url"],
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    # S3 returns 201 (success_action_status=201) with an XML body we don't need.
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=120) as resp:
        resp.read()


@with_recapture_retry
def upload_meld_file(meld_id: str, file_path: str, uploader_role: str = "manager", description: str = "") -> dict:
    """Upload a file attachment to a meld via the PM presign + S3 + commit flow.

    PM API migrated this endpoint from accepting multipart binary uploads to
    a 3-step S3-presign flow (regression observed 2026-05-24: previous multipart
    impl returned 400 `{"file": ["Not a valid string."], "filename": ["This
    field is required."]}`). The current shape mirrors what PM's web UI does:

      1. GET  /api/{presign_path}/?filename=&content_type=  → S3 POST policy
      2. POST <S3 url> multipart with the policy fields + file bytes → 201
      3. POST /api/melds/{meld_id}/{commit_endpoint}/ with JSON body
         `{file: <s3_key>, filename: <name>, meld_id: <id>}` → 201

    Routes by uploader_role:
      manager → presign melds/files/generate-policy/   → commit melds/{id}/files/
      tenant  → presign tenants/files/generate-policy/ → commit melds/{id}/tenant-files/
      vendor  → presign vendors/files/generate-policy/ → commit melds/{id}/vendor-files/

    The manager path is verified end-to-end against live PM. The tenant and
    vendor commit endpoints currently return HTTP 500 from a manager cookie
    session; their presign + S3 steps succeed but the commit step does not.
    Errors are surfaced verbatim — the existing CLI docstring notes that
    those routes may require additional auth.
    """
    meld_id = _validate_meld_id(meld_id)

    if not os.path.exists(file_path):
        return {"ok": False, "error": f"File not found: {file_path}"}

    role_to_routes = {
        "manager": ("melds/files/generate-policy/", "files"),
        "tenant": ("tenants/files/generate-policy/", "tenant-files"),
        "vendor": ("vendors/files/generate-policy/", "vendor-files"),
    }
    if uploader_role not in role_to_routes:
        return {"ok": False, "error": f"Unknown uploader_role '{uploader_role}'. Use manager|tenant|vendor."}
    presign_path, commit_endpoint = role_to_routes[uploader_role]

    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    file_name = os.path.basename(file_path)
    content_type, _enc = mimetypes.guess_type(file_name)
    content_type = content_type or "application/octet-stream"
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    try:
        policy = _pm_presign_upload(presign_path, file_name, content_type, cookie_hdr)
        _s3_post_file(policy, file_bytes, file_name, content_type)

        payload = {
            "file": policy["fields"]["key"],
            "filename": file_name,
            "meld_id": int(meld_id),
        }
        if description:
            payload["description"] = description

        # Commit POST — done inline (not via _http_post) because that helper
        # sys.exits on non-401 errors, while upload's return contract is to
        # surface the PM error verbatim via `{ok: False, ...}`.
        commit_url = f"{BASE}/api/melds/{int(meld_id)}/{commit_endpoint}/"
        req = urllib.request.Request(
            commit_url,
            data=json.dumps(payload).encode(),
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
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=30) as resp:
            result = json.loads(resp.read())
        return {
            "ok": True,
            "uploader_role": uploader_role,
            "file_id": result.get("id"),
            "result": result,
        }
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SessionExpired(e)
        body_err = e.read().decode("utf-8", errors="ignore")
        error = normalize_http_error(e.code, body_err)
        error["ok"] = False
        error["uploader_role"] = uploader_role
        return error


@with_recapture_retry
def link_receipt_to_invoice(receipt_id: str, estimate_id: str) -> dict:
    """Link a receipt to an invoice."""
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_patch(f"receipts/{receipt_id}/", {"linked_estimate_id": int(estimate_id)}, cookie_hdr, csrf_token)
    return {"ok": True, "receipt_id": receipt_id, "estimate_id": estimate_id, "result": result}


# ── Vendor Invitations ───────────────────────────────────────────────────────
