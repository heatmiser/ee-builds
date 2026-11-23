#!/usr/bin/env python3
"""Protect workflow credential values before exposing files, output or images."""

import argparse
import base64
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request


SECRET_NAMES = frozenset({
    "AH_TOKEN", "GITHUB_TOKEN", "OCP_SATELLITE_TOKEN", "QUAY_PASSWORD", "QUAY_USERNAME",
    "REDHAT_PASSWORD", "REDHAT_USERNAME", "REDHAT_SA_PASSWORD", "REDHAT_SA_USERNAME",
    "REFRESH_TOKEN", "RHVP_BAREMETAL_OCP_TOKEN", "RHVP_OCP_LANDING_ZONE_TOKEN",
})
# This particular value already names the public Quay image namespace. It is an
# identifier, not an authenticator. Other usernames remain protected values.
PUBLIC_IDENTIFIERS = frozenset({"QUAY_USERNAME"})
GIT_AUTH_USERNAME = "x-access-token"
GIT_TOKEN_NAMES = frozenset({"RHVP_OCP_LANDING_ZONE_TOKEN", "RHVP_BAREMETAL_OCP_TOKEN", "OCP_SATELLITE_TOKEN"})
BASIC_AUTH_PAIRS = (("REGISTRY_USERNAME", "REGISTRY_PASSWORD"), ("QUAY_USERNAME", "QUAY_PASSWORD"),
                    ("REDHAT_USERNAME", "REDHAT_PASSWORD"), ("REDHAT_SA_USERNAME", "REDHAT_SA_PASSWORD"))
MATCH_SOURCE_NAMES = SECRET_NAMES | {"REGISTRY_PASSWORD", "GH_TOKEN", "OTHER_CI_SECRET"}

# These are diagnostic hints, not proof of an underlying cause. Never expose
# matching text, command arguments, return-code strings or exception attributes.
COMMAND_HINTS = (
    ("storage-exhausted", (b"no space left on device", b"disk quota exceeded")),
    ("authentication-rejected", (b"unauthorized", b"authentication required", b"invalid username/password",
                                 b"invalid username or password", b"401 client error", b"403 client error")),
    ("git-access-failed", (b"repository not found", b"could not read username", b"authentication failed for")),
    ("package-hash-mismatch", (b"these packages do not match the hashes", b"hash mismatch")),
    ("tls-failure", (b"x509:", b"certificate verify failed", b"certificate signed by unknown authority")),
    ("network-failure", (b"no such host", b"temporary failure in name resolution", b"connection refused",
                          b"network is unreachable", b"i/o timeout", b"context deadline exceeded")),
    ("image-reference-unavailable", (b"manifest unknown", b"name unknown", b"manifest not found")),
    ("image-platform-mismatch", (b"no matching manifest", b"no image found in manifest list")),
    ("dependency-installation-failed", (b"no matching distribution found", b"could not find a version that satisfies",
                                       b"failed to resolve the requested dependencies map")),
)
ERROR_CATEGORIES = frozenset({
    "protection-denied", "operation-failed", "command-failed", "command-timeout",
    "command-start-failed", "command-unavailable", "output-too-large", "credential-detected",
    *(name for name, _ in COMMAND_HINTS),
})


class GuardError(ValueError):
    """A fixed diagnostic, never a value, matching text, filename or tool error."""

    def __init__(self, message, *, category="protection-denied", sources=(), stream="unknown", command_status="unknown"):
        super().__init__(message)
        self.category = safe_category(category)
        self.sources = safe_sources(sources)
        self.stream = stream if stream in ("stdout", "stderr") else "unknown"
        self.command_status = command_status if command_status in ("success", "failure") else "unknown"


def safe_sources(sources):
    if not isinstance(sources, (tuple, list, set, frozenset)):
        return ()
    return tuple(sorted({s for s in sources if isinstance(s, str) and s in MATCH_SOURCE_NAMES}))


