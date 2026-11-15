"""Offline contract tests: no mirror, registry, container build or publication."""

import contextlib
import base64
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
SPEC = importlib.util.spec_from_file_location("ocp_ee", ROOT / ".github/scripts/ocp_ee.py")
EE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EE)
COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64
BASE = "quay.io/s4v0/dcm-base-ee@" + DIGEST
IMAGE_ID = "c" * 64
PAYLOAD = "quay.io/openshift-release-dev/ocp-release@sha256:" + "d" * 64


def installer(version="4.22.22"):
    return f"openshift-install {version}\nrelease image {PAYLOAD}\nrelease architecture amd64\n"


class Mirror:
    def __init__(self, bad_archive=False, version="4.22.22"):
        self.urls = []
        self.bad_archive = bad_archive
        self.version = version
        self.archives = {name: (name + " fixture bytes").encode() for name in EE.ARCHIVES}
        self.manifest = "".join(f"{hashlib.sha256(value).hexdigest()}  {name}\n"
                                for name, value in self.archives.items())

    def __call__(self, url, timeout):
        self.urls.append(url)
        if url.endswith("/release.txt"):
            data = f"Name: {self.version}\nArchitecture: amd64\n".encode()
        elif url.endswith("/sha256sum.txt"):
            data = self.manifest.encode()
        else:
            data = self.archives[url.rsplit("/", 1)[1]]
            if self.bad_archive:
                data += b"corrupt"
        return contextlib.closing(io.BytesIO(data))


class Runner:
    def __init__(self, version="4.22.22", client="4.22.22", expiry=False, image_id=IMAGE_ID):
        self.commands = []
        self.version, self.client, self.expiry = version, client, expiry
        self.image_id = image_id

    def __call__(self, argv, cwd=None):
        self.commands.append(argv)
        if argv == ["git", "rev-parse", "HEAD"]:
            return COMMIT + "\n"
        if argv[0] == "ansible-builder" or argv[:2] == ["podman", "pull"]:
            return ""
        if "{{json .RepoDigests}}" in argv:
            return json.dumps([BASE])
        if "{{.ID}}" in argv:
            return self.image_id + "\n"
        if "{{json .Config.Labels}}" in argv:
            labels = {"ocp.version": self.version}
            if self.expiry:
                labels["quay.expires-after"] = "45d"
            return json.dumps(labels)
        if argv[-2:] == ["openshift-install", "version"]:
            return installer(self.version)
        if argv[-3:] == ["oc", "version", "--client"]:
            return f"Client Version: {self.client}\nKustomize Version: v5.0.0\n"
        if argv[:2] == ["podman", "push"]:
            Path(argv[argv.index("--digestfile") + 1]).write_text(DIGEST)
            return ""
        raise AssertionError(f"Unexpected command: {argv}")


