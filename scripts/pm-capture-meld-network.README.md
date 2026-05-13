# pm-capture-meld-network.py — Operator Guide

Reverse-engineers PropertyMeld manager-UI payloads (currently: projects
create + edit) by attaching SafariDriver to a live Safari tab, injecting a
fetch/XHR monkey-patch, and dumping captured request bodies + responses.

For PM-Blue queue #3 (project create + edit endpoint discovery).

## One-time setup

```bash
# Selenium (only if not already on the machine)
python3 -m pip install --user selenium

# Allow SafariDriver
safaridriver --enable                        # may prompt for sudo first run
# In Safari: Develop menu → Allow Remote Automation (one-time toggle)
```

If the Develop menu is missing: Safari → Preferences → Advanced → "Show
Develop menu in menu bar".

## Run

```bash
cd /Users/davidhunter/projects/cli-anything-pm
python3 scripts/pm-capture-meld-network.py --tenant <pm-dev-tenant-id>
```

`--tenant` is optional; without it the script lands on the PropertyMeld
login screen and lets you click into pm-dev from there.

Default output: `~/.cortextos/default/state/collie/pm-capture.json` (chmod
600). Override with `--output PATH`.

## What you do in Safari

The script will pause twice and wait for you to press Enter.

**Pause 1 — log in.**
Safari opens to PropertyMeld. Log in normally (handles MFA / SSO yourself).
Land on the pm-dev manager dashboard. Press Enter in the terminal.

The script then injects the capture wrapper. You should see
`Capture wrapper status: installed` in the terminal.

**Pause 2 — drive create + edit.**

1. **Create flow.** Click `+ New Project` (or whatever the manager UI labels
   it), fill the form with a clearly test-y name (e.g. `CAPTURE TEST 2026-05-13`),
   description, start + due dates, pick yourself as coordinator, pick a project
   type, pick a unit. Submit.

2. **Edit flow.** Open the project you just made, click `Edit`, change the
   name (e.g. append ` v2`), save.

Press Enter in the terminal once both flows are complete.

## What you get back

The script writes a JSON file shaped like:

```json
{
  "captured_at": "2026-05-13T...",
  "filter": "/api/projects/",
  "matched_count": 2,
  "total_intercepted": 47,
  "matched": [
    {"kind": "fetch", "method": "POST",  "url": ".../api/projects/", "reqBody": "...", "status": 201, "respBody": "..."},
    {"kind": "fetch", "method": "PATCH", "url": ".../api/projects/12345/", "reqBody": "...", "status": 200, "respBody": "..."}
  ],
  "performance_entries": [...]
}
```

The two key entries are the POST (create) and the PATCH (edit). Send the
file (or paste the `matched` array) back to Dane in chat. Dane will fan it
to me and I will land the final payload shape + run the real smoke.

## Cleanup

The test project you created sits in pm-dev — fine to leave for now, or
archive it via PM web later. pm-dev is sandbox, nothing to roll back.

## Troubleshooting

- **"Could not start SafariDriver"** — re-run `safaridriver --enable`,
  re-toggle Develop → Allow Remote Automation, close any other automated
  Safari session, retry.
- **`matched_count: 0`** — the create + edit calls did not go through
  `/api/projects/`. Re-run with a wider filter:
  ```bash
  python3 scripts/pm-capture-meld-network.py --filter "projects|melds|api/"
  ```
  Then ping Dane with the file — we will narrow it together.
- **Script hangs at "Press Enter"** — that is normal; it waits on stdin so
  you have time to do the UI flow. Press Enter when ready.
