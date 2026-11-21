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
import sys
import urllib.request
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ee_projects import CatalogError, is_active, load_catalog, require_active
import ci_secrets


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".github/ocp-ee-streams.json"
STREAM_PATTERN = r"[1-9][0-9]*\.(?:0|[1-9][0-9]*)"
ARCHIVES = ("openshift-client-linux.tar.gz", "openshift-install-linux.tar.gz")
LOGIN_REGISTRIES = ("quay.io", "ghcr.io", "registry.redhat.io")


class PublicError(ValueError):
    """An explicitly safe diagnostic, containing no arbitrary command output."""


def require(condition, message):
    if not condition:
        raise PublicError(message)


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


def check_stream(stream, config=None, catalog=None):
    config = configuration() if config is None else config
    catalog = load_catalog(ROOT) if catalog is None else catalog
    require(isinstance(stream, str) and re.fullmatch(STREAM_PATTERN, stream), "Invalid OpenShift stream")
    require(stream in config["streams"] or stream in config["legacy_streams"], "OpenShift stream is not configured")
    definition = config["streams"][stream]["definition"] if stream in config["streams"] else f"dcm-ocp-ee-{stream}"
    require_active(definition, catalog=catalog)
    return definition


def automation_matrix():
    config = configuration()
    catalog = load_catalog(ROOT)
    entries = []
    candidates = [(stream, False, f"dcm-ocp-ee-{stream}") for stream in config["legacy_streams"]]
    candidates += [(stream, True, settings["definition"]) for stream, settings in config["streams"].items()
                   if settings["automatic"]]
    for stream, versioned, definition in candidates:
        if is_active(definition, catalog):
            entries.append({"ocp_version": stream, "versioned": versioned})
        else:
            print(f"Skip example-only dependent EE: {definition}", file=sys.stderr)
    return {"include": entries}


def release_matrix(value):
    versions = [release_version(part.strip()) for part in value.split(",")]
    require(1 <= len(versions) <= 16 and len(set(versions)) == len(versions),
            "Supply 1-16 distinct exact releases")
    for version in versions:
        stream_settings(release_stream(version))
        check_stream(release_stream(version))
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
    if argv[0] == "ansible-builder" or argv[:2] == ["podman", "build"]:
        log_name = "builder.log" if argv[0] == "ansible-builder" else "build.log"
        with (Path(cwd).parent / log_name).open("w") as log:
            subprocess.run(argv, cwd=cwd, check=True, text=True, stdout=log,
                           stderr=subprocess.STDOUT, timeout=3600, env=ci_secrets.child_env())
        ci_secrets.inspect_paths([Path(cwd).parent / log_name])
        return ""
    result = subprocess.run(argv, cwd=cwd, check=True, text=True, capture_output=True, timeout=3600,
                            env=ci_secrets.child_env())
    ci_secrets.inspect_bytes(result.stdout or "")
    ci_secrets.inspect_bytes(result.stderr or "")
    return result.stdout


def registry_login(registry):
    """Authenticate without putting passwords in argv or publishing tool output."""
    require(registry in LOGIN_REGISTRIES, "Unsupported registry login target")
    environment = ci_secrets.child_env()
    username = os.environ.get("REGISTRY_USERNAME", "")
    password = os.environ.get("REGISTRY_PASSWORD", "")
    require(bool(username) and bool(password), "Registry login credentials missing")
    try:
        if os.environ.get("REGISTRY_AUTH_FILE"):
            ci_secrets.login(registry)
            return
        subprocess.run(["podman", "login", "--username", username, "--password-stdin", registry],
                       input=password + "\n", text=True, capture_output=True, check=True,
                       timeout=120, env=environment)
    except (OSError, subprocess.SubprocessError, ValueError):
        # Even exception strings may include credentials or captured tool output.
        raise PublicError(f"Registry login failed for {registry}; command output suppressed") from None


def base_diagnostic(stderr):
    """Redact arbitrary stderr, returning only fixed, allowlisted error reasons.

    Substitution-based masking cannot cover unknown credentials or encodings.
    Never interpolate original stderr, even when a recognized reason is present.
    """
    if not stderr or not stderr.strip():
        return "No stderr captured"
    lowered = stderr.lower()
    reasons = (
        (("unauthorized", "authentication required", "invalid username/password"), "Registry authentication failed"),
        (("access denied", "requested access", "permission denied", "forbidden"), "Access denied"),
        (("manifest unknown", "manifest not found", "name unknown"), "Image or manifest not found"),
        (("image not known", "no such image"), "Local image not found"),
        (("x509:", "tls handshake", "certificate verify failed"), "TLS or certificate failure"),
        (("no such host", "name resolution"), "DNS resolution failed"),
        (("timeout", "timed out", "deadline exceeded"), "Operation timed out"),
        (("connection refused", "connection reset", "network is unreachable"), "Network connection failed"),
        (("no space left on device", "disk quota exceeded"), "Storage space exhausted"),
        (("too many requests", "rate limit", "toomanyrequests"), "Registry rate limit reached"),
    )
    matched = [reason for markers, reason in reasons if any(marker in lowered for marker in markers)]
    return "; ".join(matched or ["Unrecognized error"]) + "; original stderr redacted"


