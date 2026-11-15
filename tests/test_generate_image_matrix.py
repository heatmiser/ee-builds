"""Image routing regression tests using temporary files and mocked Git diffs."""

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

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_image_matrix", ROOT / ".github/workflows/generate_image_matrix.py")
MATRIX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MATRIX)


class ImageMatrixTests(unittest.TestCase):
    def matrix(self, files, changed):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for filename in files:
                target = workspace / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("fixture\n")
            output = workspace / "matrix.json"
            args = argparse.Namespace(start_ref="origin/main", end_ref="a" * 40,
                                      output_path=str(output), log_level="INFO")
            logger = logging.Logger("matrix-test")
            logger.addHandler(logging.NullHandler())
            with patch.object(MATRIX, "parse_arguments", return_value=args), \
                    patch.object(MATRIX, "get_changed_files", return_value=changed), \
                    patch.object(MATRIX, "setup_logger", return_value=logger), \
                    patch.dict(os.environ, {"GITHUB_WORKSPACE": directory}), \
                    contextlib.redirect_stdout(io.StringIO()):
                MATRIX.main()
            return json.loads(output.read_text())

    def test_ee_readme_does_not_select_plain_image_build(self):
        result = self.matrix(["dcm-ocp-ee-4.22/README.md",
                              "dcm-ocp-ee-4.22/execution-environment.yml"],
                             ["dcm-ocp-ee-4.22/README.md"])
        self.assertEqual(result, {"include": []})

    def test_readme_change_still_selects_real_image(self):
        result = self.matrix(["codex-ai-helpers/Containerfile", "codex-ai-helpers/README.md"],
                             ["codex-ai-helpers/README.md"])
        self.assertEqual(result, {"include": [{"image": "codex-ai-helpers"}]})

    def test_nested_l8k_change_selects_buildable_top_level_root(self):
        result = self.matrix(["k8s-launch-kit/Containerfile", "k8s-launch-kit/tests/README.md"],
                             ["k8s-launch-kit/tests/test_build.py"])
        self.assertEqual(result, {"include": [{"image": "k8s-launch-kit"}]})

    def test_nested_containerfile_and_metadata_do_not_make_root_buildable(self):
        result = self.matrix(["docs/example/Containerfile", "metadata-only/image.yaml",
                              "metadata-only/README.md"],
                             ["docs/example/Containerfile", "metadata-only/README.md"])
        self.assertEqual(result, {"include": []})

    def test_mixed_changes_exclude_ee_and_deduplicate_sorted_images(self):
        result = self.matrix(["z-image/Containerfile", "a-image/Containerfile",
                              "a-image/README.md", "dcm-ocp-ee-4.22/README.md"],
                             ["z-image/Containerfile", "dcm-ocp-ee-4.22/README.md",
                              "a-image/README.md", "a-image/Containerfile"])
        self.assertEqual(result, {"include": [{"image": "a-image"}, {"image": "z-image"}]})

    def test_ci_includes_matrix_regressions(self):
        workflow = yaml.load((ROOT / ".github/workflows/test-ocp-ee.yml").read_text(),
                             Loader=yaml.BaseLoader)
        paths = workflow["on"]["pull_request"]["paths"]
        self.assertIn(".github/workflows/generate_image_matrix.py", paths)
        self.assertIn("tests/test_generate_image_matrix.py", paths)
        commands = [step.get("run", "") for step in workflow["jobs"]["test"]["steps"]]
        self.assertTrue(any("unittest discover -s tests -p 'test_*.py'" in command
                            for command in commands))


if __name__ == "__main__":
    unittest.main()
