# Local validation — 2026-09-08

All data used here is synthetic. No buyer files or private data were processed.

- `python3 -m unittest -v`: **18 tests passed**. Coverage includes two decimal conventions, strict thousands grouping, missing values, leading-zero identifiers, explicit leading-zero numeric opt-in, three duplicate policies, exact decimal sums, wrong-width quarantine, invalid CSV quoting, malformed rules, row limits, same-schema enforcement, dry runs, source preservation, existing-path/symlink refusal, idempotence, and formula-like text warnings without rewriting.
- Synthetic example: **9 input records = 3 cleaned + 1 blank removed + 1 duplicate removed + 4 quarantined**. Source and output hashes, event details, and quarantine are in `examples/expected-output/`.
- Full service-bound smoke: generated **10 same-schema files with 10,000 data records each**, processed all **100,000 records**, and verified every file's row-count reconciliation and exact numeric total. A complete rerun on all cleaned copies produced identical CSV bytes and no change/quarantine events. Temporary capacity-test files were discarded afterward.
- Entire capacity run, rerun, and fixture regeneration took approximately **1.36 seconds** in this local environment. This is an observed synthetic test, not a performance guarantee for arbitrary customer data or hardware.

The published example contains code, tests, documentation, rules and synthetic fixtures. Customer input/output, local environments and generated cache files are excluded.
