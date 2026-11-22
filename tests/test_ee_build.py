"""Exercise generated credential mounts and the actual protected build driver."""

import contextlib
import base64
import functools
import http.server
import io
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import ci_secrets as guard
import ee_build as driver

COMMIT = "a" * 40
# Same RUN shape as ansible-builder 3.0.0's _prepare_galaxy_install_steps.
GENERATED = '''FROM base as galaxy
ARG COLLECTION_TOKEN
ARG RHVP_OCP_LANDING_ZONE_TOKEN=${COLLECTION_TOKEN}
RUN git config --global url."https://${RHVP_OCP_LANDING_ZONE_TOKEN}@github.com/heatmiser/rhvp.ocp_landing_zone".insteadOf "https://github.com/heatmiser/rhvp.ocp_landing_zone"
RUN ansible-galaxy role install $ANSIBLE_GALAXY_CLI_ROLE_OPTS -r requirements.yml --roles-path "/usr/share/ansible/roles"
RUN ANSIBLE_GALAXY_DISABLE_GPG_VERIFY=1 ansible-galaxy collection install $ANSIBLE_GALAXY_CLI_COLLECTION_OPTS -r requirements.yml --collections-path "/usr/share/ansible/collections"
FROM base as final
RUN /output/scripts/check_ansible $PYCMD
'''


def isolated_git_env(root, config):
    return {"PATH": os.defpath, "HOME": str(root), "XDG_CONFIG_HOME": str(root),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config),
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
            "LC_ALL": "C", "NO_PROXY": "127.0.0.1"}


