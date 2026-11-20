# Property Meld command tool specification

## Purpose

The `pm` tool lets an authorized operator or agent read Property Meld records and request supported actions.
It reduces repeated website steps while keeping account access and action approval separate.

This document describes the shipped source and its acceptance criteria.
It does not claim that tests were run for this documentation change.
It does not certify a member's account or a live workflow.

## Features and what done means

| Feature | What done means | Existing evidence |
| --- | --- | --- |
| Install and discover commands | The package provides the `pm` command. Help and the runtime-derived index work without private account configuration. | [README](../README.md), [CLI](../cli_anything/propertymeld/cli.py), [CLI tests](../tests/test_cli.py) |
| Route requests to the correct account | Actions reject missing or malformed private routing. Nexus credentials and website sessions remain separate. | [Configuration](../cli_anything/propertymeld/config.py), [architecture notes](../PROPERTYMELD.md), [configuration tests](../tests/test_http_backend_url.py) |
| Read work orders and related records | Supported reads return records through the declared backend. Work-order detail, comments, files, notes and work entries retain their separate meanings. | [CLI](../cli_anything/propertymeld/cli.py), [API backend](../cli_anything/propertymeld/api_backend.py), [HTTP backend](../cli_anything/propertymeld/http_backend.py) |
| Exhaust a requested collection | With `--complete`, the reader follows valid page links and reports resource scope and completion metadata. Inconsistent totals, repeated records and invalid links refuse certification. | [Read protocol](../README.md#exhaustive-work-order-reads), [pagination](../cli_anything/propertymeld/pagination.py), [pagination tests](../tests/test_read_pagination.py), [CLI completeness tests](../tests/test_cli_read_completeness.py) |
| Read Insights analytics | Fixed authenticated GET endpoints supply a safe projection. Vendor joins preserve unresolved and ambiguous source names. Session expiry refuses without automatic recapture. | [Insights backend](../cli_anything/propertymeld/insights_backend.py), [Insights tests](../tests/test_insights.py) |
| Perform supported work-order actions | Commands use the declared manager or vendor path and return failure when the action fails. Manager-side completion refuses before mutation. | [HTTP backend](../cli_anything/propertymeld/http_backend.py), [completion guards](../tests/test_in_house_complete_guard.py), [work-entry tests](../tests/test_cli_work_entries_crud.py) |
| Handle assignments, files and related account records | Supported commands retain their routing, required fields and error exits. A vendor or company assignment is not proof of attendance. | [CLI](../cli_anything/propertymeld/cli.py), [file upload tests](../tests/test_upload_meld_file.py), [coordinator tests](../tests/test_exit_assign_coordinator.py), [tenant-link tests](../tests/test_link_tenant_verify.py) |
| Refresh website sessions when authorized | The packaged helper checks the session path used for website operations. It preserves credential fields and exposes MFA handling. | [Session helper](../cli_anything/propertymeld/recapture/pm-recapture-session-playwright.py), [session tests](../tests/test_recapture_write_path_gate.py), [packaging test](../tests/test_recapture_script_packaged.py) |
| Protect command output | The shared boundary redacts sensitive fields and credential-shaped text. Errors retain a nonzero exit status. | [Utilities](../cli_anything/propertymeld/utils.py), [redaction tests](../tests/test_output_redaction.py), [JSON exit tests](../tests/test_output_json_exit.py) |
| Deliver rotated API keys privately | Rotation requires a non-displaying destination before minting a key. Requested delivery failures remain visible without printing the secret. | [Architecture notes](../PROPERTYMELD.md#api-key-rotation-cli), [CLI](../cli_anything/propertymeld/cli.py) |
| Package the tool consistently | A built wheel contains the declared Python source members with byte parity and no extra file types. | [Wheel instructions](../README.md#building-the-wheel), [wheel tests](../tests/test_wheel_integrity.py) |

## Limits and real-use requirements

- The member needs permission to use the target account and its data.
- Nexus API access does not prove that website-session operations work.
- Some operations need a browser session. Multi-factor authentication, or MFA, may need the member's action.
- Command availability does not authorize a write, message, invitation, deletion or credential change.
- Manager-side work-order completion is disabled. Do not bypass that refusal.
- A `--complete` list covers its declared collection, not every child resource on every work order.
- Complete reads can refuse when the service does not provide enough pagination evidence.
- Complete reads do not freeze records while other users change them.
- An open-status list is not proof of the full nonterminal work-order board.
- Insights vendor names can remain unresolved or ambiguous. Keep those rows visible.
- Local install state can lag source changes. Follow the README refresh instructions after approved updates.
- Offline fixtures do not prove current account permissions, endpoint availability or successful live actions.
- Existing architecture and packaged skill examples can be narrower or older than the current command tree.
- Check current help and relevant source before using a command. Stop if required behavior is unclear.

## Change boundary

Use [AGENTS.md](../AGENTS.md) for agent behavior and approval rules.
Keep private configuration, sessions and real service records outside this repository.
Update this specification when an authorized behavior change alters a feature or its limit.
