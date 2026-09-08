"""Offline behavior tests for the CSV cleanup delivery kit."""

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cleanup_csv as cleaner


BASE_RULES = {
    "delimiter": ",", "encoding": "utf-8", "decimal_separator": ".",
    "thousands_separator": ",", "numeric_columns": ["amount"],
    "identifier_columns": ["id"], "text_columns": [], "blank_numeric": "keep",
    "allow_outer_whitespace": True, "allow_leading_zero_numbers": False,
    "drop_blank_rows": True, "duplicates": {"mode": "keep", "keys": []},
}


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def config(self, **changes):
        rules = copy.deepcopy(BASE_RULES)
        rules.update(changes)
        path = self.root / "rules.json"
        path.write_text(json.dumps(rules), encoding="utf-8")
        return path

    def source(self, rows, name="input.csv", delimiter=","):
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter=delimiter)
            writer.writerows(rows)
        return path

    def test_decimal_thousands_normalization_and_identifiers(self):
        path = self.source([["id", "amount"], ["0007", " 1,234.500 "], ["0008", "+42.00"]])
        before = path.read_bytes()
        results = cleaner.run([path], self.config(), self.root / "out")
        self.assertEqual(results[0]["cleaned"], [["0007", "1234.5"], ["0008", "42"]])
        self.assertEqual(path.read_bytes(), before)
        audit = json.loads((self.root / "out/audit/input.audit.json").read_text())
        self.assertEqual(audit["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(audit["numeric_totals"]["output"]["amount"], "1276.5")
        self.assertTrue((self.root / "out/COMPLETE.json").exists())

    def test_comma_decimal_and_nonbreaking_grouping(self):
        config = self.config(delimiter=";", decimal_separator=",", thousands_separator="\u00a0")
        path = self.source([["id", "amount"], ["001", "1\u00a0234,50"], ["002", "-0,00"]], delimiter=";")
        result = cleaner.run([path], config, dry_run=True)[0]
        self.assertEqual(result["cleaned"], [["001", "1234,5"], ["002", "0"]])

    def test_mixed_locales_stray_characters_and_grouping_quarantine(self):
        values = ["12,34.5", "1.234,56", "$12.00", "1e3", "NaN", "12%", "12x", "1.", "  "]
        path = self.source([["id", "amount"]] + [[str(i), value] for i, value in enumerate(values)])
        result = cleaner.run([path], self.config(), dry_run=True)[0]
        self.assertEqual(len(result["quarantine"]), len(values))
        self.assertEqual(result["cleaned"], [])
        self.assertTrue(result["audit"]["review_required"])

    def test_leading_zero_numeric_requires_explicit_opt_in(self):
        path = self.source([["id", "amount"], ["0001", "0012"], ["0002", "0.5"]])
        result = cleaner.run([path], self.config(), dry_run=True)[0]
        self.assertEqual(result["cleaned"], [["0002", "0.5"]])
        self.assertIn("identifier", result["quarantine"][0]["reasons"][0])
        opted_in = cleaner.run([path], self.config(allow_leading_zero_numbers=True), dry_run=True)[0]
        self.assertEqual(opted_in["cleaned"][0], ["0001", "12"])

    def test_empty_is_missing_not_zero_and_blank_record_removed(self):
        path = self.source([["id", "amount"], ["001", ""], ["002", "0"], [], ["", ""]])
        result = cleaner.run([path], self.config(), dry_run=True)[0]
        self.assertEqual(result["cleaned"], [["001", ""], ["002", "0"]])
        self.assertEqual(result["audit"]["counts"]["blank_rows_removed"], 2)
        self.assertEqual(result["audit"]["blank_numeric_cells_in_validated_input"]["amount"], 1)
        self.assertTrue(result["audit"]["row_counts_reconcile"])
        quarantined = cleaner.run([path], self.config(blank_numeric="quarantine"), dry_run=True)[0]
        self.assertEqual(quarantined["cleaned"], [["002", "0"]])

    def test_keep_and_explicit_normalized_exact_duplicate_policy(self):
        path = self.source([["id", "amount"], ["001", "1,000.00"], ["001", "1000"], ["002", "1000"]])
        kept = cleaner.run([path], self.config(), dry_run=True)[0]
        self.assertEqual(len(kept["cleaned"]), 3)
        dropped = cleaner.run([path], self.config(duplicates={"mode": "drop_exact", "keys": []}), dry_run=True)[0]
        self.assertEqual(dropped["cleaned"], [["001", "1000"], ["002", "1000"]])
        self.assertEqual(dropped["audit"]["numeric_totals"]["validated_input"]["amount"], "3000")
        self.assertEqual(dropped["audit"]["numeric_totals"]["removed_duplicates"]["amount"], "1000")
        self.assertEqual(dropped["events"][-1]["matches_record"], 1)

    def test_duplicate_keys_quarantine_all_conflicting_records(self):
        path = self.source([["id", "amount"], ["001", "10"], ["001", "12"], ["002", "3"], ["", "4"]])
        result = cleaner.run([path], self.config(duplicates={"mode": "quarantine_keys", "keys": ["id"]}), dry_run=True)[0]
        self.assertEqual(result["cleaned"], [["002", "3"]])
        self.assertEqual(len(result["quarantine"]), 3)
        self.assertEqual(result["audit"]["numeric_totals"]["quarantined_valid"]["amount"], "22")
        self.assertTrue(result["audit"]["validated_numeric_totals_reconcile"])

    def test_malformed_width_quarantines_original_record(self):
        path = self.source([["id", "amount"], ["001", "2", "extra"], ["002"], ["003", "4"]])
        result = cleaner.run([path], self.config(), self.root / "out")[0]
        self.assertEqual(result["cleaned"], [["003", "4"]])
        self.assertEqual([r["original"] for r in result["quarantine"]], [["001", "2", "extra"], ["002"]])
        self.assertTrue(result["audit"]["row_counts_reconcile"])

    def test_unterminated_quotes_abort_entire_batch_before_output(self):
        first = self.source([["id", "amount"], ["001", "2"]], name="good.csv")
        second = self.root / "bad.csv"
        second.write_text('id,amount\n002,"12\n003,13\n', encoding="utf-8")
        with self.assertRaisesRegex(cleaner.CleanupError, "malformed CSV"):
            cleaner.run([first, second], self.config(), self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_same_rules_rerun_is_idempotent_byte_for_byte(self):
        config = self.config(delimiter=";", decimal_separator=",", thousands_separator=".",
                             duplicates={"mode": "drop_exact", "keys": []})
        path = self.source([["id", "amount"], ["0001", "1.234,50"], ["0001", "1234,5"],
                            ["0002", ""], ["0003", "-0,0"], []], delimiter=";")
        cleaner.run([path], config, self.root / "first")
        cleaned = self.root / "first/cleaned/input.csv"
        second = cleaner.run([cleaned], config, self.root / "second")[0]
        self.assertEqual(cleaned.read_bytes(), (self.root / "second/cleaned/input.csv").read_bytes())
        self.assertEqual(second["events"], [])
        self.assertEqual(second["quarantine"], [])

    def test_output_paths_never_overwrite_files_directories_or_symlinks(self):
        path = self.source([["id", "amount"], ["001", "2"]])
        config = self.config()
        original = path.read_bytes()
        occupied = self.root / "occupied"
        occupied.mkdir()
        marker = occupied / "keep.txt"
        marker.write_text("untouched")
        link = self.root / "link"
        link.symlink_to(occupied, target_is_directory=True)
        broken = self.root / "broken"
        broken.symlink_to(self.root / "absent", target_is_directory=True)
        for output in (path, occupied, link, broken, self.root):
            with self.subTest(output=output):
                with self.assertRaises(cleaner.CleanupError):
                    cleaner.run([path], config, output)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(marker.read_text(), "untouched")

    def test_dry_run_writes_nothing(self):
        path = self.source([["id", "amount"], ["001", "bad"]])
        config = self.config()
        before = sorted(p.name for p in self.root.iterdir())
        result = cleaner.run([path], config, self.root / "preview", dry_run=True)[0]
        self.assertTrue(result["audit"]["review_required"])
        self.assertEqual(before, sorted(p.name for p in self.root.iterdir()))

    def test_duplicate_paths_names_and_schema_mismatch_fail(self):
        path = self.source([["id", "amount"], ["001", "2"]])
        config = self.config()
        with self.assertRaisesRegex(cleaner.CleanupError, "more than once"):
            cleaner.run([path, path], config, dry_run=True)
        other = self.source([["amount", "id"], ["3", "002"]], name="other.csv")
        with self.assertRaisesRegex(cleaner.CleanupError, "same ordered schema"):
            cleaner.run([path, other], config, self.root / "out")
        self.assertFalse((self.root / "out").exists())
        sub = self.root / "sub"
        sub.mkdir()
        alias = sub / "INPUT.csv"
        alias.write_bytes(path.read_bytes())
        with self.assertRaisesRegex(cleaner.CleanupError, "unique"):
            cleaner.run([path, alias], config, dry_run=True)

    def test_limits_and_unknown_policy_fail_closed(self):
        path = self.source([["id", "amount"], ["001", "2"], ["002", "3"]])
        config = self.config()
        with patch.object(cleaner, "MAX_ROWS", 1):
            with self.assertRaisesRegex(cleaner.CleanupError, "total data records"):
                cleaner.run([path], config, self.root / "out")
        self.assertFalse((self.root / "out").exists())
        rules = json.loads(config.read_text())
        rules["guess_locale"] = True
        config.write_text(json.dumps(rules))
        with self.assertRaisesRegex(cleaner.CleanupError, "exactly"):
            cleaner.run([path], config, dry_run=True)

    def test_totals_remain_exact_for_large_and_fractional_numbers(self):
        values = ["99999999999999999999999999999999999999", "0.1", "0.2"]
        path = self.source([["id", "amount"]] + [[str(i), value] for i, value in enumerate(values)])
        result = cleaner.run([path], self.config(), dry_run=True)[0]
        self.assertEqual(result["audit"]["numeric_totals"]["output"]["amount"], values[0] + ".3")

    def test_cli_exit_codes_and_blank_header_rejection(self):
        path = self.source([["id", "amount"], ["001", "bad"]])
        config = self.config()
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(cleaner.main(["--config", str(config), "--dry-run", str(path)]), 3)
        path.write_text("id,id\n1,2\n")
        with patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(cleaner.main(["--config", str(config), "--dry-run", str(path)]), 2)

    def test_malformed_policy_types_and_duplicate_json_keys_are_rejected(self):
        path = self.source([["id", "amount"], ["001", "2"]])
        for changes in ({"decimal_separator": []}, {"thousands_separator": {}},
                        {"encoding": []}, {"blank_numeric": []},
                        {"duplicates": {"mode": [], "keys": []}}):
            with self.subTest(changes=changes):
                with self.assertRaises(cleaner.CleanupError):
                    cleaner.run([path], self.config(**changes), dry_run=True)
        config = self.config()
        config.write_text('{"delimiter": ",", "delimiter": ";"}')
        with self.assertRaisesRegex(cleaner.CleanupError, "Duplicate JSON setting"):
            cleaner.load_rules(config)

    def test_formula_like_identifiers_warn_without_rewriting_or_quarantining(self):
        ids = ["=1+1", "+4912345", "-customer", "  @reference"]
        path = self.source([["id", "amount"]] + [[value, "-5"] for value in ids])
        result = cleaner.run([path], self.config(), dry_run=True)[0]
        self.assertEqual(result["cleaned"], [[value, "-5"] for value in ids])
        self.assertEqual(result["quarantine"], [])
        warnings = result["audit"]["spreadsheet_text_warnings"]
        self.assertEqual([w["record"] for w in warnings], [1, 2, 3, 4])
        self.assertTrue(all(w["column"] == "id" for w in warnings))


if __name__ == "__main__":
    unittest.main()