class VersionSelectionTests(unittest.TestCase):
    def test_matrix_validates_entire_input_before_builds(self):
        self.assertEqual(EE.release_matrix("4.22.14, 4.22.22"),
                         {"include": [{"release": "4.22.14", "stream": "4.22"},
                                      {"release": "4.22.22", "stream": "4.22"}]})
        for value in ("", "4.22.22,", "4.22.22,4.22.22", "stable-4.22", "4.21.22",
                      "4.22.022", "4.22.22;touch /tmp/file", "4.22.22\n4.22.14"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                EE.release_matrix(value)

    def test_configured_minor_and_major_streams_keep_patch_only_tags(self):
        versions = ("4.22.22", "4.22.14", "5.0.0")
        entries = EE.release_matrix(",".join(versions))["include"]
        self.assertEqual([entry["stream"] for entry in entries], ["4.22", "4.22", "5.0"])
        for version in versions:
            stream, patch = version.rsplit(".", 1)
            self.assertEqual(EE.image_name(version), "dcm-ocp-ee-" + stream)
            self.assertEqual(EE.image_tags(version, version, COMMIT, "123-1")[-1], patch)
            self.assertNotIn("latest", EE.image_tags(version, version, COMMIT, "123-1"))
            self.assertEqual(EE.image_tags(version, "stable-" + stream, COMMIT, "123-1")[-2:],
                             [patch, "latest"])
            self.assertEqual(len(EE.image_tags(version, "stable-" + stream, COMMIT, "123-1", "65")), 1)
            with self.assertRaises(ValueError):
                EE.image_tags(version, "stable-6.0", COMMIT, "123-1")
        for invalid in ("04.22.14", "5.00.0", "5.0.00", "5.0", "5.0.0-rc.1", "4.23.1", "6.0.0"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                EE.release_matrix(invalid)

    def test_automatic_streams_require_explicit_configuration(self):
        config = EE.configuration()
        self.assertEqual(set(config["streams"]), {"4.22", "5.0"})
        matrix = EE.automation_matrix()["include"]
        self.assertEqual([entry["ocp_version"] for entry in matrix if entry["versioned"]], ["4.22"])
        self.assertEqual([entry["ocp_version"] for entry in matrix if not entry["versioned"]],
                         config["legacy_streams"])
        config["streams"]["6.1"] = {"definition": "dcm-ocp-ee-4.22", "automatic": True}
        with patch.object(EE, "configuration", return_value=config):
            self.assertEqual(EE.image_name("6.1.2"), "dcm-ocp-ee-6.1")
            self.assertIn({"ocp_version": "6.1", "versioned": True}, EE.automation_matrix()["include"])

    def test_invalid_stream_configuration_fails_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "streams.json"
            for key, value in (("mirror_root", "http://example.test/ocp"),
                               ("definition", "../outside"), ("automatic", "true")):
                config = EE.configuration()
                if key == "mirror_root":
                    config[key] = value
                else:
                    config["streams"]["5.0"][key] = value
                path.write_text(json.dumps(config))
                mirror = Mirror(version="5.0.0")
                with patch.object(EE, "CONFIG", path), self.assertRaises(ValueError):
                    EE.resolve_release("stable-5.0", mirror)
                self.assertEqual(mirror.urls, [])

    def test_patch_tags_and_unique_build_identity(self):
        tags = EE.image_tags("4.22.14", "4.22.14", COMMIT, "123-1")
        self.assertEqual(tags, ["14-aaaaaaaaaaaa-123-1", "14"])
        self.assertNotIn("latest", tags)
        self.assertNotEqual(tags[0], EE.image_tags("4.22.22", "4.22.22", COMMIT, "123-1")[0])
        self.assertNotEqual(tags[0], EE.image_tags("4.22.14", "4.22.14", COMMIT, "123-2")[0])

    def test_stable_alone_updates_latest_and_pr_never_updates_release_alias(self):
        self.assertEqual(EE.image_tags("4.22.22", "stable-4.22", COMMIT, "123-1")[-2:], ["22", "latest"])
        self.assertEqual(EE.image_tags("4.22.22", "stable-4.22", COMMIT, "123-1", "65"),
                         ["22-pr-65-aaaaaaaaaaaa-123-1"])
        with self.assertRaises(ValueError):
            EE.image_tags("4.22.22", "4.22.14", COMMIT, "123-1")

    def test_stable_resolves_once_then_downloads_only_exact_paths(self):
        mirror = Mirror()
        self.assertEqual(EE.resolve_release("stable-4.22", mirror), "4.22.22")
        with tempfile.TemporaryDirectory() as directory:
            EE.stage_archives("4.22.22", Path(directory) / "release", mirror)
        self.assertEqual(sum("stable-4.22" in url for url in mirror.urls), 1)
        self.assertTrue(all("/4.22.22/" in url for url in mirror.urls[1:]))

    def test_new_stream_stable_resolution_keeps_configured_mirror_prefix(self):
        for version in ("4.22.14", "5.0.0"):
            stream = version.rsplit(".", 1)[0]
            mirror = Mirror(version=version)
            self.assertEqual(EE.resolve_release("stable-" + stream, mirror), version)
            with tempfile.TemporaryDirectory() as directory:
                EE.stage_archives(version, Path(directory) / "release", mirror)
            root = EE.configuration()["mirror_root"]
            self.assertEqual(mirror.urls[0], root + "/stable-" + stream + "/release.txt")
            self.assertTrue(all(url.startswith(root + "/" + version + "/") for url in mirror.urls[1:]))
            self.assertIn("/openshift-v4/", mirror.urls[0])
        with self.assertRaisesRegex(ValueError, "differs from requested stream"):
            EE.resolve_release("stable-5.0", Mirror(version="4.22.14"))
        mirror = Mirror()
        with self.assertRaises(ValueError):
            EE.resolve_release("stable-4.23", mirror)
        self.assertEqual(mirror.urls, [])

    def test_missing_or_duplicate_archive_checksums_are_rejected(self):
        manifest = Mirror().manifest
        for content in (manifest.splitlines()[0], manifest + manifest):
            with self.assertRaises(ValueError):
                EE.checksums(content)

    def test_bad_checksum_stops_before_builder_or_base_pull(self):
        runner = Runner()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "build"
            with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                EE.build("4.22.22", BASE, COMMIT, "123-1", output, run=runner, opener=Mirror(True))
            self.assertFalse((output / "receipt.json").exists())
        self.assertEqual(runner.commands, [["git", "rev-parse", "HEAD"]])

    def test_base_digest_must_match_requested_repository(self):
        with self.assertRaises(ValueError):
            EE.pinned_base("quay.io/other/dcm-base-ee:latest", Runner())
        with self.assertRaises(ValueError):
            EE.pinned_base("quay.io/s4v0/dcm-base-ee@sha256:" + "e" * 64, Runner())

    def test_both_final_tools_and_release_architecture_are_checked(self):
        for tool, client in ((installer("4.22.14"), "Client Version: 4.22.22\n"),
                             (installer(), "Client Version: 4.22.14\n"),
                             (installer().replace("amd64", "arm64"), "Client Version: 4.22.22\n"),
                             (installer().replace(PAYLOAD, "mutable:latest"), "Client Version: 4.22.22\n")):
            with self.assertRaises(ValueError):
                EE.verify_tools("4.22.22", tool, client)


class RegistryLoginTests(unittest.TestCase):
    def test_cli_login_suppresses_synthetic_secrets_on_success_and_failure(self):
        username, password = "synthetic-login-user", "synthetic-login-password:/+$"
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        messages = (
            f"username={username} password={password}",
            f"Authorization: Basic {encoded}\nAuthorization: Bearer {password}",
            f"https://{username}:{password}@registry.invalid/\n::error::{encoded}",
            json.dumps({"auth": encoded, "identitytoken": password}),
            f"{username}\n{password}\n{encoded}\r\n\x1b[31m{password}\x1b[0m",
        )
        for registry in ("quay.io", "ghcr.io", "registry.redhat.io"):
            for success in (True, False):
                for index, message in enumerate(messages):
                    with self.subTest(registry=registry, success=success, payload=index):
                        stdout, stderr = io.StringIO(), io.StringIO()

                        def run(argv, **kwargs):
                            self.assertEqual(argv, ["podman", "login", "--username", username,
                                                    "--password-stdin", registry])
                            self.assertNotIn(password, argv)
                            self.assertEqual(kwargs["input"], password + "\n")
                            self.assertTrue(kwargs["capture_output"])
                            self.assertTrue(kwargs["check"])
                            self.assertEqual(kwargs["timeout"], 120)
                            self.assertNotIn("REGISTRY_USERNAME", kwargs["env"])
                            self.assertNotIn("REGISTRY_PASSWORD", kwargs["env"])
                            if not success:
                                raise subprocess.CalledProcessError(125, argv, output=message, stderr=message)
                            return subprocess.CompletedProcess(argv, 0, stdout=message, stderr=message)

                        with patch.dict(os.environ, REGISTRY_USERNAME=username, REGISTRY_PASSWORD=password), \
                                patch("sys.argv", ["ocp_ee.py", "registry-login", "--registry", registry]), \
                                patch.object(EE.subprocess, "run", side_effect=run), \
                                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                            if success:
                                EE.main()
                            else:
                                with self.assertRaises(SystemExit) as error:
                                    EE.main()
                                self.assertEqual(error.exception.code, 1)
                        if success:
                            self.assertEqual(stdout.getvalue(), f"Registry login succeeded for {registry}\n")
                            self.assertEqual(stderr.getvalue(), "")
                        else:
                            self.assertEqual(stdout.getvalue(), "")
                            self.assertEqual(stderr.getvalue(),
                                             f"ERROR: OCP EE registry-login failed: Registry login failed for {registry}; "
                                             "command output suppressed; preserve build evidence.\n")
                        for secret in (username, password, encoded):
                            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())

    def test_login_runtime_exceptions_do_not_expose_credentials(self):
        secret = "synthetic-exception-secret"
        errors = (OSError(secret), ValueError(secret),
                  subprocess.TimeoutExpired(["podman", "login", secret], 120, output=secret, stderr=secret))
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                    patch.dict(os.environ, REGISTRY_USERNAME="synthetic-user", REGISTRY_PASSWORD=secret), \
                    patch.object(EE.subprocess, "run", side_effect=error), \
                    self.assertRaises(ValueError) as failure:
                EE.registry_login("quay.io")
            self.assertEqual(str(failure.exception),
                             "Registry login failed for quay.io; command output suppressed")
            self.assertIsNone(failure.exception.__cause__)
            self.assertTrue(failure.exception.__suppress_context__)

    def test_missing_credentials_and_unapproved_registries_fail_before_podman(self):
        for username, password in (("", ""), ("user", ""), ("", "password")):
            with self.subTest(username_present=bool(username), password_present=bool(password)), \
                    patch.dict(os.environ, REGISTRY_USERNAME=username, REGISTRY_PASSWORD=password), \
                    patch.object(EE.subprocess, "run") as run, \
                    self.assertRaisesRegex(ValueError, "Registry login credentials missing"):
                EE.registry_login("quay.io")
            run.assert_not_called()
        with patch.object(EE.subprocess, "run") as run, \
                self.assertRaisesRegex(ValueError, "Unsupported registry login target"):
            username, password = "user", "password"
            EE.registry_login(f"https://{username}:{password}@unknown.invalid")
        run.assert_not_called()


class PublicOutputTests(unittest.TestCase):
    def test_build_and_publish_failures_suppress_untrusted_exception_text(self):
        secret = "synthetic-build-publish-secret:/+$"
        encoded = base64.b64encode(secret.encode()).decode()
        payloads = (secret, f"Authorization: Bearer {secret}",
                    json.dumps({"auth": encoded}), f"::error::{secret}\n\x1b[31m{encoded}\x1b[0m")
        for command in ("build", "publish"):
            arguments = (["--release", "4.22.22", "--base", BASE, "--commit", COMMIT,
                          "--run-id", "123-1", "--output", "/unused/build"] if command == "build"
                         else ["--receipt", "/unused/receipt.json", "--registry", "ghcr.io/heatmiser"])
            for payload in payloads:
                errors = (ValueError(payload), OSError(payload), KeyError(payload),
                          UnicodeDecodeError("utf-8", payload.encode(), 0, 1, payload),
                          subprocess.CalledProcessError(125, ["podman", payload], output=payload, stderr=payload),
                          subprocess.TimeoutExpired(["podman", payload], 1, output=payload, stderr=payload))
                for error in errors:
                    with self.subTest(command=command, error=type(error).__name__, payload=payloads.index(payload)):
                        stdout, stderr, summary = io.StringIO(), io.StringIO(), io.StringIO()
                        with patch("sys.argv", ["ocp_ee.py", command, *arguments]), \
                                patch.object(EE, command, side_effect=error), \
                                patch.object(EE.Path, "open", return_value=summary) as opened, \
                                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                                self.assertRaises(SystemExit) as failure:
                            EE.main()
                        self.assertEqual(failure.exception.code, 1)
                        self.assertEqual(stdout.getvalue(), "")
                        self.assertEqual(stderr.getvalue(), f"ERROR: OCP EE {command} failed: "
                                         f"{type(error).__name__}; preserve build evidence.\n")
                        opened.assert_not_called()
                        self.assertEqual(summary.getvalue(), "")
                        self.assertNotIn(secret, stderr.getvalue())
                        self.assertNotIn(encoded, stderr.getvalue())

    def test_raw_builder_output_stays_local_on_success_failure_and_timeout(self):
        secret = "synthetic-builder-output-secret"
        for outcome in ("success", "failure", "timeout"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source"
                source.mkdir()
                stdout, stderr = io.StringIO(), io.StringIO()

                def run(argv, **kwargs):
                    self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
                    self.assertTrue(kwargs["check"])
                    self.assertEqual(kwargs["timeout"], 3600)
                    kwargs["stdout"].write(f"stdout={secret}\nstderr={secret}\n")
                    if outcome == "failure":
                        raise subprocess.CalledProcessError(1, argv, output=secret, stderr=secret)
                    if outcome == "timeout":
                        raise subprocess.TimeoutExpired(argv, 3600, output=secret, stderr=secret)
                    return subprocess.CompletedProcess(argv, 0)

                with patch.object(EE.subprocess, "run", side_effect=run), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    if outcome == "success":
                        self.assertEqual(EE.execute(["ansible-builder", "build"], cwd=source), "")
                    else:
                        with self.assertRaises(subprocess.SubprocessError):
                            EE.execute(["ansible-builder", "build"], cwd=source)
                self.assertIn(secret, (Path(directory) / "build.log").read_text())
                self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_raw_podman_output_is_captured_on_success_and_failure(self):
        secret = "synthetic-push-output-secret"
        for success in (True, False):
            def run(argv, **kwargs):
                self.assertTrue(kwargs["capture_output"])
                self.assertTrue(kwargs["check"])
                if not success:
                    raise subprocess.CalledProcessError(125, argv, output=secret, stderr=secret)
                return subprocess.CompletedProcess(argv, 0, stdout=secret, stderr=secret)

            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(success=success), patch.object(EE.subprocess, "run", side_effect=run), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                if success:
                    self.assertEqual(EE.execute(["podman", "push"]), secret)
                else:
                    with self.assertRaises(subprocess.CalledProcessError):
                        EE.execute(["podman", "push"])
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")


class SubprocessDiagnosticTests(unittest.TestCase):
    def test_build_and_publish_cli_identify_each_failed_operation_without_secrets(self):
        secret = "synthetic-stage-password:/+$"
        encoded = base64.b64encode(secret.encode()).decode()
        payloads = (f"Authorization: Bearer {secret}",
                    json.dumps({"auth": encoded}),
                    f"https://synthetic-user:{secret}@registry.invalid/?token={encoded}",
                    f"::error::{secret}\r\n\x1b[31m{encoded}\x1b[0m")
        stages = (
            ("build", "source revision", lambda argv: argv[0] == "git"),
            ("build", "EE build", lambda argv: argv[0] == "ansible-builder"),
            ("build", "built image inspection", lambda argv: "{{.ID}}" in argv),
            ("build", "installer verification", lambda argv: "openshift-install" in argv),
            ("build", "client verification", lambda argv: "oc" in argv),
            ("build", "image label inspection", lambda argv: "{{json .Config.Labels}}" in argv),
            ("publish", "publication installer verification", lambda argv: "openshift-install" in argv),
            ("publish", "publication client verification", lambda argv: "oc" in argv),
            ("publish", "image push", lambda argv: argv[:2] == ["podman", "push"]),
        )
        build, publish = EE.build, EE.publish
        for command, operation, matches in stages:
            for prefix, category in (("unauthorized: ", "Registry authentication failed"),
                                     ("opaque failure: ", "Unrecognized error")):
                for payload in payloads:
                    with self.subTest(operation=operation, category=category, payload=payloads.index(payload)), \
                            tempfile.TemporaryDirectory() as directory:
                        output = Path(directory) / "build"
                        receipt = output / "receipt.json"
                        if command == "publish":
                            build("4.22.22", BASE, COMMIT, "123-1", output, pr="64",
                                  run=Runner(), opener=Mirror())
                        runner = Runner()

                        def run(argv, cwd=None):
                            if matches(argv):
                                raise subprocess.CalledProcessError(125, [*argv, payload],
                                    output=payload, stderr=prefix + payload)
                            return runner(argv, cwd)

                        action = (lambda *args: build(*args, run=run, opener=Mirror())) if command == "build" \
                            else (lambda *args: publish(*args, run=run))
                        arguments = (["--release", "4.22.22", "--base", BASE, "--commit", COMMIT,
                                      "--run-id", "123-1", "--output", str(output), "--pr", "64"]
                                     if command == "build" else
                                     ["--receipt", str(receipt), "--registry", "ghcr.io/heatmiser"])
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with patch("sys.argv", ["ocp_ee.py", command, *arguments]), \
                                patch.object(EE, command, side_effect=action), \
                                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                                self.assertRaises(SystemExit) as failure:
                            EE.main()
                        self.assertEqual(failure.exception.code, 1)
                        self.assertEqual(stdout.getvalue(), "")
                        self.assertEqual(stderr.getvalue(),
                            f"ERROR: OCP EE {command} failed: {operation}: failed (exit code 125); "
                            f"{category}; original command output redacted; preserve build evidence.\n")
                        for private in (secret, encoded, payload, str(output), "synthetic-user"):
                            self.assertNotIn(private, stderr.getvalue())
                        if command == "build":
                            self.assertFalse(receipt.exists())
                        else:
                            self.assertNotIn("published_tags", json.loads(receipt.read_text()))
                        self.assertFalse(any(argv[:2] == ["podman", "push"] for argv in runner.commands))

    def test_real_execute_classifies_private_builder_log_tail_without_emitting_it(self):
        secret = "synthetic-builder-log-credential"
        encoded = base64.b64encode(secret.encode()).decode()
        for tail, category in (("no space left on device", "Storage space exhausted"),
                               ("opaque build failure", "Unrecognized error")):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source"
                source.mkdir()
                # An earlier transient message outside the bounded tail must not
                # determine the final failure category.
                payload = f"unauthorized\n{secret}\n" + "x" * 70000 + f"\n{tail}\n{encoded}"

                def run(argv, **kwargs):
                    kwargs["stdout"].write(payload)
                    raise subprocess.CalledProcessError(2, [*argv, secret])

                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(EE.subprocess, "run", side_effect=run), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                        self.assertRaises(EE.PublicError) as failure:
                    EE.checked_operation("EE build", ["ansible-builder", "build"], cwd=source)
                self.assertEqual(str(failure.exception),
                    f"EE build: failed (exit code 2); {category}; original command output redacted")
                self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
                self.assertEqual((Path(directory) / "build.log").read_text(), payload)
                self.assertNotIn(secret, str(failure.exception))
                self.assertNotIn(encoded, str(failure.exception))
                self.assertTrue(failure.exception.__suppress_context__)

    def test_missing_log_bytes_and_absent_output_remain_bounded(self):
        secret = b"synthetic-binary-credential"
        for output, diagnostic, expected in (
            (None, None, "No command output captured"),
            (b"\xff" + secret, b"x509: " + secret, "TLS or certificate failure; original command output redacted"),
            ("unauthorized" + "x" * 70000, secret, "Unrecognized error; original command output redacted"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                error = subprocess.CalledProcessError(-9, ["synthetic-private-argv"],
                                                     output=output, stderr=diagnostic)
                with self.assertRaises(EE.PublicError) as failure:
                    EE.checked_operation("EE build", ["ansible-builder", "build"],
                        run=unittest.mock.Mock(side_effect=error), cwd=Path(directory) / "source")
                self.assertEqual(str(failure.exception), f"EE build: failed (exit code -9); {expected}")

    def test_timeout_os_and_other_subprocess_errors_have_fixed_cli_messages(self):
        secret = "synthetic-exception-credential"
        errors = (
            (subprocess.TimeoutExpired([secret], 3600, output=secret, stderr=secret),
             "timed out; Operation timed out"),
            (FileNotFoundError(2, secret, secret), "failed (OS error code 2); Subprocess could not execute"),
            (OSError(secret), "failed (OS error code unavailable); Subprocess could not execute"),
            (subprocess.SubprocessError(secret), "failed; Unrecognized subprocess error"),
            (subprocess.CalledProcessError(secret, [secret], output=secret, stderr=secret),
             "failed (exit code unavailable); Unrecognized error; original command output redacted"),
        )
        for error, expected in errors:
            with self.subTest(error=type(error).__name__):
                def action(*args):
                    return EE.checked_operation("EE build", [secret],
                        run=unittest.mock.Mock(side_effect=error))

                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("sys.argv", ["ocp_ee.py", "build", "--release", "4.22.22", "--base", BASE,
                                        "--commit", COMMIT, "--run-id", "123-1", "--output", "/unused"]), \
                        patch.object(EE, "build", side_effect=action), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                        self.assertRaises(SystemExit):
                    EE.main()
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(),
                    f"ERROR: OCP EE build failed: EE build: {expected}; preserve build evidence.\n")
                self.assertNotIn(secret, stderr.getvalue())

    def test_unapproved_operation_labels_cannot_be_echoed_or_run(self):
        runner = unittest.mock.Mock()
        with self.assertRaisesRegex(EE.PublicError, "^Unsupported subprocess operation$"):
            EE.checked_operation("synthetic-secret-label", ["synthetic-private-argv"], run=runner)
        runner.assert_not_called()


class PRPreparationTests(unittest.TestCase):
    def test_base_failures_reach_cli_with_operation_exit_code_and_safe_reason(self):
        for operation, code, diagnostic, expected in (
            ("pull", 125, "Error: registry access denied\n::warning::registry detail", "Access denied"),
            ("inspect", 1, "Error: image not known", "Local image not found"),
            ("inspect", 125, None, "No stderr captured"),
        ):
            with self.subTest(operation=operation, stderr=diagnostic):
                commands = []

                def run(argv, **kwargs):
                    commands.append(argv)
                    if argv[1] == operation or operation == "inspect" and argv[1:3] == ["image", "inspect"]:
                        raise subprocess.CalledProcessError(code, argv, output="private stdout",
                                                            stderr=diagnostic)
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("sys.argv", ["ocp_ee.py", "prepare-pr"]), \
                        patch.object(EE.subprocess, "run", side_effect=run), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                        self.assertRaises(SystemExit) as failure:
                    EE.main()
                self.assertEqual(failure.exception.code, 1)
                self.assertEqual(stdout.getvalue(), "")
                message = stderr.getvalue()
                self.assertIn(f"Base image {operation} failed (exit code {code})", message)
                self.assertIn("quay.io/s4v0/dcm-base-ee:latest", message)
                self.assertIn(f"\nPodman: {expected}", message)
                if diagnostic:
                    self.assertIn("original stderr redacted", message)
                self.assertNotIn("\n::warning::", message)
                self.assertNotIn("private stdout", message)
                self.assertEqual(len(commands), 1 if operation == "pull" else 2)

    def test_base_diagnostic_classifies_only_fixed_reasons(self):
        cases = (
            ("unauthorized: authentication required", "Registry authentication failed"),
            ("permission denied", "Access denied"),
            ("manifest unknown", "Image or manifest not found"),
            ("no such image", "Local image not found"),
            ("x509: certificate signed by unknown authority", "TLS or certificate failure"),
            ("lookup quay.io: no such host", "DNS resolution failed"),
            ("context deadline exceeded", "Operation timed out"),
            ("connection refused", "Network connection failed"),
            ("no space left on device", "Storage space exhausted"),
            ("toomanyrequests", "Registry rate limit reached"),
            ("opaque diagnostic", "Unrecognized error"),
        )
        for diagnostic, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(EE.base_diagnostic(diagnostic), reason + "; original stderr redacted")
        self.assertEqual(EE.base_diagnostic("  \n"), "No stderr captured")

    def test_cli_redacts_synthetic_credentials_in_pull_and_inspect_failures(self):
        username = "synthetic-robot-account"
        password = "synthetic-Pa$$word:/+42"
        token = "synthetic-opaque-token-0987654321"
        encoded_auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        payloads = (
            f"QUAY_USERNAME={username}\nQUAY_PASSWORD={password}\nGITHUB_TOKEN={token}",
            f"https://{username}:{password}@registry.invalid/token?access_token={token}",
            f"Authorization: Basic {encoded_auth}\nAuthorization: Bearer {token}",
            json.dumps({"auths": {"registry.invalid": {"auth": encoded_auth,
                        "username": username, "password": password, "identitytoken": token}}}),
            "https://registry.invalid/?password=synthetic-Pa%24%24word%3A%2F%2B42",
            f"{username}\n{password}\n{token}\n{encoded_auth}",
            f"::error::{token}\r\n\x1b[31m{password}\x1b[0m",
        )
        for operation in ("pull", "inspect"):
            for prefix in ("unauthorized: ", "unrecognized failure: "):
                for payload in payloads:
                    with self.subTest(operation=operation, known_reason=prefix.startswith("unauthorized"),
                                      payload_index=payloads.index(payload)):
                        def run(argv, **kwargs):
                            if argv[1] == "pull" and operation == "inspect":
                                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                            raise subprocess.CalledProcessError(125, argv, output=payload,
                                                                stderr=prefix + payload)

                        stdout, stderr = io.StringIO(), io.StringIO()
                        with patch("sys.argv", ["ocp_ee.py", "prepare-pr"]), \
                                patch.object(EE.subprocess, "run", side_effect=run), \
                                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                                self.assertRaises(SystemExit) as failure:
                            EE.main()
                        self.assertEqual(failure.exception.code, 1)
                        self.assertEqual(stdout.getvalue(), "")
                        reason = ("Registry authentication failed" if prefix.startswith("unauthorized")
                                  else "Unrecognized error")
                        self.assertEqual(stderr.getvalue(),
                                         f"ERROR: OCP EE prepare-pr failed: Base image {operation} failed "
                                         "(exit code 125) for quay.io/s4v0/dcm-base-ee:latest\n"
                                         f"Podman: {reason}; original stderr redacted; preserve build evidence.\n")
                        for secret in (username, password, token, encoded_auth,
                                       "synthetic-Pa%24%24word%3A%2F%2B42"):
                            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())

    def test_unrelated_command_failures_keep_captured_output_private(self):
        error = subprocess.CalledProcessError(125, ["podman", "push"],
                                              output="private stdout", stderr="private stderr")
        stderr = io.StringIO()
        with patch("sys.argv", ["ocp_ee.py", "prepare-pr"]), \
                patch.object(EE, "prepare_pr", side_effect=error), \
                contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            EE.main()
        self.assertIn("CalledProcessError", stderr.getvalue())
        self.assertNotIn("private", stderr.getvalue())

    def test_preparation_pins_existing_base_once_without_building(self):
        runner = Runner()
        plan = EE.prepare_pr(runner)
        self.assertEqual(plan, {"base": BASE, "matrix": {"include": [
            {"release": "4.22.22", "stream": "4.22"}]}})
        self.assertEqual([argv for argv in runner.commands if argv[:2] == ["podman", "pull"]],
                         [["podman", "pull", "--quiet", "quay.io/s4v0/dcm-base-ee:latest"]])
        self.assertFalse(any(argv[0] == "ansible-builder" or argv[:2] == ["podman", "push"]
                             for argv in runner.commands))

    def test_explicit_digest_and_multiple_releases_share_one_base(self):
        config = EE.configuration()
        config["pr_qualification"] = {"base_image": BASE, "versions": ["4.22.22", "5.0.0"]}
        runner = Runner()
        with patch.object(EE, "configuration", return_value=config):
            plan = EE.prepare_pr(runner)
        self.assertEqual(plan["base"], BASE)
        self.assertEqual([entry["release"] for entry in plan["matrix"]["include"]], ["4.22.22", "5.0.0"])
        self.assertEqual(sum(argv[:2] == ["podman", "pull"] for argv in runner.commands), 1)

    def test_invalid_qualification_settings_fail_before_pulling(self):
        settings = EE.configuration()["pr_qualification"]
        invalid = [None, {}, {**settings, "base_image": None},
                   {**settings, "base_image": "docker.io/unknown/base:latest"}]
        invalid += [{**settings, "versions": versions} for versions in (
            [], "4.22.22", [None], ["stable-4.22"], ["4.23.1"],
            ["4.22.22", "4.22.22"], ["4.22.22,5.0.0"]) ]
        for value in invalid:
            with self.subTest(settings=value):
                config = EE.configuration()
                config["pr_qualification"] = value
                runner = Runner()
                with patch.object(EE, "configuration", return_value=config), self.assertRaises(ValueError):
                    EE.prepare_pr(runner)
                self.assertEqual(runner.commands, [])


class BuildAndPublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "build"

    def build(self, runner=None, selection="4.22.22", pr=""):
        runner = runner or Runner()
        receipt = EE.build(selection, BASE, COMMIT, "123-1", self.output, pr, runner, Mirror())
        return receipt, runner

    def test_verified_build_records_inputs_and_passes_exact_args(self):
        receipt, runner = self.build()
        record = json.loads(receipt.read_text())
        self.assertEqual(record["release"], "4.22.22")
        self.assertEqual(record["base_image"], BASE)
        self.assertEqual(record["release_image"], PAYLOAD)
        self.assertEqual(set(record["archive_sha256"]), set(EE.ARCHIVES))
        build = next(argv for argv in runner.commands if argv[0] == "ansible-builder")
        self.assertIn("OCP_VERSION=4.22.22", build)
        self.assertIn("EE_BASE_IMAGE=" + BASE, build)
        self.assertIn("--extra-build-cli-args=--unsetlabel=quay.expires-after", build)
        self.assertFalse(any(argv[:2] == ["podman", "push"] for argv in runner.commands))
        with self.assertRaises(FileExistsError):
            self.build()

    def test_wrong_installer_client_or_retention_never_produces_receipt(self):
        for index, runner in enumerate((Runner(version="4.22.14"), Runner(client="4.22.14"), Runner(expiry=True))):
            output = Path(self.temporary.name) / str(index)
            with self.assertRaises(ValueError):
                EE.build("4.22.22", BASE, COMMIT, "123-1", output, run=runner, opener=Mirror())
            self.assertFalse((output / "receipt.json").exists())

    def test_receipt_writer_rejects_secret_bearing_or_unknown_fields(self):
        receipt, _ = self.build()
        original = json.loads(receipt.read_text())
        secret = "synthetic-receipt-secret"
        mutations = [(key, secret) for key in original]
        mutations += [("unexpected_stdout", secret), ("published_image", secret),
                      ("archive_sha256", {**original["archive_sha256"], "auth": secret}),
                      ("archive_sha256", {name: secret for name in EE.ARCHIVES}),
                      ("tags", original["tags"] + [secret]), ("schema_version", True)]
        for index, (key, value) in enumerate(mutations):
            with self.subTest(field=key, case=index):
                record = {**original, key: value}
                target = self.output / f"rejected-{index}.json"
                with self.assertRaises(ValueError):
                    EE.write_receipt(target, record)
                self.assertFalse(target.exists())

    def test_altered_receipt_metadata_is_rejected_before_publication(self):
        receipt, _ = self.build()
        original = json.loads(receipt.read_text())
        for key in ("archive_sha256", "release_image", "base_image", "verified_at", "extra_output"):
            with self.subTest(field=key):
                receipt.write_text(json.dumps({**original, key: "synthetic-untrusted-secret"}))
                runner = Runner()
                with self.assertRaises(ValueError):
                    EE.publish(receipt, "quay.io/s4v0", runner)
                self.assertEqual(runner.commands, [])

    def test_published_receipt_cannot_carry_extra_tags_or_text(self):
        receipt, runner = self.build()
        original = EE.publish(receipt, "quay.io/s4v0", runner)
        for key, value in (("published_image", "synthetic-published-secret"),
                           ("published_tags", original["published_tags"] + ["synthetic-tag-secret"]),
                           ("published_tags", [])):
            with self.subTest(field=key, value_type=type(value).__name__), self.assertRaises(ValueError):
                EE.write_receipt(self.output / "rejected-publication.json", {**original, key: value})

    def test_publication_pushes_unique_tag_then_patch_without_latest(self):
        receipt, runner = self.build()
        result = EE.publish(receipt, "quay.io/s4v0", runner)
        pushes = [argv for argv in runner.commands if argv[:2] == ["podman", "push"]]
        self.assertEqual([argv[-1].rsplit(":", 1)[1] for argv in pushes], ["22-aaaaaaaaaaaa-123-1", "22"])
        self.assertTrue(all(argv[-2] == IMAGE_ID for argv in pushes))
        self.assertEqual(result["published_image"], "quay.io/s4v0/" + EE.image_name("4.22.22") + "@" + DIGEST)

    def test_cross_stream_builds_and_publication_use_matching_repositories(self):
        for version in ("4.22.14", "5.0.0"):
            with self.subTest(version=version):
                runner = Runner(version=version, client=version)
                receipt = EE.build(version, BASE, COMMIT, "123-1",
                                   Path(self.temporary.name) / version, run=runner, opener=Mirror(version=version))
                record = json.loads(receipt.read_text())
                stream = version.rsplit(".", 1)[0]
                self.assertEqual(record["stream"], stream)
                self.assertEqual(record["definition"], EE.stream_settings(stream)["definition"])
                result = EE.publish(receipt, "quay.io/s4v0", runner)
                self.assertEqual(result["published_image"], f"quay.io/s4v0/dcm-ocp-ee-{stream}@{DIGEST}")
                pushes = [argv for argv in runner.commands if argv[:2] == ["podman", "push"]]
                self.assertTrue(all(f"/dcm-ocp-ee-{stream}:" in argv[-1] for argv in pushes))

    def test_definition_selection_is_independent_of_output_repository(self):
        workspace = Path(self.temporary.name) / "workspace"
        definition = "dcm-ocp-ee-custom"
        shutil.copytree(ROOT / "dcm-ocp-ee-4.22", workspace / definition)
        (workspace / definition / "selected-definition.txt").write_text("custom stream definition\n")
        config = EE.configuration()
        config["legacy_streams"] = []
        config["streams"] = {"5.0": {"definition": definition, "automatic": False}}
        config_path = workspace / "streams.json"
        config_path.write_text(json.dumps(config))
        with patch.object(EE, "ROOT", workspace), patch.object(EE, "CONFIG", config_path):
            receipt = EE.build("5.0.0", BASE, COMMIT, "123-1", self.output,
                               run=Runner(version="5.0.0", client="5.0.0"), opener=Mirror(version="5.0.0"))
        record = json.loads(receipt.read_text())
        self.assertEqual(record["definition"], definition)
        self.assertTrue(record["image"].startswith("localhost/dcm-ocp-ee-5.0:"))
        self.assertEqual((self.output / "source/selected-definition.txt").read_text(),
                         "custom stream definition\n")

    def test_receipt_cannot_cross_stream_or_repository_boundaries(self):
        receipt, _ = self.build()
        original = json.loads(receipt.read_text())
        for key, value in (("stream", "5.0"), ("image", "localhost/dcm-ocp-ee-5.0:0")):
            record = dict(original)
            record[key] = value
            receipt.write_text(json.dumps(record))
            runner = Runner()
            with self.assertRaises(ValueError):
                EE.publish(receipt, "quay.io/s4v0", runner)
            self.assertEqual(runner.commands, [])

    def test_build_and_publication_normalize_bare_and_prefixed_image_ids(self):
        for index, image_id in enumerate((IMAGE_ID, "sha256:" + IMAGE_ID)):
            with self.subTest(image_id=image_id):
                runner = Runner(image_id=image_id)
                receipt = EE.build("4.22.22", BASE, COMMIT, "123-1",
                                   Path(self.temporary.name) / str(index), run=runner, opener=Mirror())
                record = json.loads(receipt.read_text())
                self.assertEqual(record["image_id"], IMAGE_ID)
                # Exercise both receipt representations, including previously written receipts.
                record["image_id"] = image_id
                EE.write_receipt(receipt, record)
                result = EE.publish(receipt, "quay.io/s4v0", runner)
                self.assertEqual(result["image_id"], IMAGE_ID)
                for argv in runner.commands:
                    if argv[:2] == ["podman", "run"]:
                        self.assertEqual(argv[4], IMAGE_ID)
                    elif argv[:2] == ["podman", "push"]:
                        self.assertEqual(argv[-2], IMAGE_ID)

    def test_malformed_inspected_image_ids_stop_before_runtime_checks(self):
        for index, image_id in enumerate(("", "c" * 12, "g" * 64, "sha512:" + IMAGE_ID,
                                         "sha256:" + "c" * 63, "sha256:sha256:" + IMAGE_ID)):
            with self.subTest(image_id=image_id):
                runner = Runner(image_id=image_id)
                output = Path(self.temporary.name) / str(index)
                with self.assertRaisesRegex(ValueError, "Invalid local image ID"):
                    EE.build("4.22.22", BASE, COMMIT, "123-1", output, run=runner, opener=Mirror())
                self.assertFalse((output / "receipt.json").exists())
                self.assertFalse(any(argv[:2] == ["podman", "run"] for argv in runner.commands))

    def test_malformed_receipt_image_ids_stop_before_runtime_or_push(self):
        receipt, _ = self.build()
        record = json.loads(receipt.read_text())
        for image_id in (None, 123, "c" * 12, "sha256:" + "g" * 64, IMAGE_ID + " extra"):
            with self.subTest(image_id=image_id):
                record["image_id"] = image_id
                receipt.write_text(json.dumps(record))
                runner = Runner()
                with self.assertRaisesRegex(ValueError, "Invalid local image ID"):
                    EE.publish(receipt, "quay.io/s4v0", runner)
                self.assertEqual(runner.commands, [])

    def test_pr_publication_is_isolated_from_quay_and_release_tags(self):
        receipt, runner = self.build(pr="65", selection="stable-4.22")
        with self.assertRaises(ValueError):
            EE.publish(receipt, "quay.io/s4v0", runner)
        result = EE.publish(receipt, "ghcr.io/heatmiser", runner)
        self.assertEqual(len(result["published_tags"]), 1)
        self.assertTrue(result["published_tags"][0].endswith(":22-pr-65-aaaaaaaaaaaa-123-1"))

    def test_independent_pr_build_uses_prepared_digest_and_only_a_ghcr_pr_tag(self):
        runner = Runner()
        plan = EE.prepare_pr(runner)
        release = plan["matrix"]["include"][0]["release"]
        receipt = EE.build(release, plan["base"], COMMIT, "123-1", self.output,
                           pr="64", run=runner, opener=Mirror())
        build = next(argv for argv in runner.commands if argv[0] == "ansible-builder")
        self.assertIn("EE_BASE_IMAGE=" + BASE, build)
        pulls = [argv[-1] for argv in runner.commands if argv[:2] == ["podman", "pull"]]
        self.assertEqual(pulls, ["quay.io/s4v0/dcm-base-ee:latest", BASE])
        record = json.loads(receipt.read_text())
        self.assertEqual(record["base_image"], plan["base"])
        with self.assertRaises(ValueError):
            EE.publish(receipt, "quay.io/s4v0", runner)
        result = EE.publish(receipt, "ghcr.io/heatmiser", runner)
        self.assertEqual(result["published_tags"], [
            "ghcr.io/heatmiser/dcm-ocp-ee-4.22:22-pr-64-aaaaaaaaaaaa-123-1"])
        self.assertEqual(sum(argv[0] == "ansible-builder" for argv in runner.commands), 1)

    def test_wrong_version_at_publication_cannot_push(self):
        receipt, _ = self.build()
        runner = Runner(version="4.22.14")
        with self.assertRaises(ValueError):
            EE.publish(receipt, "quay.io/s4v0", runner)
        self.assertFalse(any(argv[:2] == ["podman", "push"] for argv in runner.commands))

    def test_failed_push_preserves_only_completed_publication_evidence(self):
        receipt, runner = self.build()
        def fail_alias(argv, cwd=None):
            if argv[:2] == ["podman", "push"] and argv[-1].endswith(":22"):
                raise subprocess.CalledProcessError(1, argv)
            return runner(argv, cwd)
        with self.assertRaisesRegex(EE.PublicError, "image push: failed \\(exit code 1\\)"):
            EE.publish(receipt, "quay.io/s4v0", fail_alias)
        self.assertEqual(len(json.loads(receipt.read_text())["published_tags"]), 1)

    def test_altered_tags_cannot_publish(self):
        receipt, runner = self.build()
        record = json.loads(receipt.read_text())
        record["tags"].append("latest")
        receipt.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            EE.publish(receipt, "quay.io/s4v0", runner)

    def test_generated_containerfile_with_ci_builder(self):
        if not shutil.which("ansible-builder"):
            if os.environ.get("REQUIRE_BUILDER") == "1":
                self.fail("CI requires the pinned ansible-builder")
            self.skipTest("ansible-builder not installed; CI requires this render check")
        for version in ("4.22.22", "4.22.14", "5.0.0"):
            with self.subTest(version=version):
                output = Path(self.temporary.name) / version
                EE.build(version, BASE, COMMIT, "123-1", output,
                         run=Runner(version=version, client=version), opener=Mirror(version=version))
                result = subprocess.run(["ansible-builder", "create", "--file", "execution-environment.yml",
                                         "--output-filename", "Containerfile",
                                         "--context", str(output / "rendered"), "--verbosity=3"],
                                        cwd=output / "source", capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                containerfile = (output / "rendered/Containerfile").read_text()
                self.assertIn("ARG OCP_VERSION", containerfile)
                self.assertIn("RUN --mount=type=bind,source=_build/ocp-release,target=/tmp/ocp-release,ro ", containerfile)
                self.assertNotIn("COPY _build/ocp-release", containerfile)
                self.assertNotIn("rm -rf /tmp/ocp-release", containerfile)
                self.assertIn("sha256sum -c SHA256SUMS && tar xzf openshift-client-linux.tar.gz", containerfile)
                self.assertIn('LABEL ocp.version="$OCP_VERSION"', containerfile)
                for name in (*EE.ARCHIVES, "SHA256SUMS"):
                    self.assertTrue((output / "rendered/_build/ocp-release" / name).is_file())


class WorkflowTests(unittest.TestCase):
    def test_openshift_artifacts_allow_only_receipts_and_plan_with_bounded_retention(self):
        expected = {
            "pr-ocp-ee-build.yml": ("7", {
                "${{ runner.temp }}/ocp-ee-pr-plan.json",
                "${{ runner.temp }}/ocp-ee-pr-${{ matrix.release }}/receipt.json",
            }),
            "pr-ee-build.yml": ("7", {
                "${{ runner.temp }}/ocp-ee-${{ matrix.ocp_version }}/receipt.json",
            }),
            "push-ee-build.yml": ("30", {
                "${{ runner.temp }}/ocp-ee-${{ matrix.ocp_version }}/receipt.json",
            }),
            "build-versioned-ocp-ee.yml": ("30", {
                "${{ runner.temp }}/ocp-ee-${{ matrix.release }}/receipt.json",
            }),
        }
        for filename, (retention, paths) in expected.items():
            with self.subTest(workflow=filename):
                workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
                uploads = [step["with"] for job in workflow["jobs"].values() for step in job.get("steps", [])
                           if step.get("uses", "").startswith("actions/upload-artifact@")
                           and step["with"]["name"].startswith("ocp-ee-")]
                self.assertEqual(len(uploads), len(paths))
                self.assertEqual({upload["path"] for upload in uploads}, paths)
                for upload in uploads:
                    self.assertEqual(upload["retention-days"], retention)
                    self.assertNotIn("include-hidden-files", upload)

    def test_qualification_build_uses_captured_logins_for_all_three_registries(self):
        workflow = yaml.load((ROOT / ".github/workflows/pr-ocp-ee-build.yml").read_text(), Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["build"]["steps"]
        logins = [step for step in steps if "registry-login" in step.get("run", "")]
        self.assertEqual(len(logins), 3)
        expected = {
            "quay.io": ("${{ secrets.QUAY_USERNAME }}", "${{ secrets.QUAY_PASSWORD }}"),
            "ghcr.io": ("${{ github.actor }}", "${{ secrets.GITHUB_TOKEN }}"),
            "registry.redhat.io": ("${{ secrets.REDHAT_SA_USERNAME }}", "${{ secrets.REDHAT_SA_PASSWORD }}"),
        }
        for step in logins:
            registry = step["run"].rsplit(" ", 1)[1]
            self.assertEqual(step["run"], f"python3 .github/scripts/ocp_ee.py registry-login --registry {registry}")
            self.assertEqual(step["env"], dict(zip(("REGISTRY_USERNAME", "REGISTRY_PASSWORD"), expected[registry])))
            self.assertNotIn("uses", step)
        self.assertEqual({step["run"].rsplit(" ", 1)[1] for step in logins}, set(expected))
        self.assertFalse(any(step.get("uses", "").startswith("redhat-actions/podman-login") for step in steps))

    def test_manual_workflow_uses_shared_digest_and_safe_input_binding(self):
        path = ROOT / ".github/workflows/build-versioned-ocp-ee.yml"
        workflow = yaml.safe_load(path.read_text())
        build = workflow["jobs"]["build"]
        self.assertEqual(build["needs"], "prepare")
        self.assertEqual(build["strategy"]["max-parallel"], 1)
        self.assertEqual(build["concurrency"]["group"], "ocp-ee-${{ matrix.stream }}-production")
        command = next(step for step in build["steps"] if step["name"] == "Build and verify selected release")
        self.assertEqual(command["env"]["OCP_BASE"], "${{ needs.prepare.outputs.base }}")
        self.assertNotIn("${{ inputs.", command["run"])

    def test_legacy_push_cannot_publish_versioned_streams(self):
        for filename in ("pr-ee-build.yml", "push-ee-build.yml"):
            workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
            steps = workflow["jobs"]["build-ocp-ees"]["steps"]
            for step in steps:
                if step["name"] in ("Build image", "Push To GHCR", "Push To quay.io"):
                    self.assertEqual(step["if"], "${{ !matrix.versioned }}")
            special = next(step for step in steps if step["name"] == "Build and verify configured stable OpenShift release")
            self.assertEqual(special["if"], "matrix.versioned")
            self.assertIn('--release "stable-$OCP_STREAM"', special["run"])
            self.assertEqual(special["env"]["OCP_STREAM"], "${{ matrix.ocp_version }}")
            self.assertEqual(workflow["jobs"]["build-ocp-ees"]["strategy"]["matrix"],
                             "${{ fromJson(needs.prepare-matrix.outputs.ocp_matrix) }}")
            prepare = workflow["jobs"]["prepare-matrix"]
            self.assertIn("ocp_matrix", prepare["outputs"])
            self.assertTrue(any("automation-matrix" in step.get("run", "") for step in prepare["steps"]))

    def test_stream_configuration_changes_trigger_validation(self):
        workflow = yaml.load((ROOT / ".github/workflows/test-ocp-ee.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertIn(".github/ocp-ee-streams.json", workflow["on"]["pull_request"]["paths"])

    def test_pr_qualification_is_independent_and_publishes_only_to_ghcr(self):
        path = ROOT / ".github/workflows/pr-ocp-ee-build.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["jobs"]), {"prepare", "build"})
        prepare, build = workflow["jobs"]["prepare"], workflow["jobs"]["build"]
        self.assertEqual(prepare["if"], "github.event.pull_request.head.repo.full_name == github.repository")
        self.assertNotIn("needs", prepare)
        self.assertEqual(build["needs"], "prepare")
        self.assertEqual(build["permissions"]["packages"], "write")
        self.assertEqual(build["strategy"]["matrix"], "${{ fromJson(needs.prepare.outputs.matrix) }}")
        self.assertTrue(any("prepare-pr" in step.get("run", "") for step in prepare["steps"]))
        for job in (prepare, build):
            self.assertEqual(job["environment"], "test")
            checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
            self.assertEqual(checkout["with"]["ref"], "${{ github.event.pull_request.head.sha }}")
        quay_login = next(step for step in prepare["steps"] if step["name"] == "Log in to Quay for the published base")
        self.assertEqual(quay_login["env"]["REGISTRY_USERNAME"], "${{ secrets.QUAY_USERNAME }}")
        self.assertEqual(quay_login["env"]["REGISTRY_PASSWORD"], "${{ secrets.QUAY_PASSWORD }}")
        ghcr_login = next(step for step in prepare["steps"] if step["name"] == "Log in to GHCR for published bases")
        self.assertEqual(ghcr_login["env"]["REGISTRY_USERNAME"], "${{ github.actor }}")
        self.assertEqual(ghcr_login["env"]["REGISTRY_PASSWORD"], "${{ secrets.GITHUB_TOKEN }}")
        for registry, step in (("quay.io", quay_login), ("ghcr.io", ghcr_login)):
            self.assertEqual(step["run"], f"python3 .github/scripts/ocp_ee.py registry-login --registry {registry}")
        self.assertFalse(any(step.get("uses", "").startswith("redhat-actions/podman-login")
                             for step in prepare["steps"]))
        command = next(step for step in build["steps"] if step["name"] == "Build and verify PR image against the pinned base")
        self.assertNotIn("OCP_BASE", command["env"])
        self.assertEqual(command["env"]["OCP_PLAN"], "${{ runner.temp }}/ocp-ee-plan/ocp-ee-pr-plan.json")
        self.assertEqual(command["env"]["SOURCE_COMMIT"], "${{ github.event.pull_request.head.sha }}")
        self.assertIn('--pr "$PR_NUMBER"', command["run"])
        publish = next(step for step in build["steps"] if step["name"] == "Publish verified PR image to GHCR")
        self.assertEqual(publish["env"]["OCP_REGISTRY"], "ghcr.io/${{ github.repository_owner }}")
        self.assertIn(".github/ocp-ee-streams.json", workflow["on"]["pull_request"]["paths"])
        validation = yaml.load((ROOT / ".github/workflows/test-ocp-ee.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertIn(".github/workflows/pr-ocp-ee-build.yml", validation["on"]["pull_request"]["paths"])

    def test_plan_handoff_uses_preparation_artifact_name_without_exporting_base(self):
        workflow = yaml.load((ROOT / ".github/workflows/pr-ocp-ee-build.yml").read_text(), Loader=yaml.BaseLoader)
        prepare, build = workflow["jobs"]["prepare"], workflow["jobs"]["build"]
        self.assertEqual(prepare["outputs"], {"matrix": "${{ steps.prepare.outputs.matrix }}",
                                              "plan_artifact": "${{ steps.prepare.outputs.plan_artifact }}"})
        upload = next(s for s in prepare["steps"] if s["name"] == "Preserve qualification plan")
        download = next(s for s in build["steps"] if s["name"] == "Download prepared qualification plan")
        self.assertEqual(upload["with"]["name"], "ocp-ee-pr-plan-${{ github.run_id }}-${{ github.run_attempt }}")
        self.assertEqual(download["uses"], "actions/download-artifact@v4")
        self.assertEqual(download["with"], {"name": "${{ needs.prepare.outputs.plan_artifact }}",
                                            "path": "${{ runner.temp }}/ocp-ee-plan"})
        command = next(s for s in prepare["steps"] if s.get("id") == "prepare")["run"]
        script = command.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            plan = {"base": BASE, "matrix": {"include": [{"release": "4.22.22", "stream": "4.22"}]}}
            (parent / "ocp-ee-pr-plan.json").write_text(json.dumps(plan))
            target = parent / "github-output"
            environment = dict(os.environ, RUNNER_TEMP=directory, GITHUB_OUTPUT=str(target),
                               GITHUB_RUN_ID="12345", GITHUB_RUN_ATTEMPT="1")
            result = subprocess.run([sys.executable, "-"], input=script, text=True,
                                    capture_output=True, env=environment, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout + result.stderr, "")
            output = dict(line.split("=", 1) for line in target.read_text().splitlines())
            self.assertEqual(json.loads(output["matrix"]), plan["matrix"])
            self.assertEqual(output["plan_artifact"], "ocp-ee-pr-plan-12345-1")
            self.assertNotIn(BASE, target.read_text())
            self.assertNotIn("base", output)
        names = [s["name"] for s in build["steps"]]
        self.assertLess(names.index("Download prepared qualification plan"),
                        names.index("Build and verify PR image against the pinned base"))

    def test_build_reads_and_validates_file_plan_with_base_job_output_absent(self):
        workflow = yaml.load((ROOT / ".github/workflows/pr-ocp-ee-build.yml").read_text(), Loader=yaml.BaseLoader)
        command = next(s for s in workflow["jobs"]["build"]["steps"]
                       if s["name"] == "Build and verify PR image against the pinned base")["run"]
        script = command.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
        self.assertIn('--base "$OCP_BASE"', command)
        self.assertNotIn("needs.prepare.outputs.base", command)
        plan = {"base": BASE, "matrix": {"include": [{"release": "4.22.22", "stream": "4.22"}]}}
        secret = "synthetic-plan-secret:/+$"
        cases = ((plan, True), ({**plan, "base": ""}, False), ({**plan, "base": "quay.io/s4v0/dcm-base-ee:latest"}, False),
                 ({**plan, "base": secret}, False), ({**plan, "matrix": {"include": []}}, False),
                 ({**plan, "matrix": {"include": [{"release": "4.22.14", "stream": "4.22"}]}}, False),
                 ({}, False), ([], False), (secret, False))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ocp-ee-pr-plan.json"
            environment = dict(os.environ, OCP_PLAN=str(path), OCP_RELEASE="4.22.22", GITHUB_RUN_ATTEMPT="2")
            environment.pop("OCP_BASE", None)
            for index, (value, valid) in enumerate(cases):
                with self.subTest(case=index):
                    path.write_text(json.dumps(value) if not isinstance(value, str) else value)
                    result = subprocess.run([sys.executable, "-"], input=script, text=True,
                                            capture_output=True, env=environment, timeout=10)
                    self.assertEqual(result.returncode, 0 if valid else 1)
                    self.assertEqual(result.stdout, BASE + "\n" if valid else "")
                    self.assertEqual(result.stderr, "" if valid else
                                     "Invalid qualification plan; original content suppressed.\n")
                    self.assertNotIn(secret, result.stdout + result.stderr)

    def test_qualification_requires_successful_same_repo_preparation(self):
        workflow = yaml.load((ROOT / ".github/workflows/pr-ocp-ee-build.yml").read_text(), Loader=yaml.BaseLoader)
        prepare, build = workflow["jobs"]["prepare"], workflow["jobs"]["build"]
        self.assertEqual(build["if"], "github.event.pull_request.head.repo.full_name == github.repository "
                         "&& needs.prepare.result == 'success'")
        self.assertEqual(build["needs"], "prepare")
        self.assertEqual(prepare["if"], "github.event.pull_request.head.repo.full_name == github.repository")
        images = yaml.load((ROOT / ".github/workflows/pr-image-build.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(images["jobs"]["build-image"]["if"], "${{ false }}")
        self.assertEqual(prepare["permissions"], {"contents": "read", "packages": "read"})
        self.assertEqual(set(workflow["jobs"]), {"prepare", "build"})
        for step in prepare["steps"]:
            command = step.get("run", "")
            self.assertNotIn("ansible-builder", command)
            self.assertNotRegex(command, r"ocp_ee\.py\s+(?:build|publish)\b")
            self.assertNotRegex(command, r"podman\s+(?:build|push)\b")
            if step.get("uses", "").startswith("actions/upload-artifact@"):
                self.assertEqual(step["with"]["path"], "${{ runner.temp }}/ocp-ee-pr-plan.json")


if __name__ == "__main__":
    unittest.main()
