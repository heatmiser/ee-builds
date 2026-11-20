#!/usr/bin/env python3
"""Generate credential-free EE contexts and mount credentials only for Galaxy."""

import argparse
import configparser
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import urllib.parse

import ci_secrets as guard
from ee_projects import require_active


ROOT = Path(__file__).resolve().parents[2]
GIT_REPOS = {"RHVP_OCP_LANDING_ZONE_TOKEN": "rhvp.ocp_landing_zone",
             "RHVP_BAREMETAL_OCP_TOKEN": "rhvp.baremetal_ocp",
             "OCP_SATELLITE_TOKEN": "ocp_satellite"}


def protect_containerfile(text):
    """Remove legacy transports, then scope mounts to actual Galaxy invocations."""
    lines, mounts = [], 0
    for line in text.splitlines():
        if re.match(r"\s*(?:ARG|ENV)\s+(?:COLLECTION_TOKEN|AH_TOKEN|RHVP_OCP_LANDING_ZONE_TOKEN|RHVP_BAREMETAL_OCP_TOKEN|OCP_SATELLITE_TOKEN)\b", line):
            continue
        if line.startswith("ENV ANSIBLE_GALAXY_SERVER_AUTOMATION_HUB_TOKEN="):
            continue
        if line.startswith("RUN git config --global url.") and any(name in line for name in (*GIT_REPOS, "COLLECTION_TOKEN")):
            continue
        if any(re.search(r"\$\{?" + name + r"\b", line) for name in (*GIT_REPOS, "COLLECTION_TOKEN", "AH_TOKEN")):
            raise guard.GuardError("Unsupported credential transport in generated context")
        if line.startswith("RUN ") and re.search(r"\bansible-galaxy\s+(?:role|collection)\s+install\b", line):
            if "--mount=" in line or line.endswith("\\"):
                raise guard.GuardError("Unsupported Galaxy instruction shape")
            line = ("RUN --mount=type=secret,id=galaxy_config,target=/run/secrets/ansible.cfg "
                    "--mount=type=secret,id=git_config,target=/run/secrets/gitconfig "
                    "ANSIBLE_CONFIG=/run/secrets/ansible.cfg GIT_CONFIG_GLOBAL=/run/secrets/gitconfig " + line[4:])
            mounts += 1
        lines.append(line)
    if not mounts and re.search(r"(?im)^FROM .+ as galaxy$", text):
        raise guard.GuardError("Generated context has no recognized Galaxy operation")
    return "\n".join(lines) + "\n"


def build(project, tag, commit):
    require_active(project, root=ROOT)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", tag) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise guard.GuardError("Invalid build identity")
    if guard.captured(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip() != commit:
        raise guard.GuardError("Checkout differs from requested build")
    with tempfile.TemporaryDirectory(prefix="ee-build-", dir=os.environ.get("RUNNER_TEMP")) as name:
        work = Path(name)
        source = work / project
        shutil.copytree(ROOT / project, source)
        shutil.copyfile(ROOT / "ansible.cfg", work / "ansible.cfg")
        context = work / "context"
        guard.captured(["ansible-builder", "create", "--verbosity=3", "--file", "execution-environment.yml",
                        "--context", str(context)], cwd=source, capture_result=False)
        containerfile = context / "Containerfile"
        containerfile.write_text(protect_containerfile(containerfile.read_text()))
        guard.inspect_paths([context])
        # Secret files are siblings of context, never copied into it or passed as
        # ARG/ENV. Podman mounts them temporarily for each Galaxy RUN only.
        config = work / "galaxy.cfg"
        token = guard.value("AH_TOKEN")
        if "\n" in token or "\r" in token:
            raise guard.GuardError("Invalid Automation Hub credential")
        settings = configparser.ConfigParser(interpolation=None)
        settings.read(source / "ansible.cfg" if (source / "ansible.cfg").is_file() else ROOT / "ansible.cfg")
        if token:
            settings["galaxy_server.automation_hub"]["token"] = token
        else:
            settings.remove_section("galaxy_server.automation_hub")
            settings["galaxy"]["server_list"] = "release_galaxy"
        with config.open("w") as stream:
            settings.write(stream)
        git = work / "git.cfg"
        git.write_text("".join(
            '[url ' + json.dumps('https://' + urllib.parse.quote(guard.value(key), safe='') + '@github.com/heatmiser/' + repo) + ']\n'
            '\tinsteadOf = https://github.com/heatmiser/' + repo + '\n'
            for key, repo in GIT_REPOS.items() if guard.value(key)))
        for path in (config, git):
            path.chmod(0o600)
        guard.captured(["podman", "build", "--secret", f"id=galaxy_config,src={config}",
                        "--secret", f"id=git_config,src={git}", "--tag", f"{project}:{tag}",
                        "--tag", f"{project}:{commit}", str(context)], cwd=context, capture_result=False)
        guard.inspect_image(f"{project}:{tag}")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    try:
        build(args.project, args.tag, args.commit)
    except (ValueError, KeyError, TypeError, OSError):
        parser.exit(1, "ERROR: protected EE build failed; no public output prepared\n")


if __name__ == "__main__":
    main()
