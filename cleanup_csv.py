#!/usr/bin/env python3
"""Conservative, standard-library CSV cleanup with explicit rules and audit trails."""

import argparse
import csv
from decimal import Decimal, localcontext
import hashlib
import io
import json
from pathlib import Path
import re
import sys

MAX_FILES = 10
MAX_ROWS = 100_000
MAX_BYTES = 64 * 1024 * 1024
MAX_BATCH_BYTES = 128 * 1024 * 1024
MAX_CELL = 65_536
RULE_KEYS = {
    "delimiter", "encoding", "decimal_separator", "thousands_separator",
    "numeric_columns", "identifier_columns", "text_columns", "blank_numeric",
    "allow_outer_whitespace", "allow_leading_zero_numbers", "drop_blank_rows",
    "duplicates",
}


class CleanupError(ValueError):
    """An unsafe or invalid batch; no successful delivery should be inferred."""


def load_rules(path):
    """Require every policy explicitly; reject unknown/misspelled settings."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CleanupError(f"Duplicate JSON setting: {key}")
            result[key] = value
        return result

    try:
        rules = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CleanupError(f"Cannot read rules: {exc}") from exc
    if not isinstance(rules, dict) or set(rules) != RULE_KEYS:
        raise CleanupError(f"Rules must contain exactly: {', '.join(sorted(RULE_KEYS))}")
    delimiter = rules["delimiter"]
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in '\r\n"':
        raise CleanupError("delimiter must be one non-quote, non-newline character")
    if not isinstance(rules["encoding"], str) or rules["encoding"] not in {"utf-8", "utf-8-sig", "cp1252"}:
        raise CleanupError("encoding must be utf-8, utf-8-sig, or cp1252")
    if not isinstance(rules["decimal_separator"], str) or rules["decimal_separator"] not in {".", ","}:
        raise CleanupError("decimal_separator must be '.' or ','")
    if (not isinstance(rules["thousands_separator"], (str, type(None)))
            or rules["thousands_separator"] not in {None, ".", ",", " ", "\u00a0", "\u202f"}):
        raise CleanupError("Unsupported thousands_separator")
    if rules["thousands_separator"] == rules["decimal_separator"]:
        raise CleanupError("Decimal and thousands separators must differ")
    columns = []
    for key in ("numeric_columns", "identifier_columns", "text_columns"):
        group = rules[key]
        if not isinstance(group, list) or not all(isinstance(c, str) and c for c in group):
            raise CleanupError(f"{key} must be a list of nonempty column names")
        columns.extend(group)
    if not columns or len(set(columns)) != len(columns):
        raise CleanupError("Every column must be classified once, without duplicates")
    if not isinstance(rules["blank_numeric"], str) or rules["blank_numeric"] not in {"keep", "quarantine"}:
        raise CleanupError("blank_numeric must be keep or quarantine")
    for key in ("allow_outer_whitespace", "allow_leading_zero_numbers", "drop_blank_rows"):
        if type(rules[key]) is not bool:
            raise CleanupError(f"{key} must be true or false")
    policy = rules["duplicates"]
    if not isinstance(policy, dict) or set(policy) != {"mode", "keys"}:
        raise CleanupError("duplicates requires exactly mode and keys")
    if not isinstance(policy["mode"], str) or policy["mode"] not in {"keep", "drop_exact", "quarantine_keys"}:
        raise CleanupError("duplicates.mode must be keep, drop_exact, or quarantine_keys")
    keys = policy["keys"]
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise CleanupError("duplicates.keys must be a list of column names")
    if len(set(keys)) != len(keys) or not set(keys) <= set(columns):
        raise CleanupError("Duplicate keys must be distinct configured columns")
    if (policy["mode"] == "quarantine_keys") != bool(keys):
        raise CleanupError("Only quarantine_keys uses a nonempty keys list")
    return rules


def normalize_number(raw, rules):
    """Parse an explicitly configured locale, never infer IDs or replace blanks by zero."""
    if raw == "":
        if rules["blank_numeric"] == "keep":
            return "", None
        raise CleanupError("blank numeric value requires review")
    token = raw.strip() if rules["allow_outer_whitespace"] else raw
    if not token:
        raise CleanupError("whitespace-only numeric cell is not an empty value")
    if len(token) > 210:
        raise CleanupError("numeric value exceeds supported size")
    decimal = rules["decimal_separator"]
    thousands = rules["thousands_separator"]
    sign = ""
    if token[0] in "+-":
        sign, token = token[0], token[1:]
    if token.count(decimal) > 1:
        raise CleanupError("multiple decimal separators")
    pieces = token.split(decimal)
    integer = pieces[0]
    fraction = pieces[1] if len(pieces) == 2 else None
    if fraction is not None and not re.fullmatch(r"[0-9]+", fraction):
        raise CleanupError("invalid or empty fractional part")
    if thousands and thousands in integer:
        groups = integer.split(thousands)
        if not re.fullmatch(r"[0-9]{1,3}", groups[0]) or any(
            not re.fullmatch(r"[0-9]{3}", group) for group in groups[1:]
        ):
            raise CleanupError("invalid thousands grouping")
        integer = "".join(groups)
    if not re.fullmatch(r"[0-9]+", integer):
        raise CleanupError("unsupported characters or ambiguous number format")
    if len(integer) + len(fraction or "") > 100:
        raise CleanupError("numeric value exceeds 100 digits")
    if len(integer) > 1 and integer.startswith("0") and not rules["allow_leading_zero_numbers"]:
        raise CleanupError("leading-zero number may be an identifier")
    value = Decimal(sign + integer + ("." + fraction if fraction is not None else ""))
    canonical = format(value, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if value == 0:
        canonical = "0"
    return canonical.replace(".", decimal), value


def _decimal_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def process_file(path, raw, rules, expected_header, remaining_rows):
    """Read one snapshot. Structural CSV errors abort; malformed records quarantine."""
    try:
        text = raw.decode(rules["encoding"], errors="strict")
    except UnicodeError as exc:
        raise CleanupError(f"{path.name}: encoding error: {exc}") from exc
    if "\x00" in text:
        raise CleanupError(f"{path.name}: NUL bytes are not supported")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=rules["delimiter"], strict=True)
    try:
        header = next(reader)
    except (StopIteration, csv.Error) as exc:
        raise CleanupError(f"{path.name}: missing or malformed CSV header") from exc
    if not header or any(not h for h in header) or len(set(header)) != len(header):
        raise CleanupError(f"{path.name}: header must contain distinct nonempty names")
    configured = rules["numeric_columns"] + rules["identifier_columns"] + rules["text_columns"]
    if set(header) != set(configured):
        raise CleanupError(f"{path.name}: every header must match the configured column classification")
    if expected_header is not None and header != expected_header:
        raise CleanupError(f"{path.name}: all input files must have the same ordered schema")
    numeric_indexes = [(header.index(c), c) for c in rules["numeric_columns"]]
    text_indexes = [(header.index(c), c) for c in rules["identifier_columns"] + rules["text_columns"]]
    key_indexes = [header.index(c) for c in rules["duplicates"]["keys"]]
    numeric_key_columns = set(rules["duplicates"]["keys"]) & set(rules["numeric_columns"])
    key_counts, quarantined_key_reasons = {}, []
    valid, quarantine, events, text_warnings = [], [], [], []
    counts = dict(input_records=0, output_records=0, blank_rows_removed=0,
                  duplicates_removed=0, quarantined_records=0, normalized_cells=0)
    totals = {category: {column: Decimal(0) for column in rules["numeric_columns"]}
              for category in ("validated_input", "output", "removed_duplicates", "quarantined_valid")}
    blank_counts = {column: 0 for column in rules["numeric_columns"]}
    try:
        while True:
            first_line = reader.line_num + 1
            try:
                row = next(reader)
            except StopIteration:
                break
            counts["input_records"] += 1
            if counts["input_records"] > remaining_rows:
                raise CleanupError(f"Batch exceeds {MAX_ROWS:,} total data records")
            origin = {"record": counts["input_records"], "line_start": first_line,
                      "line_end": reader.line_num}
            if rules["drop_blank_rows"] and (not row or all(cell == "" for cell in row)):
                counts["blank_rows_removed"] += 1
                events.append({**origin, "action": "remove_blank", "original": row})
                continue
            reasons = []
            normalized = row.copy()
            values = {}
            changes = []
            duplicate_key = None
            if len(row) != len(header):
                reasons.append(f"expected {len(header)} fields; received {len(row)}")
            else:
                for index, column in text_indexes:
                    if row[index].lstrip().startswith(("=", "+", "-", "@")):
                        text_warnings.append({**origin, "column": column,
                                              "warning": "formula-like text prefix; import this column as text",
                                              "action": "preserved unchanged"})
                for index, column in numeric_indexes:
                    try:
                        normalized[index], values[column] = normalize_number(row[index], rules)
                        if normalized[index] != row[index]:
                            changes.append({"column": column, "before": row[index], "after": normalized[index]})
                    except CleanupError as exc:
                        reasons.append(f"{column}: {exc}")
                if key_indexes and any(normalized[i] == "" for i in key_indexes):
                    reasons.append("empty duplicate key requires review")
                # A valid key still identifies a duplicate when an unrelated
                # field fails validation. Never infer an invalid numeric key or
                # assign column positions to a record with the wrong width.
                if (key_indexes and all(normalized[i] != "" for i in key_indexes)
                        and numeric_key_columns <= values.keys()):
                    duplicate_key = tuple(normalized[i] for i in key_indexes)
                    key_counts[duplicate_key] = key_counts.get(duplicate_key, 0) + 1
            if reasons:
                quarantine.append({**origin, "original": row, "reasons": reasons})
                events.append({**origin, "action": "quarantine", "reasons": reasons})
                if duplicate_key is not None:
                    quarantined_key_reasons.append((duplicate_key, reasons))
                continue
            for column, value in values.items():
                if value is None:
                    blank_counts[column] += 1
                else:
                    totals["validated_input"][column] += value
            valid.append({**origin, "original": row, "cleaned": normalized,
                          "values": values, "changes": changes})
    except csv.Error as exc:
        raise CleanupError(f"{path.name}: malformed CSV near line {reader.line_num}: {exc}; batch aborted") from exc

    mode = rules["duplicates"]["mode"]
    duplicate_reason = "duplicate key; every member of the group requires review"
    for key, reasons in quarantined_key_reasons:
        if key_counts[key] > 1:
            # The quarantine and event entries share this reasons list. Keep
            # original validation failures and append the duplicate finding.
            reasons.append(duplicate_reason)
    seen, cleaned = {}, []
    for record in valid:
        origin = {key: record[key] for key in ("record", "line_start", "line_end")}
        row = record["cleaned"]
        exact_key = tuple(row)
        if mode == "quarantine_keys" and key_counts[tuple(row[i] for i in key_indexes)] > 1:
            reasons = [duplicate_reason]
            quarantine.append({**origin, "original": record["original"], "reasons": reasons})
            events.append({**origin, "action": "quarantine", "reasons": reasons})
            total_group = "quarantined_valid"
        elif mode == "drop_exact" and exact_key in seen:
            counts["duplicates_removed"] += 1
            events.append({**origin, "action": "remove_exact_duplicate", "original": record["original"],
                           "matches_record": seen[exact_key], "comparison": "all normalized fields"})
            total_group = "removed_duplicates"
        else:
            seen[exact_key] = record["record"]
            cleaned.append(row)
            counts["normalized_cells"] += len(record["changes"])
            if record["changes"]:
                events.append({**origin, "action": "normalize", "changes": record["changes"]})
            total_group = "output"
        for column, value in record["values"].items():
            if value is not None:
                totals[total_group][column] += value
    counts["quarantined_records"] = len(quarantine)
    counts["output_records"] = len(cleaned)
    reconciled = counts["input_records"] == sum(counts[k] for k in (
        "output_records", "blank_rows_removed", "duplicates_removed", "quarantined_records"))
    numeric_reconciled = all(totals["validated_input"][c] == (
        totals["output"][c] + totals["removed_duplicates"][c] + totals["quarantined_valid"][c]
    ) for c in rules["numeric_columns"])
    if not reconciled or not numeric_reconciled:
        raise CleanupError("Internal reconciliation failure")
    audit = {"source": path.name, "source_sha256": hashlib.sha256(raw).hexdigest(),
             "source_bytes": len(raw), "header": header, "counts": counts,
             "row_counts_reconcile": reconciled, "validated_numeric_totals_reconcile": numeric_reconciled,
             "numeric_totals": {kind: {col: _decimal_text(val) for col, val in vals.items()}
                                for kind, vals in totals.items()},
             "blank_numeric_cells_in_validated_input": blank_counts,
             "spreadsheet_text_warnings": text_warnings,
             "totals_scope": "Fully validated records only; blanks are missing, not zero. Quarantined invalid rows require client review.",
             "review_required": bool(quarantine), "rules": rules}
    return {"path": path, "header": header, "cleaned": cleaned, "audit": audit,
            "quarantine": sorted(quarantine, key=lambda r: r["record"]),
            "events": sorted(events, key=lambda r: r["record"])}


def prepare_batch(inputs, rules):
    if not 1 <= len(inputs) <= MAX_FILES:
        raise CleanupError(f"Provide 1–{MAX_FILES} input files")
    paths = [Path(p).resolve(strict=True) for p in inputs]
    if len(set(paths)) != len(paths):
        raise CleanupError("An input file was provided more than once")
    names = [p.name.casefold() for p in paths]
    stems = [p.stem.casefold() for p in paths]
    if len(set(names)) != len(names) or len(set(stems)) != len(stems):
        raise CleanupError("Input filenames/stems must be unique, ignoring case")
    if any(p.suffix.lower() != ".csv" or not p.is_file() for p in paths):
        raise CleanupError("Inputs must be regular .csv files")
    results, total_rows, total_bytes, header = [], 0, 0, None
    csv.field_size_limit(MAX_CELL)
    with localcontext() as context:
        context.prec = 256  # 100-digit cells and <=100,000 rows sum exactly.
        for path in paths:
            with path.open("rb") as source:
                raw = source.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise CleanupError(f"{path.name}: exceeds 64 MiB per-file limit")
            total_bytes += len(raw)
            if total_bytes > MAX_BATCH_BYTES:
                raise CleanupError("Batch exceeds 128 MiB input limit")
            result = process_file(path, raw, rules, header, MAX_ROWS - total_rows)
            header = result["header"]
            total_rows += result["audit"]["counts"]["input_records"]
            results.append(result)
    return results


def write_json(path, data):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_outputs(output, results, rules):
    """Reserve a NEW directory; exclusive files ensure nothing is ever overwritten."""
    output = Path(output)
    # mkdir without exist_ok also refuses existing files, empty dirs, and symlinks.
    output.mkdir(mode=0o700)
    for name in ("cleaned", "audit", "quarantine"):
        (output / name).mkdir()
    write_json(output / "cleanup-rules.json", rules)
    for result in results:
        path = result["path"]
        clean_path = output / "cleaned" / path.name
        with clean_path.open("x", encoding=rules["encoding"], newline="") as handle:
            writer = csv.writer(handle, delimiter=rules["delimiter"], lineterminator="\n")
            writer.writerow(result["header"])
            writer.writerows(result["cleaned"])
        audit = {**result["audit"], "cleaned_sha256": hashlib.sha256(clean_path.read_bytes()).hexdigest()}
        write_json(output / "audit" / (path.stem + ".audit.json"), audit)
        for folder, key, suffix in (("audit", "events", ".events.jsonl"),
                                    ("quarantine", "quarantine", ".quarantine.jsonl")):
            with (output / folder / (path.stem + suffix)).open("x", encoding="utf-8", newline="\n") as handle:
                for entry in result[key]:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # Only a batch with this final manifest is a complete output bundle.
    write_json(output / "COMPLETE.json", {
        "complete": True, "review_required": any(r["audit"]["review_required"] for r in results),
        "files": [r["path"].name for r in results], "data_is_synthetic": "not inferred; caller must label the inputs",
        "notice": "Complete processing does not mean quarantined rows are resolved or buyer acceptance is obtained.",
    })


def run(inputs, config, output=None, dry_run=False):
    rules = load_rules(config)
    if not dry_run and output is None:
        raise CleanupError("--output is required unless --dry-run is used")
    if output is not None and (Path(output).exists() or Path(output).is_symlink()):
        raise CleanupError("Output must be a new directory; existing paths are never overwritten")
    results = prepare_batch(inputs, rules)
    if not dry_run:
        write_outputs(output, results, rules)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="JSON rules; all policies explicit")
    parser.add_argument("--output", help="New output directory under an existing parent")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing any output")
    parser.add_argument("inputs", nargs="+", help="1–10 same-schema CSV files, <=100,000 total records")
    args = parser.parse_args(argv)
    try:
        results = run(args.inputs, args.config, args.output, args.dry_run)
    except (CleanupError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"dry_run": args.dry_run, "files": [
        {"source": r["path"].name, **r["audit"]["counts"], "review_required": r["audit"]["review_required"],
         "spreadsheet_text_warning_count": len(r["audit"]["spreadsheet_text_warnings"])}
        for r in results]}, indent=2))
    return 3 if any(r["audit"]["review_required"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
