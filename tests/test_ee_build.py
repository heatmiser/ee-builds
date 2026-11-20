"""Exercise generated credential mounts and the actual protected build driver."""

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


if __name__ == "__main__":
    unittest.main()
