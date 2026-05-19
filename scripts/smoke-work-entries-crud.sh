#!/bin/bash
# Live smoke for `pm work-orders work-entries` CRUD subcommands.
#
# Fixture: meld 12774440 (TKG5XYM, Blue's safe fixture — tenant-less and
# already COMPLETED so we won't disrupt active work). Agent: persona_id
# 57163 (David). Per architect doc §4 risk R1, this is the place
# partial-PATCH 400 would surface if it's going to.
#
# Requires fresh PM cookie auth (`pm probe` should succeed first). Run:
#     bash scripts/smoke-work-entries-crud.sh
#
# Smoke evidence bar (memory feedback_smoke_evidence_bar): each step prints
# the raw JSON envelope from the CLI; if any step fails the script aborts
# with `set -e` so the failing envelope is the last thing printed.

set -euo pipefail

MELD_ID=12774440
AGENT_ID=57163  # David — coordinator persona_id captured from TKG5XYM
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

echo "=== smoke-work-entries-crud start ${STAMP} ==="
echo "meld_id=${MELD_ID}  agent_id=${AGENT_ID}"
echo

echo "=== 1/3 CREATE ==="
ENTRY_OUT=$(pm work-orders work-entries create \
    --meld-id "${MELD_ID}" \
    --agent-id "${AGENT_ID}" \
    --description "smoke test from CLI work-entries CRUD ship ${STAMP}" \
    --hours 0.1)
echo "${ENTRY_OUT}"
ENTRY_ID=$(printf '%s' "${ENTRY_OUT}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
eid = data.get('entry_id')
if eid is None:
    res = data.get('result') or {}
    eid = res.get('id') if isinstance(res, dict) else None
if eid is None:
    print('ERROR: could not extract entry_id', file=sys.stderr)
    sys.exit(1)
print(eid)
")
echo "entry_id=${ENTRY_ID}"
echo

echo "=== 2/3 UPDATE ==="
pm work-orders work-entries update "${ENTRY_ID}" \
    --description "smoke test patched ${STAMP}"
echo

echo "=== 3/3 DELETE ==="
pm work-orders work-entries delete "${ENTRY_ID}" --force
echo

echo "=== smoke-work-entries-crud complete ==="
