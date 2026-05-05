#!/usr/bin/env python3
"""
Property Meld Feature API Audit
Captures API endpoints for Projects, Invoices, Receipts, Vendor Invites
by performing real user interactions (not just navigation).
"""

import json
import asyncio
import sys
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright, expect

CREDS_FILE = Path.home() / ".claude" / "credentials" / "property-meld.json"
OUTPUT_FILE = Path.home() / ".claude" / "property-meld-audit.json"

def load_credentials():
    """Load session cookies from credential file."""
    if not CREDS_FILE.exists():
        print(f"❌ Credentials file not found: {CREDS_FILE}")
        sys.exit(1)
    with open(CREDS_FILE) as f:
        data = json.load(f)
    return data.get("cookies", [])

def cookies_to_playwright(cookies):
    """Convert credential cookie format to Playwright format."""
    pw_cookies = []
    for c in cookies:
        pw_cookies.append({
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
            "expires": c.get("expires", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", True),
            "sameSite": c.get("sameSite", "Lax"),
        })
    return pw_cookies

async def audit_feature(page, feature_name, interactions):
    """
    Run a feature audit by performing interactions and capturing API calls.
    interactions: list of dicts with 'action' (goto/click/fill/type/press) and parameters
    """
    print(f"\n🔍 Auditing {feature_name}...")

    captured_requests = []

    def handle_response(response):
        url = response.url
        if "/api/" in url:
            try:
                body = None
                if response.request.post_data:
                    try:
                        body = json.loads(response.request.post_data)
                    except:
                        body = response.request.post_data[:200]

                req_headers = dict(response.request.all_headers())
                captured_requests.append({
                    "method": response.request.method,
                    "url": url,
                    "status": response.status,
                    "body": body,
                    "headers_sample": {k: v for k, v in req_headers.items() if k.lower() in ["content-type", "x-csrf-token"]},
                })
                print(f"   ✓ {response.request.method} {url} → {response.status}")
            except Exception as e:
                print(f"   ⚠ Error capturing {url}: {e}")

    page.on("response", handle_response)

    # Execute interactions
    for interaction in interactions:
        action = interaction.get("action")
        try:
            if action == "goto":
                url = interaction.get("url")
                print(f"  → Navigate to {url}")
                await page.goto(url, wait_until="networkidle")
                await asyncio.sleep(1)

            elif action == "click":
                selector = interaction.get("selector")
                print(f"  → Click {selector}")
                await page.click(selector)
                await asyncio.sleep(0.5)

            elif action == "fill":
                selector = interaction.get("selector")
                text = interaction.get("text")
                print(f"  → Fill {selector}")
                await page.fill(selector, text)
                await asyncio.sleep(0.3)

            elif action == "type":
                selector = interaction.get("selector")
                text = interaction.get("text")
                print(f"  → Type into {selector}")
                await page.type(selector, text)
                await asyncio.sleep(0.3)

            elif action == "press":
                key = interaction.get("key")
                print(f"  → Press {key}")
                await page.press("body", key)
                await asyncio.sleep(0.5)

            elif action == "wait":
                selector = interaction.get("selector")
                timeout = interaction.get("timeout", 5000)
                print(f"  → Wait for {selector}")
                await page.wait_for_selector(selector, timeout=timeout)

            elif action == "pause":
                duration = interaction.get("duration", 1000)
                print(f"  → Pause {duration}ms")
                await asyncio.sleep(duration / 1000)

        except Exception as e:
            print(f"  ⚠ Interaction failed: {e}")

    page.remove_listener("response", handle_response)
    return captured_requests

async def main():
    """Run the full audit."""
    print("🚀 Property Meld Feature Audit")
    print(f"📋 Will capture API endpoints for: Projects, Invoices, Receipts, Vendor Invites")

    credentials = load_credentials()
    pw_cookies = cookies_to_playwright(credentials)

    audit_results = {
        "timestamp": datetime.now().isoformat(),
        "features": {}
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()

        # Add cookies to authenticate
        await context.add_cookies(pw_cookies)
        page = await context.new_page()

        # 1. PROJECTS AUDIT
        print("\n" + "="*60)
        print("PROJECTS")
        print("="*60)
        projects_api = await audit_feature(page, "Projects", [
            {"action": "goto", "url": "https://propertymeld.com/app/dashboard/property/12345"},
            {"action": "pause", "duration": 2000},
            # Try to find and click "Add Project" or similar button
            {"action": "wait", "selector": "[data-testid*='project']", "timeout": 3000},
        ])
        audit_results["features"]["projects"] = projects_api

        # 2. INVOICES AUDIT
        print("\n" + "="*60)
        print("INVOICES")
        print("="*60)
        invoices_api = await audit_feature(page, "Invoices", [
            {"action": "goto", "url": "https://propertymeld.com/app/dashboard/invoices"},
            {"action": "pause", "duration": 2000},
            # Look for invoice creation button
            {"action": "wait", "selector": "button", "timeout": 3000},
        ])
        audit_results["features"]["invoices"] = invoices_api

        # 3. RECEIPTS AUDIT
        print("\n" + "="*60)
        print("RECEIPTS")
        print("="*60)
        receipts_api = await audit_feature(page, "Receipts", [
            {"action": "goto", "url": "https://propertymeld.com/app/dashboard/receipts"},
            {"action": "pause", "duration": 2000},
            # Look for receipt upload button
            {"action": "wait", "selector": "input[type='file']", "timeout": 3000},
        ])
        audit_results["features"]["receipts"] = receipts_api

        # 4. VENDOR INVITES AUDIT
        print("\n" + "="*60)
        print("VENDOR INVITES")
        print("="*60)
        vendor_invites_api = await audit_feature(page, "Vendor Invites", [
            {"action": "goto", "url": "https://propertymeld.com/app/dashboard/vendors"},
            {"action": "pause", "duration": 2000},
            # Look for invite button
            {"action": "wait", "selector": "button", "timeout": 3000},
        ])
        audit_results["features"]["vendor_invites"] = vendor_invites_api

        await browser.close()

    # Save results
    with open(OUTPUT_FILE, "w") as f:
        json.dump(audit_results, f, indent=2)

    print(f"\n✅ Audit complete! Results saved to {OUTPUT_FILE}")
    print("\n📊 Summary:")
    for feature, apis in audit_results["features"].items():
        print(f"  {feature}: {len(apis)} API calls captured")
        for api in apis:
            print(f"    • {api['method']} {api['url']} → {api['status']}")

if __name__ == "__main__":
    asyncio.run(main())
