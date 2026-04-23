# CLI-Anything: Property Meld

A CLI-Anything harness for Property Meld — the first PM work order CLI for AI agents.

## Installation

```bash
git clone https://github.com/your-org/cli-anything-propertymeld.git
cd cli-anything-propertymeld
pip install -e .
playwright install chromium  # for browser backend commands
```

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
pm assign-tech --work-order-id 12345 --tech Carlos --json
```

## Architecture

Dual backend:
- **API backend** (`api_backend.py`) — Nexus API OAuth2 for all reads
- **Browser backend** (`browser_backend.py`) — Playwright for actions API doesn't support

## Contributing

This is a CLI-Anything harness. Follow the [CLI-Anything contribution guide](https://github.com/HKUDS/CLI-Anything) for CLI-Hub submission.
