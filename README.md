# CSV Cleanup Kit

A small Python command-line tool that cleans agreed numeric formats while preserving identifier text, retaining originals, and explaining removed or quarantined records. Python 3.10+; standard library only. No installation, paid API, account, or network connection is needed to run it.

Synthetic examples show exactly which records are kept, changed, removed or held for review.

**Need a batch processed for your business?** [See the $300 custom-cleanup package and open a work inquiry](SERVICE.md). Scope and payment arrangements are agreed before work begins.

## Run the synthetic demo

From this folder:

```sh
python3 cleanup_csv.py --config examples/rules.json --dry-run examples/synthetic-sales.csv
python3 cleanup_csv.py --config examples/rules.json --output demo-output examples/synthetic-sales.csv
```

Both commands return **exit code 3 intentionally**: the demo contains four records needing review. This is a completed processing result, not a program crash. Use a new output directory each run; an existing path is always refused.

The synthetic input uses semicolons between fields, decimal commas, thousands dots, numeric `amount`/`quantity`, and text identifier `record_id`. Example effects:

| Input | Output / explanation |
| --- | --- |
| `0001;1.234,50;2` | `0001;1234,5;2`; leading-zero ID retained |
| `0001;1234,5;2` | Removed as an exact duplicate **after** approved normalization |
| `0002;;0` | Preserved; missing amount stays empty, zero quantity stays zero |
| `;;` | Empty record removed under the explicit blank-row policy |
| `0003;12,3x;4` | Quarantined; the stray character's meaning is unknown |
| `0004;1,234.56;2` | Quarantined; conflicts with the configured number format |
| `0005;42,00;1` | `0005;42;1` |
| `0006;5;2;unexpected` | Quarantined; wrong field count |
| `0007; 7,50 ;+03` | Quarantined; leading-zero quantity is not silently reinterpreted |

Expected counts: **9 input records = 3 output + 1 blank removed + 1 duplicate removed + 4 quarantined**. Header is not counted as a record. Expected amount totals on fully validated rows: **2511 input = 1276.5 output + 1234.5 removed duplicate**. One validated input amount is missing. The malformed rows' amounts are unresolved and excluded from those totals.

## Output bundle

```text
demo-output/
  cleaned/synthetic-sales.csv
  audit/synthetic-sales.audit.json
  audit/synthetic-sales.events.jsonl
  quarantine/synthetic-sales.quarantine.jsonl
  cleanup-rules.json
  COMPLETE.json
```

- `cleaned/`: new CSV copies, same column names/order, delimiter, and configured encoding. Numeric values use the configured decimal separator with thousands separators and insignificant trailing decimal zeros removed. IDs and text columns remain character-for-character unchanged. CSV quoting/newline bytes may be rewritten; original files are never edited.
- `audit/*.audit.json`: source/output SHA-256 hashes, reconciled row counts, numeric totals, missing-value counts, the exact rules used, and per-record warnings for formula-like identifier/text prefixes. Source names are recorded without private folder paths.
- `audit/*.events.jsonl`: each normalized output record and each removed/quarantined record, with original record/physical line references and reasons. Dropped duplicates identify the retained record. Records kept unchanged need no event entry.
- `quarantine/*.quarantine.jsonl`: original field values and reasons. JSON Lines keeps wrong-width records intact without forcing them into a false CSV schema. Nothing here is repaired by guesswork.
- `COMPLETE.json`: written last. Its presence marks a completely written bundle; `review_required` still means the quarantine needs resolution. A disk/permission failure can leave an incomplete directory without this marker; preserve it for diagnosis and rerun to a fresh directory.

Exit statuses: **0** processed with no quarantine; **3** processed with quarantine; **2** invalid rules/input, a limit violation, output conflict, or I/O failure. A dry run performs the same validation and calculations and prints counts without creating output files.

## Configure the agreed rules

Copy `examples/rules.json` and explicitly set every field. Missing settings, duplicate JSON keys, unknown settings, unknown CSV columns, and conflicting classifications are rejected.

