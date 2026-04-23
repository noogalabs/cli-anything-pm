"""
Property Meld CLI — pm command.
Routes read commands to Nexus API backend; write/browser-session commands to http_backend.

Usage:
    pm work-orders list --status open --json
    pm work-orders get 12345 --json
    pm work-orders comments 12345 --json
    pm work-orders send-message --meld-id 12345 --text "Heading over now"
    pm work-orders clone --meld-id 12345
    pm properties list --json
    pm vendors list --json
    pm assign-tech --work-order-id 12345 --tech Carlos --json
"""
import json
import sys

import click

from . import api_backend, http_backend
from .utils import output_json


@click.group()
@click.version_option("0.1.0", prog_name="pm")
def cli():
    """Property Meld CLI — read work orders, properties, vendors; assign techs."""
    pass


# ── work-orders group ──────────────────────────────────────────────────────────

@cli.group("work-orders")
def work_orders():
    """Work order commands."""
    pass


@work_orders.command("list")
@click.option("--status", default=None,
              type=click.Choice(["open", "pending", "completed", "canceled"], case_sensitive=False),
              help="Filter by status (default: all)")
@click.option("--limit", default=25, show_default=True, help="Maximum results")
@click.option("--json", "as_json", is_flag=True, default=True, help="Output as JSON (default)")
def list_work_orders(status, limit, as_json):
    """List work orders."""
    results = api_backend.list_work_orders(status=status, limit=limit)
    output_json(results)


@work_orders.command("get")
@click.argument("meld_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_work_order(meld_id, as_json):
    """Get a single work order by ID."""
    result = api_backend.get_work_order(meld_id)
    output_json(result)


@work_orders.command("comments")
@click.argument("meld_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_comments(meld_id, as_json):
    """Get comments/notes for a work order (plain HTTP, no Playwright)."""
    results = http_backend.get_comments(meld_id)
    output_json(results)


@work_orders.command("send-message")
@click.option("--meld-id", required=True, help="Meld ID")
@click.option("--text", required=True, help="Message body")
@click.option("--hide-tenant", is_flag=True, default=False, help="Hide from tenant")
@click.option("--hide-vendor", is_flag=True, default=False, help="Hide from vendor")
@click.option("--hide-owner", is_flag=True, default=False, help="Hide from owner")
@click.option("--json", "as_json", is_flag=True, default=True)
def send_message(meld_id, text, hide_tenant, hide_vendor, hide_owner, as_json):
    """Post a message/comment on a meld (plain HTTP, no Playwright)."""
    result = http_backend.send_message(
        meld_id, text,
        hidden_from_tenant=hide_tenant,
        hidden_from_vendor=hide_vendor,
        hidden_from_owner=hide_owner,
    )
    output_json(result)


@work_orders.command("clone")
@click.option("--meld-id", required=True, help="Source meld ID to clone")
@click.option("--description", default=None, help="Override description for the clone")
@click.option("--json", "as_json", is_flag=True, default=True)
def clone_meld(meld_id, description, as_json):
    """Clone a meld — creates a new meld with the same details (plain HTTP)."""
    result = http_backend.clone_meld(meld_id, brief_description=description)
    output_json(result)


# ── properties group ───────────────────────────────────────────────────────────

@cli.group()
def properties():
    """Property commands."""
    pass


@properties.command("list")
@click.option("--limit", default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=True)
def list_properties(limit, as_json):
    """List all properties."""
    results = api_backend.list_properties(limit=limit)
    output_json(results)


# ── vendors group ──────────────────────────────────────────────────────────────

@cli.group()
def vendors():
    """Vendor commands."""
    pass


@vendors.command("list")
@click.option("--limit", default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=True)
def list_vendors(limit, as_json):
    """List all vendors."""
    results = api_backend.list_vendors(limit=limit)
    output_json(results)


# ── assign-tech ────────────────────────────────────────────────────────────────

@cli.command("assign-tech")
@click.option("--work-order-id", required=True, help="Meld ID")
@click.option("--tech", required=True, help="Tech name (partial match ok)")
@click.option("--json", "as_json", is_flag=True, default=True)
def assign_tech(work_order_id, tech, as_json):
    """Assign an in-house tech to a work order (plain HTTP, no Playwright)."""
    result = http_backend.assign_tech(work_order_id, tech)
    output_json(result)


# ── api-keys ──────────────────────────────────────────────────────────────────

@cli.group("api-keys")
def api_keys():
    """Manage Nexus partner API keys."""
    pass


@api_keys.command("rotate")
@click.option("--update-railway", is_flag=True, default=False,
              help="Automatically push new credentials to Railway via 'railway variables --set'")
@click.option("--json", "as_json", is_flag=True, default=True)
def rotate_api_key(update_railway, as_json):
    """Create a new Nexus partner API key and output client_id + client_secret.

    The client_secret is shown ONCE — this command captures it for you.

    With --update-railway, also runs:
      railway variables --set PM_NEXUS_CLIENT_ID=<new_id>
      railway variables --set PM_NEXUS_CLIENT_SECRET=<new_secret>
    """
    result = http_backend.rotate_api_key()

    if result.get("ok") and update_railway:
        import subprocess
        client_id = result["client_id"]
        client_secret = result["client_secret"]
        for var, val in [("PM_NEXUS_CLIENT_ID", client_id), ("PM_NEXUS_CLIENT_SECRET", client_secret)]:
            proc = subprocess.run(
                ["railway", "variables", "--set", f"{var}={val}"],
                capture_output=True, text=True
            )
            result.setdefault("railway_updates", {})[var] = (
                "ok" if proc.returncode == 0 else f"error: {proc.stderr.strip()}"
            )

    output_json(result)


@api_keys.command("list")
@click.option("--json", "as_json", is_flag=True, default=True)
def list_api_keys(as_json):
    """List existing Nexus partner API keys (client IDs only — secrets not shown)."""
    result = http_backend.list_api_keys()
    output_json(result)


# ── probe ──────────────────────────────────────────────────────────────────────

@cli.command()
def probe():
    """Health check — verify API credentials and connectivity."""
    result = api_backend.probe()
    output_json(result)


if __name__ == "__main__":
    cli()
