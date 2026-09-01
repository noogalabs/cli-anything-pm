# Private-literal CI secret provenance

`PROPERTYMELD_PRIVATE_LITERALS` is generated, never typed from review
findings. Its authoritative inputs are untracked local exports from:

- `pm agents list --json` (staff and in-house technician IDs, first names,
  and last names);
- `pm vendors list --json` (vendor IDs and names);
- the active `PROPERTYMELD_CONFIG` (tenant/account IDs and credential path);
- an untracked org-identity JSON list for the org display name and internal
  path vocabulary that Property Meld does not expose.

Run `scripts/build_private_literal_vocabulary.py` over fresh exports and pipe
its stdout to `gh secret set PROPERTYMELD_PRIVATE_LITERALS`; pass this file to
`--provenance` in the same invocation. The value-free record below binds CI to
the exact complete export used for the secret. Because the
vocabulary is derived from complete live rosters, a newly added staff member,
technician, or vendor joins the next refresh without editing a hand-written
name list. The tracked structural gate remains independent of this secret.

The source exports, extras file, and generated JSON contain private data and
must remain untracked.

<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_START -->
- Agent records: `10`
- Vendor records: `17`
- Vocabulary entries: `79`
- Vocabulary SHA-256: `05d582913f43a13e2a48a4037cd92aba8f44167cd05f4b0315ddfd3486c286fd`
<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_END -->
