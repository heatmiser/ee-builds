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


class PRPreparationTests(unittest.TestCase):
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
            EE.write_receipt(receipt, record)
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
            quay_login = next(step for step in job["steps"] if step.get("with", {}).get("registry") == "quay.io")
            self.assertEqual(quay_login["with"]["username"], "${{ secrets.QUAY_USERNAME }}")
            self.assertEqual(quay_login["with"]["password"], "${{ secrets.QUAY_PASSWORD }}")
        command = next(step for step in build["steps"] if step["name"] == "Build and verify PR image against the pinned base")
        self.assertEqual(command["env"]["OCP_BASE"], "${{ needs.prepare.outputs.base }}")
        self.assertEqual(command["env"]["SOURCE_COMMIT"], "${{ github.event.pull_request.head.sha }}")
        self.assertIn('--pr "$PR_NUMBER"', command["run"])
        publish = next(step for step in build["steps"] if step["name"] == "Publish verified PR image to GHCR")
        self.assertEqual(publish["env"]["OCP_REGISTRY"], "ghcr.io/${{ github.repository_owner }}")
        self.assertIn(".github/ocp-ee-streams.json", workflow["on"]["pull_request"]["paths"])
        validation = yaml.load((ROOT / ".github/workflows/test-ocp-ee.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertIn(".github/workflows/pr-ocp-ee-build.yml", validation["on"]["pull_request"]["paths"])


if __name__ == "__main__":
    unittest.main()
