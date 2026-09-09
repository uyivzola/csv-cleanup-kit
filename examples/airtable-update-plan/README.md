# Offline CSV-to-Airtable update-plan demo

A small, standard-library Python 3.10+ CLI that compares CSV text with supplied synthetic record JSON and emits an update plan plus audit report. It has **no network/API calls, credentials, record creation or execution mode**. This is AI-assisted development with synthetic examples and local tests, not a live Airtable integration or completed customer project.

Synthetic demonstration; not a live Airtable integration or paid customer project.

## Run the synthetic example

From this directory:

```sh
python3 plan_updates.py --csv examples/input.csv --mapping examples/mapping.json --records examples/records.json
python3 plan_updates.py --csv examples/input.csv --mapping examples/mapping.json --records examples/records.json --output demo-report.json
python3 -m unittest -v
```

The example intentionally returns **exit code 3**: six CSV records produce one planned update, one unchanged record and four rejected records. `0001` changes only its name. `0003` has no matching record, both `0004` rows conflict and the last row has no key. Creates remain zero. Review the JSON; exit 3 means the report was generated but review is required. Use a new output path on each run.

Exit **0** means a report with no rejected rows or snapshot issues; **3** means review is required; **2** means invalid input, invalid configuration, an output conflict or an I/O error. All input validation completes before creating output. An I/O failure can leave a partial output file: a failed command is not a completed report. Existing output paths, including symlinks and the input paths, are refused. Without `--output`, JSON goes to stdout.

## Explicit rules

- `csv_key` and `record_key` identify the source and destination text match fields. Match is exact: `0001`, `1` and ` 0001` differ. Empty or whitespace-only keys are rejected. No numeric conversion, case folding or whitespace trimming occurs.
- `fields` maps each source column to one destination text field. Missing source columns, undeclared destination fields, duplicate destination mappings and attempts to modify the match field fail. Extra unmapped CSV columns are listed in the audit and ignored. The match field need not appear in `fields`.
- `blank_values` is required: `skip` leaves a destination untouched for an empty CSV cell; `clear` explicitly plans an empty string. Whitespace-only text is preserved as supplied. Omitted destination values represent empty strings under this text-only snapshot contract.
- The record JSON contains `text_fields`, an explicit list of allowed text fields, and `records`, each with `id` and `fields`. It is a small demonstration format, **not a raw Airtable API response or a verified live schema**. Every supplied value must be a string. The example IDs are synthetic, not valid credentials or verified Airtable record IDs.
- Duplicate CSV keys reject **every** member. A duplicate key in the snapshot rejects matching CSV rows; missing snapshot keys are separately reported. Duplicate snapshot record IDs fail the entire input. No matching record means rejection, never creation. Valid unambiguous rows can still appear in a partial plan when other rows need review.

The audit reconciles input rows into planned updates, unchanged rows and rejected rows. It lists reasons, ignored columns, snapshot issues and the rules used. Every planned change includes its previous and proposed value. CLI reports contain SHA-256 hashes of the exact source bytes and no timestamps, so identical inputs produce byte-identical output. The tests also simulate accepting the changes into a local synthetic snapshot and confirm the next comparison is unchanged; no live idempotence is claimed.

## Limits and handoff

Inputs are UTF-8 (optional BOM), comma-separated CSV and JSON. Quoted CSV fields are supported using Python's strict CSV parser; quoting errors it detects fail before output. Duplicate/empty headers, wrong-width rows, CSV NUL bytes, duplicate JSON keys and nonstandard JSON constants also fail before output. Each file is limited to 5 MiB; source and snapshot each permit at most 10,000 records. Python's CSV field-size limit also applies. Inputs and the report are held in memory.

Only text fields are modeled. Numeric/date conversion, formulas, linked records, attachments, pagination, authentication, scheduling, API rate limiting, retries, live conflict detection, live schema checks and API writes are absent. The plan is descriptive JSON, not an Airtable request payload. A stale snapshot can produce a stale plan; any later integration must revalidate authorized live records and schema before writing.

Cell content is preserved as text and never evaluated. Report files contain values; keep real data private if adapting the tool, and do not upload credentials or customer data to public repositories or AI tools without agreement. No paid package or account is required to run this demo.