def error_metadata(error):
    # Revalidate at the public boundary even if an exception was modified later.
    stream, status = getattr(error, "stream", "unknown"), getattr(error, "command_status", "unknown")
    return {"sources": safe_sources(getattr(error, "sources", ())),
            "stream": stream if stream in ("stdout", "stderr") else "unknown",
            "command_status": status if status in ("success", "failure") else "unknown"}


def safe_category(category):
    return category if isinstance(category, str) and category in ERROR_CATEGORIES else "operation-failed"


def error_category(error):
    return safe_category(getattr(error, "category", "operation-failed"))


def command_category(streams):
    """Classify already credential-checked output using constant byte phrases."""
    hits = set()
    overlap = max(len(phrase) for _, phrases in COMMAND_HINTS for phrase in phrases) - 1
    for stream in streams:
        stream.seek(0)
        tail = b""
        while chunk := stream.read(1024 * 1024):
            data = tail + chunk.lower()
            for name, phrases in COMMAND_HINTS:
                if any(phrase in data for phrase in phrases):
                    hits.add(name)
            tail = data[-overlap:]
    return next((name for name, _ in COMMAND_HINTS if name in hits), "command-failed")


def value(name, environment=None):
    env = os.environ if environment is None else environment
    return env.get("CI_SECRET_" + name, env.get(name, ""))


def patterns(environment=None):
    env = os.environ if environment is None else environment
    values = set()
    for name, item in env.items():
        canonical = name.removeprefix("CI_SECRET_")
        if item and (name.startswith("CI_SECRET_") or name in SECRET_NAMES
                     or name in {"REGISTRY_PASSWORD", "GH_TOKEN"}):
            if canonical not in PUBLIC_IDENTIFIERS:
                values.add(item)
    result = set()
    for item in values:
        if len(item) > 65536:
            raise GuardError("Credential exceeds inspection bound")
        raw = item.encode()
        quoted = urllib.parse.quote(item, safe="")
        result.update((raw, base64.b64encode(raw), raw.hex().encode(), raw.hex().upper().encode(),
                       quoted.encode(), re.sub(r"%[0-9A-F]{2}", lambda m: m[0].lower(), quoted).encode(),
                       json.dumps(item)[1:-1].encode(), json.dumps(item, ensure_ascii=False)[1:-1].encode(),
                       item.encode("utf-16le"), item.encode("utf-16be")))
    for user, password in BASIC_AUTH_PAIRS:
        username, secret = value(user, env), value(password, env)
        if username and secret:
            result.add(base64.b64encode((username + ":" + secret).encode()))
    for name in GIT_TOKEN_NAMES:
        for alias in (name, "CI_SECRET_" + name):
            if secret := env.get(alias):
                result.add(base64.b64encode((GIT_AUTH_USERNAME + ":" + secret).encode()))
    # A confidential value wins even if another source uses the same value as a
    # public namespace; only the public SOURCE is excluded above.
    return tuple(sorted(result))


def attributed_patterns(environment=None):
    """Map the existing matcher bytes to fixed source names, never raw env keys."""
    env = os.environ if environment is None else environment
    indexed = {}

    def add(encoded, sources):
        for pattern in encoded:
            indexed.setdefault(pattern, set()).update(sources)

    for name, item in env.items():
        canonical = name.removeprefix("CI_SECRET_")
        if item and (name.startswith("CI_SECRET_") or name in SECRET_NAMES
                     or name in {"REGISTRY_PASSWORD", "GH_TOKEN"}) and canonical not in PUBLIC_IDENTIFIERS:
            source = canonical if canonical in MATCH_SOURCE_NAMES else "OTHER_CI_SECRET"
            add(patterns({name: item}), (source,))
    for user, password in BASIC_AUTH_PAIRS:
        username, secret = value(user, env), value(password, env)
        if username and secret:
            sources = {name for name in (user, password)
                       if name in MATCH_SOURCE_NAMES and name not in PUBLIC_IDENTIFIERS}
            add((base64.b64encode((username + ":" + secret).encode()),), sources)
    return indexed


