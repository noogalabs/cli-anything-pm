# CLI-Anything: Property Meld

A CLI-Anything harness for Property Meld — the first PM work order CLI for AI agents.

## Installation

```bash
git clone https://github.com/your-org/cli-anything-propertymeld.git
cd cli-anything-propertymeld
pip install -e .
playwright install chromium  # for browser backend commands
```

### Post-merge install refresh (operator side)

**Person003able pipx installs (`pipx install --editable`) do not auto-update on source pulls.**
After pulling new commits, run:

```bash
pipx reinstall cli-anything-pm        # pm-stable surface
pipx reinstall cli-anything-pm-dev    # pm-dev surface (if installed separately)
```

The reinstall picks up any CLI-shape changes (new subcommands, new flags, group→command refactors).

This catches the install-lag class of bug, where an operator's `pm` binary is missing recently merged subcommands or flags. Source is correct; the editable install never refreshed.

**Triage tip:** If you see `command not found` or `no such option` for a CLI command that exists in `main` HEAD, suspect install lag first. Run `pipx reinstall cli-anything-pm` before scoping a source bug.

## Configuration

Add to your agent's `.env`:

```bash
PM_CLIENT_ID=your-client-id
PM_CLIENT_SECRET=your-client-secret
PM_MULTITENANT_ID=3287        # Your multitenant ID
PM_CREDS_PATH=~/.claude/credentials/property-meld.json  # For browser backend
```

Get API credentials from: Property Meld > Settings > API / Nexus API

## Quick Start

```bash
pm probe                                     # Verify setup
pm work-orders list --status open --json    # List open work orders
pm work-orders get 12345 --json             # Single work order
pm work-orders comments 12345 --json        # Comments (browser)
pm work-orders assign-tech --work-order-id 12345 --tech Person019 --json
pm work-orders assign-vendor --work-order-id 12345 --vendor "Dyer HVAC" --json
pm vendors invite --email vendor@example.com --first-name Fixture --last-name Person035 --company "Person035 Plumbing" --line1 "123 Main St" --postcode 37421 --phone 2025550110
pm tenants invite --unit-id 9000005 --first-name Fixture --last-name Resident --email fixture.alpha@example.com --cell 2025550110
pm tenants edit-contact 9000020 --cell 2025550110 --primary-email tenant@example.com
```

### Read-only Insights analytics

```bash
pm insights melds --limit 100
pm insights turnovers --project --limit 100
pm insights benchmarks --work-category TURNOVER --limit 100
```

Insights commands fetch only the fixed authenticated Parquet GET endpoints and
emit a safe JSON projection. Meld and turnover rows join
`vendor_assigned_name` to the complete Nexus vendor roster. Each row retains
the source name and reports `resolved`, `unresolved`, `ambiguous`, or
`not_applicable`; unresolved and ambiguous rows are never discarded. Session
expiry fails closed instead of invoking the write-capable recapture path.

## Architecture

Dual backend:
- **API backend** (`api_backend.py`) — Nexus API OAuth2 for all reads
- **Browser backend** (`browser_backend.py`) — Playwright for actions API doesn't support

## Contributing

This is a CLI-Anything harness. Follow the [CLI-Anything contribution guide](https://github.com/HKUDS/CLI-Anything) for CLI-Hub submission.
