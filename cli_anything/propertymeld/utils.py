"""Shared utilities: token cache, JSON output, error handling."""
import json
import os
import sys
from typing import Any

# Nexus API constants
TOKEN_URL = "https://app.propertymeld.com/api/v2/oauth/token/"
API_BASE = "https://app.propertymeld.com/api/v2"
MULTITENANT_ID = os.environ.get("PM_MULTITENANT_ID", "3287")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

_token_cache: dict = {}


def get_token() -> str:
    """Fetch or return cached OAuth2 bearer token."""
    if _token_cache.get("token"):
        return _token_cache["token"]

    client_id = os.environ.get("PM_CLIENT_ID", "")
    client_secret = os.environ.get("PM_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print_error("PM_CLIENT_ID or PM_CLIENT_SECRET not set in environment.")
        sys.exit(2)

    import urllib.parse
    import urllib.request
    import ssl

    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()

    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA,
            "Accept": "application/json",
        }
    )
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=15) as resp:
        body = json.loads(resp.read())

    _token_cache["token"] = body["access_token"]
    return _token_cache["token"]


def output_json(data: Any) -> None:
    """Print data as JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


def print_error(message: str) -> None:
    """Print error to stderr in JSON format."""
    print(json.dumps({"error": message}), file=sys.stderr)


def _is_html_response(body: str) -> bool:
    text = (body or "").lstrip().lower()
    return text.startswith("<") or "<html" in text


def normalize_http_error(status_code: int, body: str) -> dict:
    """Normalize PM error bodies, especially raw HTML error pages."""
    if _is_html_response(body):
        excerpt = " ".join((body or "").split())[:200]
        return {
            "error": f"HTTP {status_code}",
            "status_code": status_code,
            "body_excerpt": excerpt,
        }

    detail = (body or "").strip()
    try:
        parsed = json.loads(detail)
    except (TypeError, ValueError):
        parsed = None

    if isinstance(parsed, dict):
        parsed.setdefault("error", f"HTTP {status_code}")
        parsed.setdefault("status_code", status_code)
        return parsed

    result = {"error": f"HTTP {status_code}", "status_code": status_code}
    if detail:
        result["detail"] = detail[:300]
    return result


def _api_get_json(path: str, params: dict | None = None) -> Any:
    """Make a direct authenticated Nexus API GET request."""
    import ssl
    import urllib.parse
    import urllib.request

    token = get_token()
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Multitenant-Id": MULTITENANT_ID,
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=15) as resp:
        return json.loads(resp.read())


def _extract_results(data: Any) -> list:
    if isinstance(data, dict):
        results = data.get("results", data)
        return results if isinstance(results, list) else []
    return data if isinstance(data, list) else []


def _find_matching_meld(items: list, ref_id: str) -> dict | None:
    target = ref_id.strip().upper()
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("ref_id", "reference_id"):
            value = item.get(key)
            if isinstance(value, str) and value.strip().upper() == target:
                return item
    return None


def resolve_meld_id(maybe_ref_or_int: str) -> str:
    """Return internal int meld_id for a numeric ID or human ref_id."""
    import urllib.error

    raw = str(maybe_ref_or_int).strip()
    if raw.isdigit():
        return raw

    queries = [
        {"ref_id": raw, "limit": 25},
        {"reference_id": raw, "limit": 25},
        {"search": raw, "limit": 25},
    ]
    for params in queries:
        try:
            data = _api_get_json("/meld/", params)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                continue
            print_error(f"API error {exc.code}: {exc.reason}")
            raise SystemExit(1)
        except urllib.error.URLError as exc:
            print_error(f"Network error: {exc.reason}")
            raise SystemExit(1)
        match = _find_matching_meld(_extract_results(data), raw)
        if match and match.get("id") is not None:
            resolved = str(match["id"])
            print(f"[resolved {raw} -> {resolved}]", file=sys.stderr)
            return resolved

    next_path = "/meld/?limit=100"
    while next_path:
        try:
            data = _api_get_json(next_path)
        except urllib.error.HTTPError as exc:
            print_error(f"API error {exc.code}: {exc.reason}")
            raise SystemExit(1)
        except urllib.error.URLError as exc:
            print_error(f"Network error: {exc.reason}")
            raise SystemExit(1)

        match = _find_matching_meld(_extract_results(data), raw)
        if match and match.get("id") is not None:
            resolved = str(match["id"])
            print(f"[resolved {raw} -> {resolved}]", file=sys.stderr)
            return resolved
        if isinstance(data, dict) and data.get("next"):
            next_url = data["next"]
            if next_url.startswith(API_BASE):
                next_path = next_url[len(API_BASE):]
            elif "/api/v2" in next_url:
                next_path = next_url.split("/api/v2", 1)[1]
            else:
                next_path = None
        else:
            next_path = None

    print_error(f"Meld ref_id '{raw}' not found.")
    raise SystemExit(2)


def clear_token_cache() -> None:
    """Clear cached token (for testing)."""
    _token_cache.clear()
