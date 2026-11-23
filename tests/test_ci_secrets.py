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
    def test_git_basic_credentials_are_detected_and_attributed_for_every_source_alias(self):
        encoded = base64.b64encode((guard.GIT_AUTH_USERNAME + ":" + CANARY).encode())
        for source in guard.GIT_TOKEN_NAMES:
            for alias in (source, "CI_SECRET_" + source):
                environment = {alias: CANARY}
                with self.subTest(source=alias):
                    self.assertIn(encoded, guard.patterns(environment))
                    self.assertEqual(set(guard.attributed_patterns(environment)), set(guard.patterns(environment)))
                    with self.assertRaises(guard.GuardError):
                        guard.inspect_stream(io.BytesIO(b"Authorization: Basic " + encoded), guard.patterns(environment))
                    self.assertEqual(guard.matching_sources(io.BytesIO(encoded), environment), (source,))
        environment = {"RHVP_OCP_LANDING_ZONE_TOKEN": CANARY,
                       "CI_SECRET_RHVP_OCP_LANDING_ZONE_TOKEN": CANARY + "-override",
                       "CI_SECRET_OCP_SATELLITE_TOKEN": CANARY}
        self.assertEqual(set(guard.attributed_patterns(environment)), set(guard.patterns(environment)))
        self.assertEqual(guard.matching_sources(io.BytesIO(encoded), environment),
                         ("OCP_SATELLITE_TOKEN", "RHVP_OCP_LANDING_ZONE_TOKEN"))

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
                self.assertEqual(caught.exception.sources, ("AH_TOKEN",))
                self.assertEqual(caught.exception.stream, destination)
                self.assertEqual(caught.exception.command_status, "failure")
                self.assertNotIn(CANARY, str(caught.exception))

    def test_attribution_reuses_every_existing_encoding_and_preserves_source_collisions(self):
        for name in guard.SECRET_NAMES - guard.PUBLIC_IDENTIFIERS:
            for alias in (name, "CI_SECRET_" + name):
                environment = {alias: CANARY}
                for encoded in guard.patterns(environment):
                    with self.subTest(source=alias, encoded_length=len(encoded)):
                        stream = io.BytesIO(b"ordinary-prefix " + encoded + b" ordinary-suffix")
                        self.assertEqual(guard.matching_sources(stream, environment), (name,))
        environment = {"CI_SECRET_AH_TOKEN": CANARY, "RHVP_OCP_LANDING_ZONE_TOKEN": CANARY,
                       "CI_SECRET_QUAY_USERNAME": CANARY}
        self.assertEqual(guard.matching_sources(io.BytesIO(CANARY.encode()), environment),
                         ("AH_TOKEN", "RHVP_OCP_LANDING_ZONE_TOKEN"))
        self.assertEqual(guard.matching_sources(io.BytesIO(CANARY.encode()), {"QUAY_USERNAME": CANARY}), ())

    def test_basic_auth_pairs_and_legacy_sources_are_attributed_without_exposing_names_or_values(self):
        for user, password in guard.BASIC_AUTH_PAIRS:
            environment = {"CI_SECRET_" + user: "invented-registry-user", "CI_SECRET_" + password: CANARY}
            # Use ordinary REGISTRY_USERNAME because its CI_SECRET alias is an
            # additional confidential input, unlike a normal registry identifier.
            if user == "REGISTRY_USERNAME":
                environment = {user: "invented-registry-user", password: CANARY}
            raw = base64.b64encode(("invented-registry-user:" + CANARY).encode())
            sources = {n for n in (user, password) if n in guard.MATCH_SOURCE_NAMES and n not in guard.PUBLIC_IDENTIFIERS}
            self.assertEqual(set(guard.matching_sources(io.BytesIO(raw), environment)), sources)
        for alias, expected in (("GH_TOKEN", "GH_TOKEN"), ("REGISTRY_PASSWORD", "REGISTRY_PASSWORD"),
                                ("CI_SECRET_" + CANARY, "OTHER_CI_SECRET")):
            self.assertEqual(guard.matching_sources(io.BytesIO(CANARY.encode()), {alias: CANARY}), (expected,))

    def test_attribution_index_has_exactly_the_original_matcher_bytes_and_spans_chunks(self):
        environment = {"CI_SECRET_" + n: CANARY + "-" + n for n in guard.SECRET_NAMES}
        environment.update(GH_TOKEN="invented-legacy-token", REGISTRY_USERNAME="invented-user",
                           REGISTRY_PASSWORD=CANARY, CI_SECRET_ADDITIONAL_INPUT="invented-additional-token")
        self.assertEqual(set(guard.attributed_patterns(environment)), set(guard.patterns(environment)))
        stream = io.BytesIO(b"x" * (1024 * 1024 - 4) + CANARY.encode())
        self.assertEqual(guard.matching_sources(stream, {"CI_SECRET_AH_TOKEN": CANARY}), ("AH_TOKEN",))

    def test_capture_attributes_encoded_credentials_on_success_and_failure(self):
        encoded = base64.b64encode(CANARY.encode()).decode()
        for destination in ("stdout", "stderr"):
            for code in (0, 19):
                with self.subTest(stream=destination, exit_code=code), patch.dict(os.environ, CI_SECRET_AH_TOKEN=CANARY):
                    script = "import sys; sys." + destination + ".write(" + repr(encoded) + "); sys.exit(" + str(code) + ")"
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        with self.assertRaises(guard.GuardError) as caught:
                            guard.captured([sys.executable, "-c", script])
                    self.assertEqual(caught.exception.category, "credential-detected")
                    self.assertEqual(guard.error_metadata(caught.exception),
                                     {"sources": ("AH_TOKEN",), "stream": destination,
                                      "command_status": "failure" if code else "success"})
                    self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
                    self.assertNotIn(CANARY, str(caught.exception))
                    self.assertNotIn(encoded, str(caught.exception))

    def test_attribution_metadata_rejects_arbitrary_text_and_is_revalidated(self):
        error = guard.GuardError("fixed", category="credential-detected", sources=(CANARY, "AH_TOKEN"),
                                 stream=CANARY, command_status=CANARY)
        self.assertEqual(guard.error_metadata(error), {"sources": ("AH_TOKEN",), "stream": "unknown", "command_status": "unknown"})
        error.sources = (CANARY, "REDHAT_SA_USERNAME")
        error.stream, error.command_status = CANARY, CANARY
        self.assertEqual(guard.error_metadata(error),
                         {"sources": ("REDHAT_SA_USERNAME",), "stream": "unknown", "command_status": "unknown"})
        self.assertEqual(guard.safe_sources(CANARY), ())

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


