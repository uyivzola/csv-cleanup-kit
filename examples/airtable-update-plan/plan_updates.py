#!/usr/bin/env python3
"""Offline, text-only CSV update planner. Never connects to Airtable."""

import argparse
import csv
import hashlib
import io
import json
from collections import Counter, defaultdict
from pathlib import Path
import sys


MAX_BYTES = 5 * 1024 * 1024
MAX_RECORDS = 10_000


class InputError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InputError(message)


def named(value):
    return isinstance(value, str) and bool(value.strip())


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(raw):
    def invalid_constant(value):
        raise InputError(f"Invalid JSON constant: {value}")

    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=invalid_constant)


def build_report(csv_text, config, snapshot):
    """Return deterministic plan/audit dictionaries; mutate none of the inputs."""
    require(isinstance(config, dict), "Configuration must be an object")
    require(set(config) == {"csv_key", "record_key", "fields", "blank_values"},
            "Configuration requires only csv_key, record_key, fields, blank_values")
    require(named(config["csv_key"]) and named(config["record_key"]),
            "Match field names must be nonempty strings")
    require(config["blank_values"] in ("skip", "clear"),
            "blank_values must explicitly be skip or clear")
    mapping = config["fields"]
    require(isinstance(mapping, dict) and bool(mapping), "fields needs a mapping")
    require(all(named(k) and named(v) for k, v in mapping.items()),
            "Mapping names must be nonempty strings")
    require(len(set(mapping.values())) == len(mapping), "Duplicate destination mapping")
    require(config["record_key"] not in mapping.values(),
            "Match key is read-only; mapping cannot change it")

    require(isinstance(snapshot, dict) and set(snapshot) == {"text_fields", "records"},
            "Snapshot requires only text_fields and records")
    names, records = snapshot["text_fields"], snapshot["records"]
    require(isinstance(names, list) and all(named(x) for x in names),
            "text_fields must list nonempty field names")
    require(len(set(names)) == len(names), "Duplicate snapshot field name")
    require(config["record_key"] in names, "Snapshot match field is not declared")
    require(set(mapping.values()) <= set(names), "Mapped destination field not declared")
    require(isinstance(records, list) and len(records) <= MAX_RECORDS,
            f"Snapshot requires at most {MAX_RECORDS} records")
    index, seen_ids, snapshot_issues = defaultdict(list), set(), []
    for record in records:
        require(isinstance(record, dict) and set(record) == {"id", "fields"},
                "Each record requires only id and fields")
        record_id, values = record["id"], record["fields"]
        require(named(record_id), "Record id must be a nonempty string")
        require(record_id not in seen_ids, "Duplicate snapshot record id")
        seen_ids.add(record_id)
        require(isinstance(values, dict) and set(values) <= set(names),
                "Record fields must use declared text_fields")
        require(all(isinstance(v, str) for v in values.values()),
                "Only string field values are supported; no automatic coercion")
        key = values.get(config["record_key"], "")
        if not key.strip():
            snapshot_issues.append({"record_id": record_id, "reason": "missing_match_key"})
        else:
            index[key].append(record)
    for key in sorted(index):
        if len(index[key]) > 1:
            snapshot_issues.append({"key": key, "reason": "duplicate_match_key",
                                    "record_ids": sorted(r["id"] for r in index[key])})

    require("\x00" not in csv_text, "CSV contains a NUL byte")
    reader = csv.reader(io.StringIO(csv_text, newline=""), strict=True)
    headers = next(reader, None)
    require(headers is not None and all(named(h) for h in headers), "CSV needs a header")
    require(len(set(headers)) == len(headers), "Duplicate CSV header")
    require(config["csv_key"] in headers, "CSV match column is missing")
    require(set(mapping) <= set(headers), "Mapped CSV column is missing")
    rows = []
    for row in reader:
        require(len(row) == len(headers), f"Wrong field count at CSV line {reader.line_num}")
        rows.append(dict(zip(headers, row)))
        require(len(rows) <= MAX_RECORDS, f"CSV exceeds {MAX_RECORDS} records")
    keys = Counter(row[config["csv_key"]] for row in rows
                   if row[config["csv_key"]].strip())
    updates, outcomes = [], []
    for number, row in enumerate(rows, 1):
        key = row[config["csv_key"]]
        reasons = []
        if not key.strip():
            reasons.append("missing_match_key")
        else:
            if keys[key] > 1:
                reasons.append("duplicate_csv_key")
            if key not in index:
                reasons.append("no_existing_record_create_refused")
            elif len(index[key]) > 1:
                reasons.append("duplicate_snapshot_key")
        outcome = {"csv_record": number, "key": key}
        if reasons:
            outcomes.append({**outcome, "status": "rejected", "reasons": reasons})
            continue
        record = index[key][0]
        changes = []
        for source, target in sorted(mapping.items()):
            after = row[source]
            if after == "" and config["blank_values"] == "skip":
                continue
            # The declared text-only snapshot represents omitted blank fields as "".
            before = record["fields"].get(target, "")
            if before != after:
                changes.append({"field": target, "before": before, "after": after})
        if changes:
            updates.append({"operation": "update", "record_id": record["id"],
                            "key": key, "changes": changes})
        outcomes.append({**outcome, "record_id": record["id"],
                         "status": "update_planned" if changes else "unchanged"})
    counts = Counter(o["status"] for o in outcomes)
    return {
        "schema_version": 1,
        "mode": "offline_dry_run",
        "rules": config,
        "updates": updates,
        "audit": {
            "input_records": len(rows),
            "snapshot_records": len(records),
            "updates_planned": counts["update_planned"],
            "unchanged_records": counts["unchanged"],
            "rejected_records": counts["rejected"],
            "creates_planned": 0,
            "review_required": bool(counts["rejected"] or snapshot_issues),
            "ignored_csv_columns": sorted(set(headers) - set(mapping) - {config["csv_key"]}),
            "snapshot_issues": sorted(snapshot_issues, key=lambda x: json.dumps(x, sort_keys=True)),
            "outcomes": outcomes,
        },
    }


def read_input(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, f"Input exceeds {MAX_BYTES} bytes")
    return raw, raw.decode("utf-8-sig")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="UTF-8 comma-separated CSV")
    parser.add_argument("--mapping", required=True, help="Explicit mapping JSON")
    parser.add_argument("--records", required=True, help="Synthetic text-field snapshot JSON")
    parser.add_argument("--output", help="New JSON report path; existing paths are refused")
    args = parser.parse_args(argv)
    try:
        raw_csv, csv_text = read_input(args.csv)
        raw_mapping, mapping_text = read_input(args.mapping)
        raw_records, records_text = read_input(args.records)
        report = build_report(csv_text, load_json(mapping_text), load_json(records_text))
        report["source_sha256"] = {
            "csv": hashlib.sha256(raw_csv).hexdigest(),
            "mapping": hashlib.sha256(raw_mapping).hexdigest(),
            "records": hashlib.sha256(raw_records).hexdigest(),
        }
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if args.output:
            # Validate every input and assemble the report before creating any output.
            with Path(args.output).open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
        else:
            print(encoded, end="")
        return 3 if report["audit"]["review_required"] else 0
    except (InputError, OSError, UnicodeError, csv.Error, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
