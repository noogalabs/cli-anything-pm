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


def redact_sensitive(data: Any, *, string_scrub=None) -> Any:
    """Return a copy of ``data`` with every value under a sensitive key redacted.

    Walks dicts, lists and tuples recursively. A key matching
    SENSITIVE_KEY_PATTERN (case-insensitive substring) has its ENTIRE value
    replaced with REDACTED, whatever that value's type. When ``string_scrub``
    is given it is applied to every remaining STRING leaf, so a credential
    that arrives inside a string value (``{"detail": "client_secret=..."}``,
    or a bare string element of a list) is caught too; the key walk alone
    cannot see inside a string. Other leaves are returned unchanged. The input
    is never mutated.
    """
    if isinstance(data, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and SENSITIVE_KEY_PATTERN.search(key)
                else redact_sensitive(value, string_scrub=string_scrub)
            )
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [redact_sensitive(item, string_scrub=string_scrub) for item in data]
    if string_scrub is not None and isinstance(data, str):
        # A string leaf that IS serialized JSON is walked with the key
        # redactor and re-serialized, so a sensitive KEY inside it is caught
        # whatever shape its value has (a short letter-only secret is
        # invisible to every text rule). Two ways the walk can fail, both of
        # which fall back to scrubbing the leaf as text so the shared stdout
        # boundary always produces output: allow_nan=False raises ValueError
        # for an out-of-range number (the default would emit Infinity/NaN,
        # which a strict parser rejects); and a pathologically deep leaf
        # raises RecursionError from the parse, the walk or the dump.
        try:
            parsed = _parse_json_leaf(data)
            if parsed is not None:
                container, layers = parsed
                out = json.dumps(redact_sensitive(container, string_scrub=string_scrub), allow_nan=False)
                # Re-apply every string layer that was peeled, so a double-
                # encoded leaf comes back double-encoded (W1).
                for _ in range(layers - 1):
                    out = json.dumps(out)
                return out
        except (ValueError, RecursionError):
            pass
        return string_scrub(data)
    return data


