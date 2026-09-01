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

**Editable pipx installs (`pipx install --editable`) do not auto-update on source pulls.**
After pulling new commits, run:

```bash
pipx reinstall cli-anything-pm        # pm-stable surface
pipx reinstall cli-anything-pm-dev    # pm-dev surface (if installed separately)
```

The reinstall picks up any CLI-shape changes (new subcommands, new flags, group→command refactors).

This catches the install-lag class of bug, where an operator's `pm` binary is missing recently merged subcommands or flags. Source is correct; the editable install never refreshed.

**Triage tip:** If you see `command not found` or `no such option` for a CLI command that exists in `main` HEAD, suspect install lag first. Run `pipx reinstall cli-anything-pm` before scoping a source bug.

## Configuration

Copy the tracked synthetic example to a private local path:

```bash
cp config/propertymeld.example.json ~/.claude/credentials/propertymeld-config.json
export PROPERTYMELD_CONFIG=~/.claude/credentials/propertymeld-config.json
export PM_CLIENT_ID=your-client-id
export PM_CLIENT_SECRET=your-client-secret
```

Set `multitenant_id`, `nexus_account_id`, and `credentials_path` in the private
JSON file. Missing or malformed routing fails closed when an action runs; all
command help and the runtime command index remain available without config.

Get API credentials from: Property Meld > Settings > API / Nexus API

## Quick Start

```bash
pm probe                                     # Verify setup
pm work-orders list --status open --json    # List open work orders
pm work-orders get 900001 --json             # Single work order
pm work-orders comments 900001 --json        # Comments (browser)
pm work-orders assign-tech --work-order-id 900001 --tech Tech A --json
pm work-orders assign-vendor --work-order-id 900001 --vendor "Example HVAC" --json
pm vendors invite --email vendor@example.com --first-name Fixture --last-name Vendor --company "Fixture Plumbing" --line1 "123 Main St" --postcode 12345 --phone 2025550110
pm tenants invite --unit-id 9000007 --first-name Fixture --last-name Resident --email resident@example.com --cell 2025550110
pm tenants edit-contact 9000023 --cell 2025550110 --primary-email tenant@example.com
pm index --json                              # Runtime-derived command catalog
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

### Building the wheel

Build release wheels from the repository root with:

```bash
python setup.py bdist_wheel
```

The build command recreates both its source-copy staging directory and its final
wheel payload directory before copying modules. Generated `build/` and `dist/`
trees are ignored and must not be committed. The test suite verifies that the
complete `cli_anything/` wheel payload contains exactly the declared Python
source members with byte-for-byte parity and no extra file type. It then
installs the wheel in a fresh virtual environment and exercises the public
`pm insights` command tree.
