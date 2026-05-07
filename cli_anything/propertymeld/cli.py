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


@work_orders.command("files")
@click.argument("meld_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_files(meld_id, as_json):
    """List files attached to a work order (manager + tenant + vendor uploads)."""
    results = http_backend.list_files(meld_id)
    output_json(results)


@work_orders.command("work-entries")
@click.argument("meld_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_work_entries(meld_id, as_json):
    """List per-visit work-entries (checkin/checkout/hours/agent/notes) for a meld."""
    results = http_backend.list_work_entries(meld_id)
    output_json(results)


@work_orders.command("upload-file")
@click.argument("meld_id")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--as", "uploader_role", type=click.Choice(["manager", "tenant", "vendor"]),
              default="manager", help="Uploader role determines target endpoint.")
@click.option("--description", default="", help="Optional file description.")
@click.option("--json", "as_json", is_flag=True, default=True)
def upload_file(meld_id, file_path, uploader_role, description, as_json):
    """Upload a file (photo, doc) to a meld.

    Routes by --as:
      manager → POST /api/melds/{id}/files/         (default)
      tenant  → POST /api/melds/{id}/tenant-files/  (manager-side backfill)
      vendor  → POST /api/melds/{id}/vendor-files/  (manager-side backfill)

    Tenant + vendor endpoints may require additional auth — failure
    surfaces verbatim from PM API.
    """
    result = http_backend.upload_meld_file(meld_id, file_path, uploader_role, description)
    output_json(result)


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


@work_orders.command("merge")
@click.option("--meld-id", required=True, help="Source meld ID to merge (will be cancelled)")
@click.option("--into", "into_meld_id", required=True, help="Destination meld ID (absorbs the source)")
@click.option("--json", "as_json", is_flag=True, default=True)
def merge_meld(meld_id, into_meld_id, as_json):
    """Merge a meld into another meld. Both must be at the same unit."""
    result = http_backend.merge_meld(meld_id, into_meld_id)
    output_json(result)


@work_orders.command("complete")
@click.option("--meld-id", required=True, help="Meld ID to mark complete")
@click.option("--notes", default=None, help="Completion notes")
@click.option("--json", "as_json", is_flag=True, default=True)
def complete_meld(meld_id, notes, as_json):
    """Mark a meld complete from the manager side (meld must be PENDING_COMPLETION)."""
    result = http_backend.complete_meld(meld_id, completion_notes=notes)
    output_json(result)


@work_orders.command("cancel")
@click.option("--meld-id", required=True, help="Meld ID to cancel")
@click.option("--reason", default=None, help="Cancellation reason")
@click.option("--json", "as_json", is_flag=True, default=True)
def cancel_meld(meld_id, reason, as_json):
    """Cancel a meld from the manager side."""
    result = http_backend.cancel_meld(meld_id, reason=reason)
    output_json(result)


@work_orders.command("schedule")
@click.option("--meld-id", required=True, help="Meld ID (must have in-house tech assigned)")
@click.option("--dtstart", required=True, help="Start datetime ISO 8601, e.g. 2026-04-27T14:00:00-04:00")
@click.option("--hours", default=2.0, show_default=True, type=float, help="Duration in hours")
@click.option("--json", "as_json", is_flag=True, default=True)
def schedule_appointment(meld_id, dtstart, hours, as_json):
    """Schedule an in-house tech appointment window on a meld."""
    result = http_backend.schedule_appointment(meld_id, dtstart, duration_hours=hours)
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


# ── tenants group ─────────────────────────────────────────────────────────────

@cli.group()
def tenants():
    """Tenant commands."""
    pass


@tenants.command("list")
@click.option("--search", default=None, help="Filter by name, email, or phone (case-insensitive)")
@click.option("--limit", default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=True)
def list_tenants(search, limit, as_json):
    """List tenants, optionally filtered by name, email, or phone."""
    results = http_backend.list_tenants(search=search, limit=limit)
    output_json(results)


@tenants.command("get")
@click.argument("tenant_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_tenant(tenant_id, as_json):
    """Get a single tenant by ID."""
    result = http_backend.get_tenant(tenant_id)
    output_json(result)


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


@cli.command("assign-vendor")
@click.option("--work-order-id", required=True, help="Meld ID")
@click.option("--vendor", required=True, help="Vendor name (partial match ok)")
@click.option("--account", default="1", show_default=True, help="Account prefix for composite_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def assign_vendor_cmd(work_order_id, vendor, account, as_json):
    """Assign an external vendor to a work order by name (partial match)."""
    result = http_backend.assign_vendor_by_name(work_order_id, vendor, account_prefix=account)
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



@work_orders.command("schedule-vendor")
@click.option("--meld-id", required=True, help="Meld ID (must have vendor assigned)")
@click.option("--vendor-id", required=True, help="Vendor ID")
@click.option("--dtstart", required=True, help="Start datetime ISO 8601, e.g. 2026-04-27T14:00:00-04:00")
@click.option("--hours", default=2.0, show_default=True, type=float, help="Duration in hours")
@click.option("--json", "as_json", is_flag=True, default=True)
def schedule_vendor_appointment(meld_id, vendor_id, dtstart, hours, as_json):
    """Schedule an external vendor appointment window on a meld."""
    result = http_backend.schedule_vendor_appointment(meld_id, vendor_id, dtstart, duration_hours=hours)
    output_json(result)


# ── projects group ────────────────────────────────────────────────────────────

@cli.group()
def projects():
    """Project commands."""
    pass


@projects.command("list")
@click.option("--meld-id", default=None, help="Filter by meld ID")
@click.option("--limit", default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=True)
def list_projects(meld_id, limit, as_json):
    """List projects."""
    results = http_backend.list_projects(meld_id=meld_id, limit=limit)
    output_json(results)


@projects.command("get")
@click.argument("project_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_project(project_id, as_json):
    """Get a single project by ID."""
    result = http_backend.get_project(project_id)
    output_json(result)


# projects create/update/delete CLI commands — DROPPED per Item 3 spike
# (5/05). POST /api/projects/ is reachable but payload schema is incomplete
# in the Haiku-coauthored snapcli stub: known-required fields are name +
# description + start_date + due_date + coordinators[] + project_type +
# unit, but the "unit" field shape is unknown (passing unit.id int returns
# HTTP 500). Update + delete remained untested because they depend on a
# safe create+delete cycle. list + get commands remain available and
# verified. Endpoint-discovery follow-up tracked separately — re-add the
# CLI subcommands once the create payload is captured via Safari.


# ── estimates group ────────────────────────────────────────────────────────────

@cli.group()
def estimates():
    """Estimate commands."""
    pass


@estimates.command("list")
@click.option("--meld-id", default=None, help="Filter by meld ID")
@click.option("--status", default=None, help="Filter by status: all|draft|issued|paid")
@click.option("--limit", default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=True)
def list_estimates(meld_id, status, limit, as_json):
    """List estimates."""
    results = http_backend.list_estimates(meld_id=meld_id, status=status, limit=limit)
    output_json(results)


@estimates.command("get")
@click.argument("estimate_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_estimate(estimate_id, as_json):
    """Get a single estimate by ID."""
    result = http_backend.get_estimate(estimate_id)
    output_json(result)


@estimates.command("create")
@click.option("--meld-id", required=True, help="Meld ID")
@click.option("--estimate-number", required=True, help="Estimate number")
@click.option("--amount", required=True, help="Estimate amount")
@click.option("--description", default="", help="Description")
@click.option("--due-date", default=None, help="Due date (YYYY-MM-DD)")
@click.option("--project-id", default=None, help="Optional project ID")
@click.option("--json", "as_json", is_flag=True, default=True)
def create_estimate(meld_id, estimate_number, amount, description, due_date, project_id, as_json):
    """Create a new invoice."""
    result = http_backend.create_estimate(meld_id, estimate_number, amount, description=description, due_date=due_date, project_id=project_id)
    output_json(result)


@estimates.command("update")
@click.argument("estimate_id")
@click.option("--estimate-number", default=None, help="Estimate number")
@click.option("--amount", default=None, help="Amount")
@click.option("--description", default=None, help="Description")
@click.option("--status", default=None, help="Status: draft|issued|paid")
@click.option("--json", "as_json", is_flag=True, default=True)
def update_estimate(estimate_id, estimate_number, amount, description, status, as_json):
    """Update an invoice."""
    result = http_backend.update_estimate(estimate_id, estimate_number=estimate_number, amount=amount, description=description, status=status)
    output_json(result)


@estimates.command("link")
@click.argument("estimate_id")
@click.option("--meld-id", required=True, help="Meld ID to link to")
@click.option("--json", "as_json", is_flag=True, default=True)
def link_invoice(estimate_id, meld_id, as_json):
    """Link an estimate to a meld."""
    result = http_backend.link_estimate_to_meld(estimate_id, meld_id)
    output_json(result)


# ── receipts group ────────────────────────────────────────────────────────────

@cli.group()
def receipts():
    """Receipt commands."""
    pass


@receipts.command("list")
@click.option("--meld-id", default=None, help="Filter by meld ID")
@click.option("--limit", default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=True)
def list_receipts(meld_id, limit, as_json):
    """List receipts."""
    results = http_backend.list_receipts(meld_id=meld_id, limit=limit)
    output_json(results)


@receipts.command("get")
@click.argument("receipt_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_receipt(receipt_id, as_json):
    """Get a single receipt by ID."""
    result = http_backend.get_receipt(receipt_id)
    output_json(result)


@receipts.command("upload")
@click.option("--meld-id", required=True, help="Meld ID")
@click.option("--file", "file_path", required=True, type=click.Path(exists=True), help="File path to upload")
@click.option("--description", default="", help="Receipt description")
@click.option("--estimate-id", default=None, help="Optional estimate ID to link")
@click.option("--json", "as_json", is_flag=True, default=True)
def upload_receipt(meld_id, file_path, description, estimate_id, as_json):
    """Upload a receipt file."""
    result = http_backend.upload_receipt(meld_id, file_path, description=description, linked_estimate_id=estimate_id)
    output_json(result)


@receipts.command("link")
@click.argument("receipt_id")
@click.option("--estimate-id", required=True, help="Estimate ID to link to")
@click.option("--json", "as_json", is_flag=True, default=True)
def link_receipt(receipt_id, estimate_id, as_json):
    """Link a receipt to an invoice."""
    result = http_backend.link_receipt_to_invoice(receipt_id, estimate_id)
    output_json(result)


