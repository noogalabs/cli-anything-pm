"""Shared utilities: token cache, JSON output, error handling."""
import json
import os
import re
import sys
from typing import Any

from .config import require_propertymeld_config

# Nexus API constants
TOKEN_URL = "https://app.propertymeld.com/api/v2/oauth/token/"
API_BASE = "https://app.propertymeld.com/api/v2"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

import time

_token_cache: dict = {}
# Refresh the token a bit before the server-stated expiry so a request never
# starts with a token that expires mid-flight.
_TOKEN_EXPIRY_SAFETY_SEC = 60


def get_token() -> str:
    """Fetch or return cached OAuth2 bearer token.

    The cache keys on the client_id + client_secret currently in env so a
    rotation that swaps the credential pair invalidates the cache automatically.
    Tokens are also expired against the server-stated `expires_in` (minus a
    60s safety window) so a long-running process doesn't hand out a token
    that's about to be rejected. Without this, a stale in-process token can
    survive past server-side revocation and produce confusing 'API Key no
    longer active' errors with no clear repro.
    """
    client_id = os.environ.get("PM_CLIENT_ID", "")
    client_secret = os.environ.get("PM_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print_error("PM_CLIENT_ID or PM_CLIENT_SECRET not set in environment.")
        sys.exit(2)

    cache_key = (client_id, client_secret)
    cached = _token_cache.get("entry")
    if (
        cached
        and cached.get("key") == cache_key
        and cached.get("expires_at", 0) > time.time() + _TOKEN_EXPIRY_SAFETY_SEC
    ):
        return cached["token"]

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

    expires_in = int(body.get("expires_in", 3600))
    _token_cache["entry"] = {
        "key": cache_key,
        "token": body["access_token"],
        "expires_at": time.time() + expires_in,
    }
    return body["access_token"]


# Output-boundary secret redaction.
#
# PropertyMeld responses can carry credentials that are not ours to print: the
# work-orders comments payload nests the management company's OAuth client
# secret under comment.agent.management, and every pm command prints its
# response verbatim. Any process that captures stdout (agent transcripts, logs,
# shell history) then holds the secret. The fix is one choke point: every
# command already emits through output_json(), so redaction here covers all of
# them without per-command code.
#
# Redaction is by KEY, recursive through dicts, lists and tuples, and replaces
# the whole value under a matching key (a nested object under a key named
# ``token_config`` is dropped entirely rather than descended into). It is
# unconditional: there is no environment variable or global flag that turns it
# off, because the caller most likely to want raw output is exactly the agent
# whose transcript must not hold the value. There is no per-command reveal
# either: a command that must deliver a secret (``pm api-keys rotate``) writes
# it to a non-displaying destination (Railway, or an env file via
# update_env_file) and stdout only ever shows it redacted.
#
# Pattern note: ``api[_-]?key`` is a strict superset of the specified
# ``api_key`` so that camelCase ``apiKey`` and the header form ``x-api-key``
# are also caught; the other three tokens already match their camelCase forms
# as substrings (``clientSecret``, ``accessToken``, ``passWord``).
SENSITIVE_KEY_PATTERN = re.compile(r"secret|token|password|api[_-]?key", re.IGNORECASE)
REDACTED = "[REDACTED]"


def redact_sensitive(data: Any) -> Any:
    """Return a copy of ``data`` with every value under a sensitive key redacted.

    Walks dicts, lists and tuples recursively. A key matching
    SENSITIVE_KEY_PATTERN (case-insensitive substring) has its ENTIRE value
    replaced with REDACTED, whatever that value's type. Non-container leaves
    are returned unchanged. The input is never mutated.
    """
    if isinstance(data, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and SENSITIVE_KEY_PATTERN.search(key)
                else redact_sensitive(value)
            )
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [redact_sensitive(item) for item in data]
    return data


def output_json(data: Any) -> None:
    """Print data as JSON to stdout, redacting sensitive keys first.

    Sensitive-key redaction (see redact_sensitive) is applied to every payload
    before it is serialized, with no bypass parameter: a command that needs to
    deliver a secret does so through a non-displaying path (see
    update_env_file and ``pm api-keys rotate --update-railway/--update-env``),
    never through stdout.

    Fail loud: if the payload is a result envelope reporting failure
    (a dict with ``ok`` explicitly False), exit non-zero AFTER printing so
    that shell callers and crons checking ``$?`` see the failure. Backend
    write helpers return ``{"ok": False, ...}`` on 404/not-found/PM-4xx; without
    this, every such command printed the error but still exited 0, silently
    reporting failed assigns/schedules/merges as success. Reads pass payloads
    with no ``ok`` key (or ``ok`` True) and are unaffected. The exit decision
    reads the ORIGINAL payload so redaction can never mask a failure envelope.
    """
    print(json.dumps(redact_sensitive(data), indent=2, default=str))
    if isinstance(data, dict) and data.get("ok") is False:
        sys.exit(1)


_ENV_LINE_RE_TMPL = r"^(?:export\s+)?{key}\s*=.*$"


def update_env_file(path: str, updates: dict) -> dict:
    """Atomically write ``KEY=value`` pairs into an env file, mode 0600.

    Non-displaying delivery path for a credential: the values are written to
    disk and NEVER returned or printed. Existing lines are preserved; a line
    for a key in ``updates`` (plain or ``export KEY=`` form) is replaced in
    place, and missing keys are appended. The file is written to a temporary
    sibling in the same directory and then ``os.replace``d over the target, so
    a crash mid-write leaves the original intact and no reader ever sees a
    half-written file. The result carries names and metadata only.
    """
    target = os.path.abspath(path)
    parent = os.path.dirname(target) or "."
    if not os.path.isdir(parent):
        raise FileNotFoundError(f"env file directory does not exist: {parent}")
    if not os.access(parent, os.W_OK):
        raise PermissionError(f"env file directory is not writable: {parent}")

    existing = ""
    if os.path.exists(target):
        with open(target, "r", encoding="utf-8") as fh:
            existing = fh.read()

    lines = existing.splitlines()
    remaining = dict(updates)
    out_lines = []
    for line in lines:
        replaced = False
        for key in list(remaining):
            if re.match(_ENV_LINE_RE_TMPL.format(key=re.escape(key)), line):
                out_lines.append(f"{key}={remaining.pop(key)}")
                replaced = True
                break
        if not replaced:
            out_lines.append(line)
    for key, value in remaining.items():
        out_lines.append(f"{key}={value}")
    content = "\n".join(out_lines) + "\n"

    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".env-", suffix=".tmp", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(target, 0o600)
    return {"path": target, "keys_written": sorted(updates), "mode": "0600"}


def print_error(message: str) -> None:
    """Print error to stderr in JSON format."""
    print(json.dumps({"error": message}), file=sys.stderr)


def _is_html_response(body: str) -> bool:
    text = (body or "").lstrip().lower()
    return text.startswith("<") or "<html" in text


def normalize_http_error(status_code: int, body: str) -> dict:
    """Normalize PM error bodies, especially raw HTML error pages.

    The returned dict is passed through redact_sensitive HERE, at the source,
    because every error-emission site in http_backend and api_backend prints
    this dict to stderr directly (ten sites, none via output_json). Redacting
    once inside the normalizer means no call site, present or future, can
    print a sensitive-keyed field from a PM error body unredacted. Found in
    second-seat review: the first cut guarded one of the ten sites.
    """
    return redact_sensitive(_normalize_http_error_raw(status_code, body))


def _normalize_http_error_raw(status_code: int, body: str) -> dict:
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
    multitenant_id = require_propertymeld_config().multitenant_id
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Multitenant-Id": multitenant_id,
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