def checked_operation(operation, argv, run=execute, cwd=None):
    """Expose fixed stage/reason labels, never argv, paths or exception text."""
    require(operation in {
        "source revision", "EE context generation", "EE build", "built image inspection", "installer verification",
        "client verification", "image label inspection", "publication installer verification",
        "publication client verification", "image push",
    }, "Unsupported subprocess operation")
    try:
        return run(argv, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.TimeoutExpired):
            status, diagnostic = "timed out", "Operation timed out"
        elif isinstance(error, OSError):
            code = error.errno if type(error.errno) is int else "unavailable"
            status, diagnostic = f"failed (OS error code {code})", "Subprocess could not execute"
        elif isinstance(error, subprocess.CalledProcessError):
            code = error.returncode if type(error.returncode) is int else "unavailable"
            status = f"failed (exit code {code})"
            # Context generation and Podman redirect both streams privately. Read
            # only its tail, after execute() has closed/flushed it. Never emit it.
            captured = []
            if operation in {"EE context generation", "EE build"} and cwd is not None:
                log_name = "builder.log" if operation == "EE context generation" else "build.log"
                try:
                    with (Path(cwd).parent / log_name).open("rb") as log:
                        log.seek(0, os.SEEK_END)
                        log.seek(max(0, log.tell() - 65536))
                        captured.append(log.read(65536).decode("utf-8", errors="replace"))
                except OSError:
                    pass
            for value in (error.stdout, error.stderr):
                if isinstance(value, bytes):
                    captured.append(value[-65536:].decode("utf-8", errors="replace"))
                elif isinstance(value, str):
                    captured.append(value[-65536:])
            diagnostic = base_diagnostic("\n".join(captured)).replace("stderr", "command output")
        else:
            status, diagnostic = "failed", "Unrecognized subprocess error"
        raise PublicError(f"{operation}: {status}; {diagnostic}") from None