class PublicationReceiptTests(unittest.TestCase):
    identity = "sha256:" + "c" * 64
    manifest = "sha256:" + "d" * 64
    destination = "quay.io/public-team/dcm-base-ee:qualification"

    def test_push_uses_inspected_identity_and_retains_checked_private_receipt(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            auth = root / "ee-auth-test.json"
            env = {"RUNNER_TEMP": name, "REGISTRY_AUTH_FILE": str(auth), "CI_SECRET_AH_TOKEN": CANARY}
            calls = []

            def captured(argv, **kwargs):
                calls.append((argv, kwargs))
                Path(argv[argv.index("--digestfile") + 1]).write_text(self.manifest + "\n")
                return b""

            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(guard, "inspect_image", return_value=self.identity) as inspect, \
                    patch.object(guard, "captured", side_effect=captured), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                record = guard.push_image("mutable:tag", self.destination)
            inspect.assert_called_once_with("mutable:tag")
            receipts = list(root.glob("ee-publication-*/receipt.json"))
            self.assertEqual(len(receipts), 1)
            receipt = receipts[0]
            self.assertEqual(record, {"schema_version": 1, "image_id": self.identity,
                                      "destination": self.destination, "manifest_digest": self.manifest})
            self.assertEqual(json.loads(receipt.read_text()), record)
            self.assertEqual(receipt.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual((receipt.parent / "manifest.digest").stat().st_mode & 0o777, 0o600)
            self.assertEqual(calls, [(["podman", "push", "--authfile", str(auth), "--digestfile",
                                      str(receipt.parent / "manifest.digest"), self.identity,
                                      "docker://" + self.destination], {"capture_result": False})])
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
            self.assertNotIn(CANARY, receipt.read_text())

    def test_missing_invalid_and_unsafe_digest_evidence_never_creates_receipt(self):
        cases = (None, b"", b"not-a-digest", b"sha256:" + b"D" * 64, b"x" * 129, "symlink", "hardlink")
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as name:
                root = Path(name)

                def captured(argv, **kwargs):
                    path = Path(argv[argv.index("--digestfile") + 1])
                    if payload in ("symlink", "hardlink"):
                        target = root / "target"
                        target.write_text(self.manifest)
                        if payload == "symlink": path.symlink_to(target)
                        else: os.link(target, path)
                    elif payload is not None:
                        path.write_bytes(payload)
                    return b""

                with patch.dict(os.environ, {"RUNNER_TEMP": name,
                                             "REGISTRY_AUTH_FILE": str(root / "ee-auth-test.json")}, clear=True), \
                        patch.object(guard, "inspect_image", return_value=self.identity), \
                        patch.object(guard, "captured", side_effect=captured):
                    with self.assertRaises((ValueError, OSError)):
                        guard.push_image("local:tag", self.destination)
                self.assertEqual(list(root.glob("ee-publication-*/receipt.json")), [])

    def test_failed_later_push_preserves_only_earlier_success_receipt(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)

            def captured(argv, **kwargs):
                Path(argv[argv.index("--digestfile") + 1]).write_text(self.manifest)
                return b""

            with patch.dict(os.environ, {"RUNNER_TEMP": name,
                                         "REGISTRY_AUTH_FILE": str(root / "ee-auth-test.json")}, clear=True), \
                    patch.object(guard, "inspect_image", return_value=self.identity), \
                    patch.object(guard, "captured", side_effect=captured) as push:
                guard.push_image("local:tag", self.destination)
                earlier = next(root.glob("ee-publication-*/receipt.json"))
                content = earlier.read_bytes()
                push.side_effect = guard.GuardError("Protected command failed")
                with self.assertRaises(guard.GuardError):
                    guard.push_image("local:tag", self.destination + "-later")
            self.assertEqual(list(root.glob("ee-publication-*/receipt.json")), [earlier])
            self.assertEqual(earlier.read_bytes(), content)

    def test_credential_matches_in_digest_or_receipt_prevent_success_receipt(self):
        for secret in (self.manifest, "schema_version"):
            with self.subTest(secret=secret), tempfile.TemporaryDirectory() as name:
                root = Path(name)

                def captured(argv, **kwargs):
                    Path(argv[argv.index("--digestfile") + 1]).write_text(self.manifest)
                    return b""

                with patch.dict(os.environ, {"RUNNER_TEMP": name, "CI_SECRET_AH_TOKEN": secret,
                                             "REGISTRY_AUTH_FILE": str(root / "ee-auth-test.json")}, clear=True), \
                        patch.object(guard, "inspect_image", return_value=self.identity), \
                        patch.object(guard, "captured", side_effect=captured):
                    with self.assertRaises(guard.GuardError):
                        guard.push_image("local:tag", self.destination)
                self.assertEqual(list(root.glob("ee-publication-*/receipt.json")), [])

    def test_invalid_destination_image_or_credential_match_prevents_push(self):
        for destination, identity, secret in (("quay.io/team/image:tag;id", self.identity, CANARY),
                                               (self.destination, "invalid", CANARY),
                                               (self.destination, self.identity, "public-team")):
            with self.subTest(destination=destination, identity=identity), tempfile.TemporaryDirectory() as name:
                with patch.dict(os.environ, {"RUNNER_TEMP": name, "CI_SECRET_AH_TOKEN": secret,
                                             "REGISTRY_AUTH_FILE": str(Path(name) / "ee-auth-test.json")}, clear=True), \
                        patch.object(guard, "inspect_image", return_value=identity), \
                        patch.object(guard, "captured") as push:
                    with self.assertRaises(guard.GuardError): guard.push_image("local:tag", destination)
                    push.assert_not_called()

    def test_cli_push_failure_never_exposes_exception_or_receipt(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["ci_secrets.py", "push", "--image", "local:tag",
                                        "--destination", self.destination]), \
                patch.dict(os.environ, {"CI_SECRET_AH_TOKEN": CANARY}, clear=True), \
                patch.object(guard, "push_image", side_effect=OSError(CANARY)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught: guard.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "ERROR: credential protection denied operation\n")


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
