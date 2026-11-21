"""Offline lifecycle policy tests; no registry, credentials or image builds."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import ee_projects as POLICY


def entry(status):
    return {"status": status, "last_recorded_ci_activity": None,
            "evidence_url": None, "evidence_type": "no_recorded_ci_build_activity"}


def fixture(root):
    for name in ("active-ee", "example-ee"):
        directory = root / name
        directory.mkdir()
        (directory / "execution-environment.yml").write_text("version: 3\n")
    (root / ".github").mkdir()
    data = {"schema_version": 1, "assessment_date": "2026-11-17",
            "activity_window_start": "2025-11-17",
            "projects": {"active-ee": entry("active"), "example-ee": entry("example")}}
    (root / ".github/ee-projects.json").write_text(json.dumps(data))
    return data


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = fixture(self.root)
        self.path = self.root / ".github/ee-projects.json"

    def write(self, data):
        self.path.write_text(json.dumps(data))

    def test_repository_catalog_covers_all_definitions(self):
        catalog = POLICY.load_catalog(ROOT)
        self.assertEqual(set(catalog), {p.parent.name for p in ROOT.glob("*/execution-environment.yml")})
        self.assertEqual(catalog["dcm-base-ee"]["status"], "active")
        self.assertEqual(catalog["aws-ee"]["status"], "example")

    def test_examples_remain_on_disk_but_cannot_be_selected(self):
        POLICY.require_active("active-ee", self.root)
        with self.assertRaisesRegex(POLICY.CatalogError, "example-only"):
            POLICY.require_active("example-ee", self.root)
        self.assertTrue((self.root / "example-ee/execution-environment.yml").is_file())
        self.data["projects"]["example-ee"]["status"] = "active"
        self.write(self.data)
        POLICY.require_active("example-ee", self.root)

    def test_unknown_and_unsafe_names_deny_selection(self):
        for name in ("unknown-ee", "../active-ee", "/tmp/active-ee", "active-ee\n", None):
            with self.subTest(name=name), self.assertRaises(POLICY.CatalogError):
                POLICY.require_active(name, self.root)

    def test_missing_malformed_duplicate_and_oversized_policy_fail_closed(self):
        for content in ("not-json", "[]", '{"schema_version":1,"schema_version":1}', " " * (1024 * 1024 + 1)):
            self.path.write_text(content)
            with self.subTest(size=len(content)), self.assertRaises(POLICY.CatalogError):
                POLICY.load_catalog(self.root)
        self.path.unlink()
        with self.assertRaises(POLICY.CatalogError):
            POLICY.load_catalog(self.root)

    def test_invalid_schema_status_evidence_and_dates_are_rejected(self):
        cases = []
        for key, value in (("schema_version", True), ("assessment_date", "2026-13-17"),
                           ("activity_window_start", "2027-11-17"), ("extra", "synthetic-secret")):
            data = copy.deepcopy(self.data)
            data[key] = value
            cases.append(data)
        for key, value in (("status", "enabled"), ("evidence_type", ["bad"]),
                           ("evidence_url", "https://synthetic-secret@example.test/")):
            data = copy.deepcopy(self.data)
            data["projects"]["active-ee"][key] = value
            cases.append(data)
        for data in cases:
            self.write(data)
            with self.subTest(data=data), self.assertRaises(POLICY.CatalogError) as caught:
                POLICY.load_catalog(self.root)
            self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_new_uncataloged_definition_and_stale_catalog_entry_are_errors(self):
        new = self.root / "new-ee"
        new.mkdir()
        (new / "execution-environment.yml").write_text("version: 3\n")
        with self.assertRaisesRegex(POLICY.CatalogError, "cover every"):
            POLICY.load_catalog(self.root)
        self.data["projects"]["new-ee"] = entry("example")
        self.write(self.data)
        self.assertFalse(POLICY.is_active("new-ee", POLICY.load_catalog(self.root)))
        (new / "execution-environment.yml").unlink()
        with self.assertRaises(POLICY.CatalogError):
            POLICY.load_catalog(self.root)

    def test_symlink_catalog_definition_and_project_directory_are_rejected(self):
        saved = self.root / "saved.json"
        self.path.rename(saved)
        self.path.symlink_to(saved)
        with self.assertRaises(POLICY.CatalogError):
            POLICY.load_catalog(self.root)
        self.path.unlink()
        saved.rename(self.path)
        project = self.root / "active-ee"
        definition = project / "execution-environment.yml"
        definition.rename(project / "definition.yml")
        definition.symlink_to(project / "definition.yml")
        with self.assertRaises(POLICY.CatalogError):
            POLICY.load_catalog(self.root)
        definition.unlink()
        (project / "definition.yml").rename(definition)
        project.rename(self.root / "saved-project")
        project.symlink_to(self.root / "saved-project", target_is_directory=True)
        with self.assertRaises(POLICY.CatalogError):
            POLICY.load_catalog(self.root)

    def test_cli_rejects_examples_without_producing_a_matrix(self):
        result = subprocess.run([sys.executable, str(ROOT / ".github/scripts/ee_projects.py"),
                                 "matrix", "--project", "aws-ee"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("example-only", result.stderr)


class WorkflowEligibilityTests(unittest.TestCase):
    def workflow(self, name):
        return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)

    def test_primary_and_dependent_workers_check_before_credentials_and_publication(self):
        for filename in ("pr-ee-build.yml", "push-ee-build.yml"):
            workflow = self.workflow(filename)
            for job in ("build-ee", "build-ocp-ees"):
                steps = workflow["jobs"][job]["steps"]
                guards = [i for i, s in enumerate(steps) if "eligibility" in s["name"]]
                secret_steps = [i for i, s in enumerate(steps) if "secrets." in json.dumps(s)]
                pushes = [i for i, s in enumerate(steps) if s["name"].startswith("Push To ")]
                with self.subTest(workflow=filename, job=job):
                    self.assertEqual(len(guards), 2)
                    self.assertLess(guards[0], min(secret_steps))
                    self.assertLess(guards[-1], min(pushes))
                    self.assertIn("ocp_length != '0'", workflow["jobs"]["build-ocp-ees"]["if"])

    def test_exact_release_and_pr_preflight_precede_registry_logins(self):
        for filename in ("pr-ocp-ee-build.yml", "build-versioned-ocp-ee.yml"):
            workflow = self.workflow(filename)
            for job in ("prepare", "build"):
                steps = workflow["jobs"][job]["steps"]
                checks = [i for i, s in enumerate(steps) if "eligibility" in s["name"]
                          or s["name"] == "Validate exact releases"]
                logins = [i for i, s in enumerate(steps) if "Log in" in s["name"]]
                with self.subTest(workflow=filename, job=job):
                    self.assertTrue(checks)
                    self.assertLess(min(checks), min(logins))

    def test_manual_example_selection_fails_before_external_actions(self):
        steps = self.workflow("push-ee-build.yml")["jobs"]["prepare-matrix"]["steps"]
        step = next(s for s in steps if s["name"] == "Generate matrix (workflow_dispatch)")
        with tempfile.TemporaryDirectory() as directory:
            # The workflow writes only its matrix; execute from a copy-free repo
            # context and redirect that output into a private temporary directory.
            script = step["run"].replace('> matrix_output.json', '> "$MATRIX_OUTPUT"')
            output = Path(directory) / "matrix.json"
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=ROOT,
                                    env=dict(os.environ, EE_NAME="aws-ee", MATRIX_OUTPUT=str(output)),
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("example-only", result.stderr)
            self.assertFalse(output.exists() and output.stat().st_size)

    def test_policy_and_tests_trigger_validation(self):
        paths = self.workflow("test-ocp-ee.yml")["on"]["pull_request"]["paths"]
        for path in (".github/ee-projects.json", ".github/scripts/ee_projects.py",
                     ".github/workflows/generate_matrix.py", "tests/test_ee_projects.py",
                     "tests/test_generate_ee_matrix.py"):
            self.assertIn(path, paths)


if __name__ == "__main__":
    unittest.main()
