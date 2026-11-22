"""Offline plain-image build contracts with synthetic OCI images and credentials."""

import contextlib
import bz2
import gzip
import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
SPEC = importlib.util.spec_from_file_location("ci_publication", ROOT / ".github/scripts/ci_publication.py")
CI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CI)
COMMIT = "a" * 40
BASE = "b" * 40
SECRET = "synthetic-publication-canary:/+$42"


def tar_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, value in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    return stream.getvalue()


def cpio_bytes(entries, *, crc=False):
    """Synthetic newc/CRC archive, including its required trailer."""
    output = bytearray()
    for name, value in [*entries.items(), ("TRAILER!!!", b"")]:
        mode = stat.S_IFREG | 0o644 if name != "TRAILER!!!" else 0
        if isinstance(value, tuple):
            value, mode = value
        encoded = name.encode() + b"\0"
        fields = [1, mode, 0, 0, 1, 0, len(value), 0, 0, 0, 0, len(encoded),
                  sum(value) & 0xffffffff if crc else 0]
        output.extend((b"070702" if crc else b"070701")
                      + b"".join(f"{field:08x}".encode() for field in fields) + encoded)
        output.extend(b"\0" * ((-len(output)) % 4))
        output.extend(value)
        output.extend(b"\0" * ((-len(value)) % 4))
    return bytes(output) + b"\0" * ((-len(output)) % 512)


def rpm_bytes(payload, *, compression="gzip", metadata=b"synthetic-metadata"):
    """Minimal RPM section framing for inspection, never installation/signing."""
    lead = bytearray(96)
    lead[:6] = b"\xed\xab\xee\xdb\x03\x00"
    lead[78:80] = b"\x00\x05"

    def header(data):
        return b"\x8e\xad\xe8\x01\0\0\0\0" + struct.pack("!II", 0, len(data)) + data

    signature = header(b"signature")
    encode = {"gzip": lambda value: gzip.compress(value, mtime=0), "xz": lzma.compress,
              "bzip2": bz2.compress, "none": lambda value: value}
    return (bytes(lead) + signature + b"\0" * ((-len(signature)) % 8)
            + header(metadata) + encode[compression](payload))


