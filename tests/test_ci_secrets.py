"""Synthetic value, output, image and workflow enforcement tests; no real services."""

import base64
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import ci_secrets as guard
import ci_publication as ci
from test_ci_publication import oci_fixture, tar_bytes

CANARY = "synthetic-guard-credential:/+$42"


class SecretGuardTests(unittest.TestCase):
    def test_every_confidential_source_and_supported_encoding_is_detected(self):
        self.assertEqual(len(guard.SECRET_NAMES), 12)
        for name in guard.SECRET_NAMES - guard.PUBLIC_IDENTIFIERS:
            for alias in (name, "CI_SECRET_" + name):
                with self.subTest(source=alias):
                    variants = guard.patterns({alias: CANARY, "REGISTRY_USERNAME": "robot"})
                    self.assertGreaterEqual(len(variants), 7)
                    for encoded in variants:
                        with self.assertRaises(guard.GuardError):
                            guard.inspect_stream(io.BytesIO(b"x" * (1024 * 1024 - 3) + encoded + b"end"), variants)
                    guard.inspect_stream(io.BytesIO(b"ordinary public metadata"), variants)

    def test_public_namespace_works_and_confidential_collision_still_fails(self):
        public = {"CI_SECRET_QUAY_USERNAME": "public-team", "CI_SECRET_QUAY_PASSWORD": CANARY}
        guard.inspect_stream(io.BytesIO(b"quay.io/public-team/image:latest"), guard.patterns(public))
        basic = base64.b64encode(("public-team:" + CANARY).encode())
        self.assertIn(basic, guard.patterns(public))
        public["CI_SECRET_AH_TOKEN"] = "public-team"
        with self.assertRaises(guard.GuardError):
            guard.inspect_stream(io.BytesIO(b"quay.io/public-team/image:latest"), guard.patterns(public))

    def test_files_missing_inputs_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as name, patch.dict(os.environ, CI_SECRET_AH_TOKEN=CANARY):
            root = Path(name)
            path = root / "receipt.json"
            path.write_text(json.dumps({"extra": CANARY}))
            with self.assertRaises(guard.GuardError):
                guard.inspect_paths([path])
            path.write_text('{"status":"PASS"}')
            guard.inspect_paths([path])
            link = root / "link"
            link.symlink_to(path)
            for target in (link, root / "missing"):
                with self.assertRaises(guard.GuardError):
                    guard.inspect_paths([target])

    def test_real_child_output_never_reaches_console_and_environment_is_stripped(self):
        with patch.dict(os.environ, CI_SECRET_AH_TOKEN=CANARY, AH_TOKEN=CANARY):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                output = guard.captured([sys.executable, "-c", "import os; print('CI_SECRET_AH_TOKEN' in os.environ); print('AH_TOKEN' in os.environ)"])
                self.assertEqual(output, b"False\nFalse\n")
                for code in (0, 1):
                    script = "import sys; print(" + repr(CANARY) + "); sys.exit(" + str(code) + ")"
                    with self.assertRaises(guard.GuardError):
                        guard.captured([sys.executable, "-c", script])
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_failed_command_categories_never_echo_diagnostic_text(self):
        cases = (("unauthorized", "authentication-rejected"),
                 ("Repository not found", "git-access-failed"),
                 ("THESE PACKAGES DO NOT MATCH THE HASHES", "package-hash-mismatch"),
                 ("No space left on device", "storage-exhausted"),
                 ("x509: certificate signed by unknown authority", "tls-failure"),
                 ("Temporary failure in name resolution", "network-failure"),
                 ("manifest unknown", "image-reference-unavailable"),
                 ("no matching manifest", "image-platform-mismatch"),
                 ("No matching distribution found", "dependency-installation-failed"),
                 ("unrecognized tool diagnostic with private URL", "command-failed"))
        for text, category in cases:
            with self.subTest(category=category):
                stdout, stderr = io.StringIO(), io.StringIO()
                script = "import sys; sys.stderr.write(" + repr(text) + "); sys.exit(17)"
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(guard.GuardError) as caught:
                        guard.captured([sys.executable, "-c", script])
                self.assertEqual(caught.exception.category, category)
                self.assertNotIn(text, str(caught.exception))
                self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_credential_detection_precedes_diagnostic_classification(self):
        for destination in ("stdout", "stderr"):
            with self.subTest(destination=destination), patch.dict(os.environ, CI_SECRET_AH_TOKEN=CANARY):
                script = ("import sys; sys." + destination + ".write(" + repr("unauthorized " + CANARY)
                          + "); sys.exit(1)")
                with patch.object(guard, "command_category") as classify:
                    with self.assertRaises(guard.GuardError) as caught:
                        guard.captured([sys.executable, "-c", script])
                    classify.assert_not_called()
                self.assertEqual(caught.exception.category, "credential-detected")
                self.assertNotIn(CANARY, str(caught.exception))

    def test_classifier_handles_chunk_boundaries_and_inspects_both_streams(self):
        first = io.BytesIO(b"ordinary failure")
        second = io.BytesIO(b"x" * (1024 * 1024 - 4) + b"UNAUTHORIZED")
        self.assertEqual(guard.command_category((first, second)), "authentication-rejected")
        self.assertEqual(guard.command_category((io.BytesIO(b"unauthorized"), io.BytesIO(b"disk quota exceeded"))),
                         "storage-exhausted")

    def test_start_timeout_and_output_bound_categories_do_not_expose_exceptions(self):
        failures = ((subprocess.TimeoutExpired([CANARY], 1, output=CANARY, stderr=CANARY), "command-timeout"),
                    (FileNotFoundError(CANARY), "command-unavailable"),
                    (PermissionError(CANARY), "command-start-failed"))
        for error, category in failures:
            with self.subTest(category=category), patch.object(guard.subprocess, "run", side_effect=error):
                with self.assertRaises(guard.GuardError) as caught:
                    guard.captured(["synthetic-command"])
                self.assertEqual(caught.exception.category, category)
                self.assertNotIn(CANARY, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
        with self.assertRaises(guard.GuardError) as caught:
            guard.captured([sys.executable, "-c", "import sys; sys.stdout.write('x' * (4 * 1024 * 1024 + 1))"])
        self.assertEqual(caught.exception.category, "output-too-large")

    def test_unknown_error_category_cannot_become_public_diagnostic_metadata(self):
        self.assertEqual(guard.GuardError("fixed", category=CANARY).category, "operation-failed")
        error = ValueError(CANARY)
        error.category = CANARY
        self.assertEqual(guard.error_category(error), "operation-failed")

    def test_authfile_is_private_and_not_written_to_source_or_default_home(self):
        with tempfile.TemporaryDirectory() as name:
            auth = Path(name) / "ee-auth-test.json"
            env = {"RUNNER_TEMP": name, "REGISTRY_AUTH_FILE": str(auth),
                   "REGISTRY_USERNAME": "registry-user", "REGISTRY_PASSWORD": CANARY}
            with patch.dict(os.environ, env), patch.object(guard.subprocess, "run") as run:
                guard.login("quay.io")
                self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(auth.read_text())["auths"]["quay.io"]["auth"],
                                 base64.b64encode(("registry-user:" + CANARY).encode()).decode())
                run.assert_not_called()
                auth.unlink()

    def test_config_history_and_deleted_lower_layer_secret_are_detected(self):
        for placement in ("config", "history", "lower-layer", "nested-archive", "clean"):
            with self.subTest(placement=placement), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                fixture = root / "fixture"
                layers = [tar_bytes({"secret": CANARY.encode()}), tar_bytes({".wh.secret": b""})] if placement == "lower-layer" else None
                if placement == "nested-archive":
                    layers = [tar_bytes({"bundle.gz": gzip.compress(CANARY.encode())})]
                identity = oci_fixture(fixture, layers=layers,
                                       environment=["TOKEN=" + CANARY] if placement == "config" else None)
                if placement == "history":
                    index = json.loads((fixture / "index.json").read_text())
                    manifest_path = fixture / "blobs/sha256" / index["manifests"][0]["digest"][7:]
                    manifest = json.loads(manifest_path.read_text())
                    config_path = fixture / "blobs/sha256" / identity[7:]
                    config = json.loads(config_path.read_text())
                    config["history"] = [{"created_by": "ARG TOKEN=" + CANARY}]
                    content = json.dumps(config).encode()
                    identity = "sha256:" + hashlib.sha256(content).hexdigest()
                    config_path.unlink()
                    (fixture / "blobs/sha256" / identity[7:]).write_bytes(content)
                    manifest["config"].update(digest=identity, size=len(content))
                    content = json.dumps(manifest).encode()
                    digest = "sha256:" + hashlib.sha256(content).hexdigest()
                    manifest_path.unlink()
                    (fixture / "blobs/sha256" / digest[7:]).write_bytes(content)
                    index["manifests"][0].update(digest=digest, size=len(content))
                    (fixture / "index.json").write_text(json.dumps(index))

                def run(argv, **kwargs):
                    if argv[1:3] == ["image", "inspect"]:
                        return (identity + "\n").encode()
                    self.assertEqual(argv[:2], ["podman", "save"])
                    shutil.copytree(fixture, Path(argv[argv.index("--output") + 1]))
                    return b""

                with patch.dict(os.environ, CI_SECRET_AH_TOKEN=CANARY, RUNNER_TEMP=name), patch.object(guard, "captured", side_effect=run):
                    if placement == "clean":
                        self.assertEqual(guard.inspect_image("local:test"), identity)
                    else:
                        with self.assertRaises(ci.PublicError):
                            guard.inspect_image("local:test")

    def test_oversized_credentials_are_rejected_without_echoing_values(self):
        with self.assertRaises(guard.GuardError) as error:
            guard.patterns({"CI_SECRET_AH_TOKEN": "x" * 65537})
        self.assertNotIn("x" * 20, str(error.exception))

    def test_ocp_image_denial_prevents_receipt_and_push(self):
        from test_ocp_ee import EE, Mirror, Runner, COMMIT, BASE
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / "build"
            with patch.object(guard, "inspect_image", side_effect=guard.GuardError("Credential detected")):
                with self.assertRaises(guard.GuardError):
                    EE.build("4.22.22", BASE, COMMIT, "123-1", output, run=Runner(), opener=Mirror())
                self.assertFalse((output / "receipt.json").exists())
            with patch.object(guard, "inspect_image", return_value="sha256:" + "c" * 64):
                receipt = EE.build("4.22.22", BASE, COMMIT, "123-1", Path(name) / "clean", run=Runner(), opener=Mirror())
            runner = Runner()
            with patch.object(guard, "inspect_image", side_effect=guard.GuardError("Credential detected")):
                with self.assertRaises(guard.GuardError):
                    EE.publish(receipt, "quay.io/public-team", run=runner)
            self.assertFalse(any(argv[:2] == ["podman", "push"] for argv in runner.commands))


