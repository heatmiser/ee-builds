#!/usr/bin/env python3
"""Build plain images with captured diagnostics and checked, metadata-only outputs."""

import argparse
import base64
import bz2
from datetime import datetime
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.parse
import zipfile

import ci_report
import ci_secrets


ROOT = Path(__file__).resolve().parents[2]
IMAGES = ("antigravity-ai-helpers", "codex-ai-helpers", "gemini-ai-helpers",
          "opencode-ai-helpers", "k8s-launch-kit")
PUBLIC_FILES = {"receipt.json", "report.txt", "SHA256SUMS"}
MAX_FILE = 2 * 1024**3
MAX_TOTAL = 16 * 1024**3
MAX_FILES = 500000


class PublicError(ValueError):
    """Only fixed diagnostics from this module may reach workflow output."""


class CapturedFailure(PublicError):
    """Captured tool failure with a fixed public diagnostic."""


def require(condition, message):
    if not condition:
        raise PublicError(message)


def read_json(path):
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 4 * 1024**2,
            "Metadata file is missing, unsafe or oversized")
    return json.loads(path.read_text())


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def child_env(private):
    # Registry auth is isolated outside the build context. Do not forward tokens
    # to builds, checks, or Git clones.
    allowed = ("PATH", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "TMPDIR")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(HOME=str(private / "home"), REGISTRY_AUTH_FILE=str(private / "auth.json"))
    return env


