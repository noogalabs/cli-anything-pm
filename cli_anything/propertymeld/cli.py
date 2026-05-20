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
    pm assign-tech --work-order-id 12345 --tech Person019 --json
"""
import json
import sys

import click

from . import api_backend, http_backend
from .utils import output_json, resolve_meld_id


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


def _normalize_meld_id(meld_id):
    return resolve_meld_id(meld_id) if meld_id is not None else None


@work_orders.command("list")
@click.option("--status", default=None,
              type=click.Choice(["open", "pending", "completed", "canceled"], case_sensitive=False),
              help="Filter by status (default: all)")
@click.option("--assigned-to-tech", type=int, default=None, help="Filter to a specific in-house tech id")
@click.option("--assigned-to-vendor", type=int, default=None, help="Filter to a specific vendor id")
@click.option("--stuck-hours", type=float, default=None,
              help="Filter melds stuck in current status longer than N hours")
@click.option("--created-since", default=None, help="Filter melds created at/after ISO timestamp")
@click.option("--status-not", default=None, help="Exclude melds in this status")
@click.option("--no-tenant-linked", is_flag=True, default=False,
              help="Filter to melds with no linked tenant")
@click.option("--limit", default=25, show_default=True, help="Maximum results")
@click.option("--json", "as_json", is_flag=True, default=True, help="Output as JSON (default)")
def list_work_orders(
    status,
    assigned_to_tech,
    assigned_to_vendor,
    stuck_hours,
    created_since,
    status_not,
    no_tenant_linked,
    limit,
    as_json,
):
    """List work orders."""
    results = api_backend.list_work_orders(
        status=status,
        assigned_to_tech=assigned_to_tech,
        assigned_to_vendor=assigned_to_vendor,
        stuck_hours=stuck_hours,
        created_since=created_since,
        status_not=status_not,
        no_tenant_linked=no_tenant_linked,
        limit=limit,
    )
    output_json(results)


@work_orders.command("get")
@click.argument("meld_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_work_order(meld_id, as_json):
    """Get a single work order by ID."""
    meld_id = _normalize_meld_id(meld_id)
    result = api_backend.get_work_order(meld_id)
    output_json(result)


@work_orders.command("comments")
@click.argument("meld_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_comments(meld_id, as_json):
    """Get comments/notes for a work order (plain HTTP, no Playwright)."""
    meld_id = _normalize_meld_id(meld_id)
    results = http_backend.get_comments(meld_id)
    output_json(results)


@work_orders.command("files")
@click.argument("meld_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_files(meld_id, as_json):
    """List files attached to a work order (manager + tenant + vendor uploads)."""
    meld_id = _normalize_meld_id(meld_id)
    results = http_backend.list_files(meld_id)
    output_json(results)


class _WorkEntriesGroup(click.Group):
    """Routes the legacy positional invocation `work-entries <meld_id>` to `list <meld_id>`.

    Before this sub-group refactor, `pm work-orders work-entries 90000014`
    listed entries directly. Codex review of PR #7 flagged that the new
    group rejects that shape with "No such command". Keep back-compat by
    falling through to `list` when the first arg is not a known
    subcommand name.
    """

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            if args and not args[0].startswith("-"):
                return super().resolve_command(ctx, ["list"] + args)
            raise


@work_orders.group("work-entries", cls=_WorkEntriesGroup)
def work_entries():
    """Manage per-visit work-entries on a meld (list/create/update/delete)."""
    pass


@work_entries.command("list")
@click.argument("meld_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def list_work_entries_cmd(meld_id, as_json):
    """List per-visit work-entries (checkin/checkout/hours/agent/notes) for a meld."""
    meld_id = _normalize_meld_id(meld_id)
    results = http_backend.list_work_entries(meld_id)
    output_json(results)


@work_entries.command("create")
@click.option("--meld-id", required=True, help="Meld ID")
@click.option("--agent-id", "agent", required=True, type=int,
              help="Persona ID of the agent who performed the work "
                   "(e.g. 90025=Person031, 90026=Person019)")
@click.option("--description", required=True, help="Short summary, shown in the meld feed.")
@click.option("--long-description", "long_description", default="",
              help="Optional long-form notes.")
@click.option("--checkin", default=None,
              help="ISO 8601 start time, e.g. 2026-05-18T09:00:00-04:00")
@click.option("--checkout", default=None,
              help="ISO 8601 end time, e.g. 2026-05-18T10:30:00-04:00")
@click.option("--hours", default=None, type=float,
              help="Hours worked. PM auto-computes from checkin/checkout if omitted.")
@click.option("--json", "as_json", is_flag=True, default=True)
def create_work_entry_cmd(meld_id, agent, description, long_description,
                          checkin, checkout, hours, as_json):
    """Create a new work-entry on a meld.

    Manager-side, POST nested at /melds/{id}/work-entries/. Path-shape
    asymmetry: CREATE is nested, UPDATE/DELETE are top-level.
    """
    meld_id = _normalize_meld_id(meld_id)
    result = http_backend.create_work_entry(
        meld_id,
        agent=agent,
        description=description,
        long_description=long_description,
        checkin=checkin,
        checkout=checkout,
        hours=hours,
    )
    output_json(result)


@work_entries.command("update")
@click.argument("entry_id", type=int)
@click.option("--description", default=None)
@click.option("--long-description", "long_description", default=None)
@click.option("--checkin", default=None, help="ISO 8601 start time")
@click.option("--checkout", default=None, help="ISO 8601 end time")
@click.option("--hours", default=None, type=float)
@click.option("--agent-id", "agent", default=None, type=int,
              help="Replace agent (persona_id)")
@click.option("--json", "as_json", is_flag=True, default=True)
def update_work_entry_cmd(entry_id, description, long_description,
                          checkin, checkout, hours, agent, as_json):
    """Update an existing work-entry (partial PATCH, top-level path).

    PATCH /melds/work-entries/{entry_id}/ — pass only the fields you want
    to change. Fails fast (exit 2) if no fields are supplied.
    """
    if all(v is None for v in (description, long_description, checkin,
                               checkout, hours, agent)):
        output_json({"ok": False, "error": "no fields to update"})
        sys.exit(2)
    result = http_backend.update_work_entry(
        entry_id,
        description=description,
        long_description=long_description,
        checkin=checkin,
        checkout=checkout,
        hours=hours,
        agent=agent,
    )
    output_json(result)


@work_entries.command("delete")
@click.argument("entry_id", type=int)
@click.option("--force", is_flag=True, default=False,
              help="Skip the interactive confirm prompt (required in no-TTY contexts).")
@click.option("--json", "as_json", is_flag=True, default=True)
def delete_work_entry_cmd(entry_id, force, as_json):
    """Delete a work-entry (top-level path, irreversible).

    DELETE /melds/work-entries/{entry_id}/. Without --force, prompts for
    confirmation; if stdin is not a TTY and --force is not set, aborts
    with a clear error rather than hanging.
    """
    if not force:
        if not sys.stdin.isatty():
            output_json({
                "ok": False,
                "error": "delete requires --force in non-interactive context",
                "entry_id": entry_id,
            })
            sys.exit(2)
        click.confirm(
            f"Delete work-entry {entry_id}? This is irreversible.",
            abort=True,
        )
    result = http_backend.delete_work_entry(entry_id)
    output_json(result)


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
    meld_id = _normalize_meld_id(meld_id)
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
    meld_id = _normalize_meld_id(meld_id)
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
@click.option("--long-description", "long_description", default=None,
              help="Override long-form description for the clone")
@click.option("--no-tenant-presence-required", "tenant_presence_required",
              flag_value=False, default=None,
              help="Force tenant_presence_required=false on the clone")
@click.option("--unit-id", type=int, default=None,
              help="Override unit id for the clone")
@click.option("--priority", type=click.Choice(["LOW", "MED", "HIGH", "EMERGENCY"], case_sensitive=False),
              default=None, help="Override priority for the clone")
@click.option("--json", "as_json", is_flag=True, default=True)
def clone_meld(
    meld_id,
    description,
    long_description,
    tenant_presence_required,
    unit_id,
    priority,
    as_json,
):
    """Clone a meld — creates a new meld with the same details (plain HTTP)."""
    meld_id = _normalize_meld_id(meld_id)
    result = http_backend.clone_meld(
        meld_id,
        brief_description=description,
        description=long_description,
        tenant_presence_required=tenant_presence_required,
        unit_id=unit_id,
        priority=priority.upper() if isinstance(priority, str) else None,
    )
    output_json(result)


@work_orders.command("merge")
@click.option(
    "--destination",
    "destination_id",
    default=None,
    help="Destination meld ID — the meld that absorbs the source(s).",
)
@click.option(
    "--source",
    "source_ids",
    multiple=True,
    help="Source meld ID to merge into destination. Pass multiple times to merge several at once: --source X --source Y.",
)
@click.option(
    "--meld-id",
    default=None,
    help="DEPRECATED — old source-id flag. Use --destination + --source instead. Still accepted for backwards compat.",
)
@click.option(
    "--into",
    "into_meld_id",
    default=None,
    help="DEPRECATED — old destination flag. Use --destination + --source instead. Still accepted for backwards compat.",
)
@click.option("--json", "as_json", is_flag=True, default=True)
def merge_meld(destination_id, source_ids, meld_id, into_meld_id, as_json):
    """Merge one or more source melds into a destination meld. All must be at the same unit.

    Captured payload shape (2026-05-19 HAR diff): web UI POSTs to
    /api/melds/{destination_id}/merge/ with body {destination_id, source_ids[]}.
    Previously the CLI used a flipped shape (source in URL, {meld: dest} in body)
    which returned HTTP 400 across every meld-state combination. The --meld-id /
    --into flags remain as deprecated shims; new callers should use
    --destination + --source.
    """
    # Backwards-compat shim: legacy (--meld-id + --into) maps to (source + destination).
    if meld_id and into_meld_id:
        destination_id = into_meld_id
        source_ids = (meld_id,)

    if not destination_id or not source_ids:
        raise click.UsageError(
            "merge requires --destination + --source (or legacy --meld-id + --into). "
            "Example: pm work-orders merge --destination 12819946 --source 12820134"
        )

    destination_id = _normalize_meld_id(destination_id)
    normalized_sources = [_normalize_meld_id(s) for s in source_ids]
    result = http_backend.merge_meld(destination_id, normalized_sources)
    output_json(result)


@work_orders.command("complete")
@click.option("--meld-id", required=True, help="Meld ID to mark complete")
@click.option("--notes", default=None, help="Completion notes")
@click.option("--json", "as_json", is_flag=True, default=True)
def complete_meld(meld_id, notes, as_json):
    """Mark a meld complete from the manager side (meld must be PENDING_COMPLETION)."""
    meld_id = _normalize_meld_id(meld_id)
    result = http_backend.complete_meld(meld_id, completion_notes=notes)
    output_json(result)


@work_orders.command("cancel")
@click.option("--meld-id", required=True, help="Meld ID to cancel")
@click.option("--reason", default=None, help="Cancellation reason")
@click.option("--json", "as_json", is_flag=True, default=True)
def cancel_meld(meld_id, reason, as_json):
    """Cancel a meld from the manager side."""
    meld_id = _normalize_meld_id(meld_id)
    result = http_backend.cancel_meld(meld_id, reason=reason)
    output_json(result)


@work_orders.command("schedule")
@click.option("--meld-id", required=True, help="Meld ID (must have in-house tech assigned)")
@click.option("--dtstart", required=True, help="Start datetime ISO 8601, e.g. 2026-04-27T14:00:00-04:00")
@click.option("--hours", default=2.0, show_default=True, type=float, help="Duration in hours")
@click.option("--json", "as_json", is_flag=True, default=True)
def schedule_appointment(meld_id, dtstart, hours, as_json):
    """Schedule an in-house tech appointment window on a meld."""
    meld_id = _normalize_meld_id(meld_id)
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


# ── units group ────────────────────────────────────────────────────────────────

@cli.group()
def units():
    """Unit commands."""
    pass


@units.command("edit-notes")
@click.argument("unit_id", type=int)
@click.option("--notes", required=True,
              help="New maintenance_notes text for the unit (overwrites existing)")
@click.option("--json", "as_json", is_flag=True, default=True)
def edit_unit_notes_cmd(unit_id, notes, as_json):
    """Update the maintenance_notes on a unit (per-unit quirks like water shut-off location).

    Distinct from `pm-dev work-orders update-notes` (meld-level notes).
    Use this for facts that apply to EVERY meld on a given unit.

    PATCH /api/units/{unit_id}/ — verified shape from pm-capture 2026-05-14.

    Example:
      pm units edit-notes 1754419 --notes "Water shut-off in basement near furnace"
    """
    result = http_backend.update_unit_notes(unit_id, notes)
    output_json(result)


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


@tenants.command("edit-notes")
@click.argument("tenant_id", type=int)
@click.option("--notes", required=True,
              help="New notes text for the tenant (overwrites existing)")
@click.option("--json", "as_json", is_flag=True, default=True)
def edit_tenant_notes_cmd(tenant_id, notes, as_json):
    """Update the notes on a tenant (per-resident context, recallable across melds).

    Field is `notes`, not `maintenance_notes`. Distinct from `pm units edit-notes`
    (per-unit quirks) and `pm work-orders update-notes` (per-meld notes).
    Use this for facts that follow the RESIDENT — schedule, preferences, access
    constraints, household composition.

    Endpoint requires a full-body PATCH (helper handles the GET → mutate → PATCH
    roundtrip). Verified shape from pm-tenant-notes-endpoint-capture-2026-05-18.

    Example:
      pm tenants edit-notes 9000014 --notes "Schedule access after 3pm weekdays; husband home during day"
    """
    result = http_backend.update_tenant_notes(tenant_id, notes)
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
    work_order_id = _normalize_meld_id(work_order_id)
    result = http_backend.assign_tech(work_order_id, tech)
    output_json(result)


@cli.command("assign-vendor")
@click.option("--work-order-id", required=True, help="Meld ID")
@click.option("--vendor", required=True, help="Vendor name (partial match ok)")
@click.option("--account", default="1", show_default=True, help="Account prefix for composite_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def assign_vendor_cmd(work_order_id, vendor, account, as_json):
    """Assign an external vendor to a work order by name (partial match)."""
    work_order_id = _normalize_meld_id(work_order_id)
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
      railway variables --set PM_CLIENT_ID=<new_id>
      railway variables --set PM_CLIENT_SECRET=<new_secret>

    These names match what utils.get_token() actually reads. Older versions
    of this command wrote PM_NEXUS_CLIENT_ID/PM_NEXUS_CLIENT_SECRET, which
    Railway accepted but the runtime ignored — silent rotation that never
    took effect.
    """
    result = http_backend.rotate_api_key()

    if result.get("ok") and update_railway:
        import subprocess
        client_id = result["client_id"]
        client_secret = result["client_secret"]
        for var, val in [("PM_CLIENT_ID", client_id), ("PM_CLIENT_SECRET", client_secret)]:
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
    meld_id = _normalize_meld_id(meld_id)
    result = http_backend.schedule_vendor_appointment(meld_id, vendor_id, dtstart, duration_hours=hours)
    output_json(result)


