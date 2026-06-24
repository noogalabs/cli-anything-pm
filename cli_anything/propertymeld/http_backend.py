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

from .utils import _is_html_response, normalize_http_error

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
    os.path.dirname(os.path.abspath(__file__)),
    "recapture",
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
        # --force: this caller only runs AFTER a real SessionExpired (401 on a
        # write), so the session is definitively stale. Bypass the script's own
        # validity gate — re-probing here would just waste a round trip, and the
        # gate is only meaningful for manual/standalone invocations.
        result = subprocess.run(
            [sys.executable, _RECAPTURE_SCRIPT, "--force"],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            # 180s, not 120: a single headless Playwright login attempt can spend
            # up to ~100s on its own wait budget (nav + per-element + post-submit),
            # plus the app-host nav and the post-write write-path probe. 120s could
            # SIGKILL a healthy-but-slow first attempt mid-write; 180s gives one full
            # worst-case attempt real headroom. This stays the headless/auto path —
            # the --mfa-relay path (360s poll) is never invoked from here.
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "Recapture timed out (180s)"}), file=sys.stderr)
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


# Positive WRITE-session marker: the authenticated manager page carries
# ``window.PM.csrf_token`` — the exact token writes need (see _get_csrf_token).
# A login / MFA / interstitial page lacks it, so REQUIRING it is what closes the
# false-valid-on-HTTP-200 hole.
_WRITE_SESSION_CSRF_RE = re.compile(r"window\.PM\.csrf_token\s*=\s*[\"']([\w-]+)[\"']")
# Belt: a login or MFA form in the body means we were bounced to an auth page
# served as a 200 (no redirect). Reject it even on the off chance the csrf marker
# co-occurs.
_LOGIN_OR_MFA_FORM_RE = re.compile(
    r"""type=["']password["']|name=["']password["']"""
    r"""|autocomplete=["']one-time-code["']|name=["'](?:otp|code|token)["']""",
    re.IGNORECASE,
)


def session_cookie_valid(timeout: int = 15) -> bool:
    """Validate the UI session COOKIE against the manager WRITE surface.

    This is the write-path counterpart to ``api_backend.probe()``, which only
    validates the Nexus API TOKEN (the READ path). A write-only outage — API/read
    up, UI cookie stale — is invisible to the token probe but caught here: we
    issue a non-mutating GET to the same ``/m/`` manager surface that writes use
    (modeled on ``_get_csrf_token``'s GET to ``{BASE}/melds/``).

    A 200 status is NOT sufficient: PM serves the login / MFA / interstitial page
    as an HTTP **200 with no redirect**, so a status-only check would FALSE-VALID
    on a stale session and skip recapture — re-introducing the exact write-blind
    no-op this fix exists to kill. So we also READ THE BODY and require the
    positive write marker ``window.PM.csrf_token`` (what writes actually need),
    and reject any login/MFA form.

    FAIL-CLOSED by design: a missing creds file, empty cookie header, 401/403,
    redirect to ``/login``, a 200 that lacks the write marker (or shows a login/
    MFA form), or any network/parse error all return ``False`` so a caller
    proceeds to recapture. This helper never raises and never calls ``sys.exit``.
    """
    if not os.path.exists(CREDS_PATH):
        return False
    try:
        with open(CREDS_PATH) as f:
            creds = json.load(f)
    except (OSError, ValueError):
        return False

    cookie_hdr = _cookie_header(creds)
    if not cookie_hdr:
        return False

    req = urllib.request.Request(
        f"{BASE}/melds/",
        headers={"Cookie": cookie_hdr, "User-Agent": UA, "Accept": "text/html"},
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=timeout) as resp:
            # PM bounces a stale session to /login as an HTTP-200 login page, so a
            # 200 alone is not proof — the final URL must still be inside the app.
            if "/login" in resp.geturl():
                return False
            if resp.status != 200:
                return False
            body = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError:
        # 401/403 (and anything else) => not a usable write session.
        return False
    except Exception:
        return False

    # Body inspection (the load-bearing fail-closed check): reject a login/MFA
    # form served as 200, and REQUIRE the positive write-session marker.
    if _LOGIN_OR_MFA_FORM_RE.search(body):
        return False
    return bool(_WRITE_SESSION_CSRF_RE.search(body))


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


# Statuses on the OPTIONAL read path that mean "this endpoint is legitimately
# unavailable for this session" (forbidden / not-found) and so are downgraded to
# an empty result + note instead of being fatal. This is the SINGLE shared
# status->action rule applied to BOTH catch branches of
# _http_get_optional_results, so the transport (a real HTTP status vs a status
# inferred from an HTML-200 interstitial body) no longer changes fatality.
# Everything NOT in this set (5xx, 400, 429, ...) stays fatal — that is the
# whole point: a proxy/app-server 5xx error page must not masquerade as an
# empty success. Do NOT widen this set without revisiting that invariant.
_OPTIONAL_UNAVAILABLE_STATUSES = frozenset({403, 404})


# Known HTTP reason phrases keyed by status code. A bare 4xx/5xx-looking
# number in an HTML body is only honored as a status when it sits in a real
# status CONTEXT — adjacent to its reason phrase, inside a title/heading, or
# preceded by an HTTP/Error/Status label. This prevents incidental markup
# (`font-weight: 500`, `width="500"`, a footer support code "403") from being
# mis-read as the response status.
_HTTP_REASON_PHRASES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}

# code-then-phrase ("403 Forbidden") OR phrase-then-code ("Forbidden 403").
_REASON_PHRASE_PATTERNS = [
    (
        code,
        re.compile(
            r"\b" + str(code) + r"\b\s*[:\-–]?\s*" + re.escape(phrase)
            + r"|" + re.escape(phrase) + r"\s*[:\-–]?\s*\b" + str(code) + r"\b",
            re.IGNORECASE,
        ),
    )
    for code, phrase in _HTTP_REASON_PHRASES.items()
]

# A 4xx/5xx code inside a <title>, <h1>, or <h2> heading.
_HEADING_STATUS_RE = re.compile(
    r"<(?:title|h1|h2)\b[^>]*>(.*?)</(?:title|h1|h2)>",
    re.IGNORECASE | re.DOTALL,
)
_CODE_IN_TEXT_RE = re.compile(r"\b([45]\d{2})\b")

# A code preceded by an explicit HTTP/Error/Status label ("HTTP 503",
# "Error 404", "Status: 500").
_LABELED_STATUS_RE = re.compile(
    r"\b(?:HTTP|Error|Status)\b\s*[:\-–]?\s*\b([45]\d{2})\b",
    re.IGNORECASE,
)