class WorkflowGuardTests(unittest.TestCase):
    def test_every_upload_requires_its_exact_file_guard(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            for job in workflow["jobs"].values():
                steps = job.get("steps", [])
                for index, upload in enumerate(steps):
                    if not upload.get("uses", "").startswith("actions/upload-artifact@"):
                        continue
                    with self.subTest(workflow=path.name, upload=upload["name"]):
                        gate = steps[index - 1]
                        self.assertIn("ci_secrets.py files", gate["run"])
                        self.assertIn("steps." + gate["id"] + ".outcome == 'success'", upload["if"])
                        for candidate in upload["with"]["path"].splitlines():
                            self.assertIn('"' + candidate.strip() + '"', gate["run"])

    def test_guard_steps_receive_every_secret_referenced_by_their_job(self):
        import re
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            for job_id, job in workflow["jobs"].items():
                names = set(re.findall(r"secrets\.([A-Z_]+)", json.dumps(job)))
                for step in job.get("steps", []):
                    command = step.get("run", "")
                    if not any(marker in command for marker in ("ci_secrets.py files", "ee_build.py", "ocp_ee.py build")):
                        continue
                    with self.subTest(workflow=path.name, job=job_id, step=step["name"]):
                        for source in names:
                            self.assertEqual(step["env"]["CI_SECRET_" + source], "${{ secrets." + source + " }}")

    def test_credential_bearing_build_args_and_token_substitution_are_gone_from_workflows(self):
        for name in ("pr-ee-build.yml", "push-ee-build.yml"):
            text = (ROOT / ".github/workflows" / name).read_text()
            self.assertNotIn("--build-arg AH_TOKEN", text)
            self.assertNotIn("sed -i \"s/my_ah_token", text)
            self.assertNotIn("redhat-actions/podman-login", text)


if __name__ == "__main__":
    unittest.main()