def matching_sources(stream, environment=None):
    """Identify all source aliases in the first rejected stream; retain no text."""
    indexed = attributed_patterns(environment)
    overlap = max((len(p) for p in indexed), default=1) - 1
    matched, tail = set(), b""
    stream.seek(0)
    while chunk := stream.read(1024 * 1024):
        data = tail + chunk
        for pattern, sources in indexed.items():
            if pattern in data:
                matched.update(sources)
        tail = data[-overlap:] if overlap else b""
    return safe_sources(matched)


def inspect_stream(stream, forbidden=None):
    forbidden = patterns() if forbidden is None else forbidden
    overlap = max((len(p) for p in forbidden), default=1) - 1
    tail = b""
    while chunk := stream.read(1024 * 1024):
        data = tail + chunk
        if any(p in data for p in forbidden):
            raise GuardError("Credential detected; public output denied", category="credential-detected")
        tail = data[-overlap:] if overlap else b""


def inspect_bytes(content):
    inspect_stream(io.BytesIO(content.encode() if isinstance(content, str) else content))


def inspect_paths(paths):
    forbidden = patterns()
    for root in map(Path, paths):
        if not root.exists() or root.is_symlink():
            raise GuardError("Inspection input missing or unsafe")
        entries = [root] if root.is_file() else root.rglob("*")
        for path in entries:
            if path.is_symlink():
                raise GuardError("Inspection input unsafe")
            inspect_stream(io.BytesIO(path.name.encode()), forbidden)
            if path.is_file():
                with path.open("rb") as stream:
                    inspect_stream(stream, forbidden)
            elif not path.is_dir():
                raise GuardError("Inspection input unsupported")


def child_env():
    # The supervisor knows the values; ordinary tools/containers do not need them.
    env = {k: v for k, v in os.environ.items()
           if k not in SECRET_NAMES and not k.startswith("CI_SECRET_")
           and k not in {"REGISTRY_USERNAME", "REGISTRY_PASSWORD", "GH_TOKEN"}}
    return env


