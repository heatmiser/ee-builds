"""Exercise generated credential mounts and the actual protected build driver."""

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

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


class ProtectedEEBuildTests(unittest.TestCase):
    def test_mounts_only_install_commands_and_preserves_signature_options(self):
        output = driver.protect_containerfile(GENERATED)
        self.assertEqual(output.count("--mount=type=secret,id=galaxy_config"), 2)
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

        with patch.dict(os.environ, CI_SECRET_AH_TOKEN=secret), patch.object(driver.guard, "captured", side_effect=run), patch.object(driver.guard, "inspect_image") as image:
            driver.build("dcm-base-ee", "test-build", COMMIT)
            image.assert_called_once_with("dcm-base-ee:test-build")
        self.assertTrue(temporary_paths)
        self.assertTrue(all(not p.exists() for p in temporary_paths))
        self.assertEqual((ROOT / "ansible.cfg").read_text().count("my_ah_token"), 1)

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
