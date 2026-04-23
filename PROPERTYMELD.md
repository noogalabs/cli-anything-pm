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
| Vendors | `GET /vendor/` | |
| Comments | Browser only | Cookie-session API: `/m/{multitenant}/api/comments/?meld={id}` |
| Assign tech | Browser only | Playwright UI automation |

## Status Values (Nexus API)

- `open` — Active work orders
- `pending_completion` — Work done, awaiting review
- `completed` — Closed
- `canceled` — Canceled

## Browser Backend Session

Comments and tech assignment require a browser session. Credentials stored at `PM_CREDS_PATH` (JSON with `username`, `password`, `cookies` fields). Cookies are refreshed automatically on session expiry.

## Rate Limits

No documented rate limit from Property Meld. AscendOps convention: max 7 Nexus API calls/day (morning report window only).

## API Key Rotation (CLI)

When the Nexus OAuth credentials expire, use the CLI to rotate them:

```bash
# Rotate and print new credentials (then update Railway manually)
pm api-keys rotate

# Rotate AND push new credentials to Railway automatically
pm api-keys rotate --update-railway

# List existing API keys (names + client IDs, no secrets)
pm api-keys list
```

**Manual flow the CLI automates:**
1. `app.propertymeld.com` → click user icon (top right) → Switch Account Type
2. Select "Nexus Partner / Ascend Property Management"
3. Navigate to Settings > API Keys (`/338/n/338/nexus/api-keys/`)
4. Click "Create API Key"
5. Copy Client ID and Client Secret (shown ONCE)
6. Update Railway env vars: `PM_NEXUS_CLIENT_ID`, `PM_NEXUS_CLIENT_SECRET`
7. `railway redeploy --yes` in `emergency-dispatch-middleware/`

**Notes:**
- Nexus account ID is `338`, management account is `3287`
- Client secret is shown only once — always capture it immediately
- After Railway redeploy, confirm via `railway logs | grep "Fetched PM melds"`
