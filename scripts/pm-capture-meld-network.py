#!/usr/bin/env python3
"""
PropertyMeld network capture via SafariDriver.

Drives a Safari tab using SafariDriver and captures /api/projects/ POST + PATCH
requests (URL, method, body, response) by injecting a fetch + XHR monkey-patch
before the page makes the call. window.performance.getEntries() is also dumped
as a sanity check for request URLs and timing.

Use this to reverse-engineer the PropertyMeld manager UI payload for endpoints
that public docs do not cover (projects create/edit, etc).

Why monkey-patch instead of plain performance.getEntries:
    performance.getEntries() returns PerformanceResourceTiming entries — URLs +
    timing only, no request or response bodies. To recover the actual JSON
    payload posted by the manager UI we have to wrap window.fetch and
    XMLHttpRequest.prototype.send before the request fires.

Flow:
    1. Script opens Safari (or attaches to a running session) and navigates to
       the PropertyMeld dashboard.
    2. Operator logs in interactively (handles SSO / MFA themselves).
    3. Operator presses Enter in the terminal. Script injects the capture
       wrapper into the active tab.
    4. Operator drives the create-project flow (open form, fill fields, save).
       Operator drives the edit-project flow (open project, edit, save).
    5. Operator presses Enter again. Script reads window.__pmCaptured back,
       filters for /api/projects/, and writes the result to OUTPUT_PATH.

Usage:
    python3 pm-capture-meld-network.py [--output PATH] [--filter REGEX]
                                       [--tenant TENANT] [--no-launch]

Requirements:
    pip install selenium
    safaridriver --enable   (one-time, may need sudo on first run)
    Safari ▸ Develop ▸ Allow Remote Automation (one-time)
"""

import argparse
import json
import os
import re
import sys
import time

DEFAULT_OUTPUT = os.path.expanduser("~/.cortextos/default/state/collie/pm-capture.json")
DEFAULT_FILTER = r"/api/projects/"
PM_BASE = "https://app.propertymeld.com"


CAPTURE_INJECT = r"""
(function() {
    if (window.__pmCaptured) { return 'already-installed'; }
    window.__pmCaptured = [];

    var origFetch = window.fetch;
    window.fetch = function(input, init) {
        var url = typeof input === 'string' ? input : (input && input.url) || '';
        var method = ((init && init.method) ||
                      (typeof input === 'object' && input && input.method) ||
                      'GET').toUpperCase();
        var body = (init && init.body) || null;
        var entry = {
            ts: Date.now(),
            kind: 'fetch',
            url: url,
            method: method,
            reqBody: typeof body === 'string' ? body : (body ? String(body) : null)
        };
        try {
            return origFetch.apply(this, arguments).then(function(resp) {
                entry.status = resp.status;
                var clone = resp.clone();
                return clone.text().then(function(text) {
                    entry.respBody = text;
                    window.__pmCaptured.push(entry);
                    return resp;
                });
            }).catch(function(err) {
                entry.error = String(err);
                window.__pmCaptured.push(entry);
                throw err;
            });
        } catch (e) {
            entry.error = String(e);
            window.__pmCaptured.push(entry);
            throw e;
        }
    };

    var origOpen = XMLHttpRequest.prototype.open;
    var origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__pmMethod = method;
        this.__pmUrl = url;
        return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
        var xhr = this;
        var entry = {
            ts: Date.now(),
            kind: 'xhr',
            url: xhr.__pmUrl || '',
            method: (xhr.__pmMethod || 'GET').toUpperCase(),
            reqBody: typeof body === 'string' ? body : (body ? String(body) : null)
        };
        xhr.addEventListener('loadend', function() {
            entry.status = xhr.status;
            try { entry.respBody = xhr.responseText; } catch (e) { entry.respBody = null; }
            window.__pmCaptured.push(entry);
        });
        return origSend.apply(this, arguments);
    };

    return 'installed';
})();
"""


READ_BUFFER = r"return JSON.stringify(window.__pmCaptured || []);"
READ_PERF = r"return JSON.stringify(window.performance.getEntries().map(function(e){return {name:e.name,entryType:e.entryType,startTime:e.startTime,duration:e.duration,initiatorType:e.initiatorType||null};}));"


def _enable_safaridriver() -> None:
    import subprocess
    try:
        subprocess.run(
            ["/usr/bin/safaridriver", "--enable"],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        pass


def _start_driver():
    try:
        from selenium import webdriver
        from selenium.webdriver.safari.options import Options
    except ImportError:
        print("ERROR: selenium is not installed. Run: pip install selenium", file=sys.stderr)
        sys.exit(1)

    _enable_safaridriver()
    try:
        return webdriver.Safari(options=Options())
    except Exception as e:
        print(f"ERROR: Could not start SafariDriver: {e}", file=sys.stderr)
        print(
            "\nTroubleshooting:\n"
            "  1. Safari → Develop menu → Allow Remote Automation\n"
            "  2. safaridriver --enable (may need sudo)\n"
            "  3. Close any existing SafariDriver session and retry",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Output JSON path (default: {DEFAULT_OUTPUT})")
    ap.add_argument("--filter", default=DEFAULT_FILTER, help=f"Regex applied to entry URL (default: {DEFAULT_FILTER!r})")
    ap.add_argument("--tenant", default=None, help="If set, navigate to https://app.propertymeld.com/<tenant>/ on start")
    ap.add_argument("--no-launch", action="store_true", help="Do not navigate; use whatever tab SafariDriver attaches to")
    args = ap.parse_args()

    pattern = re.compile(args.filter)
    driver = _start_driver()

    try:
        if not args.no_launch:
            start_url = f"{PM_BASE}/{args.tenant}/" if args.tenant else f"{PM_BASE}/accounts/login/"
            print(f"Navigating Safari to {start_url}")
            driver.get(start_url)

        print()
        print("Step 1 — log into PropertyMeld in the Safari window (handle SSO/MFA).")
        input("       Press Enter once you are on the PropertyMeld manager dashboard...")

        result = driver.execute_script(CAPTURE_INJECT)
        print(f"Capture wrapper status: {result}")

        print()
        print("Step 2 — drive the CREATE project flow in Safari:")
        print("         click + New Project, fill the form, submit.")
        print("Step 3 — drive the EDIT project flow in Safari:")
        print("         open a project, click Edit, change a field, save.")
        input("       Press Enter once both flows are complete...")

        raw = driver.execute_script(READ_BUFFER)
        captured = json.loads(raw) if raw else []
        perf_raw = driver.execute_script(READ_PERF)
        perf = json.loads(perf_raw) if perf_raw else []

        matched = [e for e in captured if pattern.search(e.get("url", ""))]
        out = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "filter": args.filter,
            "matched_count": len(matched),
            "total_intercepted": len(captured),
            "performance_entries_count": len(perf),
            "matched": matched,
            "performance_entries": [p for p in perf if pattern.search(p.get("name", ""))],
        }

        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        os.chmod(args.output, 0o600)

        print()
        print(f"Captured {len(matched)} matching request(s) (of {len(captured)} intercepted).")
        print(f"Saved to: {args.output}")
        if matched:
            for e in matched:
                print(f"  {e.get('method')} {e.get('url')} -> {e.get('status')}")

    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