| Setting | Meaning |
| --- | --- |
| `delimiter` | One character, e.g. `,`, `;`, or `\t`; quoted fields supported |
| `encoding` | `utf-8`, `utf-8-sig` for UTF-8 BOM handling, or `cp1252`; no auto-detection |
| `decimal_separator` | `.` or `,`; applies to all numeric columns in the batch |
| `thousands_separator` | `null`, `.`, `,`, space, NBSP, or narrow NBSP; must differ from decimal separator |
| `numeric_columns` | Columns authorized for numeric normalization |
| `identifier_columns` / `text_columns` | All remaining columns, preserved exactly; every column must be classified once |
| `blank_numeric` | `keep` retains empty cells as missing; `quarantine` flags them |
| `allow_outer_whitespace` | Whether numeric values may be trimmed; whitespace-only cells still require review |
| `allow_leading_zero_numbers` | False quarantines values such as `0012` in numeric columns; true permits their conversion to `12` |
| `drop_blank_rows` | Whether genuinely empty records are removed; whitespace text is not treated as empty |
| `duplicates.mode` | `keep`, `drop_exact`, or `quarantine_keys` |
| `duplicates.keys` | Empty except with `quarantine_keys`; then names the agreed duplicate-key columns |

`drop_exact` compares **all normalized fields**, retains the first occurrence, and operates **within each file**. It does not deduplicate across files or assume matching amounts alone prove duplicates. `quarantine_keys` also operates within each file and quarantines **every** record sharing a resolvable repeated key, including its first member and rows with errors in other columns. Original validation errors remain in the audit alongside the duplicate finding. Key comparisons use normalized numeric values and untouched ID/text values. Rows with wrong field counts, empty keys or malformed numeric keys are quarantined individually; their keys are not guessed or included in duplicate groups.

Numbers support ordinary signed decimals and strictly grouped thousands. Locale is declared, never inferred. Thus `1.234` means one thousand two hundred thirty-four when the decimal separator is comma and thousands separator is dot. Get that rule approved from a representative sample first. Currency symbols, percentages, scientific notation, accounting parentheses, non-ASCII digits, and mixed formats are quarantined rather than reinterpreted.

The tool supports **up to 10 same-schema files, 100,000 total data records, 64 MiB per file, 128 MiB per batch, 65,536 characters per cell, and 100 digits per number**. Files are held in memory to validate the complete batch before output starts. Exact decimal totals use sufficient precision for these limits. Blank numeric cells remain missing and are separately counted; no numeric total invents a zero cell.

Malformed field counts are quarantined. Unreadable encoding, malformed headers, incompatible schemas, NUL bytes, exceeded limits, or invalid CSV quoting stop the entire batch before output is created. A syntactically broken quoted CSV cannot be safely split into guessed records, so it must first be corrected at the source. Parsing uses Python's strict CSV reader; this is not a universal spreadsheet file-repair tool.

IDs/text are not evaluated or sanitized. Prefixes `=`, `+`, `-`, or `@` after leading whitespace produce per-record/column spreadsheet-import warnings in the audit, without rewriting or quarantining otherwise valid rows. This intentionally includes legitimate phone numbers starting `+`. Import identifier/text columns as text so spreadsheet software does not reinterpret leading zeros, dates, or formula-like strings. Warnings do not change exit status; check the warning count and audit even on exit 0. The CLI itself does not execute cell content. Audit and quarantine files contain row data: keep real customer bundles private.

## Verify idempotence and tests

After generating `demo-output`:

```sh
python3 cleanup_csv.py --config demo-output/cleanup-rules.json --output rerun-output demo-output/cleaned/synthetic-sales.csv
python3 -m unittest -v
```

The rerun should return 0, produce the same cleaned CSV bytes, and contain no normalization/removal/quarantine events. Tests cover number formats, exact sums, identifiers, blanks, three duplicate policies, idempotence, malformed CSV, schema/row limits, dry runs, malformed configuration, and refused overwrites including symlinks.

## Customer handover checklist

1. Agree delimiter/encoding, column classifications, decimal/thousands conventions, allowed formats, missing-value policy, duplicate definition/scope, and an expected output sample. Agree the file/row limits and any totals that are meaningful for the business.
2. Work from a private copy of the supplied inputs. Record the supplied source hashes and run a dry run. Obtain the customer's approval of rules and sample results before the full batch.
3. Generate a fresh output bundle. Reconcile input records against output, blank removals, duplicate removals, and quarantine. Review the per-file totals and missing-value counts; unresolved quarantine prevents claiming complete business reconciliation.
4. Return cleaned copies, audit/events, quarantine, the approved rules, this script, and run instructions through the agreed private delivery channel. Reprocess source data with revised approved rules where required; do not hand-edit undocumented fixes.
5. Demonstrate a zero-change rerun and get acceptance against the agreed result. A successful command or public demo alone does not establish customer acceptance or payment.

Implementation and synthetic examples were prepared with AI assistance and tested locally. This repository demonstrates the workflow; it does not represent a completed customer project or payment.