def _parse_json_leaf(text: str):
    """Return ``(container, layers)`` if ``text`` is serialized JSON, else None.

    ``layers`` is the number of string encodings that were peeled to reach
    the container: 1 for an ordinary serialized document, 2 when the text is a
    JSON string whose content is itself a JSON document (double encoding).
    The caller re-encodes the redacted container the same number of times so
    the layer count round-trips; deeper encodings are not accepted and fall
    back to the text scrub. Only containers are returned; scalars are not
    treated as JSON so ordinary strings keep their normal text scrubbing.
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[\"":
        return None
    layers = 0
    for _ in range(2):
        try:
            value = json.loads(stripped)
        except (TypeError, ValueError, RecursionError):
            return None
        layers += 1
        if isinstance(value, (dict, list)):
            return value, layers
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped[0] not in "{[":
                return None
            continue
        return None
    return None


# Value-shape scrubbing for free text. Key-based redaction cannot see inside a
# string, and normalize_http_error emits raw excerpts of NON-JSON error bodies
# (an HTML gateway page, a plaintext proxy error). A realistic such page echoes
# an Authorization header or a query string, so the excerpt is scrubbed by the
# SHAPE of a credential before it is emitted: Bearer/Basic runs, key=value and
# key: value pairs for the same key pattern (quoted or bare), and long
# high-entropy runs. The surrounding text is kept so the excerpt stays useful.
# Long URL-like runs that carry a digit are over-scrubbed on purpose: this is a
# diagnostic excerpt, and under-scrubbing is the failure that matters.
_TEXT_AUTH_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]+")
# S2 (performance): the key prefix and the entropy lookaheads used to be retried
# from EVERY character, which is quadratic on a long letter-only string (an
# 8 KB leaf took seconds, larger ones minutes). Both patterns are now anchored
# to a token boundary with a lookbehind, so a match is attempted once per
# token and each token is scanned once; the per-token scan is capped by the
# {1,N} bounds so a pathological single token stays linear too.
#
# S1 (escaped JSON): a string leaf can carry serialized JSON whose quotes are
# backslash-escaped ({\"client_secret\": \"x\"}), which put a backslash
# between the key and its quote, and a leaf encoded more than once carries
# runs of several backslashes. Key and value delimiters therefore accept any
# run of backslashes before the quote, and the opener's run must match the
# closer's (backreference). The primary defence for that shape is
# the JSON-leaf walk in redact_sensitive; this is the text-level backstop.
_TEXT_KV_RE = re.compile(
    '(?i)(?<![A-Za-z0-9_\\-])([A-Za-z0-9_\\-]{0,64}?(?:secret|token|password|api[_-]?key)[A-Za-z0-9_\\-]{0,64})'
    '(\\s*(?:\\\\*[\\"\'])?\\s*[=:]\\s*)'
    '(?:(?P<dqe>\\\\*)\\"(?P<dq>(?:[^\\"\\\\]|\\\\.)*?)(?P=dqe)\\"|(?P<sqe>\\\\*)\'(?P<sq>(?:[^\'\\\\]|\\\\.)*?)(?P=sqe)\'|(?P<udqe>\\\\*)\\"(?P<udq>(?:[^\\"\\\\]|\\\\.)*)$|(?P<usqe>\\\\*)\'(?P<usq>(?:[^\'\\\\]|\\\\.)*)$|(?P<bare>[^\\s\\"\'&;,<>\\\\]+))'
)


# S2 (performance, second cut): the previous entropy rule used two unbounded
# lookaheads that were retried from every character, quadratic on a long
# letter-only string (a 64 KB leaf took 14 s). It is now a plain run pattern
# plus a Python check, linear by construction: every run of 24+ token-alphabet
# characters is redacted only if it carries both a digit and a letter. Same
# semantics, no lookaheads.
_TEXT_ENTROPY_RUN_RE = re.compile('[A-Za-z0-9\\-._~+/=]{24,}')


def _kv_redact(m) -> str:
    key, sep = m.group(1), m.group(2)
    g = m.groupdict()
    # The opener decides the closer: a plain quote closes on a plain quote (so
    # an escaped quote INSIDE the value stays content), and a backslash-escaped
    # opener closes on a backslash-escaped quote (serialized JSON embedded in
    # text). The backreference in the pattern enforces it; here the same
    # escaping is written back so the surrounding text keeps its shape.
    if g["dq"] is not None:
        return f'{key}{sep}{g["dqe"]}"{REDACTED}{g["dqe"]}"'
    if g["sq"] is not None:
        return f"{key}{sep}{g['sqe']}'{REDACTED}{g['sqe']}'"
    # Unterminated quote: fail closed, the value runs to the end of the text.
    if g["udq"] is not None:
        return f'{key}{sep}{g["udqe"]}"{REDACTED}'
    if g["usq"] is not None:
        return f"{key}{sep}{g['usqe']}'{REDACTED}"
    return f"{key}{sep}{REDACTED}"


def scrub_sensitive_text(text: str, *, high_entropy: bool = True) -> str:
    """Replace credential-shaped values inside free text with REDACTED.

    Keeps the surrounding text (labels, separators, prose) so an error excerpt
    remains readable. Applied to excerpts BEFORE truncation so a value that
    would straddle the cap cannot leak a tail.

    ``high_entropy=False`` applies only the precise shapes (Bearer/Basic runs
    and key=value / key: value for the sensitive key pattern). That mode is
    used on ordinary command output, where the high-entropy rule would redact
    UUIDs and long identifiers that belong in the payload; the full mode is
    reserved for error normalization, where over-redaction is acceptable.
    """
    if not text:
        return text
    text = _TEXT_AUTH_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _TEXT_KV_RE.sub(_kv_redact, text)
    if high_entropy:
        text = _TEXT_ENTROPY_RUN_RE.sub(_entropy_redact, text)
    return text


def _entropy_redact(m) -> str:
    run = m.group(0)
    if any(c.isdigit() for c in run) and any(c.isalpha() for c in run):
        return REDACTED
    return run


def scrub_sensitive_text_narrow(text: str) -> str:
    """Precise-shape scrub for ordinary output string leaves (no entropy rule)."""
    return scrub_sensitive_text(text, high_entropy=False)


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
    print(json.dumps(redact_sensitive(data, string_scrub=scrub_sensitive_text_narrow), indent=2, default=str))
    if isinstance(data, dict) and data.get("ok") is False:
        sys.exit(1)


_ENV_LINE_RE_TMPL = r"^(\s*(?:export\s+)?){key}\s*=.*$"


def update_env_file(path: str, updates: dict) -> dict:
    """Atomically write ``KEY=value`` pairs into an env file, mode 0600.

    Non-displaying delivery path for a credential: the values are written to
    disk and NEVER returned or printed. Existing lines are preserved; a line
    for a key in ``updates`` (plain or ``export KEY=`` form) is replaced in
    place, and missing keys are appended. The file is written to a temporary
    sibling in the same directory and then ``os.replace``d over the target, so
    a crash mid-write leaves the original intact and no reader ever sees a
    half-written file. A replaced line keeps its leading whitespace and its
    ``export`` prefix if it had them; the first definition of a key is
    replaced and any later duplicate definitions are removed, INCLUDING
    indented ones (a shell sources an indented assignment just the same, and
    the last one wins), so exactly one remains. Keys and values containing a newline or carriage return are refused
    BEFORE the file is touched: a newline would inject a line into a 0600
    credential file. The result carries names and metadata only.
    """
    for key, value in updates.items():
        if any(ch in str(key) for ch in "\r\n") or any(ch in str(value) for ch in "\r\n"):
            raise ValueError(f"env key/value for {key!r} must not contain a newline or carriage return")
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
    replaced_keys: set = set()
    out_lines = []
    for line in lines:
        handled = False
        for key in updates:
            m = re.match(_ENV_LINE_RE_TMPL.format(key=re.escape(key)), line)
            if m:
                handled = True
                if key in replaced_keys:
                    # Duplicate definition: drop it. Shell loading lets the LAST
                    # assignment win, so leaving a stale later line would let
                    # the old secret survive a reported-successful rotation.
                    break
                prefix = m.group(1) or ""
                out_lines.append(f"{prefix}{key}={remaining.pop(key)}")
                replaced_keys.add(key)
                break
        if not handled:
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


def emit_error(payload, *, exit_code=None) -> None:
    """The single scrubbed boundary for every refusal / error written to stderr.

    Aussie's PR66 seat found the real `pm work-orders complete` refusal path
    printing a credential-shaped --notes value straight to stderr, bypassing
    output_json's redaction. The fix is at the source shape, not the site:
    every refusal / error emitter routes its payload through here, so a value
    can never reach stderr unredacted regardless of which key it sits under.

    A dict is walked by the key redactor AND every string leaf is scrubbed with
    the full text scrubber (this is a diagnostic error line, so over-redaction
    is acceptable, matching normalize_http_error); a bare string is scrubbed
    directly. ``exit_code`` exits after printing when given.
    """
    if isinstance(payload, str):
        printable = scrub_sensitive_text(payload)
    else:
        printable = redact_sensitive(payload, string_scrub=scrub_sensitive_text)
    print(json.dumps(printable) if not isinstance(printable, str) else printable, file=sys.stderr)
    if exit_code is not None:
        sys.exit(exit_code)


def print_error(message: str) -> None:
    """Print error to stderr in JSON format (scrubbed via emit_error)."""
    emit_error({"error": message})


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
    # Full scrub on every string leaf as well as the key walk: a parsed error
    # body can carry a credential INSIDE a string value or as a bare string
    # element of a list, which keys cannot see (second Codex review, X2).
    return redact_sensitive(_normalize_http_error_raw(status_code, body), string_scrub=scrub_sensitive_text)


def _normalize_http_error_raw(status_code: int, body: str) -> dict:
    if _is_html_response(body):
        # Scrub BEFORE the 200-char cut so a credential straddling the cap
        # cannot leak its tail; the pre-scrub slice bounds regex work.
        excerpt = scrub_sensitive_text(" ".join((body or "").split())[:4000])[:200]
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
    if isinstance(parsed, list):
        # A JSON ARRAY error body (DRF returns these) must stay structured so
        # the caller's redact_sensitive can walk its keys. Stringifying it into
        # `detail` would hide every key from the key-match; the text scrubber
        # is only the backstop for that case.
        result["detail"] = parsed
        return result
    if detail:
        result["detail"] = scrub_sensitive_text(detail[:4000])[:300]
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
