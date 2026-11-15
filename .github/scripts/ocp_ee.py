#!/usr/bin/env python3
"""Build and publish configured OpenShift release streams with patch-only tags.

Network, container builds and publication are explicit subcommands. Unit tests
inject the downloader and command runner; importing this module has no effects.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".github/ocp-ee-streams.json"
STREAM_PATTERN = r"[1-9][0-9]*\.(?:0|[1-9][0-9]*)"
ARCHIVES = ("openshift-client-linux.tar.gz", "openshift-install-linux.tar.gz")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def configuration():
    config = json.loads(CONFIG.read_text())
    require(isinstance(config, dict), "Require an OpenShift stream configuration")
    mirror = config.get("mirror_root")
    require(isinstance(mirror, str), "Require an HTTPS mirror root")
    parsed = urlsplit(mirror)
    require(parsed.scheme == "https" and bool(parsed.netloc) and parsed.username is None
            and parsed.password is None and not parsed.query and not parsed.fragment,
            "Require an HTTPS mirror root without credentials, query or fragment")
    streams, legacy = config.get("streams"), config.get("legacy_streams")
    require(isinstance(streams, dict) and bool(streams) and isinstance(legacy, list),
            "Require configured streams and a legacy stream list")
    for stream, settings in streams.items():
        require(re.fullmatch(STREAM_PATTERN, stream) and isinstance(settings, dict), "Invalid configured stream")
        definition = settings.get("definition")
        require(isinstance(definition, str) and re.fullmatch(r"dcm-ocp-ee-[A-Za-z0-9_.-]+", definition),
                "Require a repository-local EE definition directory")
        require((ROOT / definition / "execution-environment.yml").is_file(), "Configured EE definition is absent")
        require(type(settings.get("automatic")) is bool, "Require an explicit automatic-build flag")
    require(all(isinstance(stream, str) and re.fullmatch(STREAM_PATTERN, stream) for stream in legacy)
            and len(set(legacy)) == len(legacy) and not set(legacy).intersection(streams),
            "Legacy streams must be distinct from configured versioned streams")
    return config


def stream_settings(stream):
    config = configuration()
    require(stream in config["streams"], f"OpenShift stream is not configured: {stream}")
    return config["streams"][stream]


def release_version(value):
    require(isinstance(value, str) and re.fullmatch(STREAM_PATTERN + r"\.(?:0|[1-9][0-9]*)", value),
            "Expected an exact OpenShift major.minor.patch release")
    return value


def release_stream(version):
    return release_version(version).rsplit(".", 1)[0]


def image_name(version):
    stream = release_stream(version)
    stream_settings(stream)
    return f"dcm-ocp-ee-{stream}"


def automation_matrix():
    config = configuration()
    entries = [{"ocp_version": stream, "versioned": False} for stream in config["legacy_streams"]]
    entries += [{"ocp_version": stream, "versioned": True}
                for stream, settings in config["streams"].items() if settings["automatic"]]
    return {"include": entries}


def release_matrix(value):
    versions = [release_version(part.strip()) for part in value.split(",")]
    require(1 <= len(versions) <= 16 and len(set(versions)) == len(versions),
            "Supply 1-16 distinct exact releases")
    for version in versions:
        stream_settings(release_stream(version))
    return {"include": [{"release": version, "stream": release_stream(version)} for version in versions]}


def read_metadata(url, opener=urllib.request.urlopen):
    with opener(url, timeout=60) as response:
        content = response.read(1024 * 1024 + 1)
    require(len(content) <= 1024 * 1024, "Oversized mirror metadata")
    return content.decode("utf-8")


def resolve_release(selection, opener=urllib.request.urlopen):
    stable = re.fullmatch(r"stable-(" + STREAM_PATTERN + r")", selection)
    if not stable:
        version = release_version(selection)
        stream_settings(release_stream(version))
        return version
    stream = stable[1]
    stream_settings(stream)
    mirror = configuration()["mirror_root"].rstrip("/")
    metadata = read_metadata(f"{mirror}/{selection}/release.txt", opener)
    matches = re.findall(r"(?m)^Name:[ \t]*(\S+)[ \t]*$", metadata)
    require(len(matches) == 1, "Stable release metadata must identify one exact release")
    version = release_version(matches[0])
    require(release_stream(version) == stream, "Stable metadata differs from requested stream")
    return version


def checksums(metadata):
    result = {}
    for line in metadata.splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *](?:\./)?([^\s]+)", line)
        if match and match[2] in ARCHIVES:
            require(match[2] not in result, "Duplicate archive checksum")
            result[match[2]] = match[1].lower()
    require(set(result) == set(ARCHIVES), "Mirror checksums must cover both tool archives")
    return result


def stage_archives(version, destination, opener=urllib.request.urlopen):
    version = release_version(version)
    stream_settings(release_stream(version))
    mirror = configuration()["mirror_root"].rstrip("/")
    expected = checksums(read_metadata(f"{mirror}/{version}/sha256sum.txt", opener))
    destination.mkdir(parents=True, exist_ok=False)
    for name in ARCHIVES:
        digest, size = hashlib.sha256(), 0
        with opener(f"{mirror}/{version}/{name}", timeout=60) as response:
            with (destination / name).open("xb") as stream:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    require(size <= 1024 * 1024 * 1024, "Oversized tool archive")
                    digest.update(block)
                    stream.write(block)
        require(size > 0 and digest.hexdigest() == expected[name], f"Checksum mismatch: {name}")
    (destination / "SHA256SUMS").write_text("".join(f"{expected[name]}  {name}\n" for name in ARCHIVES))
    return expected


def image_tags(version, selection, commit, run_id, pr=""):
    patch = release_version(version).rsplit(".", 1)[1]
    stable_selection = f"stable-{release_stream(version)}"
    require(selection in (version, stable_selection), "Release differs from build selection")
    require(re.fullmatch(r"[a-f0-9]{40}", commit), "Require full source commit SHA")
    require(re.fullmatch(r"[0-9]+-[0-9]+", run_id), "Require run-id and attempt, for example 12345-1")
    if pr:
        require(re.fullmatch(r"[1-9][0-9]*", pr), "Invalid PR number")
        return [f"{patch}-pr-{pr}-{commit[:12]}-{run_id}"]
    tags = [f"{patch}-{commit[:12]}-{run_id}", patch]
    if selection == stable_selection:
        tags.append("latest")
    return tags


def execute(argv, cwd=None):
    if argv[0] == "ansible-builder":
        with (Path(cwd).parent / "build.log").open("w") as log:
            subprocess.run(argv, cwd=cwd, check=True, text=True, stdout=log,
                           stderr=subprocess.STDOUT, timeout=3600)
        return ""
    return subprocess.run(argv, cwd=cwd, check=True, text=True, capture_output=True, timeout=3600).stdout


def pinned_base(reference, run=execute):
    require(re.fullmatch(r"(?:quay\.io|ghcr\.io)/[a-z0-9_-]+/dcm-base-ee(?::[A-Za-z0-9_.-]+|@sha256:[a-f0-9]{64})",
                         reference), "Require a Quay/GHCR dcm-base-ee reference")
    run(["podman", "pull", "--quiet", reference])
    digests = json.loads(run(["podman", "image", "inspect", "--format", "{{json .RepoDigests}}", reference]))
    repository = reference.split("@", 1)[0].split(":", 1)[0]
    candidates = [item for item in digests if re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", item)]
    require(bool(candidates), "Base image has no matching repository digest")
    if "@" in reference:
        require(reference in candidates, "Base image digest differs from request")
        return reference
    return candidates[0]


def verify_tools(version, installer, client):
    require(re.search(r"(?m)^openshift-install " + re.escape(version) + r"$", installer),
            "Final installer version differs from requested release")
    require(re.search(r"(?m)^Client Version:\s*" + re.escape(version) + r"\s*$", client),
            "Final oc client version differs from requested release")
    payloads = re.findall(r"(?m)^release image (\S+@sha256:[a-f0-9]{64})$", installer)
    require(len(payloads) == 1 and re.search(r"(?m)^release architecture (amd64|x86_64)$", installer),
            "Installer must report an immutable amd64 release payload")
    return payloads[0]


def write_receipt(path, record):
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def normalize_image_id(value):
    match = re.fullmatch(r"(?:sha256:)?([a-f0-9]{64})", value) if isinstance(value, str) else None
    require(match is not None, "Invalid local image ID")
    return match[1]


def build(selection, base, commit, run_id, output, pr="", run=execute, opener=urllib.request.urlopen):
    version = resolve_release(selection, opener)
    stream = release_stream(version)
    settings = stream_settings(stream)
    image = image_name(version)
    tags = image_tags(version, selection, commit, run_id, pr)
    require(run(["git", "rev-parse", "HEAD"], cwd=ROOT).strip() == commit,
            "Source checkout differs from requested commit")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    shutil.copytree(ROOT / settings["definition"], source)
    archive_hashes = stage_archives(version, source / "files/release", opener)
    base_digest = pinned_base(base, run)
    local_image = f"localhost/{image}:{tags[0]}"
    # --unsetlabel also removes the expiry inherited from dcm-base-ee.
    run(["ansible-builder", "build", "--container-runtime=podman", "--verbosity=3",
         "--file", "execution-environment.yml", "--context", str(output / "context"),
         "--tag", local_image, "--build-arg", f"OCP_VERSION={version}",
         "--build-arg", f"EE_BASE_IMAGE={base_digest}",
         "--extra-build-cli-args=--unsetlabel=quay.expires-after"], cwd=source)
    image_id = normalize_image_id(run(["podman", "image", "inspect", "--format", "{{.ID}}", local_image]).strip())
    installer = run(["podman", "run", "--rm", "--pull=never", image_id, "openshift-install", "version"])
    client = run(["podman", "run", "--rm", "--pull=never", image_id, "oc", "version", "--client"])
    payload = verify_tools(version, installer, client)
    labels = json.loads(run(["podman", "image", "inspect", "--format", "{{json .Config.Labels}}", image_id]))
    require(not labels.get("quay.expires-after") and labels.get("ocp.version") == version,
            "Final release image must record its version and have no expiry label")
    record = {"schema_version": 2, "verified": True, "selection": selection, "release": version,
              "stream": stream, "definition": settings["definition"],
              "mirror_root": configuration()["mirror_root"],
              "image": local_image, "image_id": image_id, "tags": tags, "pr": pr,
              "commit": commit, "run_id": run_id, "base_image": base_digest,
              "archive_sha256": archive_hashes, "release_image": payload,
              "verified_at": datetime.now(timezone.utc).isoformat()}
    write_receipt(output / "receipt.json", record)
    return output / "receipt.json"


def publish(receipt, registry, run=execute):
    require(re.fullmatch(r"(?:quay\.io|ghcr\.io)/[a-z0-9_-]+", registry), "Invalid image registry namespace")
    record = json.loads(receipt.read_text())
    require(record.get("schema_version") == 2 and record.get("verified") is True, "Require a verified v2 build receipt")
    image = image_name(record["release"])
    require(record["stream"] == release_stream(record["release"]), "Receipt stream differs from release")
    require(record["tags"] == image_tags(record["release"], record["selection"], record["commit"],
                                        record["run_id"], record["pr"]), "Receipt tags differ from build selection")
    require(record["image"] == f"localhost/{image}:{record['tags'][0]}", "Receipt image differs from release")
    require(bool(record["pr"]) == registry.startswith("ghcr.io/"), "PR builds publish only to GHCR; releases only to Quay")
    record["image_id"] = normalize_image_id(record["image_id"])
    # Recheck immutable local content before any tag can reach a registry.
    installer = run(["podman", "run", "--rm", "--pull=never", record["image_id"], "openshift-install", "version"])
    client = run(["podman", "run", "--rm", "--pull=never", record["image_id"], "oc", "version", "--client"])
    require(verify_tools(record["release"], installer, client) == record["release_image"], "Release payload changed")
    destinations, expected_digest = [], None
    for index, tag in enumerate(record["tags"]):
        destination = f"{registry}/{image}:{tag}"
        digestfile = receipt.parent / f"push-{index}.digest"
        require(not digestfile.exists(), "Push evidence already exists; inspect the previous publication")
        run(["podman", "push", "--digestfile", str(digestfile), record["image_id"], f"docker://{destination}"])
        digest = digestfile.read_text().strip()
        require(re.fullmatch(r"sha256:[a-f0-9]{64}", digest), "Registry returned an invalid digest")
        require(expected_digest in (None, digest), "Published tag digests differ")
        expected_digest = digest
        destinations.append(destination)
        record.update(published_tags=destinations, published_image=f"{registry}/{image}@{digest}")
        write_receipt(receipt, record)
    return record


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    matrix = commands.add_parser("matrix")
    matrix.add_argument("--versions", required=True)
    commands.add_parser("automation-matrix")
    base = commands.add_parser("resolve-base")
    base.add_argument("--reference", required=True)
    builder = commands.add_parser("build")
    for argument in ("release", "base", "commit", "run-id"):
        builder.add_argument("--" + argument, required=True)
    builder.add_argument("--output", required=True, type=Path)
    builder.add_argument("--pr", default="")
    publisher = commands.add_parser("publish")
    publisher.add_argument("--receipt", required=True, type=Path)
    publisher.add_argument("--registry", required=True)
    args = parser.parse_args()
    try:
        if args.command == "matrix":
            print(json.dumps(release_matrix(args.versions)))
        elif args.command == "automation-matrix":
            print(json.dumps(automation_matrix()))
        elif args.command == "resolve-base":
            print(pinned_base(args.reference))
        elif args.command == "build":
            receipt = build(args.release, args.base, args.commit, args.run_id, args.output, args.pr)
            print(receipt)
        else:
            record = publish(args.receipt, args.registry)
            summary = (f"OpenShift {record['release']} EE published\n\n"
                       + "\n".join(f"- `{tag}`" for tag in record["published_tags"])
                       + f"\n\nDeployment reference: `{record['published_image']}`\n"
                       + f"\nBase: `{record['base_image']}`\n"
                       + f"\nRelease payload: `{record['release_image']}`\n"
                       + f"\nArchive SHA256: `{json.dumps(record['archive_sha256'], sort_keys=True)}`\n"
                       + f"\nSource: `{record['commit']}`; run: `{record['run_id']}`\n")
            print(summary)
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
                    stream.write(summary)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        # Do not echo captured command output: inherited build steps may contain credentials.
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, f"ERROR: OCP EE {args.command} failed: {detail}; preserve build evidence.\n")


if __name__ == "__main__":
    main()