def _status_from_html_body(text: str) -> Optional[int]:
    """Best-effort extract an HTTP status code from an HTML error/interstitial.

    PM sometimes serves a permission-denied / not-found interstitial as an
    HTTP **200** with an HTML body (e.g. "<h1>403 Forbidden</h1>"). The status
    line in the body is the only trustworthy signal of the real condition.

    A naked ``[45]\\d{2}`` scan of the whole document is UNSAFE: incidental
    markup numbers (``font-weight: 500``, ``width="500"``, a support code in
    footer copy) get mis-read as the status. Worse, on a real 5xx error page a
    stray "403"/"404" elsewhere in the body would be mis-inferred as a 4xx and
    then SILENTLY DOWNGRADED on the optional path, masking a server error.

    So a 4xx/5xx code is only honored when it appears in a real status CONTEXT:
      1. adjacent to its known HTTP reason phrase, in either order
         ("403 Forbidden" / "Forbidden 403", "500 Internal Server Error"); OR
      2. inside a ``<title>`` / ``<h1>`` / ``<h2>`` heading; OR
      3. preceded by an "HTTP" / "Error" / "Status" label ("HTTP 503",
         "Error 404", "Status: 500").

    SELECTION is EARLIEST-POSITION-WINS across ALL THREE context sources, not
    tier-ordered. Servers render the real status highest in the document (the
    <title>/<h1>), so the candidate with the smallest start position is the
    trustworthy one. A tier-ordered scan was UNSAFE: a numeric-only 5xx heading
    ("<h1>500</h1>", no reason phrase) would be skipped while a LATER, lower-code
    "403 Forbidden" reason phrase in footer copy returned first — silently
    MASKING the server error as a 4xx downgrade on the optional path. Collecting
    every (position, code) candidate and returning the earliest closes that gap.

    When NO status context is found, returns ``None`` — there is deliberately NO
    bare ``[45]\\d{2}`` fallback. ``None`` is SAFE: the optional path treats it
    as a forbidden-class (403) downgrade, which is the common permission
    interstitial. Returning a guessed status here is the dangerous direction.
    """
    if not text:
        return None

    # Collect (position, priority, code) candidates from all three context
    # sources, then pick the EARLIEST position. ``priority`` is only a
    # deterministic tie-breaker when two candidates share the same start
    # position (e.g. "<title>403 Forbidden</title>" matches both reason-phrase
    # and heading at the same spot — they yield the same code anyway, so the
    # tie-break never changes the result). Lower priority wins the tie;
    # reason-phrase (0) is preferred, matching the prior strongest-signal intent.
    candidates: list[tuple[int, int, int]] = []

    # Source 1 — reason-phrase context (code adjacent to its canonical phrase,
    # either order). Strongest signal; never incidental markup.
    for code, pattern in _REASON_PHRASE_PATTERNS:
        m = pattern.search(text)
        if m:
            candidates.append((m.start(), 0, code))

    # Source 2 — a [45]\d{2} code inside a <title>/<h1>/<h2> heading. Position is
    # the code's ABSOLUTE position in the document (heading match start + the
    # code's offset within the heading's inner text), so it compares correctly
    # against the other sources.
    for heading_match in _HEADING_STATUS_RE.finditer(text):
        inner = heading_match.group(1)
        code_match = _CODE_IN_TEXT_RE.search(inner)
        if code_match:
            abs_pos = heading_match.start(1) + code_match.start(1)
            candidates.append((abs_pos, 1, int(code_match.group(1))))

    # Source 3 — a code preceded by an HTTP/Error/Status label.
    for labeled in _LABELED_STATUS_RE.finditer(text):
        candidates.append((labeled.start(), 2, int(labeled.group(1))))

    if not candidates:
        # No status context found — SAFE None (optional path downgrades as 403).
        return None

    # Earliest position wins; priority breaks an exact-position tie deterministically.
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


class _NonJsonBody(Exception):
    """Raised when a 2xx body is not JSON (typically an HTML interstitial).

    Carries the inferred HTTP status (when the body is recognizably an HTML
    error page) so callers can either fail loud (REQUIRED path) or downgrade
    to an empty result + note (OPTIONAL path) — both reusing the same
    status-inference logic instead of duplicating it.
    """

    def __init__(self, text: str, inferred_status: Optional[int]):
        super().__init__("Non-JSON response body")
        self.text = text
        self.inferred_status = inferred_status


