"""Containment contracts for the EE workflows before credential qualification."""

from pathlib import Path
import json
import os
import re
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
EE_WORKFLOWS = (
    "pr-ee-build.yml",
    "push-ee-build.yml",
    "pr-ocp-ee-build.yml",
    "build-versioned-ocp-ee.yml",
)


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


class CredentialContainmentWorkflowTests(unittest.TestCase):
    def test_runner_context_is_absent_from_workflow_and_job_env(self):
        # GitHub resolves these mappings before assigning a runner. runner.temp
        # is allowed in step fields, but rejects the entire workflow here.
        for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
            data = workflow(path.name)
            scopes = [("workflow", data.get("env", {}))]
            scopes += [(name, job.get("env", {})) for name, job in data["jobs"].items()]
            for name, env in scopes:
                with self.subTest(workflow=path.name, scope=name):
                    for value in env.values():
                        expressions = re.findall(r"\$\{\{(.*?)\}\}", value, re.DOTALL)
                        for expression in expressions:
                            self.assertNotRegex(expression, r"\brunner\s*\.")

    def test_all_authfile_paths_initialize_on_runner_before_checkout(self):
        expected = {
            "pr-ee-build.yml": {"build-ee", "build-ocp-ees"},
            "push-ee-build.yml": {"build-ee", "build-ocp-ees"},
            "pr-image-build.yml": {"build-image"},
            "push-image-build.yml": {"build-image"},
            "pr-ocp-ee-build.yml": {"prepare", "build"},
            "build-versioned-ocp-ee.yml": {"prepare", "build"},
        }
        initialized = 0
        for name, job_ids in expected.items():
            jobs = workflow(name)["jobs"]
            for job_id in job_ids:
                with self.subTest(workflow=name, job=job_id), tempfile.TemporaryDirectory() as directory:
                    job = jobs[job_id]
                    self.assertNotIn("REGISTRY_AUTH_FILE", job.get("env", {}))
                    first = job["steps"][0]
                    self.assertEqual(first["name"], "Initialize job registry authentication")
                    self.assertEqual(first["shell"], "bash")
                    self.assertNotIn("if", first)
                    self.assertNotIn("env", first)
                    runner_temp = Path(directory) / "runner temp"
                    runner_temp.mkdir()
                    output = Path(directory) / "environment file"
                    result = subprocess.run(["bash", "-euo", "pipefail", "-c", first["run"]],
                                            env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(runner_temp),
                                                 "GITHUB_ENV": str(output)}, capture_output=True, timeout=30, check=False)
                    self.assertEqual(result.returncode, 0)
                    filename = "ee-auth-" + name.removesuffix(".yml") + "-" + job_id + ".json"
                    self.assertEqual(output.read_text(), "REGISTRY_AUTH_FILE=" + str(runner_temp / filename) + "\n")
                    self.assertEqual(result.stdout, b"")
                    self.assertEqual(result.stderr, b"")
                    self.assertFalse((runner_temp / filename).exists())
                    cleanup = [s for s in job["steps"] if "ci_secrets.py cleanup-auth" in s.get("run", "")]
                    self.assertEqual(len(cleanup), 1)
                    self.assertEqual(cleanup[0]["if"], "always()")
                    initialized += 1
        self.assertEqual(initialized, 10)

    def test_every_ee_publisher_is_explicitly_held(self):
        publishers = []
        for name in EE_WORKFLOWS:
            for job_id, job in workflow(name)["jobs"].items():
                for step in job.get("steps", []):
                    if (step.get("uses", "").startswith("redhat-actions/push-to-registry@")
                            or re.search(r"(?:ocp_ee\.py\s+publish|ci_secrets\.py\s+push)\b", step.get("run", ""))):
                        publishers.append((name, job_id, step))
        self.assertEqual(len(publishers), 8)
        for name, job_id, step in publishers:
            with self.subTest(workflow=name, job=job_id, step=step["name"]):
                # A held job is insufficient: each publisher must remain held if moved.
                self.assertEqual(step.get("if"), "${{ false }}")

    def test_dependent_jobs_hold_without_discarding_fresh_base_requirements(self):
        for name in EE_WORKFLOWS[:2]:
            jobs = workflow(name)["jobs"]
            with self.subTest(workflow=name):
                dependent = jobs["build-ocp-ees"]
                self.assertTrue(dependent["if"].startswith("false && "))
                self.assertIn("needs.build-ee.result == 'success'", dependent["if"])
                self.assertIn("contains(needs.prepare-matrix.outputs.matrix, 'dcm-base-ee')",
                              dependent["if"])
                self.assertIn("ocp_length != '0'", dependent["if"])
                self.assertEqual(dependent["needs"], ["prepare-matrix", "build-ee"])
                primary = jobs["build-ee"]
                self.assertEqual(primary["if"], "${{ needs.prepare-matrix.outputs.length != '0' }}")
                self.assertTrue(any("ee_build.py" in step.get("run", "")
                                    for step in primary["steps"]))

    def test_ee_checkout_never_persists_credentials(self):
        for name in EE_WORKFLOWS:
            checkouts = [step for job in workflow(name)["jobs"].values()
                         for step in job.get("steps", [])
                         if step.get("uses", "").startswith("actions/checkout@")]
            with self.subTest(workflow=name):
                self.assertTrue(checkouts)
                for checkout in checkouts:
                    self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_only_comment_job_can_request_write_permissions(self):
        for name in EE_WORKFLOWS:
            data = workflow(name)
            self.assertEqual(data["permissions"]["contents"], "read")
            for job_id, job in data["jobs"].items():
                permissions = job.get("permissions", data["permissions"])
                with self.subTest(workflow=name, job=job_id):
                    if job_id == "post-comment":
                        self.assertEqual(permissions, {"contents": "read", "pull-requests": "write"})
                    else:
                        self.assertEqual(permissions["contents"], "read")
                        self.assertLessEqual(set(permissions), {"contents", "packages"})
                        self.assertTrue(all(value == "read" for value in permissions.values()))
            if name != "build-versioned-ocp-ee.yml":
                for job_id in (("prepare", "build") if name == "pr-ocp-ee-build.yml"
                               else ("prepare-matrix", "build-ee", "build-ocp-ees")):
                    job = data["jobs"][job_id]
                    self.assertEqual(job.get("permissions", data["permissions"])["packages"], "read")

    def test_no_raw_debug_or_runner_global_git_configuration(self):
        for name in EE_WORKFLOWS:
            data = workflow(name)
            with self.subTest(workflow=name):
                self.assertNotIn("debug", data["jobs"])
                for job in data["jobs"].values():
                    for step in job.get("steps", []):
                        command = step.get("run", "")
                        self.assertNotRegex(command, r"\bprintenv\b|\bgit\s+diff\b")
                        self.assertNotRegex(command, r"\bgit\s+config\s+--global\b")

    def test_devel_preparation_accepts_manual_or_same_repository_pr(self):
        prepare = workflow("pr-ee-build.yml")["jobs"]["prepare-matrix"]
        self.assertEqual(prepare["if"].strip(), "github.event_name == 'workflow_dispatch' || "
                         "(github.event_name == 'pull_request' && "
                         "github.event.pull_request.head.repo.full_name == github.repository)")

    def test_manual_ee_selection_is_single_active_target_before_credentials(self):
        data = workflow("pr-ee-build.yml")
        choice = data["on"]["workflow_dispatch"]["inputs"]["ee_name"]
        self.assertEqual(choice["options"], ["dcm-base-ee"])
        prepare = data["jobs"]["prepare-matrix"]
        step = next(s for s in prepare["steps"] if s["name"] == "Select one manual qualification EE")
        self.assertEqual(step["if"], "github.event_name == 'workflow_dispatch'")
        # Execute the actual selection shell; no network, credentials or engine.
        for project, accepted in (("dcm-base-ee", True), ("aws-ee", False),
                                  ("dcm-ocp-ee-4.22", False), ("../dcm-base-ee", False)):
            with self.subTest(project=project), tempfile.TemporaryDirectory() as directory:
                env = {"PATH": os.environ["PATH"], "EE_NAME": project,
                       "PYTHONDONTWRITEBYTECODE": "1"}
                command = step["run"].replace(".github/scripts/ee_projects.py",
                                              str(ROOT / ".github/scripts/ee_projects.py"))
                result = subprocess.run(["bash", "-e", "-c", command], cwd=directory,
                                        env=env, capture_output=True, timeout=30, check=False)
                self.assertEqual(result.returncode == 0, accepted)
                if accepted:
                    matrix = json.loads((Path(directory) / "matrix_output.json").read_text())
                    self.assertEqual(matrix, {"include": [{"ee": "dcm-base-ee"}]})

    def test_manual_ee_source_and_reports_use_exact_event_commit(self):
        jobs = workflow("pr-ee-build.yml")["jobs"]
        source = "${{ github.event.pull_request.head.sha || github.sha }}"
        for name in ("prepare-matrix", "build-ee"):
            checkout = next(s for s in jobs[name]["steps"] if s.get("uses", "").startswith("actions/checkout@"))
            self.assertEqual(checkout["with"]["ref"], source)
            self.assertEqual(checkout["with"]["repository"],
                             "${{ github.event.pull_request.head.repo.full_name || github.repository }}")
        steps = jobs["build-ee"]["steps"]
        build = next(s for s in steps if s["name"] == "Build image and create artifact")
        report = next(s for s in steps if s["name"] == "Generate bounded build report")
        self.assertEqual(build["env"]["SOURCE_COMMIT"], source)
        self.assertEqual(report["env"]["REPORT_COMMIT"], source)
        upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        prerequisite = next(i for i, s in enumerate(steps) if s["name"] == "Require authenticated base EE credential inputs")
        first_login = next(i for i, s in enumerate(steps) if "ci_secrets.py login" in s.get("run", ""))
        self.assertLess(prerequisite, first_login)

    def test_manual_and_pr_tags_are_unique_and_do_not_enable_dependents(self):
        jobs = workflow("pr-ee-build.yml")["jobs"]
        step = next(s for s in jobs["build-ee"]["steps"] if s["name"] == "Define environment variables")
        for event, expected in (("workflow_dispatch", "manual-123-1-abcdef0"), ("pull_request", "pr-7-abcdef0")):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "env"
                env = {"PATH": os.environ["PATH"], "GITHUB_ENV": str(output), "SOURCE_COMMIT": "abcdef0" + "0" * 33,
                       "BUILD_EVENT": event, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "PR_NUMBER": "7"}
                result = subprocess.run(["bash", "-e", "-c", step["run"].replace("${{ matrix.ee }}", "dcm-base-ee")],
                                        env=env, capture_output=True, timeout=30, check=False)
                self.assertEqual(result.returncode, 0)
                self.assertIn("IMAGE_TAG=" + expected + "\n", output.read_text())
        step = next(s for s in jobs["prepare-matrix"]["steps"] if s.get("id") == "ocp-matrix")
        self.assertIn("MATRIX='{\"include\":[]}'", step["run"])
        self.assertTrue(jobs["build-ocp-ees"]["if"].startswith("false && "))

    def test_push_success_reporting_requires_successful_push(self):
        for name, push_id in (("pr-ee-build.yml", "push_to_ghcr"),
                              ("push-ee-build.yml", "push_to_quay")):
            steps = workflow(name)["jobs"]["build-ee"]["steps"]
            push = next(step for step in steps if step.get("id") == push_id)
            reports = [step for step in steps if step["name"] in ("Print summary", "Set push success flag")]
            with self.subTest(workflow=name):
                self.assertEqual(push["if"], "${{ false }}")
                self.assertTrue(reports)
                for report in reports:
                    self.assertEqual(report["if"], f"steps.{push_id}.outcome == 'success'")

    def test_independent_plan_handoffs_and_builds_remain_available(self):
        for name, prefix, directory in (
                ("pr-ocp-ee-build.yml", "ocp-ee-pr-plan", "ocp-ee-plan"),
                ("build-versioned-ocp-ee.yml", "ocp-ee-release-plan", "ocp-ee-release-plan")):
            jobs = workflow(name)["jobs"]
            prepare, build = jobs["prepare"], jobs["build"]
            with self.subTest(workflow=name):
                self.assertNotIn("false", prepare.get("if", ""))
                self.assertNotIn("false", build.get("if", ""))
                self.assertEqual(build["needs"], "prepare")
                self.assertIn("plan_artifact", prepare["outputs"])
                upload = next(step for step in prepare["steps"]
                              if step.get("uses", "").startswith("actions/upload-artifact@"))
                self.assertEqual(upload["with"]["path"], "${{ runner.temp }}/" + prefix + ".json")
                self.assertNotIn("false", upload.get("if", ""))
                download = next(step for step in build["steps"]
                                if step.get("uses", "").startswith("actions/download-artifact@"))
                self.assertEqual(download["with"]["name"], "${{ needs.prepare.outputs.plan_artifact }}")
                self.assertEqual(download["with"]["path"], "${{ runner.temp }}/" + directory)
                self.assertNotIn("false", download.get("if", ""))
                commands = [step for step in build["steps"]
                            if re.search(r"ocp_ee\.py\s+build\b", step.get("run", ""))]
                self.assertEqual(len(commands), 1)
                self.assertNotIn("false", commands[0].get("if", ""))

    def test_all_five_plain_projects_remain_selectable_and_buildable(self):
        projects = {"antigravity-ai-helpers", "codex-ai-helpers", "gemini-ai-helpers",
                    "opencode-ai-helpers", "k8s-launch-kit"}
        for name in ("pr-image-build.yml", "push-image-build.yml"):
            data = workflow(name)
            with self.subTest(workflow=name):
                choices = data["on"]["workflow_dispatch"]["inputs"]["image_name"]["options"]
                self.assertEqual(set(choices), projects | {"all"})
                worker = data["jobs"]["build-image"]
                self.assertEqual(worker["if"], "${{ needs.prepare-matrix.outputs.length != '0' }}")
                builds = [step for step in worker["steps"]
                          if re.search(r"ci_publication\.py\s+build\b", step.get("run", ""))]
                self.assertEqual(len(builds), 1)
                self.assertNotIn("if", builds[0])
                publishers = [step for step in worker["steps"]
                              if re.search(r"ci_publication\.py\s+publish\b", step.get("run", ""))]
                self.assertEqual(len(publishers), 1)
                self.assertEqual(publishers[0]["if"], "${{ false }}")


if __name__ == "__main__":
    unittest.main()
