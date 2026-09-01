# Private-literal HMAC census provenance

The committed digest list is generated, never typed from review findings. Its
authoritative inputs are untracked local exports from:

- `pm agents list --json` (staff and in-house technician IDs, first names,
  and last names);
- `pm vendors list --json` (vendor IDs and names);
- `pm tenants list --limit 10000 --json` (resident names, phone numbers, and
  email addresses from the complete paginated tenant roster);
- the active `PROPERTYMELD_CONFIG` (tenant/account IDs and credential path);

Generate a random 32-byte hexadecimal `PROPERTYMELD_VOCAB_SALT`, set that
single value as the repository secret, and run
`scripts/build_private_literal_vocabulary.py` over fresh exports with
`--digests docs/private-literal-digests.json` and this file as `--provenance`.
The script HMACs every normalized non-name vocabulary value and every two- or
three-word full-name n-gram locally. Bare first and last names are deliberately
excluded because they collide with ordinary English in public prose. Only the HMAC digest list is committed; source
values never leave the machine. CI normalizes the governed tracked corpus,
HMACs candidates with the secret salt, and compares digests.

The 256-bit salt is the only secret. Without it, the committed HMAC of even a
common full name is not practically brute-forceable. The value-free record below
binds CI to the exact digest list and complete source counts. A newly added
staff member, technician, vendor, or resident joins the next refresh without
editing a hand-written name list. The phone/path/record-ID structural gate
remains independent of the HMAC census.

The source exports and pre-HMAC vocabulary contain private data and must
remain untracked. Store conventional local inputs under the ignored
`private-exports/` directory and store any local salt file with the ignored
`*.vocab-salt` suffix. The contract test also rejects either pattern if a
forced add ever makes one tracked.

<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_START -->
- Agent records: `10`
- Vendor records: `17`
- Tenant records: `648`
- HMAC digests: `2062`
- Digest-list SHA-256: `35d56067b14862810ff66001810d3dd6d717bc8762eaf1b253987255e532a20f`
<!-- GENERATED_PRIVATE_LITERAL_PROVENANCE_END -->