class ProtectedEEBuildTests(unittest.TestCase):
    def test_cleaned_definitions_keep_galaxy_dependencies_without_credential_transports(self):
        for project in ("dcm-base-ee", "dcm-bootstrap-ee", "ocp-satellite-ee-4.22"):
            with self.subTest(project=project):
                source = ROOT / project
                text = (source / "execution-environment.yml").read_text()
                definition = yaml.safe_load(text)
                self.assertEqual(definition["version"], 3)
                self.assertEqual(definition["dependencies"]["galaxy"], "files/requirements.yml")
                requirements = yaml.safe_load((source / definition["dependencies"]["galaxy"]).read_text())
                self.assertTrue(requirements["collections"])
                for obsolete in ("COLLECTION_TOKEN", "RHVP_OCP_LANDING_ZONE_TOKEN", "RHVP_BAREMETAL_OCP_TOKEN",
                                 "OCP_SATELLITE_TOKEN", "ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_TOKEN",
                                 "insteadOf", "zizmor: ignore[secrets-outside-env]"):
                    self.assertNotIn(obsolete, text)
                prepend = definition["additional_build_steps"].get("prepend_galaxy", [])
                if project == "ocp-satellite-ee-4.22":
                    self.assertEqual(prepend, ["ADD _build/configs/ansible.cfg /etc/ansible/ansible.cfg"])
                    self.assertIn({"src": "ansible.cfg", "dest": "configs"}, definition["additional_build_files"])
                    collections = {c["name"]: c for c in requirements["collections"]}
                    self.assertEqual(collections["elise.quadlet"]["source"],
                                     "https://github.com/heatmiser/elise.quadlet.git")
                    self.assertEqual(collections["heatmiser.ocp_satellite"]["source"],
                                     "https://github.com/heatmiser/ocp_satellite.git")
                    self.assertIn("redhat.satellite", collections)
                else:
                    self.assertNotIn("prepend_galaxy", definition["additional_build_steps"])
                # Feed the cleaned prepend steps into Builder's known install shape.
                # Actual Builder rendering remains a separate CI qualification.
                installs = "\n".join(line for line in GENERATED.splitlines()
                                     if line.startswith("RUN ") and "ansible-galaxy" in line)
                generated = "FROM base as galaxy\n" + "\n".join(prepend) + "\n" + installs + "\n"
                protected = driver.protect_containerfile(generated)
                for mount in ("galaxy_config", "git_config", "git_credentials"):
                    self.assertEqual(protected.count("--mount=type=secret,id=" + mount + ","), 2)
                for line in prepend:
                    self.assertIn(line + "\n", protected)

    def test_mounts_only_install_commands_and_preserves_signature_options(self):
        output = driver.protect_containerfile(GENERATED)
        self.assertEqual(output.count("--mount=type=secret,id=galaxy_config"), 2)
        self.assertEqual(output.count("--mount=type=secret,id=git_credentials"), 2)
        self.assertEqual(output.count("GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false"), 2)
        self.assertNotIn("ARG COLLECTION_TOKEN", output)
        self.assertNotIn("git config --global", output)
        self.assertIn("ANSIBLE_GALAXY_DISABLE_GPG_VERIFY=1 ansible-galaxy collection install", output)
        self.assertIn("$ANSIBLE_GALAXY_CLI_COLLECTION_OPTS", output)
        self.assertIn("RUN /output/scripts/check_ansible $PYCMD", output)

    def test_unknown_transport_and_changed_install_shape_fail_closed(self):
        for text in (GENERATED + 'RUN echo "$AH_TOKEN"\n',
                     GENERATED.replace("RUN ansible-galaxy role", "RUN --mount=type=bind ansible-galaxy role"),
                     "FROM base as galaxy\nRUN /unrecognized/installer\n"):
            with self.assertRaises(guard.GuardError):
                driver.protect_containerfile(text)

    def test_build_passes_secret_file_paths_not_values_and_cleans_them(self):
        secret = "synthetic-ee-build-token:/+$42"
        commands, temporary_paths = [], []

        def run(argv, **kwargs):
            commands.append(argv)
            self.assertNotIn(secret, " ".join(argv))
            if argv[0] == "git":
                return (COMMIT + "\n").encode()
            if argv[0] == "ansible-builder":
                context = Path(argv[argv.index("--context") + 1])
                context.mkdir()
                (context / "Containerfile").write_text(GENERATED)
            else:
                self.assertEqual(argv[:2], ["podman", "build"])
                self.assertNotIn("--build-arg", argv)
                for item in argv:
                    if item.startswith("id="):
                        path = Path(item.split("src=", 1)[1])
                        self.assertNotIn(Path(argv[-1]), path.parents)
                        temporary_paths.append(path)
                        self.assertTrue(path.is_file())
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return b""

        with patch.dict(os.environ, CI_SECRET_AH_TOKEN=secret, CI_SECRET_RHVP_OCP_LANDING_ZONE_TOKEN=secret), patch.object(driver.guard, "captured", side_effect=run), patch.object(driver.guard, "inspect_image") as image:
            driver.build("dcm-base-ee", "test-build", COMMIT)
            image.assert_called_once_with("dcm-base-ee:test-build")
        self.assertEqual(len(temporary_paths), 3)
        self.assertTrue(all(not p.exists() for p in temporary_paths))
        self.assertEqual((ROOT / "ansible.cfg").read_text().count("my_ah_token"), 1)

    def test_git_helper_scopes_credentials_and_ignores_persistence_requests(self):
        self.assertEqual(set(driver.GIT_REPOS), guard.GIT_TOKEN_NAMES)
        secrets = {key: "synthetic-git-credential:/+%@42-" + str(i) for i, key in enumerate(driver.GIT_REPOS)}
        with tempfile.TemporaryDirectory(prefix="ee git credentials ") as name:
            root = Path(name)
            config, credentials = root / "git.cfg", root / "git-credentials"
            with patch.dict(os.environ, {"CI_SECRET_" + k: v for k, v in secrets.items()}, clear=True):
                driver.write_git_credentials(config, credentials, mounted_path=str(credentials))
            environment = isolated_git_env(root, config)
            self.assertNotIn("insteadOf", config.read_text())
            for secret in secrets.values():
                self.assertNotIn(secret, config.read_text())
            before = credentials.read_bytes()
            for source, repo in driver.GIT_REPOS.items():
                for suffix in ("", ".git"):
                    query = f"protocol=https\nhost=github.com\npath=heatmiser/{repo}{suffix}\n\n".encode()
                    result = subprocess.run(["git", "credential", "fill"], cwd=root, env=environment,
                                            input=query, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0)
                    fields = dict(line.split(b"=", 1) for line in result.stdout.splitlines())
                    self.assertEqual(fields[b"username"], guard.GIT_AUTH_USERNAME.encode())
                    self.assertEqual(fields[b"password"], secrets[source].encode())
                    self.assertEqual(result.stderr, b"")
                    for action in ("approve", "reject"):
                        outcome = subprocess.run(["git", "credential", action], cwd=root, env=environment,
                                                 input=result.stdout + b"\n", capture_output=True, timeout=10)
                        self.assertEqual(outcome.returncode, 0)
                        self.assertEqual(outcome.stdout + outcome.stderr, b"")
            self.assertEqual(credentials.read_bytes(), before)
            self.assertFalse((root / ".git-credentials").exists())
            self.assertEqual(set(root.iterdir()), {config, credentials})

    def test_git_helper_denies_other_repositories_hosts_and_protocols_without_fallback(self):
        secret = "synthetic-git-isolation-token-42"
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config, credentials, inherited, marker = [root / n for n in ("git.cfg", "git-credentials", "system.cfg", "fallback-called")]
            with patch.dict(os.environ, {"CI_SECRET_RHVP_OCP_LANDING_ZONE_TOKEN": secret}, clear=True):
                driver.write_git_credentials(config, credentials, mounted_path=str(credentials))
            inherited.write_text('[credential]\n\thelper = "!touch ' + str(marker) + '; echo username=fallback; echo password=fallback"\n')
            environment = isolated_git_env(root, config)
            del environment["GIT_CONFIG_NOSYSTEM"]
            environment["GIT_CONFIG_SYSTEM"] = str(inherited)
            for url in ("https://github.com/heatmiser/unrelated.git",
                        "https://github.com/heatmiser/rhvp.ocp_landing_zone-extra.git",
                        "https://github.com/other/rhvp.ocp_landing_zone.git",
                        "https://other.example/heatmiser/rhvp.ocp_landing_zone.git",
                        "http://github.com/heatmiser/rhvp.ocp_landing_zone.git",
                        "https://github.com:444/heatmiser/rhvp.ocp_landing_zone.git"):
                with self.subTest(url=url):
                    result = subprocess.run(["git", "credential", "fill"], cwd=root, env=environment,
                                            input=("url=" + url + "\n\n").encode(), capture_output=True, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(secret.encode(), result.stdout + result.stderr)
            self.assertFalse(marker.exists())

    def test_git_authentication_success_and_rejection_on_loopback_never_echo_token(self):
        secret = "synthetic-loopback-git-token-42"
        encoded = base64.b64encode((guard.GIT_AUTH_USERNAME + ":" + secret).encode())
        authorized, reject = [], [False]

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                valid = self.headers.get("Authorization") == "Basic " + encoded.decode()
                authorized.append(valid)
                if reject[0] or not valid:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="synthetic-local-only"')
                    self.end_headers()
                else:
                    super().do_GET()

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bare = root / "server/heatmiser/rhvp.ocp_landing_zone.git"
            bare.mkdir(parents=True)
            config, credentials = root / "git.cfg", root / "git-credentials"
            environment = isolated_git_env(root, config)
            subprocess.run(["git", "init", "--bare", str(bare)], env=environment, capture_output=True, check=True, timeout=10)
            subprocess.run(["git", "update-server-info"], cwd=bare, env=environment, capture_output=True, check=True, timeout=10)
            with patch.dict(os.environ, {"CI_SECRET_RHVP_OCP_LANDING_ZONE_TOKEN": secret}, clear=True):
                driver.write_git_credentials(config, credentials, mounted_path=str(credentials))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root / "server")))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # Map only the fixture's origin to loopback; use the production helper.
                origin = f"http://127.0.0.1:{server.server_port}/"
                endpoint = urllib.parse.urlsplit(origin)
                mapped = []
                for line in credentials.read_text().splitlines():
                    entry = urllib.parse.urlsplit(line)
                    local = entry._replace(scheme=endpoint.scheme,
                                           netloc=entry.netloc.rsplit("@", 1)[0] + "@" + endpoint.netloc)
                    self.assertEqual((local.hostname, local.port), (endpoint.hostname, endpoint.port))
                    self.assertEqual((local.username, local.password), (entry.username, entry.password))
                    mapped.append(local.geturl())
                credentials.write_text("\n".join(mapped) + "\n")
                credentials.chmod(0o400)
                before = credentials.read_bytes()
                for denied in (False, True):
                    reject[0] = denied
                    result = subprocess.run(["git", "ls-remote", origin + "heatmiser/rhvp.ocp_landing_zone.git"],
                                            cwd=root, env=environment, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode == 0, not denied)
                    self.assertNotIn(secret.encode(), result.stdout + result.stderr)
                    self.assertNotIn(encoded, result.stdout + result.stderr)
                    self.assertEqual(credentials.read_bytes(), before)
                    self.assertFalse((root / ".git-credentials").exists())
                self.assertIn(True, authorized)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_git_tokens_with_control_characters_are_rejected_without_echo(self):
        with tempfile.TemporaryDirectory() as name:
            for suffix in ("\n", "\r", "\0"):
                with patch.object(guard, "value", return_value="synthetic-invalid-token" + suffix):
                    with self.assertRaises(guard.GuardError) as caught:
                        driver.write_git_credentials(Path(name) / "config", Path(name) / "credentials")
                    self.assertNotIn("synthetic-invalid-token", str(caught.exception))

    def test_examples_fail_before_any_build_process(self):
        with patch.object(driver.guard, "captured") as run, self.assertRaises(ValueError):
            driver.build("aws-ee", "test-build", COMMIT)
        run.assert_not_called()

    def test_failures_report_stage_and_category_without_changing_cleanup(self):
        cases = (("identity", "command-unavailable"), ("builder-create", "command-failed"),
                 ("context-protection", "protection-denied"), ("credential-configuration", "protection-denied"),
                 ("podman-build", "authentication-rejected"), ("podman-build", "credential-detected"),
                 ("image-inspection", "credential-detected"))
        for stage, category in cases:
            with self.subTest(stage=stage):
                workspaces = []

                def run(argv, **kwargs):
                    if argv[0] == "git":
                        if stage == "identity":
                            raise guard.GuardError("private diagnostic must stay private", category=category)
                        return (COMMIT + "\n").encode()
                    if argv[0] == "ansible-builder":
                        context = Path(argv[argv.index("--context") + 1])
                        workspaces.append(context.parent)
                        if stage == "builder-create":
                            raise guard.GuardError("private diagnostic must stay private", category=category)
                        context.mkdir()
                        text = GENERATED + 'RUN echo "$AH_TOKEN"\n' if stage == "context-protection" else GENERATED
                        (context / "Containerfile").write_text(text)
                        return b""
                    self.assertEqual(argv[:2], ["podman", "build"])
                    if stage == "podman-build":
                        raise guard.GuardError("private diagnostic must stay private", category=category,
                                               sources=("REDHAT_SA_USERNAME",), stream="stdout", command_status="success")
                    return b""

                token = "synthetic-stage-token\ninvalid" if stage == "credential-configuration" else "synthetic-stage-token"
                with patch.dict(os.environ, CI_SECRET_AH_TOKEN=token), \
                        patch.object(driver.guard, "captured", side_effect=run), \
                        patch.object(driver.guard, "inspect_image") as image:
                    if stage == "image-inspection":
                        image.side_effect = guard.GuardError("private diagnostic must stay private", category=category)
                    with self.assertRaises(driver.BuildError) as caught:
                        driver.build("dcm-base-ee", "test-diagnostic", COMMIT)
                    self.assertEqual(caught.exception.stage, stage)
                    self.assertEqual(caught.exception.category, category)
                    if stage == "podman-build":
                        self.assertEqual(guard.error_metadata(caught.exception),
                                         {"sources": ("REDHAT_SA_USERNAME",), "stream": "stdout", "command_status": "success"})
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertNotIn("private diagnostic", str(caught.exception))
                    if stage != "image-inspection":
                        image.assert_not_called()
                self.assertTrue(all(not p.exists() for p in workspaces))

    def test_cli_reports_only_fixed_metadata_and_never_raw_exception_values(self):
        secret = "synthetic-cli-diagnostic:/+$42"
        cases = (driver.BuildError("podman-build", "authentication-rejected"),
                 driver.BuildError(secret, secret), OSError(secret),
                 guard.GuardError(secret, category="credential-detected"),
                 driver.BuildError("podman-build", "credential-detected", sources=("AH_TOKEN", secret),
                                   stream="stderr", command_status="failure"))
        for error in cases:
            with self.subTest(error_type=type(error).__name__):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(sys, "argv", ["ee_build.py", "--project", "dcm-base-ee", "--tag", "test", "--commit", COMMIT]), \
                        patch.dict(os.environ, CI_SECRET_AH_TOKEN=secret), \
                        patch.object(driver, "build", side_effect=error), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as caught:
                        driver.main()
                self.assertEqual(caught.exception.code, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn(secret, stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                expected_stage = error.stage if isinstance(error, driver.BuildError) else "unknown"
                metadata = guard.error_metadata(error)
                attribution = ("; sources=" + (",".join(metadata["sources"]) or "unattributed")
                               + "; stream=" + metadata["stream"] + "; command-status=" + metadata["command_status"]
                               if guard.error_category(error) == "credential-detected" else "")
                self.assertEqual(stderr.getvalue(), "ERROR: protected EE build failed; stage=" + expected_stage
                                 + "; category=" + guard.error_category(error) + attribution + "; no public output prepared\n")

    def test_cli_denies_even_fixed_diagnostics_when_they_match_a_supplied_value(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["ee_build.py", "--project", "dcm-base-ee", "--tag", "test", "--commit", COMMIT]), \
                patch.dict(os.environ, CI_SECRET_AH_TOKEN="podman-build"), \
                patch.object(driver, "build", side_effect=driver.BuildError("podman-build", "command-failed")), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                driver.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_cli_revalidates_attribution_and_denies_source_label_collisions(self):
        secret = "synthetic-attribution-diagnostic:/+$42"
        for token in (secret, "AH_TOKEN"):
            with self.subTest(label_collision=token == "AH_TOKEN"):
                error = driver.BuildError("podman-build", "credential-detected")
                error.sources, error.stream, error.command_status = (secret, "AH_TOKEN"), secret, secret
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(sys, "argv", ["ee_build.py", "--project", "dcm-base-ee", "--tag", "test", "--commit", COMMIT]), \
                        patch.dict(os.environ, CI_SECRET_AH_TOKEN=token), \
                        patch.object(driver, "build", side_effect=error), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as caught:
                        driver.main()
                self.assertEqual(caught.exception.code, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn(secret, stderr.getvalue())
                if token == "AH_TOKEN":
                    self.assertEqual(stderr.getvalue(), "")
                else:
                    self.assertIn("sources=AH_TOKEN; stream=unknown; command-status=unknown", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
