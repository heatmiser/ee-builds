#!/usr/bin/env python3
"""Enforce the reviewed EE lifecycle catalog without network or build operations."""

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
EVIDENCE_TYPES = {"build_step_executed", "build_job_record_step_details_unavailable",
                  "no_recorded_ci_build_activity"}


class CatalogError(ValueError):
    """A bounded diagnostic safe for public workflow output."""


def require(condition, message):
    if not condition:
        raise CatalogError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "EE catalog contains duplicate keys")
        result[key] = value
    return result


def load_catalog(root=ROOT):
    """Reject incomplete policy instead of silently discovering build eligibility."""
    root = Path(root)
    path = root / ".github/ee-projects.json"
    require(not (root / ".github").is_symlink() and not path.is_symlink(),
            "EE catalog must not be a symlink")
    try:
        require(path.is_file() and path.stat().st_size <= 1024 * 1024,
                "EE catalog is missing or oversized")
        data = json.loads(path.read_text(), object_pairs_hook=unique_object)
        require(isinstance(data, dict) and set(data) == {
            "schema_version", "assessment_date", "activity_window_start", "projects"},
            "EE catalog has missing or unsupported fields")
        require(type(data["schema_version"]) is int and data["schema_version"] == 1,
                "Unsupported EE catalog schema")
        for key in ("assessment_date", "activity_window_start"):
            require(isinstance(data[key], str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", data[key]),
                    "Invalid EE catalog assessment date")
        require(date.fromisoformat(data["activity_window_start"]) <= date.fromisoformat(data["assessment_date"]),
                "Invalid EE catalog activity window")
        projects = data["projects"]
        require(isinstance(projects, dict) and bool(projects), "EE catalog must list projects")
        for name, entry in projects.items():
            require(NAME.fullmatch(name) is not None, "Invalid EE project name in catalog")
            require(isinstance(entry, dict) and set(entry) == {
                "status", "last_recorded_ci_activity", "evidence_url", "evidence_type"},
                "Invalid EE catalog project fields")
            require(entry["status"] in ("active", "example"), "Invalid EE catalog project status")
            require(isinstance(entry["evidence_type"], str) and entry["evidence_type"] in EVIDENCE_TYPES,
                    "Invalid EE catalog evidence type")
            stamp, url = entry["last_recorded_ci_activity"], entry["evidence_url"]
            if entry["evidence_type"] == "no_recorded_ci_build_activity":
                require(stamp is None and url is None, "Invalid absent EE activity evidence")
            else:
                require(isinstance(stamp, str) and re.fullmatch(
                    r"[0-9]{4}(?:-[0-9]{2}){2}T[0-9]{2}(?::[0-9]{2}){2}Z", stamp),
                    "Invalid EE activity timestamp")
                datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
                require(isinstance(url, str) and re.fullmatch(
                    r"https://github\.com/heatmiser/ee-builds/actions/runs/[1-9][0-9]*/job/[1-9][0-9]*", url),
                    "Invalid EE activity evidence URL")
            definition = root / name / "execution-environment.yml"
            require(not definition.parent.is_symlink() and not definition.is_symlink() and definition.is_file(),
                    "Cataloged EE definition is missing or a symlink")
        discovered = {p.parent.name for p in root.glob("*/execution-environment.yml")}
        require(set(projects) == discovered, "EE catalog must cover every EE definition exactly")
        return projects
    except CatalogError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise CatalogError("EE catalog is invalid or unreadable; content suppressed") from None


def is_active(project, catalog):
    require(isinstance(project, str) and NAME.fullmatch(project) is not None, "Invalid EE project name")
    require(project in catalog, "EE project is not cataloged")
    return catalog[project]["status"] == "active"


def require_active(project, root=ROOT, catalog=None):
    catalog = load_catalog(root) if catalog is None else catalog
    require(is_active(project, catalog), "EE project is example-only; reviewed re-enablement is required")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "check", "matrix"))
    parser.add_argument("--project")
    args = parser.parse_args()
    try:
        catalog = load_catalog()
        if args.command == "validate":
            print("EE catalog is valid")
        else:
            require_active(args.project, catalog=catalog)
            if args.command == "matrix":
                print(json.dumps({"include": [{"ee": args.project}]}))
            else:
                print("EE project is active: " + args.project)
    except CatalogError as error:
        parser.exit(1, "ERROR: " + str(error) + "\n")


if __name__ == "__main__":
    main()
