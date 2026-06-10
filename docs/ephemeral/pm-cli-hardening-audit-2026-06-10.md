# PM CLI hardening audit - 2026-06-10

Scope: systematic robustness pass over `cli_anything/propertymeld` for three recurring failure classes:

- destructive defaults lacking already-populated probes
- endpoints assuming read shapes are safe write input
- missing pagination / page-clamp guards

Branch: `codie/pm-hardening-audit`

## Staged fixes

### F1 - Nexus work-order list stopped after one page

Severity: medium-high

`api_backend._list_work_orders_nexus()` passed the requested `limit` to `/api/v2/meld/` and returned the first response page. For `--limit > 100`, PM/DRF-style list endpoints may clamp the page and provide `next`; the CLI would silently return the first page only.

Fix staged:

- Page size capped at 100.
- Walk `/api/v2` `next` links until requested limit or exhaustion.
- Shared `_next_api_v2_path()` parser reused by the existing property/vendor paginator.
- Regression: `TestListWorkOrders.test_work_orders_paginates_nexus_next_until_limit`.

### F2 - Cookie project/estimate list helpers stopped after one page

Severity: medium

`http_backend.list_projects()` and `http_backend.list_estimates()` read one cookie API page and unwrapped `results`, so `--limit > server page size` could silently truncate if PM returned `next`.

Fix staged:

- Unscoped `list_projects()` now uses `_paginate_all("projects/?limit=100")`.
- Meld-scoped `list_estimates()` now uses `_paginate_all("estimates/meld/{id}/?limit=100...")`.
- Meld-scoped `list_projects(meld_id=...)` is left unchanged because the endpoint is scoped to one meld and existing path contracts depend on it.
- Regressions: `TestListProjects.test_unscoped_projects_paginate_next_until_limit`, `TestListEstimatesMeldRequired.test_meld_scoped_paginates_next_until_limit`.

### F3 - Lower-level vendor schedule helper had replace-all default

Severity: high

`http_backend.vendor_set_schedule()` defaulted `segments_to_keep` to `[]`, producing a replace-all schedule payload even when a caller had not probed existing vendor segments. Higher-level `schedule_vendor_appointment()` already guards booked appointments, but this lower-level helper remained an exported footgun.

Fix staged:

- `segments_to_keep` must be supplied explicitly.
- Passing `[]` remains allowed only after the caller has verified no existing segments need preservation.
- Regression: `TestVendorSetSchedule.test_requires_explicit_segments_to_keep`.

### F4 - Destructive file/project deletes lacked CLI force guard

Severity: medium-high

`pm work-orders delete-file` and `pm projects delete` called destructive DELETE helpers directly. `work-entries delete` already had a no-TTY/`--force` guard, so these two commands were inconsistent and unsafe for cron/agent usage.

Fix staged:

- Added shared `_require_force_for_delete()`.
- `delete-file` and `projects delete` now require `--force` in non-interactive contexts and prompt interactively otherwise.
- Regressions: `test_delete_file_without_force_in_no_tty_aborts`, `test_delete_project_without_force_in_no_tty_aborts`.

### F5 - Estimate update allowed empty PATCH

Severity: medium

`http_backend.update_estimate()` built a partial payload and sent PATCH even when no update fields were provided. `work-entries update` already had a no-field fail-loud contract; estimates should follow the same no-op-write rule.

Fix staged:

- Empty payload returns `{"ok": false, "error": "no fields to update"}` before loading credentials / CSRF.
- CLI `output_json()` converts that envelope to a non-zero exit.
- Regression: `TestUpdateEstimateNoOp.test_update_estimate_no_fields_fails_before_http`.

## Existing safeguards verified

- `complete_meld()` manager path fetches current meld state, refuses unless `PENDING_COMPLETION`, then re-GETs and verifies `COMPLETED`.
- `schedule_appointment()` fetches the meld and refuses if an in-house appointment already has an `availability_segment` or proposed management availability segments before sending `segments_to_keep: []`.
- `schedule_vendor_appointment()` resolves vendor request -> appointment by `assignment_request`, refuses wrong-vendor matches, rejected/canceled requests, and already-booked vendor appointments before sending `segments_to_keep: []`.
- Cookie client-side filters for `--assigned-to-vendor`, `--stuck-hours`, and `--no-tenant-linked` warn whenever the effective 100-row cap is hit, including sparse-match and `--limit > 25` cases.
- `--assigned-to-tech` on `work-orders list` fails loud instead of silently returning unfiltered rows.
- Work-entry delete already has a no-TTY `--force` guard.

## Findings not patched without further gate

### G1 - Assignment replacement semantics need live/design confirmation

`assign_tech()` and `assign_vendor()` send `{"maintenance": [new_obj]}` to `assign-maintenance/`. If PM treats this as replace-list, assigning one party can clear other assigned parties. If PM treats it as append/assign action, this is fine. The code does not currently GET current assignment state or preserve existing maintenance entries.

Recommended next step: live probe on a safe fixture or PM capture comparison before changing shape. Do not patch speculatively because this endpoint may intentionally accept only the new assignment target.

### G2 - Cancel/merge/link destructive commands lack pre-state verification

`cancel_meld()`, `merge_meld()`, `patch_meld_project_link()`, `link_estimate_to_meld()`, and `link_receipt_to_invoice()` are explicit write commands and require identifying IDs/reasons where relevant, but most do not verify post-state. Some endpoints may already be atomic/status-validated server-side.

Recommended next step: per-command probe matrix, starting with cancel and merge. Add post-state re-GET only after confirming the resulting artifact state and field names.

### G3 - Cookie rich work-order filter still intentionally uses one page

`list_work_orders_rich()` clamps to one 100-row page by design today. The client-side filters now warn on full page, which prevents silent completeness claims, but true completeness past 100 rows requires a `_paginate_all`-backed follow-up. This should be handled with the tech-detail fan-out/server-filter design gate because the same cost/completeness tradeoff applies.

## Validation

Focused command:

```bash
pytest -q tests/test_api_backend.py tests/test_http_backend_vendor_side.py tests/test_http_backend_top_level.py tests/test_estimates_receipts_meld_required.py tests/test_cli.py tests/test_cli_work_entries_crud.py
```

Result:

```text
279 passed in 0.32s
```