def captured(stage, argv, private, *, cwd=None, input=None, timeout=3600):
    require(re.fullmatch(r"[a-z0-9-]+", stage), "Invalid operation name")
    with (private / (stage + ".stdout")).open("wb") as out, (private / (stage + ".stderr")).open("wb") as err:
        try:
            result = subprocess.run(argv, cwd=cwd, input=input, stdout=out, stderr=err,
                                    env=child_env(private), timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            raise CapturedFailure(stage + " could not complete; raw diagnostics retained privately") from None
    if result.returncode != 0:
        raise CapturedFailure(stage + " failed; raw diagnostics retained privately")
    return private / (stage + ".stdout")


def project_name(value):
    require(value in IMAGES, "Unknown plain-container project")
    directory = ROOT / value
    require(not directory.is_symlink() and directory.is_dir()
            and not (directory / "Containerfile").is_symlink() and (directory / "Containerfile").is_file(),
            "Plain-container definition is missing or unsafe")
    return value


def context(channel):
    require(channel in ("devel", "production"), "Invalid build channel")
    event_path = Path(os.environ.get("GITHUB_EVENT_PATH", "/nonexistent"))
    event = read_json(event_path)
    name = os.environ.get("GITHUB_EVENT_NAME")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    require(re.fullmatch(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+", repository), "Invalid source repository")
    sha = os.environ.get("GITHUB_SHA", "")
    before = None
    if name == "pull_request":
        require(channel == "devel", "Production builds require main")
        pr = event["pull_request"]
        require(pr["head"]["repo"]["full_name"] == repository, "Image builds require a same-repository PR")
        sha, before = pr["head"]["sha"], pr["base"]["sha"]
        number = event["number"]
        require(type(number) is int and number > 0, "Invalid PR number")
        tag = f"pr-{number}-{sha[:7]}"
    elif name in ("push", "workflow_dispatch"):
        require(name != "push" or channel == "production", "Invalid build event")
        if channel == "production":
            require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Production builds require main")
        if name == "push":
            require(event["after"] == sha, "Push source differs from workflow source")
            before = event["before"]
        run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
        require(re.fullmatch(r"[1-9][0-9]*", run_id) and re.fullmatch(r"[1-9][0-9]*", attempt),
                "Invalid workflow identity")
        tag = "latest" if channel == "production" else f"manual-{run_id}-{attempt}-{sha[:7]}"
    else:
        raise PublicError("Unsupported image build event")
    require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha), "Invalid source commit")
    if before is not None:
        require(isinstance(before, str) and re.fullmatch(r"[a-f0-9]{40}", before), "Invalid base commit")
    return {"channel": channel, "commit": sha, "before": before, "tag": tag,
            "repository": repository, "event": name}


def prepare(channel, private):
    data = context(channel)
    head = captured("source", ["git", "rev-parse", "HEAD"], private, cwd=ROOT).read_text().strip()
    require(head == data["commit"], "Checkout differs from requested source")
    if data["event"] == "workflow_dispatch":
        event = read_json(Path(os.environ["GITHUB_EVENT_PATH"]))
        selected = event.get("inputs", {}).get("image_name", "")
        images = list(IMAGES) if selected == "all" else [project_name(selected)]
    else:
        before = data["before"]
        argv = (["git", "ls-tree", "-r", "--name-only", head] if before == "0" * 40
                else ["git", "diff", "--name-only", before, head])
        paths = captured("changed-paths", argv, private, cwd=ROOT).read_text().splitlines()
        shared = {".github/scripts/ci_publication.py", ".github/scripts/ci_report.py",
                  ".github/workflows/pr-image-build.yml", ".github/workflows/push-image-build.yml",
                  ".github/scripts/ci_secrets.py", "tests/test_ci_secrets.py"}
        images = list(IMAGES) if shared.intersection(paths) else sorted(
            {name for name in IMAGES if any(p.startswith(name + "/") for p in paths)})
    for image in images:
        project_name(image)
    return {"matrix": {"include": [{"image": name} for name in images]}, "commit": head}


def variants(environment=None):
    try:
        return ci_secrets.patterns(environment)
    except ci_secrets.GuardError:
        raise PublicError("Known credential exceeds inspection bound") from None


def inspect_known(stream, secrets):
    try:
        ci_secrets.inspect_stream(stream, secrets)
    except ci_secrets.GuardError:
        raise PublicError("Known credential found; publication denied") from None


class Payloads:
    """Expand common archives to numeric files without following container links."""

    def __init__(self, directory, secrets):
        self.root, self.secrets = directory, secrets
        directory.mkdir(mode=0o700)
        self.total = self.count = 0

    def add(self, stream, depth=0):
        self.count += 1
        require(self.count <= MAX_FILES and depth <= 8, "Archive inspection limit exceeded")
        target = self.root / str(self.count)
        size = 0
        with target.open("wb") as out:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                self.total += len(chunk)
                require(size <= MAX_FILE and self.total <= MAX_TOTAL, "Payload inspection size limit exceeded")
                out.write(chunk)
        with target.open("rb") as content:
            inspect_known(content, self.secrets)
        with target.open("rb") as content:
            magic = content.read(8)
        expanded = False
        if magic.startswith(b"PK\x03\x04"):
            expanded = True
            with zipfile.ZipFile(target) as archive:
                for item in archive.infolist():
                    require(item.file_size <= MAX_FILE, "Archive member exceeds inspection bound")
                    self.metadata(item.filename, "", depth)
                    if not item.is_dir():
                        with archive.open(item) as content:
                            self.add(content, depth + 1)
        elif magic.startswith((b"\x1f\x8b", b"\xfd7zXZ\x00", b"BZh")):
            expanded = True
            opener = gzip.open if magic.startswith(b"\x1f\x8b") else lzma.open if magic.startswith(b"\xfd7zXZ") else bz2.open
            with opener(target, "rb") as content:
                self.add(content, depth + 1)
        elif tarfile.is_tarfile(target):
            expanded = True
            self.tar(target, depth)
        elif magic.startswith((b"7z\xbc\xaf\x27\x1c", b"Rar!", b"\xed\xab\xee\xdb")):
            raise PublicError("Unsupported nested archive; publication denied")
        if expanded:
            target.unlink()

    def metadata(self, name, link, depth, **extra):
        require(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts
                and "\\" not in name, "Unsafe archive member path")
        import io
        payload = json.dumps({"name": name, "link": link, **extra}).encode()
        require(len(payload) <= 4 * 1024**2, "Archive metadata exceeds inspection bound")
        self.total += len(payload)
        require(self.total <= MAX_TOTAL, "Payload inspection size limit exceeded")
        # Metadata is scanned as data, never used as an extraction path.
        self.count += 1
        require(self.count <= MAX_FILES, "Archive inspection limit exceeded")
        with io.BytesIO(payload) as content:
            inspect_known(content, self.secrets)
        (self.root / str(self.count)).write_bytes(payload)

    def tar(self, target, depth=0):
        with tarfile.open(target, "r:*") as archive:
            for item in archive:
                self.metadata(item.name, item.linkname, depth, pax=item.pax_headers,
                              uname=item.uname, gname=item.gname)
                if item.isfile():
                    require(item.size <= MAX_FILE, "Archive member exceeds inspection bound")
                    with archive.extractfile(item) as stream:
                        self.add(stream, depth + 1)


def inspect_oci(directory, payloads, expected_id):
    require(not directory.is_symlink() and not any(p.is_symlink() for p in directory.rglob("*")), "Unsafe OCI layout")
    require(read_json(directory / "oci-layout") == {"imageLayoutVersion": "1.0.0"}, "Invalid OCI layout version")
    index = read_json(directory / "index.json")
    require(index.get("schemaVersion") == 2 and len(index.get("manifests", [])) == 1, "Require one OCI manifest")
    referenced = {"oci-layout", "index.json"}

    def blob(descriptor):
        value = descriptor["digest"]
        require(isinstance(value, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", value), "Invalid OCI digest")
        path = directory / "blobs/sha256" / value[7:]
        require(path.is_file() and type(descriptor["size"]) is int and path.stat().st_size == descriptor["size"]
                and path.stat().st_size <= MAX_FILE and digest(path) == value[7:], "OCI blob integrity check failed")
        referenced.add(path.relative_to(directory).as_posix())
        return path

    manifest_path = blob(index["manifests"][0])
    manifest = read_json(manifest_path)
    config_path = blob(manifest["config"])
    require(manifest["config"]["digest"] == expected_id, "OCI configuration differs from built image")
    config = read_json(config_path)
    layers = manifest.get("layers")
    require(isinstance(layers, list) and bool(layers), "OCI manifest has no layers")
    diff_ids = config["rootfs"]["diff_ids"]
    require(len(diff_ids) == len(layers), "OCI layer count differs from configuration")
    for path in (directory / "oci-layout", directory / "index.json", manifest_path, config_path):
        with path.open("rb") as stream:
            payloads.add(stream)
    for descriptor, diff_id in zip(layers, diff_ids):
        path = blob(descriptor)
        media = descriptor["mediaType"]
        require(media in ("application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip"),
                "Unsupported OCI layer compression")
        # Save requests uncompressed layers. Reject alternate formats instead of
        # silently overlooking layer data; hosted qualification verifies transport.
        require(media == "application/vnd.oci.image.layer.v1.tar" and diff_id == "sha256:" + digest(path),
                "OCI uncompressed layer integrity check failed")
        with path.open("rb") as stream:
            inspect_known(stream, payloads.secrets)
        payloads.tar(path)
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
    require(actual == referenced, "OCI layout contains unreferenced files")
    return len(layers)


def new_private(output):
    temp = Path(os.environ.get("RUNNER_TEMP", "/nonexistent")).resolve()
    require(output.is_absolute() and temp in output.resolve().parents and ROOT not in output.resolve().parents,
            "Results must be outside source and within runner temporary storage")
    require(not output.exists() and output.resolve() == output, "Results directory must be fresh and canonical")
    output.mkdir(mode=0o700)
    private = output / "private"
    private.mkdir(mode=0o700)
    (private / "home").mkdir(mode=0o700)
    (private / "auth.json").write_text('{"auths":{}}\n')
    return private


def tree_digest(directory):
    result = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        require(not path.is_symlink(), "Unsafe inspection evidence path")
        if path.is_file():
            result.update(path.relative_to(directory).as_posix().encode() + b"\x00")
            result.update(digest(path).encode() + b"\n")
    return result.hexdigest()


def l8k_evidence(results, private):
    path = captured("l8k-verifier", [sys.executable, str(ROOT / "k8s-launch-kit/public-evidence.py"),
                                   "verified-json", "--results", str(results)], private, timeout=120)

    def unique_fields(pairs):
        record = {}
        for key, value in pairs:
            require(key not in record, "Invalid L8K verifier output")
            record[key] = value
        return record

    require(not path.is_symlink() and path.is_file(), "Invalid L8K verifier output")
    with path.open("rb") as stream:
        raw = stream.read(16385)
    require(len(raw) <= 16384, "L8K verifier output exceeds bound")
    try:
        record = json.loads(raw, object_pairs_hook=unique_fields)
    except (ValueError, UnicodeError):
        raise PublicError("Invalid L8K verifier output") from None
    patterns = {
        "version": r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?",
        "sourceRevision": r"[a-f0-9]{40}", "buildDate": r"[0-9]{4}(?:-[0-9]{2}){2}T[0-9]{2}(?::[0-9]{2}){2}Z",
        "imageID": r"sha256:[a-f0-9]{64}", "lockSHA256": r"[a-f0-9]{64}",
        "sourceTreeDigest": r"[a-f0-9]{64}", "platform": r"linux/amd64",
        "builderImage": r"[a-z0-9][a-zA-Z0-9./:_-]*@sha256:[a-f0-9]{64}",
        "runtimeImage": r"[a-z0-9][a-zA-Z0-9./:_-]*@sha256:[a-f0-9]{64}",
    }
    require(isinstance(record, dict) and set(record) == set(patterns), "Invalid L8K public evidence fields")
    require(all(isinstance(record[key], str) and re.fullmatch(pattern, record[key])
                for key, pattern in patterns.items()), "Invalid L8K public evidence values")
    try:
        datetime.strptime(record["buildDate"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise PublicError("Invalid L8K public evidence values") from None
    return record


def normalize_image_id(value):
    match = re.fullmatch(r"(?:sha256:)?([a-f0-9]{64})", value) if isinstance(value, str) else None
    require(match is not None, "Invalid local image identity")
    return "sha256:" + match[1]


def build(project, channel, output):
    project_name(project)
    data = context(channel)
    private = new_private(output)
    head = captured("source", ["git", "rev-parse", "HEAD"], private, cwd=ROOT).read_text().strip()
    require(head == data["commit"], "Checkout differs from requested source")
    local = project + ":" + data["tag"]
    l8k = None
    if project == "k8s-launch-kit":
        captured("build", ["bash", str(ROOT / project / "ci-build.sh"), str(private / "l8k"),
                            project, data["tag"], head], private, cwd=ROOT / project)
    else:
        captured("build", ["podman", "build", "--tag", local, "--tag", project + ":" + head, "."],
                 private, cwd=ROOT / project)
    image_id = normalize_image_id(captured("image-id", ["podman", "image", "inspect", "--format", "{{.Id}}", local],
                                          private).read_text().strip())
    if project == "k8s-launch-kit":
        l8k = l8k_evidence(private / "l8k", private)
        require(set(l8k) == {"version", "sourceRevision", "buildDate", "imageID", "lockSHA256",
                             "sourceTreeDigest", "platform", "builderImage", "runtimeImage"},
                "Invalid L8K public evidence fields")
        require(l8k["imageID"] == image_id, "L8K evidence differs from built image")
    else:
        namespace = data["repository"].split("/")[0].lower() if channel == "devel" else "s4v0"
        registry = "ghcr.io" if channel == "devel" else "quay.io"
        checks = private / "checks"

        def run(argv, **kwargs):
            kwargs["env"] = child_env(private)
            return subprocess.run(argv, **kwargs)

        container_env = {"AGY_HEADLESS": "1", "AGY_TRUST_WORKSPACE": "true"} if project == "antigravity-ai-helpers" else {}
        status = ci_report.generate("image", local, f"{registry}/{namespace}/{local}", head, checks,
                                    run=run, network="none", container_env=container_env)
        require(status == 0, "Image runtime checks failed; raw diagnostics retained privately")
    oci = private / "oci"
    captured("export", ["podman", "save", "--format", "oci-dir", "--uncompressed", "--output", str(oci), image_id], private)
    secrets = variants()
    payloads = Payloads(private / "payloads", secrets)
    layer_count = inspect_oci(oci, payloads, image_id)
    record = {"schema_version": 2, "project": project, "channel": channel, "source_commit": head,
              "tag": data["tag"], "image_id": image_id, "image_layers": layer_count,
              "runtime_checks": "PASS", "credential_check": "PASS", "upstream_scan": "NOT_RUN"}
    if l8k is not None:
        record["l8k"] = l8k
    candidate = private / "candidate"
    candidate.mkdir(mode=0o700)
    (candidate / "receipt.json").write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    (candidate / "report.txt").write_text(
        f'Image: {project}\nSource: {head}\nLocal image: {image_id}\n'
        f'Runtime checks: PASS\nWorkflow credential check: PASS\nUpstream pattern scan: NOT_RUN\nLayers inspected: {layer_count}\n'
        'Publication: not performed by this build step\n')
    (candidate / "SHA256SUMS").write_text(''.join(digest(candidate / n) + "  " + n + "\n"
                                                 for n in ("receipt.json", "report.txt")))
    require({p.name for p in candidate.iterdir()} == PUBLIC_FILES
            and all(p.is_file() and not p.is_symlink() for p in candidate.iterdir()), "Invalid public file set")
    for path in candidate.iterdir():
        with path.open("rb") as stream:
            inspect_known(stream, secrets)
    # Check captured diagnostics against our credentials. Upstream-content scanning
    # is a separate review concern, not part of this credential-value gate.
    logs = private / "logs-to-scan"
    logs.mkdir(mode=0o700)
    for path in private.glob("*.stdout"):
        shutil.copyfile(path, logs / path.name)
    for path in private.glob("*.stderr"):
        shutil.copyfile(path, logs / path.name)
    if (private / "checks/private-logs").exists():
        shutil.copytree(private / "checks/private-logs", logs / "runtime")
    if project == "k8s-launch-kit":
        for index, path in enumerate(sorted((private / "l8k").rglob("*"))):
            if path.is_file() and path.suffix in (".log", ".stdout", ".stderr"):
                require(not path.is_symlink(), "Unsafe L8K diagnostic path")
                shutil.copyfile(path, logs / ("l8k-" + str(index)))
    for path in logs.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                inspect_known(stream, secrets)
    shutil.copytree(candidate, output / "public")
    inspection = {name: tree_digest(path) for name, path in (
        ("oci", oci), ("payloads", payloads.root), ("public", output / "public"), ("logs", logs))}
    (private / "inspection.json").write_text(json.dumps(inspection, sort_keys=True) + "\n")
    return record


def publish(project, channel, output):
    project_name(project)
    data = context(channel)
    temp = Path(os.environ.get("RUNNER_TEMP", "/nonexistent")).resolve()
    require(output.is_absolute() and temp in output.resolve().parents and output.resolve() == output,
            "Invalid inspected results directory")
    require(not output.is_symlink(), "Unsafe results directory")
    private, public = output / "private", output / "public"
    require(not private.is_symlink() and not public.is_symlink(), "Unsafe inspected results path")
    state = read_json(private / "inspection.json")
    require(set(state) == {"oci", "payloads", "public", "logs"}, "Invalid inspection binding")
    secrets = variants()
    for name in state:
        directory = public if name == "public" else private / ("logs-to-scan" if name == "logs" else name)
        require(directory.is_dir() and not directory.is_symlink() and state[name] == tree_digest(directory),
                "Inspected image or public evidence changed; publication denied")
        for path in directory.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    inspect_known(stream, secrets)
    require({p.name for p in public.iterdir()} == PUBLIC_FILES, "Invalid public file set")
    record = read_json(public / "receipt.json")
    fields = {"schema_version", "project", "channel", "source_commit", "tag", "image_id", "image_layers",
              "runtime_checks", "credential_check", "upstream_scan"}
    require(set(record) == fields | ({"l8k"} if project == "k8s-launch-kit" else set()), "Invalid image receipt fields")
    require(record["schema_version"] == 2 and record["project"] == project and record["channel"] == channel
            and record["source_commit"] == data["commit"] and record["tag"] == data["tag"]
            and record["upstream_scan"] == "NOT_RUN"
            and record["runtime_checks"] == record["credential_check"] == "PASS"
            and isinstance(record["image_id"], str) and re.fullmatch(r"sha256:[a-f0-9]{64}", record["image_id"]),
            "Image receipt differs from requested publication")
    username, password = os.environ.get("REGISTRY_USERNAME", ""), os.environ.get("REGISTRY_PASSWORD", "")
    require(bool(username) and bool(password), "Registry credentials are missing")
    work = private / "publication"
    work.mkdir(mode=0o700)
    (work / "home").mkdir(mode=0o700)
    registry = "ghcr.io" if channel == "devel" else "quay.io"
    namespace = data["repository"].split("/")[0].lower() if channel == "devel" else "s4v0"
    head = captured("source", ["git", "rev-parse", "HEAD"], work, cwd=ROOT).read_text().strip()
    require(head == data["commit"], "Checkout differs from inspected source")
    image_id = normalize_image_id(captured("image-id", ["podman", "image", "inspect", "--format", "{{.Id}}", record["image_id"]],
                                          work).read_text().strip())
    require(image_id == record["image_id"], "Local image differs from inspected image")
    destinations, manifest_digest = [], None
    try:
        captured("login", ["podman", "login", "--authfile", str(work / "auth.json"), "--username", username,
                           "--password-stdin", registry], work, input=(password + "\n").encode(), timeout=120)
        tags = [data["tag"]] if channel == "devel" else [data["tag"], data["commit"]]
        for index, tag in enumerate(tags):
            destination = f"{registry}/{namespace}/{project}:{tag}"
            digestfile = work / f"digest-{index}"
            captured("push-" + str(index), ["podman", "push", "--authfile", str(work / "auth.json"),
                "--digestfile", str(digestfile), image_id, "docker://" + destination], work)
            current = digestfile.read_text().strip()
            require(re.fullmatch(r"sha256:[a-f0-9]{64}", current) and manifest_digest in (None, current),
                    "Registry digest is invalid or differs across tags")
            manifest_digest = current
            destinations.append(destination)
            (work / "published.json").write_text(json.dumps({"tags": destinations, "digest": current}) + "\n")
    finally:
        (work / "auth.json").unlink(missing_ok=True)
    ci_secrets.inspect_bytes(json.dumps({"tags": destinations, "digest": manifest_digest}))
    return {"tags": destinations, "digest": manifest_digest}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "build", "publish"))
    parser.add_argument("--channel", required=True, choices=("devel", "production"))
    parser.add_argument("--project")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            private = new_private(args.output)
            result = prepare(args.channel, private)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
                stream.write("matrix=" + json.dumps(result["matrix"]) + "\n")
                stream.write("length=" + str(len(result["matrix"]["include"])) + "\n")
                stream.write("commit=" + result["commit"] + "\n")
            count = len(result["matrix"]["include"])
            print(f"Image selection validated: {count} build target(s)" if count else
                  "No relevant image changes; no build targets selected")
        elif args.command == "build":
            build(args.project, args.channel, args.output)
            print("Image built and inspected; metadata-only artifact prepared")
        else:
            publish(args.project, args.channel, args.output)
            print("Inspected image published; registry evidence retained privately")
        return 0
    except Exception as error:
        # Never interpolate arbitrary tool, archive, JSON or network exception text.
        detail = str(error) if isinstance(error, PublicError) else "Operation failed; raw content suppressed"
        print("ERROR: " + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