def oci_fixture(directory, layers=None, environment=None):
    directory.mkdir()
    blobs = directory / "blobs/sha256"
    blobs.mkdir(parents=True)

    def blob(data, media):
        checksum = hashlib.sha256(data).hexdigest()
        (blobs / checksum).write_bytes(data)
        return {"digest": "sha256:" + checksum, "size": len(data), "mediaType": media}

    layers = layers or [tar_bytes({"usr/share/example.txt": b"public build fixture"})]
    descriptors = [blob(layer, "application/vnd.oci.image.layer.v1.tar") for layer in layers]
    config = {"architecture": "amd64", "os": "linux", "config": {"Env": environment or []},
              "rootfs": {"type": "layers", "diff_ids": [d["digest"] for d in descriptors]},
              "history": [{"created_by": "synthetic build fixture"}]}
    configuration = blob(json.dumps(config).encode(), "application/vnd.oci.image.config.v1+json")
    manifest = blob(json.dumps({"schemaVersion": 2, "config": configuration, "layers": descriptors}).encode(),
                    "application/vnd.oci.image.manifest.v1+json")
    (directory / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [manifest]}))
    (directory / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    return configuration["digest"]


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.source = self.work / "source"
        self.source.mkdir()
        for image in CI.IMAGES:
            (self.source / image).mkdir()
            (self.source / image / "Containerfile").write_text("FROM scratch\n")
        (self.source / ".github").mkdir()
        self.event = self.work / "event.json"
        self.event.write_text(json.dumps({"inputs": {"image_name": "all"}}))
        self.env = dict(GITHUB_EVENT_PATH=str(self.event), GITHUB_EVENT_NAME="workflow_dispatch",
                        GITHUB_REPOSITORY="heatmiser/ee-builds", GITHUB_SHA=COMMIT,
                        GITHUB_REF="refs/heads/main", GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1",
                        RUNNER_TEMP=str(self.work))
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env).start()
        patch.object(CI, "ROOT", self.source).start()
        self.oci = self.work / "fixture-oci"
        self.image_id = oci_fixture(self.oci)
        self.commands = []

    def captured(self, stage, argv, private, **kwargs):
        self.commands.append((stage, argv, kwargs))
        output = private / (stage + ".stdout")
        output.write_text(COMMIT if stage == "source" else getattr(self, "inspection_output", self.image_id)
                          if stage == "image-id" else "")
        (private / (stage + ".stderr")).write_text("")
        if stage == "export":
            shutil.copytree(self.oci, Path(argv[argv.index("--output") + 1]))
        if stage == "login":
            (private / "auth.json").write_text("synthetic registry auth")
        if stage.startswith("push-"):
            Path(argv[argv.index("--digestfile") + 1]).write_text("sha256:" + "d" * 64)
        return output

    def build(self, image="codex-ai-helpers", output=None, channel="devel"):
        output = output or self.work / "results"
        l8k = {"imageID": self.image_id, "version": "v1.2.3", "sourceRevision": COMMIT,
               "buildDate": "2026-11-18T00:00:00Z", "lockSHA256": "b" * 64,
               "sourceTreeDigest": "c" * 64, "platform": "linux/amd64",
               "builderImage": "docker.io/example/builder@sha256:" + "e" * 64,
               "runtimeImage": "nvcr.io/example/runtime@sha256:" + "f" * 64}
        with patch.object(CI, "captured", side_effect=self.captured), \
                patch.object(CI.ci_report, "generate", return_value=0) as runtime, \
                patch.object(CI, "l8k_evidence", return_value=l8k) as l8k_check:
            record = CI.build(image, channel, output)
        return record, output, runtime.call_count, l8k_check.call_count

    def test_all_five_build_paths_preserve_runtime_checks_and_publish_only_fixed_files(self):
        for form, value in enumerate((self.image_id, self.image_id.removeprefix("sha256:"))):
            self.inspection_output = value
            for index, image in enumerate(CI.IMAGES):
                with self.subTest(image=image, prefixed=form == 0):
                    record, output, runtime, l8k = self.build(image, self.work / f"{form}-{index}")
                    self.assertEqual(record["project"], image)
                    self.assertEqual(record["credential_check"], "PASS")
                    self.assertEqual(record["upstream_scan"], "NOT_RUN")
                    self.assertNotIn("secret_scan", record)
                    self.assertEqual(record["image_id"], self.image_id)
                    self.assertEqual(json.loads((output / "public/receipt.json").read_text())["image_id"], self.image_id)
                    export = next(argv for stage, argv, _ in reversed(self.commands) if stage == "export")
                    self.assertEqual(export[-1], self.image_id)
                    self.assertEqual(set(p.name for p in (output / "public").iterdir()), CI.PUBLIC_FILES)
                    self.assertEqual(runtime, 0 if image == "k8s-launch-kit" else 1)
                    self.assertEqual(l8k, 1 if image == "k8s-launch-kit" else 0)
                    self.assertFalse(any(stage.startswith(("push", "login")) for stage, _, _ in self.commands))

    def test_normalization_accepts_only_full_lowercase_sha256_ids(self):
        for value in (self.image_id, self.image_id.removeprefix("sha256:")):
            self.assertEqual(CI.normalize_image_id(value), self.image_id)
        invalid = (None, 1, {}, b"a" * 64, "", "a" * 63, "a" * 65, "A" * 64,
                   "sha512:" + "a" * 64, "sha256:sha256:" + "a" * 64, "sha256:" + "a" * 63,
                   " " + "a" * 64, "a" * 64 + "\n", "a" * 64 + "\n" + "b" * 64, SECRET)
        for index, value in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(CI.PublicError) as error:
                CI.normalize_image_id(value)
            self.assertEqual(str(error.exception), "Invalid local image identity")

    def test_invalid_inspection_output_prevents_runtime_export_and_public_artifacts(self):
        for index, value in enumerate(("a" * 63, "A" * 64, "sha512:" + "a" * 64,
                                      "a" * 64 + "\n" + "b" * 64, SECRET)):
            self.inspection_output = value
            self.commands.clear()
            output = self.work / f"invalid-build-{index}"
            with self.subTest(case=index), self.assertRaises(CI.PublicError):
                self.build(output=output)
            self.assertEqual([stage for stage, _, _ in self.commands], ["source", "build", "image-id"])
            self.assertFalse((output / "private/checks").exists())
            self.assertFalse((output / "public").exists())

    def test_publication_normalizes_inspection_id_before_comparison_and_push(self):
        for channel in ("devel", "production"):
            for index, value in enumerate((self.image_id, self.image_id.removeprefix("sha256:"))):
                self.inspection_output = value
                _, output, _, _ = self.build(output=self.work / f"publish-{channel}-{index}", channel=channel)
                self.commands.clear()
                with patch.dict(os.environ, REGISTRY_USERNAME="robot", REGISTRY_PASSWORD=SECRET), \
                        patch.object(CI, "captured", side_effect=self.captured):
                    result = CI.publish("codex-ai-helpers", channel, output)
                self.assertEqual(len(result["tags"]), 1 if channel == "devel" else 2)
                pushes = [argv for stage, argv, _ in self.commands if stage.startswith("push-")]
                self.assertTrue(all(argv[-2] == self.image_id for argv in pushes))
                self.assertFalse((output / "private/publication/auth.json").exists())

    def test_malformed_or_different_inspection_id_prevents_login_and_push(self):
        invalid = ("sha256:" + "f" * 64, "f" * 64, "a" * 63, "sha512:" + "a" * 64,
                   "a" * 64 + "\n" + "b" * 64, SECRET)
        for index, value in enumerate(invalid):
            _, output, _, _ = self.build(output=self.work / f"invalid-publish-{index}")
            self.commands.clear()

            def inspect(stage, argv, private, **kwargs):
                path = self.captured(stage, argv, private, **kwargs)
                if stage == "image-id":
                    path.write_text(value)
                return path

            with self.subTest(case=index), patch.dict(os.environ, REGISTRY_USERNAME="robot", REGISTRY_PASSWORD=SECRET), \
                    patch.object(CI, "captured", side_effect=inspect), self.assertRaises(CI.PublicError) as error:
                CI.publish("codex-ai-helpers", "devel", output)
            self.assertNotIn(SECRET, str(error.exception))
            self.assertFalse(any(stage == "login" or stage.startswith("push-") for stage, _, _ in self.commands))

    def test_failed_credential_check_creates_no_public_artifact(self):
        with patch.dict(os.environ, CI_SECRET_AH_TOKEN="public build fixture"), self.assertRaises(CI.PublicError):
            self.build()
        self.assertFalse((self.work / "results/public").exists())

    def test_real_runtime_command_construction_and_environment_for_all_four_helpers(self):
        for index, image in enumerate(name for name in CI.IMAGES if name != "k8s-launch-kit"):
            (self.source / image / "image.yaml").write_text("cli_binary: example\n")
            calls = []

            def run(argv, **kwargs):
                calls.append((argv, kwargs["env"]))
                self.assertNotIn(SECRET, json.dumps(kwargs["env"]))
                self.assertEqual(kwargs["timeout"], 120)
                return subprocess.CompletedProcess(argv, 0)

            output = self.work / ("runtime-" + str(index))
            with patch.dict(os.environ, CI_SECRET_GITHUB_TOKEN=SECRET, REGISTRY_PASSWORD=SECRET), \
                    patch.object(CI, "captured", side_effect=self.captured), \
                    patch.object(CI.ci_report, "ROOT", self.source), \
                    patch.object(CI.subprocess, "run", side_effect=run):
                CI.build(image, "devel", output)
            prefix = ["podman", "run", "--rm", "--pull=never", "--network=none"]
            if image == "antigravity-ai-helpers":
                prefix += ["--env", "AGY_HEADLESS=1", "--env", "AGY_TRUST_WORKSPACE=true"]
            local = image + ":manual-123-1-" + COMMIT[:7]
            self.assertEqual([argv for argv, _ in calls], [
                prefix + ["--entrypoint", "example", local, "--version"],
                prefix + ["--entrypoint", "sh", local, "-c", CI.ci_report.TOOL_CHECK]])

    def test_l8k_image_identity_mismatch_prevents_public_artifact_creation(self):
        with patch.object(CI, "captured", side_effect=self.captured), \
                patch.object(CI, "l8k_evidence", return_value=dict(
                    version="v1.2.3", sourceRevision=COMMIT, buildDate="2026-11-18T00:00:00Z",
                    imageID="sha256:" + "f" * 64, lockSHA256="b" * 64, sourceTreeDigest="c" * 64,
                    platform="linux/amd64", builderImage="unused", runtimeImage="unused")), \
                self.assertRaisesRegex(CI.PublicError, "differs from built image"):
            CI.build("k8s-launch-kit", "devel", self.work / "mismatched-l8k")
        self.assertFalse((self.work / "mismatched-l8k/public").exists())

    def test_credentials_in_configuration_are_rejected_before_artifact_creation(self):
        shutil.rmtree(self.oci)
        self.image_id = oci_fixture(self.oci, environment=["PASSWORD=" + SECRET])
        with patch.dict(os.environ, CI_SECRET_CANARY=SECRET), self.assertRaisesRegex(CI.PublicError, "Known credential"):
            self.build()
        self.assertFalse((self.work / "results/public").exists())

    def test_lower_layer_credential_is_detected_after_whiteout(self):
        shutil.rmtree(self.oci)
        self.image_id = oci_fixture(self.oci, layers=[tar_bytes({"secret.txt": SECRET.encode()}),
                                                    tar_bytes({".wh.secret.txt": b""})])
        with patch.dict(os.environ, CI_SECRET_CANARY=SECRET), self.assertRaisesRegex(CI.PublicError, "Known credential"):
            self.build()

    def test_oci_corruption_and_unreferenced_files_are_rejected(self):
        (self.oci / "extra-file").write_text("unreviewed")
        with self.assertRaisesRegex(CI.PublicError, "unreferenced"):
            CI.inspect_oci(self.oci, CI.Payloads(self.work / "payloads1", ()), self.image_id)
        (self.oci / "extra-file").unlink()
        blob = next((self.oci / "blobs/sha256").iterdir())
        blob.write_bytes(blob.read_bytes() + b"corruption")
        with self.assertRaisesRegex(CI.PublicError, "integrity"):
            CI.inspect_oci(self.oci, CI.Payloads(self.work / "payloads2", ()), self.image_id)

    def test_nested_archives_are_inspected_and_unsafe_paths_rejected(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("nested.gz", gzip.compress(SECRET.encode()))
        with self.assertRaisesRegex(CI.PublicError, "Known credential"):
            CI.Payloads(self.work / "payloads1", (SECRET.encode(),)).add(io.BytesIO(archive.getvalue()))
        with self.assertRaisesRegex(CI.PublicError, "Unsafe archive"):
            CI.Payloads(self.work / "payloads2", ()).add(io.BytesIO(tar_bytes({"../escape": b"data"})))
        self.assertFalse((self.work / "escape").exists())

    def test_archive_size_and_nesting_limits_fail_closed(self):
        with patch.object(CI, "MAX_FILE", 4), self.assertRaisesRegex(CI.PublicError, "limit"):
            CI.Payloads(self.work / "payloads1", ()).add(io.BytesIO(b"12345"))
        with self.assertRaisesRegex(CI.PublicError, "limit"):
            CI.Payloads(self.work / "payloads2", ()).add(io.BytesIO(b"data"), depth=9)

    def test_rpm_clean_cpio_members_with_supported_compression(self):
        for compression in ("gzip", "xz", "bzip2", "none"):
            for crc in (False, True):
                with self.subTest(compression=compression, crc=crc):
                    output = self.work / f"rpm-{compression}-{crc}"
                    data = rpm_bytes(cpio_bytes({"usr/share/example.txt": b"clean fixture"}, crc=crc),
                                     compression=compression)
                    CI.Payloads(output, ()).add(io.BytesIO(data))
                    self.assertIn(b"clean fixture", [p.read_bytes() for p in output.iterdir()])

    def test_rpm_headers_names_links_and_nested_contents_detect_credentials(self):
        fixtures = [rpm_bytes(cpio_bytes({"file": b"clean"}), metadata=SECRET.encode()),
                    rpm_bytes(cpio_bytes({SECRET: b"clean"})),
                    rpm_bytes(cpio_bytes({"link": (SECRET.encode(), stat.S_IFLNK | 0o777)})),
                    rpm_bytes(cpio_bytes({"nested.gz": gzip.compress(SECRET.encode())}, crc=True))]
        for index, data in enumerate(fixtures):
            with self.subTest(index=index), self.assertRaisesRegex(CI.PublicError, "Known credential"):
                CI.Payloads(self.work / f"rpm-secret-{index}", (SECRET.encode(),)).add(io.BytesIO(data))

    def test_rpm_credential_in_lower_layer_prevents_public_artifact(self):
        shutil.rmtree(self.oci)
        data = rpm_bytes(cpio_bytes({"private.txt": SECRET.encode()}))
        self.image_id = oci_fixture(self.oci, layers=[tar_bytes({"fixture.rpm": data}),
                                                    tar_bytes({".wh.fixture.rpm": b""})])
        with patch.dict(os.environ, CI_SECRET_CANARY=SECRET), self.assertRaisesRegex(CI.PublicError, "Known credential"):
            self.build()
        self.assertFalse((self.work / "results/public").exists())

    def test_rpm_malformed_headers_and_unsupported_payloads_fail_closed(self):
        good = rpm_bytes(cpio_bytes({"file": b"clean"}))
        wrong_header = bytearray(good)
        wrong_header[96] = 0
        oversized = bytearray(good)
        oversized[104:108] = struct.pack("!I", 0xffffffff)
        wrong_padding = bytearray(good)
        wrong_padding[121] = 1
        fixtures = [good[:40], bytes(wrong_header), bytes(oversized), bytes(wrong_padding),
                    good[:-5], rpm_bytes(b"070707unsupported"),
                    rpm_bytes(b"\x28\xb5\x2f\xfdunsupported", compression="none")]
        for index, data in enumerate(fixtures):
            with self.subTest(index=index), self.assertRaises(CI.PublicError):
                CI.Payloads(self.work / f"rpm-invalid-{index}", ()).add(io.BytesIO(data))

    def test_rpm_cpio_checksum_truncation_and_trailing_data_fail_closed(self):
        payload = cpio_bytes({"file": b"clean"}, crc=True)
        checksum = bytearray(payload)
        checksum[102:110] = b"ffffffff"
        nonhex = bytearray(payload)
        nonhex[6:14] = b"invalid!"
        for index, data in enumerate((bytes(checksum), bytes(nonhex), payload[:125], payload + b"unexpected")):
            with self.subTest(index=index), self.assertRaises(CI.PublicError):
                CI.Payloads(self.work / f"rpm-cpio-invalid-{index}", ()).add(io.BytesIO(rpm_bytes(data)))

    def test_rpm_cpio_paths_and_member_sizes_are_checked(self):
        for index, name in enumerate(("../escape", "/absolute", "bad\\path")):
            with self.subTest(name=name), self.assertRaisesRegex(CI.PublicError, "Unsafe archive"):
                CI.Payloads(self.work / f"rpm-path-{index}", ()).add(io.BytesIO(rpm_bytes(cpio_bytes({name: b"data"}))))
        oversized = bytearray(cpio_bytes({"file": b"data"}))
        oversized[54:62] = b"ffffffff"
        with self.assertRaisesRegex(CI.PublicError, "bound"):
            CI.Payloads(self.work / "rpm-member-size", ()).add(io.BytesIO(rpm_bytes(bytes(oversized))))
        self.assertFalse((self.work / "escape").exists())

    def test_rpm_expansion_count_depth_and_total_limits_are_preserved(self):
        data = rpm_bytes(cpio_bytes({"file": b"x" * 4096}))
        for label, attribute, limit, depth in (("file", "MAX_FILE", 1024, 0),
                                               ("total", "MAX_TOTAL", len(data), 0),
                                               ("count", "MAX_FILES", 1, 0),
                                               ("depth", "MAX_FILES", CI.MAX_FILES, 8)):
            with self.subTest(label=label), patch.object(CI, attribute, limit), self.assertRaisesRegex(CI.PublicError, "limit"):
                CI.Payloads(self.work / f"rpm-limit-{label}", ()).add(io.BytesIO(data), depth)

    def test_rpm_support_keeps_other_unsupported_archives_blocked(self):
        for index, data in enumerate((b"7z\xbc\xaf\x27\x1cfixture", b"Rar!fixture")):
            with self.subTest(index=index), self.assertRaisesRegex(CI.PublicError, "Unsupported nested"):
                CI.Payloads(self.work / f"unsupported-{index}", ()).add(io.BytesIO(data))

    def test_secret_variants_are_detected_across_stream_boundaries(self):
        for value in CI.variants({"CI_SECRET_CANARY": SECRET, "REGISTRY_USERNAME": "robot"}):
            with self.subTest(length=len(value)), self.assertRaises(CI.PublicError):
                CI.inspect_known(io.BytesIO(b"x" * (1024 * 1024 - 4) + value + b"suffix"), (value,))

    def test_child_environment_excludes_credentials(self):
        with patch.dict(os.environ, REGISTRY_PASSWORD=SECRET, CI_SECRET_CANARY=SECRET, GH_TOKEN=SECRET):
            child = CI.child_env(self.work)
        self.assertNotIn(SECRET, json.dumps(child))
        self.assertEqual(child["REGISTRY_AUTH_FILE"], str(self.work / "auth.json"))

    def test_failed_subprocess_output_is_captured_without_echoing_secrets(self):
        private = CI.new_private(self.work / "capture")

        def run(argv, **kwargs):
            kwargs["stdout"].write(SECRET.encode())
            kwargs["stderr"].write(SECRET.encode())
            self.assertNotIn("REGISTRY_PASSWORD", kwargs["env"])
            return subprocess.CompletedProcess(argv, 1)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(CI.subprocess, "run", side_effect=run), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                self.assertRaises(CI.PublicError) as caught:
            CI.captured("build", ["synthetic-build"], private)
        self.assertNotIn(SECRET, str(caught.exception) + stdout.getvalue() + stderr.getvalue())
        self.assertEqual((private / "build.stderr").read_text(), SECRET)

    def test_tampered_evidence_prevents_login_and_push(self):
        _, output, _, _ = self.build()
        (output / "public/report.txt").write_text("changed after inspection")
        self.commands.clear()
        with patch.object(CI, "captured", side_effect=self.captured), \
                self.assertRaisesRegex(CI.PublicError, "changed"):
            CI.publish("codex-ai-helpers", "devel", output)
        self.assertEqual(self.commands, [])

    def test_new_registry_secret_is_checked_against_already_inspected_content(self):
        _, output, _, _ = self.build()
        # This public source value is deliberately selected as a synthetic secret
        # only at publication, after the initial scan completed.
        with patch.dict(os.environ, REGISTRY_PASSWORD="public build fixture"), \
                patch.object(CI, "captured", side_effect=self.captured), \
                self.assertRaisesRegex(CI.PublicError, "Known credential"):
            CI.publish("codex-ai-helpers", "devel", output)

    def test_publication_uses_immutable_identity_stdin_login_and_cleans_auth(self):
        _, output, _, _ = self.build()
        self.commands.clear()
        with patch.dict(os.environ, REGISTRY_USERNAME="robot", REGISTRY_PASSWORD=SECRET), \
                patch.object(CI, "captured", side_effect=self.captured):
            result = CI.publish("codex-ai-helpers", "devel", output)
        self.assertEqual(len(result["tags"]), 1)
        login = next(item for item in self.commands if item[0] == "login")
        push = next(item for item in self.commands if item[0].startswith("push-"))
        self.assertNotIn(SECRET, " ".join(login[1]))
        self.assertEqual(login[2]["input"], (SECRET + "\n").encode())
        self.assertIn(self.image_id, push[1])
        self.assertFalse((output / "private/publication/auth.json").exists())

    def test_unknown_project_or_stale_results_cannot_build(self):
        with self.assertRaises(CI.PublicError):
            CI.build("../aws-ee", "devel", self.work / "bad")
        self.assertFalse((self.work / "bad").exists())
        self.build()
        with self.assertRaisesRegex(CI.PublicError, "fresh"):
            self.build()

    def test_partial_production_push_retains_completed_tag_and_removes_auth(self):
        _, output, _, _ = self.build(channel="production")

        def fail_second(stage, argv, private, **kwargs):
            if stage == "push-1":
                raise CI.PublicError("push failed; raw diagnostics retained privately")
            return self.captured(stage, argv, private, **kwargs)

        with patch.dict(os.environ, REGISTRY_USERNAME="robot", REGISTRY_PASSWORD=SECRET), \
                patch.object(CI, "captured", side_effect=fail_second), self.assertRaises(CI.PublicError):
            CI.publish("codex-ai-helpers", "production", output)
        evidence = json.loads((output / "private/publication/published.json").read_text())
        self.assertEqual(evidence["tags"], ["quay.io/s4v0/codex-ai-helpers:latest"])
        self.assertFalse((output / "private/publication/auth.json").exists())


class VerifierIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.source = self.work / "source"
        (self.source / "k8s-launch-kit").mkdir(parents=True)
        self.verifier = self.source / "k8s-launch-kit/public-evidence.py"
        self.private = self.work / "private"
        self.private.mkdir(mode=0o700)
        (self.private / "home").mkdir()
        self.results = self.work / "results with spaces"
        self.record = dict(version="v1.2.3", sourceRevision=COMMIT, buildDate="2026-11-18T00:00:00Z",
                           imageID="sha256:" + "b" * 64, lockSHA256="c" * 64, sourceTreeDigest="d" * 64,
                           platform="linux/amd64", builderImage="docker.io/example/builder@sha256:" + "e" * 64,
                           runtimeImage="nvcr.io/example/runtime@sha256:" + "f" * 64)

    def invoke(self):
        with patch.object(CI, "ROOT", self.source):
            return CI.l8k_evidence(self.results, self.private)

    def test_verifier_executes_out_of_process_without_credentials_and_captures_output(self):
        self.verifier.write_text("import json, os, sys\n"
            "assert sys.argv[1:] == ['verified-json', '--results', " + repr(str(self.results)) + "]\n"
            "assert not any(k.startswith('CI_SECRET_') or k in ('REGISTRY_PASSWORD', 'GH_TOKEN', 'GITHUB_TOKEN') for k in os.environ)\n"
            "print(" + repr(SECRET) + ", file=sys.stderr)\n"
            "print(" + repr(json.dumps(self.record)) + ")\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, CI_SECRET_GITHUB_TOKEN=SECRET, REGISTRY_PASSWORD=SECRET,
                        GH_TOKEN=SECRET, GITHUB_TOKEN=SECRET), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(self.invoke(), self.record)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        self.assertEqual((self.private / "l8k-verifier.stderr").read_text(), SECRET + "\n")

    def test_failed_verifier_keeps_stdout_stderr_and_exception_content_private(self):
        self.verifier.write_text("import sys\nprint(" + repr(SECRET) + ")\nprint(" + repr(SECRET)
                                 + ", file=sys.stderr)\nsys.exit(17)\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                self.assertRaises(CI.PublicError) as error:
            self.invoke()
        self.assertNotIn(SECRET, stdout.getvalue() + stderr.getvalue() + str(error.exception))
        for name in ("stdout", "stderr"):
            self.assertEqual((self.private / ("l8k-verifier." + name)).read_text(), SECRET + "\n")

    def test_noisy_malformed_oversized_duplicate_and_invalid_metadata_are_rejected(self):
        valid = json.dumps(self.record).encode()
        cases = [SECRET.encode() + valid, b"[]", b"{}", b"\xff", b" " * 16385,
                 valid[:-1] + b', "version": "v9.9.9"}',
                 json.dumps({**self.record, "extra": SECRET}).encode(),
                 json.dumps({**self.record, "buildDate": "2026-99-99T00:00:00Z"}).encode()]
        cases += [json.dumps({**self.record, key: SECRET}).encode() for key in self.record]
        cases += [json.dumps({**self.record, "imageID": None}).encode()]
        for index, raw in enumerate(cases):
            self.verifier.write_text("import sys\nsys.stdout.buffer.write(" + repr(raw) + ")\n")
            with self.subTest(case=index), self.assertRaises(CI.PublicError) as error:
                self.invoke()
            self.assertNotIn(SECRET, str(error.exception))


class ImageSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.event = self.work / "event.json"
        self.event.write_text(json.dumps({"inputs": {"image_name": "all"}}))
        self.environment = dict(GITHUB_EVENT_PATH=str(self.event), GITHUB_EVENT_NAME="workflow_dispatch",
                               GITHUB_REPOSITORY="heatmiser/ee-builds", GITHUB_SHA=COMMIT,
                               GITHUB_REF="refs/heads/main", GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.environment).start()

    def prepare(self, channel="devel", paths=None):
        def captured(stage, argv, private, **kwargs):
            path = self.work / (stage + ".txt")
            path.write_text(COMMIT if stage == "source" else "\n".join(paths or []))
            return path
        with patch.object(CI, "captured", side_effect=captured):
            return CI.prepare(channel, self.work)

    def test_manual_builds_select_every_one_of_the_five_and_all(self):
        for channel in ("devel", "production"):
            for image in (*CI.IMAGES, "all"):
                self.event.write_text(json.dumps({"inputs": {"image_name": image}}))
                selected = self.prepare(channel)["matrix"]["include"]
                self.assertEqual({p["image"] for p in selected}, set(CI.IMAGES) if image == "all" else {image})

    def test_manual_unknown_ee_and_shell_input_are_rejected(self):
        for image in ("aws-ee", "../codex-ai-helpers", "codex-ai-helpers; echo canary", ""):
            self.event.write_text(json.dumps({"inputs": {"image_name": image}}))
            with self.subTest(image=image), self.assertRaises(CI.PublicError):
                self.prepare()

    def push(self):
        os.environ["GITHUB_EVENT_NAME"] = "push"
        self.event.write_text(json.dumps({"before": BASE, "after": COMMIT}))

    def test_automatic_changes_select_affected_image_and_shared_changes_select_all(self):
        self.push()
        self.assertEqual(self.prepare("production", ["codex-ai-helpers/Containerfile"])["matrix"],
                         {"include": [{"image": "codex-ai-helpers"}]})
        selected = self.prepare("production", [".github/scripts/ci_publication.py"])["matrix"]["include"]
        self.assertEqual({p["image"] for p in selected}, set(CI.IMAGES))
        self.assertEqual(self.prepare("production", ["aws-ee/requirements.yml"])["matrix"], {"include": []})

    def test_production_topic_branch_and_fork_pr_are_rejected(self):
        os.environ["GITHUB_REF"] = "refs/heads/topic"
        with self.assertRaisesRegex(CI.PublicError, "main"):
            CI.context("production")
        os.environ["GITHUB_EVENT_NAME"] = "pull_request"
        self.event.write_text(json.dumps({"number": 1, "pull_request": {
            "head": {"sha": COMMIT, "repo": {"full_name": "fork/ee-builds"}}, "base": {"sha": BASE}}}))
        with self.assertRaisesRegex(CI.PublicError, "same-repository"):
            CI.context("devel")

    def test_cli_failure_never_echoes_arbitrary_exception(self):
        stderr = io.StringIO()
        argv = ["ci_publication.py", "build", "--channel", "devel", "--project", "codex-ai-helpers",
                "--output", str(self.work / "results")]
        with patch.object(sys, "argv", argv), patch.object(CI, "build", side_effect=RuntimeError(SECRET)), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(CI.main(), 1)
        self.assertNotIn(SECRET, stderr.getvalue())

    def test_workflows_enable_builds_hold_publication_and_offer_all_five(self):
        for filename in ("pr-image-build.yml", "push-image-build.yml"):
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            self.assertEqual(workflow["permissions"], {"contents": "read"})
            choices = workflow["on"]["workflow_dispatch"]["inputs"]["image_name"]["options"]
            self.assertEqual(set(choices), {"all", *CI.IMAGES})
            job = workflow["jobs"]["build-image"]
            self.assertIn("outputs.length != '0'", job["if"])
            build = next(s for s in job["steps"] if s["name"] == "Build, verify and scan image")
            self.assertIn("ci_publication.py build", build["run"])
            upload = next(s for s in job["steps"] if s["name"] == "Upload build artifact")
            self.assertIn("steps.secret_files_0.outcome == 'success'", upload["if"])
            paths = upload["with"]["path"].splitlines()
            self.assertEqual({p.rsplit("/", 1)[1] for p in paths}, CI.PUBLIC_FILES)
            self.assertTrue(all("/public/" in p and "*" not in p for p in paths))
            publish = next(s for s in job["steps"] if s["name"] == "Publish inspected image after hosted qualification")
            self.assertEqual(publish["if"], "${{ false }}")
            self.assertTrue(all(s.get("with", {}).get("persist-credentials") == "false"
                                for j in workflow["jobs"].values() for s in j["steps"]
                                if s.get("uses", "").startswith("actions/checkout@")))


if __name__ == "__main__":
    unittest.main()
