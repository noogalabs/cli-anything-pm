# CLI-Anything: Property Meld

A CLI-Anything harness for Property Meld — the first PM work order CLI for AI agents.

## Installation

```bash
git clone https://github.com/noogalabs/cli-anything-pm.git
cd cli-anything-pm
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
pm work-orders assign-vendor --work-order-id 900001 --vendor "Fixture Service" --json
pm vendors invite --email vendor@example.com --first-name Fixture --last-name Vendor --company "Fixture Service" --line1 "123 Main St" --postcode 12345 --phone 2025550110
pm tenants invite --unit-id 9000025 --first-name Fixture --last-name Resident --email resident@example.com --cell 2025550110
pm tenants edit-contact 9000026 --cell 2025550110 --primary-email tenant@example.com
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

### Exhaustive work-order reads

`--complete` is an opt-in read protocol. Existing list/get output shapes stay
unchanged without it. Comments, work entries and each uploader-role file source
now follow their pagination chains even for legacy list output.

```bash
pm work-orders list --complete --limit 1
pm work-orders get 900001 --complete
pm work-orders comments 900001 --complete
pm work-orders files 900001 --complete
pm work-orders work-entries list 900001 --complete
pm work-orders notes 900001 --complete
```

The first command explicitly **ignores `--limit`** and exhausts the list. Its
scope is the meld roster, not every child resource on every meld. A per-row
work-entry marker still directs callers to the dedicated read. Cookie-path
client filters run after source exhaustion; `source_count` and `source_returned`
describe the pre-filter source, while `count` describes the filtered result.
Existing unsupported filter combinations still refuse. This change adds no
`updated-since` option and makes no claim that such a server filter works.

Collection envelopes use `schema_version: 1`, `results`, `count`, `next`,
`complete`, `pages`, `returned`, `basis`, and `resource`. Resource identity binds
the backend, endpoint, meld ID where applicable, and query filters. An absent
server count remains `null`; a terminal declared chain can still be certified.
A plain array or a payload lacking both a count and an explicit terminal `next`
does not prove completion and is refused in `--complete` mode. Provenance is
reported rather than assuming an undocumented array pagination convention.

Malformed links, changed filter scope, cross-origin/other-endpoint links,
cycles, repeated pages/IDs, a 50-page safety cap, inconsistent counts and a
terminal row count mismatch fail with a structured nonzero error. Next links
are checked before another authenticated GET. Only `cursor`, `offset`, `page`
and `limit` may change between pages; other filters, including repeated status
values and the comments meld ID, remain bound. No partial result is certified.

`get --complete` returns `result` (the meld, with `work_entries`), `comments`,
`files`, `notes`, and individual `resources` metadata for meld/notes/comments/
files/work_entries. All requested resources must succeed. Files preserve role
identity and per-role counts; identical numeric IDs in different uploader-role
tables remain separate files. `notes` reads existing `maintenance_notes` and
`completion_notes` fields plus comments and work-entry text, not a new notes
endpoint. Reuse these fields from `get --complete` instead of fetching notes
again. Missing note fields or unreadable child pages refuse completeness.

Completion means the reported endpoint chain was exhausted consistently. It
does not claim a transactionally frozen snapshot while remote records change.
All regression fixtures are synthetic; no live response bodies are test data.
Opt-in complete reads fail closed on session expiry without invoking session
recapture, a browser or an authentication helper; legacy auth behavior stays.

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