def pinned_base(reference, run=execute):
    require(re.fullmatch(r"(?:quay\.io|ghcr\.io)/[a-z0-9_-]+/dcm-base-ee(?::[A-Za-z0-9_.-]+|@sha256:[a-f0-9]{64})",
                         reference), "Require a Quay/GHCR dcm-base-ee reference")
    # Only fixed classifications of these base-resolution failures are exposed.
    # Other commands retain the CLI's restricted error output.
    for operation, argv in (
        ("pull", ["podman", "pull", "--quiet", reference]),
        ("inspect", ["podman", "image", "inspect", "--format", "{{json .RepoDigests}}", reference]),
    ):
        try:
            result = run(argv)
        except subprocess.CalledProcessError as error:
            diagnostic = base_diagnostic(error.stderr)
            raise PublicError(f"Base image {operation} failed (exit code {error.returncode})"
                             f" for {reference}\nPodman: {diagnostic}") from None
    digests = json.loads(result)
    repository = reference.split("@", 1)[0].split(":", 1)[0]
    candidates = [item for item in digests if re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", item)]
    require(bool(candidates), "Base image has no matching repository digest")
    if "@" in reference:
        require(reference in candidates, "Base image digest differs from request")
        return reference
    return candidates[0]


def pr_matrix():
    settings = configuration().get("pr_qualification")
    require(isinstance(settings, dict), "Require PR qualification settings")
    versions = settings.get("versions")
    require(isinstance(versions, list) and 1 <= len(versions) <= 16,
            "Require 1-16 exact PR qualification releases")
    for version in versions:
        release_version(version)
    return release_matrix(",".join(versions))


def prepare_pr(run=execute):
    matrix = pr_matrix()
    settings = configuration()["pr_qualification"]
    base = settings.get("base_image")
    require(isinstance(base, str), "Require a published PR base image reference")
    return {"matrix": matrix, "base": pinned_base(base, run)}


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
    validate_receipt(record)
    content = json.dumps(record, indent=2, sort_keys=True) + "\n"
    ci_secrets.inspect_bytes(content)
    path.write_text(content)


def normalize_image_id(value):
    match = re.fullmatch(r"(?:sha256:)?([a-f0-9]{64})", value) if isinstance(value, str) else None
    require(match is not None, "Invalid local image ID")
    return match[1]


def validate_receipt(record):
    """Accept only bounded provenance fields suitable for a public artifact.

    Never include tool output, arbitrary image labels or unknown JSON fields.
    Validate before writing and before consuming an on-disk receipt for publish.
    """
    fields = {"schema_version", "verified", "selection", "release", "stream", "definition",
              "mirror_root", "image", "image_id", "tags", "pr", "commit", "run_id",
              "base_image", "archive_sha256", "release_image", "verified_at"}
    published = {"published_tags", "published_image"}
    require(isinstance(record, dict) and (set(record) == fields or set(record) == fields | published),
            "Receipt contains missing or unsupported fields")
    require(type(record["schema_version"]) is int and record["schema_version"] == 2
            and record["verified"] is True, "Require a verified v2 build receipt")
    require(all(isinstance(record[key], str) for key in fields - {
        "schema_version", "verified", "tags", "archive_sha256", "image_id"}), "Invalid receipt field type")
    version = release_version(record["release"])
    stream = release_stream(version)
    image = image_name(version)
    require(record["stream"] == stream, "Receipt stream differs from release")
    require(record["definition"] == stream_settings(stream)["definition"]
            and record["mirror_root"] == configuration()["mirror_root"], "Receipt source differs from configuration")
    tags = image_tags(version, record["selection"], record["commit"], record["run_id"], record["pr"])
    require(record["tags"] == tags, "Receipt tags differ from build selection")
    require(record["image"] == f"localhost/{image}:{tags[0]}", "Receipt image differs from release")
    normalize_image_id(record["image_id"])
    require(re.fullmatch(r"(?:quay\.io|ghcr\.io)/[a-z0-9_-]+/dcm-base-ee@sha256:[a-f0-9]{64}",
                         record["base_image"]), "Invalid receipt base digest")
    require(re.fullmatch(r"quay\.io/openshift-release-dev/(?:ocp-release|ocp-v4\.0-art-dev)@sha256:[a-f0-9]{64}",
                         record["release_image"]), "Invalid receipt release payload")
    hashes = record["archive_sha256"]
    require(isinstance(hashes, dict) and set(hashes) == set(ARCHIVES)
            and all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes.values()),
            "Invalid receipt archive checksums")
    require(re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}){2}T[0-9]{2}(?::[0-9]{2}){2}(?:\.[0-9]{1,6})?\+00:00",
                         record["verified_at"]), "Invalid receipt verification timestamp")
    datetime.fromisoformat(record["verified_at"])
    if published <= set(record):
        destination = record["published_image"]
        require(isinstance(destination, str), "Invalid published image reference")
        match = re.fullmatch(r"((quay\.io|ghcr\.io)/[a-z0-9_-]+/" + re.escape(image)
                             + r")@sha256:[a-f0-9]{64}", destination)
        require(match is not None and bool(record["pr"]) == (match[2] == "ghcr.io"),
                "Invalid published image reference")
        completed = record["published_tags"]
        require(isinstance(completed, list) and 1 <= len(completed) <= len(tags)
                and completed == [f"{match[1]}:{tag}" for tag in tags[:len(completed)]],
                "Invalid completed publication tags")