@work_orders.command("inspect")
@click.argument("meld_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def inspect_work_order(meld_id, as_json):
    """Inspect a work order across detail, photos, notes, work entries, and comments."""
    meld_id = _normalize_meld_id(meld_id)
    result = http_backend.inspect_meld(meld_id)
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
    meld_id = _normalize_meld_id(meld_id)
    results = http_backend.list_projects(meld_id=meld_id, limit=limit)
    output_json(results)


@projects.command("get")
@click.argument("project_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def get_project(project_id, as_json):
    """Get a single project by ID."""
    result = http_backend.get_project(project_id)
    output_json(result)


@projects.command("add-melds")
@click.argument("project_id")
@click.argument("meld_ids", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, default=True)
def add_melds_to_project_cmd(project_id, meld_ids, as_json):
    """Attach one or more existing melds to a project.

    Example: pm projects add-melds 222959 12772756 12772757
    """
    result = http_backend.add_melds_to_project(project_id, list(meld_ids))
    output_json(result)


@projects.command("create-meld-in")
@click.argument("project_id")
@click.option("--brief-description", required=True)
@click.option("--description", required=True)
@click.option("--work-category", required=True, help="e.g. APPLIANCES, PLUMBING, HVAC")
@click.option("--work-type", required=True, help="e.g. TURN, REPAIR")
@click.option("--due-date", required=True, help="ISO 8601, e.g. 2026-05-16T02:52:41.393Z")
@click.option("--unit-id", type=int, default=None,
              help="Unit PK — auto-hydrated via GET /units/{id}/. Use this OR --unit-json.")
@click.option("--unit-json", default=None,
              help="Full JSON unit object (power-user path). Use this OR --unit-id.")
@click.option("--maintenance-id", "maintenance_ids", type=int, multiple=True,
              help="ManagementAgent PK — auto-hydrated via GET /agents/{id}/. Repeat for multiple. Use this OR --maintenance-json.")
@click.option("--maintenance-json", default=None,
              help="Full JSON list of ManagementAgent objects (power-user path). Use this OR --maintenance-id.")
@click.option("--tenant-id", "tenant_ids", type=int, multiple=True,
              help="Tenant PK — auto-hydrated via GET /tenants/{id}/. Repeat for multiple. Use this OR --tenants-json.")
@click.option("--tenants-json", default=None,
              help="JSON list of tenant objects (power-user path). Use this OR --tenant-id.")
@click.option("--work-location", default="")
@click.option("--priority", default="LOW")
@click.option("--permission-to-enter/--no-permission-to-enter", default=True)
@click.option("--tenant-presence-required/--no-tenant-presence-required", default=False)
@click.option("--notify-tenants/--no-notify-tenants", default=True)
@click.option("--notify-owner/--no-notify-owner", default=False)
@click.option("--has-pets/--no-has-pets", default=False)
@click.option("--pets", default="")
@click.option("--json", "as_json", is_flag=True, default=True)
def create_meld_in_project_cmd(
    project_id, brief_description, description, work_category, work_type,
    due_date, unit_id, unit_json, maintenance_ids, maintenance_json, tenant_ids, tenants_json,
    work_location, priority, permission_to_enter, tenant_presence_required,
    notify_tenants, notify_owner, has_pets, pets, as_json,
):
    """Create a new meld INSIDE an existing project.

    PMs /list-create-meld/ endpoint requires fully-hydrated unit and
    ManagementAgent objects. Two paths:

    \b
    - Ergonomic: --unit-id 9000005 --maintenance-id 90025 (repeatable)
      --tenant-id 9000009 (repeatable)
      → CLI auto-hydrates via GET /units/{id}/ and GET /agents/{id}/.
    - Power-user: --unit-json '<full obj>' --maintenance-json '<full list>'
      → passed through; must include nested fields or backend raises ValueError.
    """
    import json as _json

    if (unit_id is None) == (unit_json is None):
        raise click.UsageError("Exactly one of --unit-id or --unit-json is required.")
    if bool(maintenance_ids) == bool(maintenance_json):
        raise click.UsageError("Exactly one of --maintenance-id (repeatable) or --maintenance-json is required.")
    if tenant_ids and tenants_json is not None:
        raise click.UsageError("Use either --tenant-id (repeatable) or --tenants-json, not both.")

    if unit_id is not None:
        unit = {"id": unit_id}
    else:
        unit = _json.loads(unit_json)

    if maintenance_ids:
        maintenance = [{"id": mid} for mid in maintenance_ids]
    else:
        maintenance = _json.loads(maintenance_json)
    if tenant_ids:
        tenants = [{"id": tid} for tid in tenant_ids]
    elif tenants_json is not None:
        tenants = _json.loads(tenants_json)
    else:
        tenants = []

    result = http_backend.create_meld_in_project(
        project_id=project_id,
        brief_description=brief_description,
        description=description,
        work_category=work_category,
        work_type=work_type,
        due_date=due_date,
        unit=unit,
        maintenance=maintenance,
        tenants=tenants,
        work_location=work_location,
        priority=priority,
        permission_to_enter=permission_to_enter,
        tenant_presence_required=tenant_presence_required,
        notify_tenants=notify_tenants,
        notify_owner=notify_owner,
        has_pets=has_pets,
        pets=pets,
    )
    output_json(result)


@projects.command("create")
@click.option("--name", required=True, help="Project name")
@click.option("--project-type", required=True, help="e.g. TURN, MAINTENANCE")
@click.option("--due-date", required=True, help="ISO 8601, e.g. 2026-05-30T04:00:00.000Z")
@click.option("--start-date", required=True, help="ISO 8601, e.g. 2026-05-14T03:00:00Z")
@click.option("--coordinator", "coordinators", multiple=True, required=True,
              type=int, help="Coordinator management-agent id (repeat for multiple)")
@click.option("--unit-id", required=True, type=int, help="Unit PK")
@click.option("--unit-label", required=True, help="Unit address label, e.g. '123 Main St, Chattanooga, TN, 37421'")
@click.option("--description", default="", help="Project description")
@click.option("--meld-location", default="Unit", show_default=True,
              help='Discriminator — usually "Unit"; PM also accepts property-bound shapes')
@click.option("--prop-id", "prop_id", default=None, type=int,
              help="Property PK; set when meld-location is property-bound (default null)")
@click.option("--json", "as_json", is_flag=True, default=True)
def create_project_cmd(name, project_type, due_date, start_date, coordinators,
                       unit_id, unit_label, description, meld_location, prop_id, as_json):
    """Create a new top-level project.

    Captured shape from pm-capture 2026-05-13 + live-smoked 2026-05-14:
    coordinators are plain int ids, unit is {id, label}, meld_location='Unit'
    for unit-bound projects.
    """
    result = http_backend.create_project(
        name=name,
        project_type=project_type,
        due_date=due_date,
        start_date=start_date,
        coordinators=list(coordinators),
        unit={"id": unit_id, "label": unit_label},
        description=description,
        meld_location=meld_location,
        prop={"id": prop_id} if prop_id is not None else None,
    )
    output_json(result)


@projects.command("edit")
@click.argument("project_id")
@click.option("--name", default=None)
@click.option("--project-type", default=None)
@click.option("--description", default=None)
@click.option("--due-date", default=None)
@click.option("--start-date", default=None)
@click.option("--coordinator", "coordinators", multiple=True, type=int,
              help="Replace coordinator list (repeat for multiple)")
@click.option("--unit-id", default=None, type=int)
@click.option("--unit-label", default=None)
@click.option("--meld-location", default=None)
@click.option("--json", "as_json", is_flag=True, default=True)
def edit_project_cmd(project_id, name, project_type, description, due_date, start_date,
                     coordinators, unit_id, unit_label, meld_location, as_json):
    """Person003 a top-level project.

    PM requires a full-payload echo on PATCH; the backend handles that by
    fetching current state and overlaying only the fields you pass here.
    """
    unit = None
    if unit_id is not None or unit_label is not None:
        unit = {"id": unit_id, "label": unit_label or ""}
    result = http_backend.update_project(
        project_id=project_id,
        name=name,
        project_type=project_type,
        description=description,
        due_date=due_date,
        start_date=start_date,
        coordinators=list(coordinators) if coordinators else None,
        unit=unit,
        meld_location=meld_location,
    )
    output_json(result)


@projects.command("detach-meld")
@click.argument("meld_id")
@click.option("--json", "as_json", is_flag=True, default=True)
def detach_meld_cmd(meld_id, as_json):
    """Detach a meld from any project (sets meld.project = null)."""
    meld_id = _normalize_meld_id(meld_id)
    result = http_backend.patch_meld_project_link(meld_id, None)
    output_json(result)


@projects.command("delete")
@click.argument("project_id", type=int)
@click.option("--json", "as_json", is_flag=True, default=True)
def delete_project_cmd(project_id, as_json):
    """Delete a project by id."""
    result = http_backend.delete_project(project_id)
    output_json(result)


@work_orders.command("update-notes")
@click.argument("meld_id")
@click.option("--maintenance", "maintenance_notes", required=True,
              help="New maintenance_notes text (overwrites existing)")
@click.option("--json", "as_json", is_flag=True, default=True)
def update_meld_notes_cmd(meld_id, maintenance_notes, as_json):
    """Update the maintenance_notes on a meld (PATCH /api/v2/melds/{id}/notes/)."""
    meld_id = _normalize_meld_id(meld_id)
    result = http_backend.update_meld_notes(meld_id, maintenance_notes)
    output_json(result)


@work_orders.command("invoice-hold")
@click.argument("invoice_id", type=int)
@click.option("--reason", required=True, help="Reason the invoice is being put on hold")
@click.option("--json", "as_json", is_flag=True, default=True)
def invoice_hold_cmd(invoice_id, reason, as_json):
    """Place a meld invoice on hold."""
    result = http_backend.hold_meld_invoice(invoice_id, reason=reason)
    output_json(result)


@work_orders.command("invoice-decline")
@click.argument("invoice_id", type=int)
@click.option("--reason", required=True, help="Reason the invoice is being declined")
@click.option("--json", "as_json", is_flag=True, default=True)
def invoice_decline_cmd(invoice_id, reason, as_json):
    """Decline a meld invoice."""
    result = http_backend.decline_meld_invoice(invoice_id, reason=reason)
    output_json(result)


@work_orders.command("delete-file")
@click.argument("file_id", type=int)
@click.option("--json", "as_json", is_flag=True, default=True)
def delete_file_cmd(file_id, as_json):
    """Delete a manager-uploaded work-order file by id."""
    result = http_backend.delete_meld_file(file_id)
    output_json(result)


@work_orders.command("link-tenant")
@click.argument("meld_id")
@click.argument("tenant_id", type=int)
@click.option("--json", "as_json", is_flag=True, default=True)
def link_tenant_cmd(meld_id, tenant_id, as_json):
    """Link a tenant to a meld by appending to the meld's tenants array.

    Closes the gmail-tenant-link skill Step 5 gap — no more Playwright fallback
    for the GV-intake → tenant-link flow. Idempotent: if tenant is already on
    the meld, returns already_linked=True without modifying.

    Example:
      pm work-orders link-tenant 90000003 9000009
    """
    meld_id = _normalize_meld_id(meld_id)
    result = http_backend.link_tenant_to_meld(meld_id, tenant_id)
    output_json(result)


# ── estimates group ────────────────────────────────────────────────────────────

@cli.group()
def estimates():
    """Estimate commands."""
    pass


@estimates.command("list")
@click.option("--meld-id", required=True, help="Meld ID to scope the listing to (required — PM exposes estimates only per-meld).")
@click.option("--status", default=None, help="Filter by status: all|draft|issued|paid")
@click.option("--limit", default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=True)
def list_estimates(meld_id, status, limit, as_json):
    """List estimates for a specific meld.

    --meld-id is required because PM does not expose an unscoped /api/estimates/
    list endpoint. The previous unscoped CLI default returned HTTP 404 silently
    until the 2026-05-19 smoke matrix surfaced it.
    """
    meld_id = _normalize_meld_id(meld_id)
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
    meld_id = _normalize_meld_id(meld_id)
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
    meld_id = _normalize_meld_id(meld_id)
    result = http_backend.link_estimate_to_meld(estimate_id, meld_id)
    output_json(result)


# ── receipts group ────────────────────────────────────────────────────────────

@cli.group()
def receipts():
    """Receipt commands."""
    pass


@receipts.command("list")
@click.option("--meld-id", required=True, help="Meld ID to scope the listing to (required — PM exposes receipts only per-meld).")
@click.option("--limit", default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=True)
def list_receipts(meld_id, limit, as_json):
    """List receipts for a specific meld.

    --meld-id is required because PM does not expose an unscoped /api/receipts/
    list endpoint. The previous unscoped CLI default returned HTTP 404 silently
    until the 2026-05-19 smoke matrix surfaced it.
    """
    meld_id = _normalize_meld_id(meld_id)
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
    meld_id = _normalize_meld_id(meld_id)
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
