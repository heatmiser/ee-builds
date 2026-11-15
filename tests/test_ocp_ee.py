"""Offline contract tests: no mirror, registry, container build or publication."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
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
    def __init__(self, bad_archive=False):
        self.urls = []
        self.bad_archive = bad_archive
        self.archives = {name: (name + " fixture bytes").encode() for name in EE.ARCHIVES}
        self.manifest = "".join(f"{hashlib.sha256(value).hexdigest()}  {name}\n"
                                for name, value in self.archives.items())

    def __call__(self, url, timeout):
        self.urls.append(url)
        if url.endswith("/release.txt"):
            data = b"Name: 4.22.22\nArchitecture: amd64\n"
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
                         {"include": [{"release": "4.22.14"}, {"release": "4.22.22"}]})
        for value in ("", "4.22.22,", "4.22.22,4.22.22", "stable-4.22", "4.21.22",
                      "4.22.022", "4.22.22;touch /tmp/file", "4.22.22\n4.22.14"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                EE.release_matrix(value)

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

    def test_publication_pushes_unique_tag_then_patch_without_latest(self):
        receipt, runner = self.build()
        result = EE.publish(receipt, "quay.io/s4v0", runner)
        pushes = [argv for argv in runner.commands if argv[:2] == ["podman", "push"]]
        self.assertEqual([argv[-1].rsplit(":", 1)[1] for argv in pushes], ["22-aaaaaaaaaaaa-123-1", "22"])
        self.assertTrue(all(argv[-2] == IMAGE_ID for argv in pushes))
        self.assertEqual(result["published_image"], "quay.io/s4v0/" + EE.IMAGE + "@" + DIGEST)

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
                EE.write_receipt(receipt, record)
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
        with self.assertRaises(subprocess.CalledProcessError):
            EE.publish(receipt, "quay.io/s4v0", fail_alias)
        self.assertEqual(len(json.loads(receipt.read_text())["published_tags"]), 1)

    def test_altered_tags_cannot_publish(self):
        receipt, runner = self.build()
        record = json.loads(receipt.read_text())
        record["tags"].append("latest")
        EE.write_receipt(receipt, record)
        with self.assertRaises(ValueError):
            EE.publish(receipt, "quay.io/s4v0", runner)

    def test_generated_containerfile_with_ci_builder(self):
        if not shutil.which("ansible-builder"):
            if os.environ.get("REQUIRE_BUILDER") == "1":
                self.fail("CI requires the pinned ansible-builder")
            self.skipTest("ansible-builder not installed; CI requires this render check")
        self.build()
        result = subprocess.run(["ansible-builder", "create", "--file", "execution-environment.yml",
                                 "--output-filename", "Containerfile",
                                 "--context", str(self.output / "rendered"), "--verbosity=3"],
                                cwd=self.output / "source", capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        containerfile = (self.output / "rendered/Containerfile").read_text()
        self.assertIn("ARG OCP_VERSION", containerfile)
        self.assertIn("RUN --mount=type=bind,source=_build/ocp-release,target=/tmp/ocp-release,ro ", containerfile)
        self.assertNotIn("COPY _build/ocp-release", containerfile)
        self.assertNotIn("rm -rf /tmp/ocp-release", containerfile)
        self.assertIn("sha256sum -c SHA256SUMS && tar xzf openshift-client-linux.tar.gz", containerfile)
        self.assertIn('LABEL ocp.version="$OCP_VERSION"', containerfile)
        for name in (*EE.ARCHIVES, "SHA256SUMS"):
            self.assertTrue((self.output / "rendered/_build/ocp-release" / name).is_file())


class WorkflowTests(unittest.TestCase):
    def test_manual_workflow_uses_shared_digest_and_safe_input_binding(self):
        path = ROOT / ".github/workflows/build-versioned-ocp-ee.yml"
        workflow = yaml.safe_load(path.read_text())
        build = workflow["jobs"]["build"]
        self.assertEqual(build["needs"], "prepare")
        self.assertEqual(build["strategy"]["max-parallel"], 1)
        command = next(step for step in build["steps"] if step["name"] == "Build and verify selected release")
        self.assertEqual(command["env"]["OCP_BASE"], "${{ needs.prepare.outputs.base }}")
        self.assertNotIn("${{ inputs.", command["run"])

    def test_legacy_push_cannot_publish_four_twenty_two(self):
        for filename in ("pr-ee-build.yml", "push-ee-build.yml"):
            workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
            steps = workflow["jobs"]["build-ocp-ees"]["steps"]
            for step in steps:
                if step["name"] in ("Build image", "Push To GHCR", "Push To quay.io"):
                    self.assertEqual(step["if"], "matrix.ocp_version != '4.22'")
            special = next(step for step in steps if step["name"] == "Build and verify exact stable OpenShift 4.22 release")
            self.assertEqual(special["if"], "matrix.ocp_version == '4.22'")
            self.assertIn("--release stable-4.22", special["run"])


if __name__ == "__main__":
    unittest.main()