def captured(argv, *, cwd=None, input=None, timeout=3600, capture_result=True):
    """Never forward raw command output, including output from failed commands."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(argv, cwd=cwd, input=input, env=child_env(),
                                    stdout=stdout, stderr=stderr, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise GuardError("Protected command could not complete", category="command-timeout") from None
        except FileNotFoundError:
            raise GuardError("Protected command could not complete", category="command-unavailable") from None
        except (OSError, subprocess.SubprocessError):
            raise GuardError("Protected command could not complete", category="command-start-failed") from None
        for stream_name, stream in (("stdout", stdout), ("stderr", stderr)):
            stream.seek(0)
            try:
                inspect_stream(stream)
            except GuardError as error:
                if error.category != "credential-detected":
                    raise
                raise GuardError("Credential detected; public output denied", category="credential-detected",
                                 sources=matching_sources(stream), stream=stream_name,
                                 command_status="failure" if result.returncode else "success") from None
        if result.returncode:
            raise GuardError("Protected command failed", category=command_category((stdout, stderr)))
        if not capture_result:
            return b""
        stdout.seek(0)
        # Callers consume bounded machine-readable output, never forward it raw.
        output = stdout.read(4 * 1024 * 1024 + 1)
        if len(output) > 4 * 1024 * 1024:
            raise GuardError("Protected command output exceeds bound", category="output-too-large")
        return output


def inspect_image(image):
    # Reuse the existing digest-checked all-layer/archive inspector. Imported
    # lazily because the plain helper also uses this module's matching functions.
    import ci_publication as ci
    image_id = captured(["podman", "image", "inspect", "--format", "{{.Id}}", image]).decode().strip()
    image_id = ci.normalize_image_id(image_id)
    with tempfile.TemporaryDirectory(prefix="ee-inspect-", dir=os.environ.get("RUNNER_TEMP")) as name:
        private = Path(name)
        oci = private / "oci"
        captured(["podman", "save", "--format", "oci-dir", "--uncompressed", "--output", str(oci), image_id], capture_result=False)
        ci.inspect_oci(oci, ci.Payloads(private / "payloads", patterns()), image_id)
    return image_id


def auth_path():
    root = Path(os.environ["RUNNER_TEMP"]).resolve()
    path = Path(os.environ["REGISTRY_AUTH_FILE"])
    if (not path.is_absolute() or path.parent.resolve() != root or path.is_symlink()
            or not path.name.startswith("ee-auth-")):
        raise GuardError("Registry authentication path is unsafe")
    return path


def login(registry):
    if registry not in {"quay.io", "ghcr.io", "registry.redhat.io"}:
        raise GuardError("Unsupported registry")
    username, password = os.environ.get("REGISTRY_USERNAME", ""), os.environ.get("REGISTRY_PASSWORD", "")
    if not username or not password:
        raise GuardError("Registry credentials missing")
    # Write the documented auth format directly: no confidential username/token
    # appears in argv, and no login action can echo or copy it into Docker config.
    path = auth_path()
    record = json.loads(path.read_text()) if path.exists() else {"auths": {}}
    record["auths"][registry] = {"auth": base64.b64encode((username + ":" + password).encode()).decode()}
    with open(path, "w", opener=lambda p, flags: os.open(p, flags | os.O_NOFOLLOW, 0o600)) as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(record, stream)


def push_image(image, destination):
    """Push inspected immutable bytes; retain only a checked private receipt."""
    inspect_bytes(destination)
    if not re.fullmatch(r"(?:ghcr\.io|quay\.io)/[a-z0-9_-]+/[a-z0-9_.-]+:[A-Za-z0-9_.-]+", destination):
        raise GuardError("Invalid publication reference")
    auth = auth_path()
    identity = inspect_image(image)
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", identity):
        raise GuardError("Invalid inspected image identity")
    # Keep completed receipts between calls. A later failed tag does not erase
    # earlier success, and no receipt is uploaded or printed by this helper.
    work = Path(tempfile.mkdtemp(prefix="ee-publication-", dir=Path(os.environ["RUNNER_TEMP"]).resolve()))
    digestfile = work / "manifest.digest"
    captured(["podman", "push", "--authfile", str(auth), "--digestfile", str(digestfile),
              identity, "docker://" + destination], capture_result=False)
    fd = os.open(digestfile, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 128:
            raise GuardError("Invalid publication digest evidence")
        os.fchmod(stream.fileno(), 0o600)
        raw = stream.read(129)
    inspect_bytes(raw)
    digest = raw.decode("ascii").strip()
    if len(raw) > 128 or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise GuardError("Invalid publication manifest digest")
    record = {"schema_version": 1, "image_id": identity, "destination": destination,
              "manifest_digest": digest}
    content = json.dumps(record, sort_keys=True, indent=2) + "\n"
    inspect_bytes(content)
    with open(work / "receipt.json", "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        stream.write(content)
    return record


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    files = sub.add_parser("files")
    files.add_argument("paths", nargs="+", type=Path)
    image = sub.add_parser("image")
    image.add_argument("reference")
    auth = sub.add_parser("login")
    auth.add_argument("--registry", required=True)
    push = sub.add_parser("push")
    push.add_argument("--image", required=True)
    push.add_argument("--destination", required=True)
    sub.add_parser("cleanup-auth")
    sub.add_parser("refresh-token")
    args = parser.parse_args()
    try:
        if args.command == "files":
            inspect_paths(args.paths)
        elif args.command == "image":
            inspect_image(args.reference)
        elif args.command == "login":
            login(args.registry)
        elif args.command == "cleanup-auth":
            auth_path().unlink(missing_ok=True)
        elif args.command == "push":
            push_image(args.image, args.destination)
        else:
            token = value("REFRESH_TOKEN")
            if not token:
                raise GuardError("Refresh credential missing")
            body = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": "rhsm-api", "refresh_token": token}).encode()
            request = urllib.request.Request("https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token", data=body)
            with urllib.request.urlopen(request, timeout=120):
                pass  # Preserve existing behavior: response is not retained.
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        message = "ERROR: credential protection denied operation\n"
        try:
            inspect_bytes(message)
        except GuardError:
            message = ""
        parser.exit(1, message)


if __name__ == "__main__":
    main()
