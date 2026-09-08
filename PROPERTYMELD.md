# Property Meld — Architecture SOP

## API Access

Property Meld uses OAuth2 client credentials (Nexus API).

**Token endpoint:** `POST https://app.propertymeld.com/api/v2/oauth/token/`
**Required header on all requests:** `X-Multitenant-Id: <your multitenant ID>`
**Base URL:** `https://app.propertymeld.com/api/v2`

## Endpoint Map

| Resource | Endpoint | Notes |
|----------|----------|-------|
| Work orders | `GET /meld/` | Singular "meld", NOT "melds" |
| Single work order | `GET /meld/{id}/` | |
| Properties | `GET /property/` | |
| Vendors | `GET /vendor/` | Nexus read path |
| Vendor invite | `POST /vendors/invite/` | Cookie-session manager API; creates vendor + sends portal invite |
| Tenant invite | `POST /tenants/` | Cookie-session manager API; creates tenant and optionally sends invite |
| Comments | Browser only | Cookie-session API: `/m/{multitenant}/api/comments/?meld={id}` |
| Assign tech | Browser-session API | Canonical CLI: `pm work-orders assign-tech`; top-level `pm assign-tech` remains as a deprecated alias |
| Assign vendor | Browser-session API | Canonical CLI: `pm work-orders assign-vendor`; top-level `pm assign-vendor` remains as a deprecated alias |

## Status Values (Nexus API)

- `open` — Active work orders
- `pending_completion` — Work done, awaiting review
- `completed` — Closed
- `canceled` — Canceled

## Browser Backend Session

Comments, tech assignment, vendor assignment, vendor invites, tenant invites, and tenant contact edits require a browser session. The session file is selected by `credentials_path` in the private `PROPERTYMELD_CONFIG` JSON (with `username`, `password`, `cookies` fields). Cookies are refreshed automatically on session expiry.

## Vendor Person004 (CLI)

Create the vendor and send the portal invite in one manager-side call:

```bash
pm vendors invite \
  --email vendor@example.com \
  --first-name Fixture \
  --last-name Vendor \
  --company "Fixture Service" \
  --line1 "123 Main St" \
  --postcode 12345 \
  --phone 2025550110
```

Captured request shape:

```json
{"email":"vendor@example.com","first_name":"Fixture","last_name":"Vendor","name":"Fixture Service","line_1":"123 Main St","state":"","postcode":"12345","phone":"2025550110"}
```

PM returns HTTP 400 when the invite email already exists; the CLI surfaces that as `ok: false` with `already_exists` / `already_invited`.

## Tenant Person004 (CLI)

Create a tenant on a unit and send the portal invite:

```bash
pm tenants invite \
  --unit-id 9000025 \
  --first-name Fixture \
  --last-name Resident \
  --email fixture.alpha@example.com \
  --cell 2025550110
```

Use `--no-invite` to create the tenant record without sending the invite email.
The CLI hydrates the full unit object before posting to PM because the captured
manager UI request sends `units: [<full unit>]`, not a stripped id-only unit.

## Tenant Person001 Person003 (CLI)

Person003 tenant contact fields through the manager-side full-echo tenant PUT:

```bash
pm tenants edit-contact 9000026 \
  --cell 2025550110 \
  --home 2025550111 \
  --business 2025550112 \
  --primary-email tenant@example.com \
  --secondary-email tenant.alt@example.com
```

The CLI fetches `tenants/{id}/`, mutates only the specified nested `contact`
fields, then PUTs the full tenant object back to `tenants/{id}/`. This mirrors
the web UI capture and avoids the broken `PATCH /contacts/{id}/` path, which
returns 403.

## Rate Limits

Property Meld does not document a rate limit. Each installation must set its own operational call budget outside this shared repository.

## API Key Rotation (CLI)

When the Nexus OAuth credentials expire, use the CLI to rotate them. The
new client_secret is never printed: PropertyMeld shows it exactly once and
the CLI redacts every sensitive key at its output boundary, so the command
refuses to run unless you give it at least one non-displaying destination.

```bash
# Rotate and write PM_CLIENT_ID + PM_CLIENT_SECRET into an env file
# (atomic, mode 0600; an existing definition is replaced in place)
pm api-keys rotate --update-env /path/to/.env

# Rotate and push the new credentials to Railway
pm api-keys rotate --update-railway

# Both destinations in one run
pm api-keys rotate --update-railway --update-env /path/to/.env

# List existing API keys (names + client IDs, no secrets)
pm api-keys list
```

Output redaction note: every command's output passes through a redaction
boundary that replaces values under sensitive keys and scrubs credential
shapes inside free text. A string value that itself parses as a JSON object
or array is parsed, walked and re-serialized, even when it contains no
sensitive key, so its formatting (whitespace, key order preservation, float
representation) can differ from the input on payloads that needed no
redaction; a value that cannot be re-serialized as strict JSON is scrubbed
as text instead.

The result reports each delivery under `deliveries` (path, variable, status).
If any requested delivery fails after the key is minted, the command exits 1
and says so; the value is still never displayed. Running `pm api-keys rotate`
with no destination exits 1 before minting anything.

**Manual flow the CLI automates:**
1. `app.propertymeld.com` → click user icon (top right) → Switch Account Type
2. Select the Nexus Partner entry for your organization.
3. Navigate to Settings > API Keys (`/2000/n/2000/nexus/api-keys/`)
4. Click "Create API Key"
5. Copy Client ID and Client Secret (shown ONCE)
6. Update the Railway env vars the runtime actually reads: `PM_CLIENT_ID`, `PM_CLIENT_SECRET` (NOT `PM_NEXUS_CLIENT_ID` / `PM_NEXUS_CLIENT_SECRET`, which the runtime ignores). Prefer `pm api-keys rotate --update-railway` over this manual step: it mints and delivers the pair in one call and never displays the secret.
7. `railway redeploy --yes` in `emergency-dispatch-middleware/`

**Notes:**
- Record the Nexus and management account IDs in your private config file.
- Client secret is shown only once — always capture it immediately
- After Railway redeploy, confirm via `railway logs | grep "Fetched PM melds"`
