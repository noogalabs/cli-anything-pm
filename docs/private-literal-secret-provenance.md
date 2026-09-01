# Private-literal CI secret provenance

`PROPERTYMELD_PRIVATE_LITERALS` is generated, never typed from review
findings. Its authoritative inputs are untracked local exports from:

- `pm agents list --json` (staff and in-house technician IDs, first names,
  and last names);
- `pm vendors list --json` (vendor IDs and names);
- `pm tenants list --limit 10000 --json` (resident names, phone numbers, and
  email addresses from the complete paginated tenant roster);
- the active `PROPERTYMELD_CONFIG` (tenant/account IDs and credential path);
- an untracked org-identity JSON list for the org display name and internal
  path vocabulary that Property Meld does not expose.

Run `scripts/build_private_literal_vocabulary.py` over fresh exports and pipe
its stdout to `gh secret set PROPERTYMELD_PRIVATE_LITERALS`; pass this file to
`--provenance` in the same invocation. The value-free record below binds CI to
the exact complete export used for the secret. The stdout secret is a
`zlib64:` lossless encoding because the complete resident roster exceeds
GitHub's plaintext secret-size limit; CI decodes it before comparing the
canonical vocabulary hash below. Because the
vocabulary is derived from complete live rosters, a newly added staff member,
technician, or vendor joins the next refresh without editing a hand-written
name list. The tracked structural gate remains independent of this secret.

The source exports, extras file, and generated JSON contain private data and
must remain untracked.

<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_START -->
- Agent records: `10`
- Vendor records: `17`
- Tenant records: `648`
- Vocabulary entries: `2363`
- Vocabulary SHA-256: `9a6c64737bf53a8ac73220b0f8ae10f4ddd3dbf6b5aba20f6e6e4972d0c87395`
<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_END -->