def build(selection, base, commit, run_id, output, pr="", run=execute, opener=urllib.request.urlopen):
    # Resolve eligibility before mirror reads, source staging or container commands.
    requested_stream = selection.removeprefix("stable-") if selection.startswith("stable-") else release_stream(selection)
    check_stream(requested_stream)
    version = resolve_release(selection, opener)
    stream = release_stream(version)
    settings = stream_settings(stream)
    image = image_name(version)
    tags = image_tags(version, selection, commit, run_id, pr)
    require(checked_operation("source revision", ["git", "rev-parse", "HEAD"], run, cwd=ROOT).strip() == commit,
            "Source checkout differs from requested commit")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    shutil.copytree(ROOT / settings["definition"], source)
    archive_hashes = stage_archives(version, source / "files/release", opener)
    base_digest = pinned_base(base, run)
    local_image = f"localhost/{image}:{tags[0]}"
    context = output / "context"
    # Builder 3.0.0 cannot pass arbitrary runtime options. Generate the context,
    # then build it directly so Podman can remove expiry inherited from the base.
    checked_operation("EE context generation", ["ansible-builder", "create", "--verbosity=3",
         "--file", "execution-environment.yml", "--output-filename", "Containerfile",
         "--context", str(context)], run, cwd=source)
    checked_operation("EE build", ["podman", "build", "--file", str(context / "Containerfile"),
         "--tag", local_image, "--build-arg", f"OCP_VERSION={version}",
         "--build-arg", f"EE_BASE_IMAGE={base_digest}",
         "--unsetlabel=quay.expires-after", str(context)], run, cwd=context)
    image_id = normalize_image_id(checked_operation("built image inspection",
        ["podman", "image", "inspect", "--format", "{{.ID}}", local_image], run).strip())
    installer = checked_operation("installer verification",
        ["podman", "run", "--rm", "--pull=never", image_id, "openshift-install", "version"], run)
    client = checked_operation("client verification",
        ["podman", "run", "--rm", "--pull=never", image_id, "oc", "version", "--client"], run)
    payload = verify_tools(version, installer, client)
    labels = json.loads(checked_operation("image label inspection",
        ["podman", "image", "inspect", "--format", "{{json .Config.Labels}}", image_id], run))
    require(not labels.get("quay.expires-after") and labels.get("ocp.version") == version,
            "Final release image must record its version and have no expiry label")
    record = {"schema_version": 2, "verified": True, "selection": selection, "release": version,
              "stream": stream, "definition": settings["definition"],
              "mirror_root": configuration()["mirror_root"],
              "image": local_image, "image_id": image_id, "tags": tags, "pr": pr,
              "commit": commit, "run_id": run_id, "base_image": base_digest,
              "archive_sha256": archive_hashes, "release_image": payload,
              "verified_at": datetime.now(timezone.utc).isoformat()}
    ci_secrets.inspect_paths([source, context])
    ci_secrets.inspect_image("sha256:" + image_id)
    write_receipt(output / "receipt.json", record)
    return output / "receipt.json"


def publish(receipt, registry, run=execute):
    require(re.fullmatch(r"(?:quay\.io|ghcr\.io)/[a-z0-9_-]+", registry), "Invalid image registry namespace")
    record = json.loads(receipt.read_text())
    validate_receipt(record)
    check_stream(record["stream"])
    image = image_name(record["release"])
    require(bool(record["pr"]) == registry.startswith("ghcr.io/"), "PR builds publish only to GHCR; releases only to Quay")
    record["image_id"] = normalize_image_id(record["image_id"])
    # Recheck immutable local content before any tag can reach a registry.
    installer = checked_operation("publication installer verification",
        ["podman", "run", "--rm", "--pull=never", record["image_id"], "openshift-install", "version"], run)
    client = checked_operation("publication client verification",
        ["podman", "run", "--rm", "--pull=never", record["image_id"], "oc", "version", "--client"], run)
    require(verify_tools(record["release"], installer, client) == record["release_image"], "Release payload changed")
    ci_secrets.inspect_bytes(json.dumps(record))
    ci_secrets.inspect_image("sha256:" + record["image_id"])
    destinations, expected_digest = [], None
    for index, tag in enumerate(record["tags"]):
        destination = f"{registry}/{image}:{tag}"
        digestfile = receipt.parent / f"push-{index}.digest"
        require(not digestfile.exists(), "Push evidence already exists; inspect the previous publication")
        checked_operation("image push",
            ["podman", "push", "--digestfile", str(digestfile), record["image_id"], f"docker://{destination}"], run)
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
    commands.add_parser("check-pr")
    stream_check = commands.add_parser("check-stream")
    stream_check.add_argument("--stream", required=True)
    commands.add_parser("prepare-pr")
    login = commands.add_parser("registry-login")
    login.add_argument("--registry", required=True, choices=LOGIN_REGISTRIES)
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
        elif args.command == "check-pr":
            print(json.dumps(pr_matrix()))
        elif args.command == "check-stream":
            check_stream(args.stream)
            print("OpenShift EE project is active")
        elif args.command == "prepare-pr":
            content = json.dumps(prepare_pr())
            ci_secrets.inspect_bytes(content)
            print(content)
        elif args.command == "registry-login":
            registry_login(args.registry)
            print(f"Registry login succeeded for {args.registry}")
        elif args.command == "resolve-base":
            reference = pinned_base(args.reference)
            ci_secrets.inspect_bytes(reference)
            print(reference)
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
            ci_secrets.inspect_bytes(summary)
            print(summary)
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
                    stream.write(summary)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        # Do not echo captured command output: inherited build steps may contain credentials.
        detail = str(error) if isinstance(error, (PublicError, CatalogError)) else type(error).__name__
        message = f"ERROR: OCP EE {args.command} failed: {detail}; preserve build evidence.\n"
        try:
            ci_secrets.inspect_bytes(message)
        except ci_secrets.GuardError:
            message = "ERROR: credential protection denied operation\n"
        parser.exit(1, message)


if __name__ == "__main__":
    main()
