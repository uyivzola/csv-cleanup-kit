import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from plan_updates import InputError, build_report, load_json, main


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.config = {"csv_key": "id", "record_key": "ID",
                       "fields": {"name": "Name"}, "blank_values": "skip"}
        self.snapshot = {"text_fields": ["ID", "Name"], "records": [
            {"id": "recSyntheticA", "fields": {"ID": "0001", "Name": "Before"}},
            {"id": "recSyntheticB", "fields": {"ID": "1", "Name": "Other"}},
        ]}

    def plan(self, csv_text):
        return build_report(csv_text, self.config, self.snapshot)

    def test_existing_updates_preserve_leading_zero_ids_and_unmapped_record(self):
        before = copy.deepcopy(self.snapshot)
        result = self.plan("id,name\n0001,After\n")
        self.assertEqual(result["updates"], [{"operation": "update",
                         "record_id": "recSyntheticA", "key": "0001",
                         "changes": [{"field": "Name", "before": "Before", "after": "After"}]}])
        self.assertEqual(self.snapshot, before)
        self.assertEqual(result["audit"]["creates_planned"], 0)

    def test_unmatched_key_never_creates(self):
        result = self.plan("id,name\n0002,New\n")
        self.assertEqual(result["updates"], [])
        self.assertEqual(result["audit"]["outcomes"][0]["reasons"],
                         ["no_existing_record_create_refused"])
        self.assertEqual(result["audit"]["creates_planned"], 0)

    def test_both_duplicate_csv_rows_rejected_with_all_reasons(self):
        result = self.plan("id,name\n0001,First\n0001,Second\n0002,X\n0002,Y\n")
        self.assertEqual(result["updates"], [])
        self.assertEqual(result["audit"]["rejected_records"], 4)
        for outcome in result["audit"]["outcomes"]:
            self.assertIn("duplicate_csv_key", outcome["reasons"])
        self.assertIn("no_existing_record_create_refused",
                      result["audit"]["outcomes"][2]["reasons"])

    def test_duplicate_snapshot_key_blocks_only_ambiguous_match(self):
        self.snapshot["records"].append({"id": "recSyntheticC",
                                         "fields": {"ID": "0001", "Name": "Duplicate"}})
        result = self.plan("id,name\n0001,After\n1,Clear match\n")
        self.assertEqual([u["record_id"] for u in result["updates"]], ["recSyntheticB"])
        self.assertEqual(result["audit"]["outcomes"][0]["reasons"], ["duplicate_snapshot_key"])
        self.assertTrue(result["audit"]["review_required"])

    def test_missing_csv_and_snapshot_keys_are_reported(self):
        self.snapshot["records"].append({"id": "recSyntheticC", "fields": {"Name": "No ID"}})
        result = self.plan("id,name\n,Missing\n   ,Whitespace\n")
        self.assertEqual(result["audit"]["rejected_records"], 2)
        self.assertEqual(result["updates"], [])
        self.assertEqual(result["audit"]["snapshot_issues"],
                         [{"record_id": "recSyntheticC", "reason": "missing_match_key"}])

    def test_missing_mapping_and_unknown_destination_fail(self):
        with self.assertRaisesRegex(InputError, "Mapped CSV column"):
            self.plan("id,wrong\n0001,After\n")
        self.config["fields"] = {"name": "Typo"}
        with self.assertRaisesRegex(InputError, "destination field"):
            self.plan("id,name\n0001,After\n")

    def test_duplicate_destinations_readonly_key_and_create_option_refused(self):
        for fields in ({"name": "Name", "other": "Name"}, {"name": "ID"}, {}):
            with self.subTest(fields=fields), self.assertRaises(InputError):
                build_report("id,name,other\n0001,X,Y\n", {**self.config, "fields": fields}, self.snapshot)
        with self.assertRaises(InputError):
            build_report("id,name\n0001,X\n", {**self.config, "allow_create": True}, self.snapshot)

    def test_no_type_coercion_for_snapshot_ids_or_values(self):
        for field in ("ID", "Name"):
            with self.subTest(field=field):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot["records"][0]["fields"][field] = 1
                with self.assertRaisesRegex(InputError, "string field values"):
                    build_report("id,name\n0001,After\n", self.config, snapshot)

    def test_blank_policy_must_be_explicit_and_only_clear_changes_empty_cells(self):
        self.assertEqual(self.plan("id,name\n0001,\n")["updates"], [])
        self.config["blank_values"] = "clear"
        self.assertEqual(self.plan("id,name\n0001,\n")["updates"][0]["changes"][0]["after"], "")
        del self.config["blank_values"]
        with self.assertRaises(InputError):
            self.plan("id,name\n0001,\n")

    def test_exact_whitespace_and_formula_like_text_preserved(self):
        result = self.plan('id,name\n0001," =literal text "\n 0001,X\n')
        self.assertEqual(result["updates"][0]["changes"][0]["after"], " =literal text ")
        self.assertEqual(result["audit"]["rejected_records"], 1)

    def test_rerun_on_updated_synthetic_snapshot_is_noop(self):
        csv_text = "id,name\n0001,After\n"
        first = self.plan(csv_text)
        for update in first["updates"]:
            record = next(r for r in self.snapshot["records"] if r["id"] == update["record_id"])
            for change in update["changes"]:
                record["fields"][change["field"]] = change["after"]
        second = self.plan(csv_text)
        self.assertEqual(second["updates"], [])
        self.assertEqual(second["audit"]["unchanged_records"], 1)

    def test_deterministic_report_and_reconciled_counts(self):
        csv_text = "id,name,ignored\n0001,After,A\n1,Other,B\n0002,Missing,C\n"
        a = self.plan(csv_text)
        self.snapshot["records"].reverse()
        b = self.plan(csv_text)
        self.assertEqual(a, b)
        audit = a["audit"]
        self.assertEqual(audit["input_records"], sum(audit[k] for k in
                         ("updates_planned", "unchanged_records", "rejected_records")))
        self.assertEqual(audit["ignored_csv_columns"], ["ignored"])

    def test_duplicate_json_keys_and_nonstandard_constants_refused(self):
        for value in ('{"x":1,"x":2}', '{"x":NaN}'):
            with self.subTest(value=value), self.assertRaises(InputError):
                load_json(value)

    def test_duplicate_record_ids_refused(self):
        self.snapshot["records"][1]["id"] = "recSyntheticA"
        with self.assertRaisesRegex(InputError, "Duplicate snapshot record id"):
            self.plan("id,name\n0001,X\n")

    def test_cli_exit_codes_determinism_and_refused_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_file, mapping, records, output = (root / n for n in ("in.csv", "mapping.json", "records.json", "plan.json"))
            csv_file.write_text("id,name\n0001,After\n0002,Missing\n", encoding="utf-8")
            mapping.write_text(json.dumps(self.config), encoding="utf-8")
            records.write_text(json.dumps(self.snapshot), encoding="utf-8")
            args = ["--csv", str(csv_file), "--mapping", str(mapping), "--records", str(records)]
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(args + ["--output", str(output)]), 3)
                saved = output.read_bytes()
                self.assertEqual(main(args), 3)
                self.assertEqual(saved, stdout.getvalue().encode("utf-8"))
                self.assertEqual(main(args + ["--output", str(output)]), 2)
                self.assertEqual(saved, output.read_bytes())
                self.assertEqual(main(args + ["--output", str(csv_file)]), 2)
                csv_file.write_text("id,name\n1,Other\n", encoding="utf-8")
                self.assertEqual(main(args), 0)

    def test_invalid_csv_and_mapping_fail_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mapping.json").write_text(json.dumps(self.config), encoding="utf-8")
            (root / "records.json").write_text(json.dumps(self.snapshot), encoding="utf-8")
            args = ["--csv", str(root / "in.csv"), "--mapping", str(root / "mapping.json"),
                    "--records", str(root / "records.json"), "--output", str(root / "out.json")]
            for value in ('id,name\n0001,X,extra\n', 'id,name\n0001,"unterminated',
                          'id,id\n0001,X\n', 'id,wrong\n0001,X\n'):
                with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                    (root / "in.csv").write_text(value, encoding="utf-8")
                    self.assertEqual(main(args), 2)
                    self.assertFalse((root / "out.json").exists())


if __name__ == "__main__":
    unittest.main()
