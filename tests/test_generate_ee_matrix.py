"""Exercise changed-file EE routing against a complete temporary lifecycle catalog."""

import argparse
import contextlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("generate_ee_matrix", ROOT / ".github/workflows/generate_matrix.py")
MATRIX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MATRIX)


class EEMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.projects = {"active-ee": "active", "example-ee": "example", "dcm-ocp-ee-4.22": "active"}
        entries = {}
        for name, status in self.projects.items():
            directory = self.root / name
            directory.mkdir()
            (directory / "execution-environment.yml").write_text("version: 3\n")
            (directory / "files").mkdir()
            (directory / "files/requirements.yml").write_text("collections: []\n")
            entries[name] = {"status": status, "last_recorded_ci_activity": None,
                             "evidence_url": None, "evidence_type": "no_recorded_ci_build_activity"}
        (self.root / ".github").mkdir()
        self.catalog = self.root / ".github/ee-projects.json"
        self.catalog.write_text(json.dumps({"schema_version": 1, "assessment_date": "2026-11-17",
                                           "activity_window_start": "2025-11-17", "projects": entries}))
        self.output = self.root / "matrix.json"

    def generate(self, changed):
        args = argparse.Namespace(start_ref="origin/main", end_ref="a" * 40,
                                  output_path=str(self.output), log_level="INFO")
        self.messages = io.StringIO()
        logger = logging.Logger("ee-matrix-test")
        logger.addHandler(logging.StreamHandler(self.messages))
        with patch.object(MATRIX, "parse_arguments", return_value=args), \
                patch.object(MATRIX, "get_changed_files", return_value=changed), \
                patch.object(MATRIX, "setup_logger", return_value=logger), \
                patch.dict(os.environ, {"GITHUB_WORKSPACE": str(self.root)}), \
                contextlib.redirect_stdout(io.StringIO()):
            MATRIX.main()
        return json.loads(self.output.read_text())

    def test_mixed_changes_keep_active_skip_examples_and_leave_dependent_routing_separate(self):
        result = self.generate(["example-ee/files/requirements.yml", "active-ee/files/requirements.yml",
                                "active-ee/execution-environment.yml", "dcm-ocp-ee-4.22/files/requirements.yml"])
        self.assertEqual(result, {"include": [{"ee": "active-ee"}]})
        self.assertIn("Skip example-only EE: example-ee", self.messages.getvalue())

    def test_example_only_change_produces_an_empty_matrix(self):
        self.assertEqual(self.generate(["example-ee/execution-environment.yml"]), {"include": []})

    def test_explicit_reenable_changes_routing_without_moving_definition(self):
        data = json.loads(self.catalog.read_text())
        data["projects"]["example-ee"]["status"] = "active"
        self.catalog.write_text(json.dumps(data))
        self.assertEqual(self.generate(["example-ee/files/requirements.yml"]),
                         {"include": [{"ee": "example-ee"}]})

    def test_missing_or_incomplete_policy_stops_before_git_diff(self):
        args = argparse.Namespace(start_ref="origin/main", end_ref="a" * 40,
                                  output_path=str(self.output), log_level="INFO")
        self.catalog.unlink()
        with patch.object(MATRIX, "parse_arguments", return_value=args), \
                patch.object(MATRIX, "get_changed_files") as changed, \
                patch.dict(os.environ, {"GITHUB_WORKSPACE": str(self.root)}), \
                self.assertRaises(MATRIX.CatalogError):
            MATRIX.main()
        changed.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