def _parse_json_body_or_none(raw: bytes) -> Any:
    """Decode a 2xx body as JSON, or raise _NonJsonBody (non-fatal).

    Shared core for both the REQUIRED and OPTIONAL read paths. On a non-JSON
    body (almost always an HTML forbidden/error interstitial served at HTTP
    200) it raises _NonJsonBody carrying the inferred status (403 default when
    the body is recognizably HTML, else None). Callers decide fatality:
      - REQUIRED (_http_get -> _parse_json_body_or_exit): sys.exit(1)
      - OPTIONAL (_http_get_optional_results): downgrade to ([], note)
    """
    text = raw.decode("utf-8", errors="ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        inferred = (_status_from_html_body(text) or 403) if _is_html_response(text) else None
        raise _NonJsonBody(text, inferred)


def _parse_json_body_or_exit(raw: bytes) -> Any:
    """Decode a 2xx body as JSON, or fail loud via the standard error convention.

    PM occasionally returns a forbidden / not-found interstitial as an HTTP 200
    with an HTML (non-JSON) body. The naked `json.loads()` on that body raised
    an uncaught json.JSONDecodeError -> bare traceback crash. This funnels that
    case into the SAME normalize_http_error + stderr + sys.exit(1) convention
    already used for non-401 HTTPErrors, so callers surface a clean, actionable
    error instead of crashing — and never mask the failure as an empty success.

    REQUIRED-path behavior: any non-JSON 200 body is fatal (sys.exit(1)). The
    OPTIONAL path uses _parse_json_body_or_none directly to downgrade instead.
    """
    try:
        return _parse_json_body_or_none(raw)
    except _NonJsonBody as nb:
        if nb.inferred_status is not None:
            print(json.dumps(normalize_http_error(nb.inferred_status, nb.text)), file=sys.stderr)
        else:
            print(
                json.dumps({
                    "error": "Non-JSON response body",
                    "body_excerpt": " ".join((nb.text or "").split())[:200],
                }),
                file=sys.stderr,
            )
        sys.exit(1)


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
            return _parse_json_body_or_exit(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        print(json.dumps(normalize_http_error(e.code, body)), file=sys.stderr)
        sys.exit(1)


def _http_options(path: str, cookie_hdr: str, *, side: str = "manager", vendor_id: Optional[str] = None) -> Any:
    """OPTIONS a browser-session API path, return parsed JSON metadata."""
    req = urllib.request.Request(
        _build_url(path, side=side, vendor_id=vendor_id),
        method="OPTIONS",
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
            return _parse_json_body_or_exit(resp.read())
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


def _paginate_all(path: str, cookie_hdr: str, max_pages: int = 50, stop_at: Optional[int] = None) -> list:
    """Walk the DRF `next` link chain and concatenate `results` arrays.

    Many cookie-API list endpoints return at most 100 items per page (and
    often default to 25). Helpers that read only the first page silently
    truncate when the underlying record set is larger — this masked photo
    sources past 100 items in the inspect aggregator and capped name-based
    tech/vendor matching at the first 100 records. Use this helper anywhere
    the caller's intent is "all of them, not just page 1".

    `max_pages` is a defensive cap to prevent runaway pagination on a
    misconfigured endpoint; raise it if a real list legitimately exceeds it.
    `stop_at` preserves the "all rows" default but lets user-facing bounded
    list commands stop fetching once enough rows have been collected.
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
                if stop_at is not None and len(results) >= stop_at:
                    next_path = None
                    continue
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


def _http_post_no_exit(
    path: str,
    payload: dict,
    cookie_hdr: str,
    csrf_token: str,
    *,
    side: str = "manager",
    vendor_id: Optional[str] = None,
) -> Any:
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
    """GET an optional list endpoint, downgrading "unavailable" to ([], note).

    NON-FATAL BY DESIGN for the "endpoint legitimately unavailable for this
    session" case (forbidden / not-found), which this path exists to tolerate
    (e.g. tenant/vendor file endpoints on a manager session).

    A SINGLE shared rule (_OPTIONAL_UNAVAILABLE_STATUSES = {403, 404}) decides
    fatality, applied identically to BOTH catch branches so the transport does
    not change the outcome:
      - status in {403, 404} -> downgrade to ([], note)
      - any other recognized status (5xx, 400, 429, ...) -> fail loud
        (normalize_http_error + stderr + sys.exit(1))
    This holds whether the status came from a real HTTPError OR was inferred
    from an HTML-200 permission/error interstitial body. A 5xx served as an
    HTML-200 proxy/app-server error page is therefore NO LONGER silently
    downgraded to an empty success — it fails loud, matching a real 5xx.

    Behavior note: a real HTTP-403 now DOWNGRADES here (previously fatal). This
    is intentional — it makes the real-403 case match both the documented
    forbidden-interstitial purpose and the HTML-200-403 case, removing the last
    transport asymmetry.
    """
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
            # NON-FATAL BY DESIGN: this path may legitimately hit a forbidden
            # interstitial (tenant/vendor file endpoints on a manager session).
            # An HTML-200 interstitial is routed by its INFERRED status through
            # the SAME _OPTIONAL_UNAVAILABLE_STATUSES rule used for real
            # HTTPErrors below — so a forbidden/not-found page downgrades while a
            # 5xx error page fails loud. Do NOT use _parse_json_body_or_exit
            # here; that is the REQUIRED path's fatal funnel.
            data = _parse_json_body_or_none(resp.read())
    except _NonJsonBody as nb:
        status = nb.inferred_status
        if status is None:
            # HTML interstitial on this OPTIONAL endpoint with no parseable
            # 4xx/5xx code in the body. Preserve the documented intent that an
            # unrecognized HTML interstitial here means "unavailable" — treat it
            # as a forbidden-class (403) downgrade rather than guessing fatal.
            return [], f"{note_label} endpoint unavailable (403): /api/{path}"
        if status in _OPTIONAL_UNAVAILABLE_STATUSES:
            return [], f"{note_label} endpoint unavailable ({status}): /api/{path}"
        # Recognized non-unavailable code inferred from the body (e.g. a 5xx
        # proxy/app-server error page served at HTTP 200, or 400/429). Fail loud
        # — do not mask a server error as an empty success.
        print(json.dumps(normalize_http_error(status, nb.text)), file=sys.stderr)
        sys.exit(1)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SessionExpired(e)
        body = e.read().decode("utf-8", errors="ignore")
        if e.code in _OPTIONAL_UNAVAILABLE_STATUSES:
            return [], f"{note_label} endpoint unavailable ({e.code}): /api/{path}"
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
        tech_name: Partial name match (case-insensitive). e.g. "Carlos" or "Carlos Calel".
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

    Manager surface (default): meld must be in PENDING_COMPLETION. The CLI
        checks that state before PATCH and fails loud without sending the
        complete request otherwise.
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

    if side == "vendor":
        payload: dict = {
            "is_complete": True,
            "date": completion_date,
            "reason": completion_notes or "",
        }
    else:
        current = _http_get(f"melds/{meld_id}/", cookie_hdr)
        current_status = _meld_status(current)
        if current_status != "PENDING_COMPLETION":
            _complete_meld_fail(
                meld_id,
                current_status,
                completion_notes,
                (
                    f"meld {meld_id} is {current_status}, must be PENDING_COMPLETION to complete. "
                    "Move it to PENDING_COMPLETION first, or relabel via the web UI."
                ),
            )
        # In-house melds cannot be closed via the manager complete/ path: PM
        # routes them to MAINTENANCE_COULD_NOT_COMPLETE. The differentiator is the
        # completion ACTION, not meld state — the tech-app checkout completes an
        # in-house meld, but manager complete/ strands it. Confirmed live
        # 2026-06-23 (TQY8B7DB stranded WITH completed work-entries; identical
        # state to a tech-app-completed meld). Fail loud BEFORE the PATCH so we
        # never strand a meld.
        if _meld_is_in_house(current):
            _complete_meld_fail(
                meld_id,
                current_status,
                completion_notes,
                (
                    f"meld {meld_id} is an in-house meld; the manager complete path "
                    "strands it in MAINTENANCE_COULD_NOT_COMPLETE. Complete it via the "
                    "tech-app checkout (tech checks out), or relabel via the web UI. "
                    "(Vendor melds use the vendor complete path and are unaffected.)"
                ),
            )
        payload = {}
        if completion_notes:
            payload["completion_notes"] = completion_notes

    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_patch(
        f"melds/{meld_id}/complete/", payload, cookie_hdr, csrf_token,
        side=side, vendor_id=vendor_id,
    )
    verified_status = None
    if side != "vendor":
        verified = _http_get(f"melds/{meld_id}/", cookie_hdr)
        verified_status = _meld_status(verified)
        if verified_status != "COMPLETED":
            _complete_meld_fail(
                meld_id,
                verified_status,
                completion_notes,
                f"meld {meld_id} complete request did not reach COMPLETED; actual state is {verified_status}.",
                result=result,
            )
    return {
        "ok": True,
        "meld_id": meld_id,
        "completion_notes": completion_notes,
        "side": side,
        "result": result,
        **({"status": verified_status} if verified_status is not None else {}),
    }


def _meld_is_in_house(meld: Any) -> bool:
    """True if the meld has an in-house servicer assignment.

    In-house melds complete via the tech-app checkout; the manager complete/
    path strands them in MAINTENANCE_COULD_NOT_COMPLETE (PM behavior confirmed
    live 2026-06-23). Vendor melds have an empty `in_house_servicers` and carry a
    `vendorassignment` instead, so they do not trip this guard.
    """
    if not isinstance(meld, dict):
        return False
    servicers = meld.get("in_house_servicers")
    return isinstance(servicers, list) and len(servicers) > 0


def _meld_status(meld: Any) -> Optional[str]:
    if isinstance(meld, dict):
        status = meld.get("status")
        if status is not None:
            return str(status)
    return None


def _complete_meld_fail(
    meld_id: str,
    status: Optional[str],
    completion_notes: Optional[str],
    message: str,
    *,
    result: Optional[Any] = None,
) -> None:
    body: dict = {
        "ok": False,
        "error": message,
        "meld_id": meld_id,
        "status": status,
        "completion_notes": completion_notes,
    }
    if result is not None:
        body["result"] = result
    print(json.dumps(body), file=sys.stderr)
    sys.exit(1)


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
        segments_to_keep: Existing segment IDs to retain. Must be supplied
            explicitly; pass [] only after the caller has verified there are no
            existing segments to preserve.
        mark_scheduled: PM flag — leave False unless echoing the web UI.
        appointments_required: Number of appointment windows needed (default 1).
    """
    if not vendor_id:
        raise ValueError("vendor_id is required")
    vendor_id = str(vendor_id)
    assignment_id = int(assignment_id)
    if segments_to_keep is None:
        raise ValueError(
            "segments_to_keep is required for vendor_set_schedule; "
            "pass existing segment ids to preserve, or [] only after probing "
            "that no existing segments would be replaced."
        )

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
        "segments_to_keep": segments_to_keep,
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
    """Edit an existing work entry (top-level path, not nested).

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


def _compute_dtend(dtstart: str, duration_hours: float) -> str:
    """Return ISO 8601 dtend = dtstart + duration_hours.

    PM's management availability event takes (dtstart, dtend), NOT
    (dtstart, duration). We compute dtend client-side, mirroring the
    schedule_vendor flow. Falls back to dtstart if the input can't be
    parsed (PM then rejects the shape with a 400 instead of us guessing).
    """
    from datetime import datetime, timedelta
    try:
        # Python's fromisoformat handles "+04:00" since 3.11; pad "Z" to "+00:00".
        start_dt = datetime.fromisoformat(dtstart.replace("Z", "+00:00"))
        return (start_dt + timedelta(hours=duration_hours)).isoformat()
    except Exception:
        return dtstart


def _management_appointment_for_accept(meld: Any) -> tuple[Optional[dict], Optional[dict]]:
    if not isinstance(meld, dict):
        return None, {
            "ok": False,
            "error": "Unexpected PM meld schema: expected object response",
            "schema_divergence": True,
        }
    if "managementappointment" not in meld:
        return None, {
            "ok": False,
            "error": "Unexpected PM meld schema: missing managementappointment field",
            "schema_divergence": True,
        }
    appts = meld.get("managementappointment")
    if not isinstance(appts, list):
        return None, {
            "ok": False,
            "error": "Unexpected PM meld schema: managementappointment is not a list",
            "schema_divergence": True,
        }
    if not appts:
        return None, {"ok": False, "error": "No in-house tech assignment found on this meld"}
    appt = appts[0]
    if not isinstance(appt, dict) or "id" not in appt:
        return None, {
            "ok": False,
            "error": "Unexpected PM meld schema: managementappointment[0].id missing",
            "schema_divergence": True,
        }
    return appt, None


def _destructive_accept_guard(meld: dict, appt: dict, command_name: str) -> Optional[dict]:
    # Fail loud rather than silently destroy existing availability data. The
    # accept/ PATCH sends segments_to_keep:[] — correct for the first-schedule
    # or empty-zombie case, but destructive if any real window already exists.
    if (
        appt.get("availability_segment")
        or appt.get("management_availability_segments")
        or meld.get("management_availability_segments")
    ):
        return {
            "ok": False,
            "error": (
                "Meld already has scheduled or proposed availability segments; "
                f"`{command_name}` would replace them. Use the reschedule flow instead."
            ),
        }
    return None


def _accept_with_window(
    meld_id: int,
    dtstart: str,
    duration_hours: float,
    cookie_hdr: str,
    csrf_token: str,
) -> tuple[dict, str]:
    dtend = _compute_dtend(dtstart, duration_hours)
    payload = {
        "mark_scheduled": True,
        "segments_to_keep": [],
        "management_availability_segments": [
            {"event": {"dtstart": dtstart, "dtend": dtend}}
        ],
    }
    result = _http_patch(f"melds/{meld_id}/accept/", payload, cookie_hdr, csrf_token)

    booked_start = dtstart
    segs = result.get("management_availability_segments") if isinstance(result, dict) else None
    if segs and isinstance(segs[0], dict):
        event = segs[0].get("event") or {}
        booked_start = event.get("dtstart", dtstart)
    return result, booked_start


@with_recapture_retry
def schedule_appointment(meld_id: str, dtstart: str, duration_hours: float = 2.0) -> dict:
    """Schedule an in-house tech appointment window on a meld.

    Args:
        meld_id: Meld ID.
        dtstart: ISO 8601 datetime string, e.g. '2026-04-27T14:00:00-04:00'.
        duration_hours: Appointment duration in hours (default 2).

    The meld must have an in-house tech assigned — PM creates the
    managementappointment object at assignment time, and the meld sits at
    status PENDING_MORE_MANAGEMENT_AVAILABILITY with an empty
    management_availability_segments list.

    Root cause of the prior HTTP 500 (diagnosed live 2026-06-03, demo
    fixture meld 12937555): the old flow PUT an `availability_segment`
    payload to `management-appointments/{appt_id}/schedule/`. That endpoint
    is a SELECT-from-existing action — it has no availability segment to
    bind and the server-side handler 500s (null deref) for EVERY payload
    shape that passes serializer validation. The PM management-app frontend
    never calls that endpoint for an unstarted in-house meld; it calls the
    meld-level `accept` action, supplying the availability window as a NEW
    management availability segment:

        PATCH melds/{meld_id}/accept/
        {
          "mark_scheduled": true,
          "segments_to_keep": [],
          "management_availability_segments": [{"event": {"dtstart", "dtend"}}]
        }

    Verified live: this 200s, creates the availability segment, populates
    the appointment's availability_segment, and flips the meld to
    PENDING_COMPLETION. The event takes (dtstart, dtend) — NOT duration —
    so we compute dtend = dtstart + duration_hours.
    """
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    meld = _http_get(f"melds/{meld_id}/", cookie_hdr)
    appt, error = _management_appointment_for_accept(meld)
    if error:
        return error
    appt_id = appt["id"]

    destructive_error = _destructive_accept_guard(meld, appt, "schedule")
    if destructive_error:
        return destructive_error

    result, booked_start = _accept_with_window(
        meld_id, dtstart, duration_hours, cookie_hdr, csrf_token
    )

    return {
        "ok": True,
        "meld_id": meld_id,
        "appointment_id": appt_id,
        "dtstart": booked_start,
        "duration_hours": duration_hours,
        "result": result,
    }


@with_recapture_retry
def force_pending_completion(meld_id: str, dtstart: Optional[str] = None, duration_hours: float = 0.25) -> dict:
    """DISABLED. Previously moved an in-house meld to PENDING_COMPLETION via accept/.

    Hard-guarded 2026-06-23: this only ever produces an IN-HOUSE PENDING_COMPLETION
    meld, and in-house melds cannot be closed via the manager complete/ path (they
    strand in MAINTENANCE_COULD_NOT_COMPLETE — see complete_meld + the in-house
    guard). It also mutated PM state by booking a synthetic appointment. There is
    no safe close use-case, so the function refuses up front without mutating.
    Complete in-house melds via the tech-app checkout, or relabel via the web UI.
    """
    meld_id = _validate_meld_id(meld_id)
    return {
        "ok": False,
        "deprecated": True,
        "error": (
            "force-pending-completion is disabled: it only produces in-house "
            "PENDING_COMPLETION melds, which strand in MAINTENANCE_COULD_NOT_COMPLETE "
            "when closed via manager complete/. Complete in-house melds via the "
            "tech-app checkout, or relabel via the web UI."
        ),
        "meld_id": meld_id,
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
        # trailing-space first_names (e.g. "Erica " / "Mapp") that would
        # otherwise produce "erica  mapp" and miss a "erica mapp" needle.
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
    string (e.g. ``"(706) 913-7178"``) and email is top-level ``email``.
    There is NO nested ``contact`` or ``user`` object on the list shape (the
    detail endpoint ``/api/tenants/{id}/`` does return the nested objects;
    see ``get_tenant``).

    Search semantics (case-insensitive):
      * Name: matched against the combined ``"first_name last_name"`` string
        so multi-word queries like ``"Erica Mapp"`` work.
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
def invite_tenant(
    unit_id,
    first_name: str,
    last_name: str,
    email: str,
    cell_phone: str,
    home_phone: str = "",
    secondary_email: Optional[str] = None,
    notes: str = "",
    should_invite: bool = True,
) -> dict:
    """Create a tenant on a unit and optionally send the PM invite email.

    Captured web UI contract (2026-05-31): POST /api/tenants/ with contact,
    names, notes, should_invite, and units containing the fully hydrated unit
    object from GET /api/units/{id}/. A stripped {"id": unit_id} is not enough
    for this endpoint.
    """
    unit_id_int = int(unit_id)
    unit = get_unit(unit_id_int)
    if not isinstance(unit, dict):
        raise RuntimeError(
            f"GET units/{unit_id_int}/ returned non-dict (cannot tenant-create)"
        )

    contact = {
        "primary_email": email,
        "secondary_email": secondary_email if secondary_email is not None else email,
        "cell_phone": cell_phone,
        "home_phone": home_phone or "",
    }

    payload = {
        "contact": contact,
        "units": [unit],
        "first_name": first_name,
        "last_name": last_name,
        "notes": notes or "",
        "should_invite": should_invite,
    }

    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    result = _http_post_no_exit("tenants/", payload, cookie_hdr, csrf_token)

    if isinstance(result, dict) and result.get("status_code"):
        cell_errors = ((result.get("contact") or {}).get("cell_phone") or [])
        if result.get("status_code") == 400 and cell_errors:
            return {
                "ok": False,
                "error": "malformed cell phone",
                "cell_phone_errors": cell_errors,
                "status_code": 400,
                "detail": result,
            }
        return {"ok": False, "error": result.get("error", "tenant invite failed"), "detail": result}

    contact_result = result.get("contact") if isinstance(result, dict) else {}
    last_invite = result.get("last_invite") if isinstance(result, dict) else None
    return {
        "ok": True,
        "tenant_id": result.get("id") if isinstance(result, dict) else None,
        "contact_id": contact_result.get("id") if isinstance(contact_result, dict) else None,
        "unit_id": unit_id_int,
        "invited": result.get("invited") if isinstance(result, dict) else None,
        "should_invite": should_invite,
        "last_invite": last_invite,
        "result": result,
    }


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
    (HAR capture against tenant 4043079, status 200, round-trip-reverted).

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
def edit_tenant_contact(
    tenant_id,
    *,
    primary_email: Optional[str] = None,
    secondary_email: Optional[str] = None,
    cell_phone: Optional[str] = None,
    home_phone: Optional[str] = None,
    business_phone: Optional[str] = None,
) -> dict:
    """Update nested tenant contact fields via full-body PUT.

    NEW-2 capture (tenant-PUT-contact-edit-200, 2026-05-31) showed the PM web UI
    edits contact data through PUT /api/tenants/{id}/ with the full tenant object,
    not PATCH /api/contacts/{contact_id}/. We GET the tenant, mutate only nested
    contact fields, and PUT the whole tenant back so unrelated tenant fields
    survive the round trip.
    """
    tenant_id_int = int(tenant_id)
    updates = {
        "primary_email": primary_email,
        "secondary_email": secondary_email,
        "cell_phone": cell_phone,
        "home_phone": home_phone,
        "business_phone": business_phone,
    }
    updates = {key: value for key, value in updates.items() if value is not None}
    if not updates:
        raise ValueError("at least one contact field is required")

    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
    current = _http_get(f"tenants/{tenant_id_int}/", cookie_hdr)
    if not isinstance(current, dict):
        raise RuntimeError(
            f"GET tenants/{tenant_id_int}/ returned non-dict (cannot full-body-echo)"
        )
    contact = current.get("contact")
    if not isinstance(contact, dict):
        raise RuntimeError(
            f"GET tenants/{tenant_id_int}/ returned tenant without contact object"
        )

    contact.update(updates)
    result = _http_put(f"tenants/{tenant_id_int}/", current, cookie_hdr, csrf_token)
    result_contact = result.get("contact", contact) if isinstance(result, dict) else contact
    return {
        "ok": True,
        "tenant_id": tenant_id_int,
        "contact": result_contact,
        "updated_fields": sorted(updates),
        "result": result,
    }


update_tenant_contact = edit_tenant_contact


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


@with_recapture_retry
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
# PM API endpoints expect the integer PK (e.g. 12701108), not the human-facing
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
            f"meld_id must be the integer PK (e.g. 12701108), got {meld_id!r}. "
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

    Returns {'ok': False} without issuing any PATCH when no appointment on the
    meld is linked to the requested vendor_id (fail-loud; never books a
    different vendor).
    """
    meld_id = _validate_meld_id(meld_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)

    # Get the vendor appointment from the meld (live API uses `vendorappointment`,
    # NOT the legacy `vendorassignment` field that earlier mocks referenced).
    meld = _http_get(f"melds/{meld_id}/", cookie_hdr)
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
    matched_appt = None
    for appt in appointments:
        if not isinstance(appt, dict):
            continue
        linked_req = appt.get("assignment_request")
        if linked_req is not None and str(req_to_vendor.get(linked_req)) == str(vendor_id):
            appt_id = appt.get("id")
            request_id = linked_req
            matched_appt = appt
            break

    if appt_id is None:
        # No appointment on the meld is linked to the requested vendor. Fail
        # loud instead of booking a DIFFERENT vendor's appointment. The prior
        # "first appointment" fallback silently scheduled the wrong vendor and
        # reported success — a wrong-target/destructive default. Reaching here
        # means the match loop above found nothing, so we return before any
        # _http_patch is issued.
        return {
            "ok": False,
            "error": (
                f"Vendor {vendor_id} has no matching appointment on meld {meld_id}; "
                "refusing to schedule a different vendor"
            ),
        }

    if request_id is None:
        return {"ok": False, "error": f"Could not resolve assignment_request id for vendor {vendor_id}"}

    # Fail loud rather than silently destroy an existing booking. The PATCH
    # below sends segments_to_keep:[] — a replace-all correct only for the
    # first-schedule case (an unbooked appointment with no availability_segment).
    # If this vendor appointment is ALREADY scheduled (a booked
    # availability_segment) or the meld carries proposed vendor availability
    # windows, that empty keep-list would WIPE them. Refuse the destructive case
    # and point the caller at the reschedule flow. Mirrors the in-house guard in
    # schedule_appointment. The load-bearing signal is the appointment-level
    # availability_segment (verified present in the response shape); the
    # meld-level vendor_availability_segments check is defensive symmetry and is
    # simply absent/falsy if PM does not expose that key. Use .get() truthiness
    # (NOT `is not None`) so an empty {} placeholder for an unbooked appt still
    # allows scheduling.
    if (
        (matched_appt and matched_appt.get("availability_segment"))
        or meld.get("vendor_availability_segments")
    ):
        return {
            "ok": False,
            "error": (
                "Vendor appointment already has a scheduled availability segment; "
                "`schedule` would replace it. Use the reschedule flow instead."
            ),
        }

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
    if meld_id:
        data = _http_get(f"melds/{meld_id}/projects/", cookie_hdr)
        return data.get("results", data) if isinstance(data, dict) else data
    stop_at = limit if limit > 0 else None
    page_limit = min(limit, 100) if limit > 0 else 100
    results = _paginate_all(f"projects/?limit={page_limit}", cookie_hdr, stop_at=stop_at)
    return results[:limit] if limit > 0 else results


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
        coordinators (list of management-agent int ids, e.g. [57163]),
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
    """Edit a top-level project.

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


# ── unit-PK resolver ─────────────────────────────────────────────────────────
# A property's units carry an integer PK (`units[].id`) but are addressed by a
# human-typed label (`units[].unit`, e.g. "Unit A", "Apt 12", or a bare street
# address for single-unit properties). Callers (tenant invite, work-order
# create) need the integer PK. These helpers resolve a messy label to that PK
# WITHOUT ever silently committing the wrong unit: an inexact match returns a
# disambiguation list, never a guess.
#
# Shape verified live 2026-06-02 on the cookie/manager auth path:
#   GET /api/properties/{id}/            -> property object, EMBEDS units[]
#   GET /api/properties/?limit=N         -> {count,next,previous,results[]}, each
#                                           property EMBEDS units[]
#   unit PK field:    units[].id  (int, e.g. 1754320)
#   unit label field: units[].unit (str) + apartment/building/floor/suite/room
# The list endpoints do NOT honour server-side filters (prop=, search=,
# property_name= are all ignored — count is unchanged), so property-by-name
# resolution and unit-label matching are both CLIENT-SIDE. This mirrors the
# list_tenants client-filter note above.

_UNIT_LABEL_PREFIXES = ("apartment", "apt", "unit", "suite", "ste", "room", "rm", "no", "number")


def normalize_unit_label(raw: Any) -> str:
    """Normalize a messy unit label for comparison.

    "Apt 12", "Unit 12", "#12", "no. 12", "  12 " all normalize to "12";
    "Unit A" -> "a". Lowercases, strips a leading unit-designator word and any
    leading '#', and collapses internal whitespace. Pure function — unit-tested
    without the network. Used to match a human-typed address against the
    `unit` / apartment / suite / etc. fields on a property's units.
    """
    text = str(raw or "").strip().lower()
    text = text.lstrip("#").strip()
    # Strip a single leading designator word ("apt 12" -> "12"), but only when
    # something follows it — so a bare "unit" stays "unit".
    parts = text.split()
    if len(parts) >= 2:
        head = parts[0].rstrip(".")
        if head in _UNIT_LABEL_PREFIXES:
            text = " ".join(parts[1:])
    # The designator word can mask a leading '#': "Unit #12" becomes "#12" after
    # the prefix strip. Re-strip so "Unit #12" / "Apt #12" normalize to "12" —
    # the contract this function's docstring advertises.
    text = text.lstrip("#").strip()
    # Collapse internal whitespace.
    return " ".join(text.split())


# Fields that decisively identify a SINGLE unit. A normalized query that equals
# one of these is treated as a confident match. Deliberately EXCLUDES grouping
# fields (building, floor, department): those identify a GROUP of units, not one,
# so a match there can only ever be ambiguous. Including them let a query like
# "Unit A" false-match a unit whose building is "A" and silently return the wrong
# PK — violating the never-guess-a-wrong-PK guarantee. Excluded fields fall to the
# backstop path (list units, caller picks) instead of producing a confident PK.
_DECISIVE_UNIT_LABEL_FIELDS = ("unit", "apartment", "suite", "room")


def _unit_label_candidates(unit: dict) -> list[str]:
    """Comparable label forms for a unit's *decisive* (single-unit) address fields."""
    out: list[str] = []
    for field in _DECISIVE_UNIT_LABEL_FIELDS:
        val = unit.get(field)
        if val is None:
            continue
        norm = normalize_unit_label(val)
        if norm:
            out.append(norm)
    return out


def _summarize_unit(unit: dict) -> dict:
    """Compact unit descriptor for disambiguation / backstop output."""
    return {
        "id": unit.get("id"),
        "unit": unit.get("unit"),
        "apartment": unit.get("apartment") or None,
        "building": unit.get("building") or None,
        "suite": unit.get("suite") or None,
    }


@with_recapture_retry
def get_property_with_units(property_id) -> dict:
    """GET /api/properties/{property_id}/ — property object with embedded units[].

    The detail endpoint embeds the full units[] array (each with its integer
    `id` PK), so a single call yields every unit for the property. No separate
    units-by-property route exists; the list endpoints ignore server-side
    filters, so this detail fetch is the canonical "units for this property"
    primitive.
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    return _http_get(f"properties/{property_id}/", cookie_hdr)


def _resolve_property(property_ref: str, cookie_hdr: str) -> dict:
    """Resolve a property ref (int id or name/address substring) to its object.

    Numeric ref -> direct detail fetch (cheap, exact). Non-numeric -> paginate
    the full property roster (server-side filtering is unavailable) and match
    `property_name` / `line_1` by case-insensitive substring.

    Returns a dict with one of:
      {"property": {...}}                      single match
      {"ambiguous_properties": [ {...}, ... ]} >1 substring match
      {"not_found": "<ref>"}                   0 matches
    """
    raw = str(property_ref).strip()
    if raw.isdigit():
        prop = _http_get_no_exit(f"properties/{raw}/", cookie_hdr)
        if isinstance(prop, dict) and prop.get("id") is not None:
            return {"property": prop}
        return {"not_found": raw}

    needle = raw.lower()
    matches: list[dict] = []
    for prop in _paginate_all("properties/?limit=100", cookie_hdr):
        if not isinstance(prop, dict):
            continue
        hay = " ".join(
            str(prop.get(f) or "") for f in ("property_name", "line_1", "line_2")
        ).lower()
        if needle in hay:
            matches.append(prop)
    if len(matches) == 1:
        return {"property": matches[0]}
    if len(matches) > 1:
        return {
            "ambiguous_properties": [
                {"id": p.get("id"), "property_name": p.get("property_name"), "line_1": p.get("line_1")}
                for p in matches
            ]
        }
    return {"not_found": raw}


@with_recapture_retry
def resolve_unit_pk(property_ref, unit_address: str) -> dict:
    """Resolve (property, unit-address) -> integer unit PK, or a clear non-commit.

    Resolution order, never guessing:
      1. Resolve the property (int id -> detail fetch; name -> client-side
         substring match over the full roster).
      2. Pull the property's embedded units[].
      3. Match unit_address against each unit's normalized label candidates
         (unit / apartment / building / floor / suite / room). Exact normalized
         match wins; if exactly one unit on the property, it is returned as a
         confident single match regardless of label.
      4. On 0 or >1 matches, return a disambiguation/backstop payload listing
         every unit (id + label) — the caller picks, we never auto-commit.

    Returns one of:
      {"unit_id": <int>, "property_id": <int>, "matched_on": "<label>"}
      {"ambiguous": [ {id,unit,...}, ... ], "property_id": <int>, "query": "<addr>"}
      {"not_found": "<addr>", "property_id": <int>, "units": [ ... backstop ... ]}
      {"ambiguous_properties": [...]} | {"error": "property not found ..."}
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)

    resolved = _resolve_property(property_ref, cookie_hdr)
    if "property" not in resolved:
        if "ambiguous_properties" in resolved:
            return resolved
        return {
            "error": f"property '{resolved.get('not_found', property_ref)}' not found",
            "hint": "pass an integer property id, or run `pm units list-by-property <id>`",
        }

    prop = resolved["property"]
    property_id = prop.get("id")
    units = prop.get("units")
    if not isinstance(units, list):
        units = []

    backstop = [_summarize_unit(u) for u in units if isinstance(u, dict)]

    # A single-unit property: the lone unit IS the answer, label or not.
    real_units = [u for u in units if isinstance(u, dict) and u.get("id") is not None]
    if len(real_units) == 1:
        only = real_units[0]
        return {
            "unit_id": only["id"],
            "property_id": property_id,
            "matched_on": only.get("unit"),
            "note": "single-unit property",
        }

    target = normalize_unit_label(unit_address)
    exact: list[dict] = []
    for u in real_units:
        if target and target in _unit_label_candidates(u):
            exact.append(u)

    if len(exact) == 1:
        return {
            "unit_id": exact[0]["id"],
            "property_id": property_id,
            "matched_on": exact[0].get("unit"),
        }
    if len(exact) > 1:
        return {
            "ambiguous": [_summarize_unit(u) for u in exact],
            "property_id": property_id,
            "query": unit_address,
        }

    return {
        "not_found": unit_address,
        "property_id": property_id,
        "units": backstop,
        "hint": "no unit matched; pick an id from `units` above or run "
                "`pm units list-by-property <property_id>`",
    }


@with_recapture_retry
def list_units_by_property(property_ref) -> dict:
    """List every unit (with its integer PK) for a property — disambiguation backstop.

    Resolves the property the same way as resolve_unit_pk, then returns the
    embedded units[] with their PKs. The result reports the unit count so a
    caller can never be misled by a silently-truncated list (the units are
    embedded in the property object, so there is no pagination to truncate).
    """
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)

    resolved = _resolve_property(property_ref, cookie_hdr)
    if "property" not in resolved:
        if "ambiguous_properties" in resolved:
            return resolved
        return {
            "error": f"property '{resolved.get('not_found', property_ref)}' not found",
        }

    prop = resolved["property"]
    units = prop.get("units")
    if not isinstance(units, list):
        units = []
    real = [u for u in units if isinstance(u, dict)]
    return {
        "property_id": prop.get("id"),
        "property_name": prop.get("property_name"),
        "count": len(real),
        "units": [_summarize_unit(u) for u in real],
    }


@with_recapture_retry
def get_unit_by_address(property_ref, unit_address: str) -> dict:
    """Convenience lookup: resolve (property, address) and, on a confident single
    match, return the FULL unit object via GET /api/units/{id}/.

    On anything other than a confident single match, returns the same
    disambiguation/not-found payload as resolve_unit_pk so the caller still
    never gets a silently-wrong unit.
    """
    res = resolve_unit_pk(property_ref, unit_address)
    if "unit_id" in res:
        full = get_unit(res["unit_id"])
        if isinstance(full, dict):
            full.setdefault("_resolved", {"matched_on": res.get("matched_on"), "note": res.get("note")})
        return full
    return res


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
    silent misread class where in-house techs (Carlos / Casey / Silvano / etc)
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


def _writable_put_fields_from_options(options_meta: Any) -> set[str]:
    actions = options_meta.get("actions") if isinstance(options_meta, dict) else None
    put_fields = actions.get("PUT") if isinstance(actions, dict) else None
    if not isinstance(put_fields, dict):
        raise RuntimeError(
            "PropertyMeld did not advertise a PUT serializer for meld tenant linking"
        )
    return {
        key for key, meta in put_fields.items()
        if isinstance(meta, dict) and not meta.get("read_only")
    }


def _required_put_fields_from_options(options_meta: Any) -> set[str]:
    actions = options_meta.get("actions") if isinstance(options_meta, dict) else None
    put_fields = actions.get("PUT") if isinstance(actions, dict) else None
    if not isinstance(put_fields, dict):
        return set()
    return {
        key for key, meta in put_fields.items()
        if isinstance(meta, dict) and meta.get("required")
    }


def _build_meld_tenants_put_payload(current: dict, options_meta: Any, tenants: list) -> dict:
    writable_fields = _writable_put_fields_from_options(options_meta)
    payload = {
        key: current[key]
        for key in writable_fields
        if key in current
    }
    payload["tenants"] = tenants

    missing_required = sorted(
        key for key in _required_put_fields_from_options(options_meta)
        if key not in payload or payload[key] is None
    )
    if missing_required:
        raise RuntimeError(
            "PropertyMeld meld-tenants PUT payload is missing required field(s): "
            + ", ".join(missing_required)
        )
    return payload



@with_recapture_retry
def link_tenant_to_meld(meld_id: str, tenant_id) -> dict:
    """Link a tenant to a meld through PM's dedicated meld-tenants endpoint.

    Earlier versions tried to PATCH /api/melds/{meld_id}/ with a merged
    tenants array. PM accepted that PATCH with HTTP 2xx but left the relation
    unchanged. The management app exposes the real relation endpoint as
    /api/melds/{meld_id}/tenants/. Its live OPTIONS metadata advertises
    `actions.PUT`: PUT replaces the whole relation object, so we round-trip
    every writable field from the relation GET unchanged and replace only
    tenants.

    Hydration mirrors the create_meld_in_project fix (P1 #2): PM serializers
    may walk nested fields on the tenants array, so we send full objects from
    GET /api/tenants/{id}/ rather than stripped {"id": N} placeholders.

    Idempotent: if tenant_id is already linked, returns {"already_linked": True}
    without firing the PUT.

    Closes P1 #14 — gmail-tenant-link skill Step 5 was Playwright-only before.
    """
    meld_id = _validate_meld_id(meld_id)
    tenant_id_int = int(tenant_id)
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)

    current = _http_get(f"melds/{meld_id}/tenants/", cookie_hdr)
    existing_tenants = current.get("tenants") or []
    # str()-normalize ids (PM may return them as strings): a str-vs-int slip
    # here would skip the already-linked short-circuit and fall through to the
    # merge below, duplicating an already-present tenant on the meld.
    existing_ids = {
        str(t.get("id")) for t in existing_tenants
        if isinstance(t, dict) and t.get("id") is not None
    }

    if str(tenant_id_int) in existing_ids:
        return {
            "ok": True,
            "meld_id": meld_id,
            "tenant_id": tenant_id_int,
            "already_linked": True,
            "tenant_count": len(existing_tenants),
        }

    new_tenant = get_tenant(tenant_id_int)
    options_meta = _http_options(f"melds/{meld_id}/tenants/", cookie_hdr)
    # Dedup guard by normalized id: belt-and-suspenders so the no-dedup append
    # can never duplicate a tenant even if the short-circuit above is bypassed.
    merged_tenants = list(existing_tenants)
    if str(tenant_id_int) not in existing_ids:
        merged_tenants.append(new_tenant)

    csrf_token = _get_csrf_token(cookie_hdr)
    payload = _build_meld_tenants_put_payload(current, options_meta, merged_tenants)
    result = _http_put(f"melds/{meld_id}/tenants/", payload, cookie_hdr, csrf_token)

    # Verify-and-fail-loud: never trust the local merge or the PUT response.
    # Re-GET the relation endpoint and confirm the tenant actually persisted.
    verify = _http_get(f"melds/{meld_id}/tenants/", cookie_hdr)
    persisted_tenants = verify.get("tenants") or []
    # Normalize both sides to str: PM may return tenant ids as strings, and a
    # str-vs-int mismatch here would raise "did NOT persist" on a genuine
    # success (this module already coerces string ids on input elsewhere).
    persisted_ids = {
        str(t.get("id")) for t in persisted_tenants
        if isinstance(t, dict) and t.get("id") is not None
    }

    if str(tenant_id_int) not in persisted_ids:
        raise RuntimeError(
            f"Tenant {tenant_id_int} link did NOT persist on meld {meld_id}: "
            f"PropertyMeld accepted the dedicated meld-tenants PUT (HTTP 2xx) "
            f"but the relation is unchanged."
        )

    return {
        "ok": True,
        "meld_id": meld_id,
        "tenant_id": tenant_id_int,
        "linked": True,
        "tenant_count": len(persisted_tenants),
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
    page_limit = min(limit, 100) if limit > 0 else 100
    path = f"estimates/meld/{meld_id}/?limit={page_limit}"
    if status:
        path += "&" + urllib.parse.urlencode({"status": status})
    stop_at = limit if limit > 0 else None
    results = _paginate_all(path, cookie_hdr, stop_at=stop_at)
    return results[:limit] if limit > 0 else results


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
    payload = {}
    if estimate_number:
        payload["estimate_number"] = estimate_number
    if amount:
        payload["amount"] = float(amount)
    if description:
        payload["description"] = description
    if status:
        payload["status"] = status
    if not payload:
        return {"ok": False, "error": "no fields to update", "estimate_id": estimate_id}
    creds = _load_creds()
    cookie_hdr = _cookie_header(creds)
    csrf_token = _get_csrf_token(cookie_hdr)
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
