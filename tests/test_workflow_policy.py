from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
USIGN_COMMIT = "c4c72b1b07945ee192361dc751291a7c98d6adcd"


def load_workflow(name: str) -> tuple[dict, str]:
    path = WORKFLOWS / name
    text = path.read_text(encoding="utf-8")
    value = yaml.load(text, Loader=yaml.BaseLoader)
    if not isinstance(value, dict):
        raise AssertionError(f"{name} is not a YAML mapping")
    return value, text


def steps(job: dict) -> list[dict]:
    value = job.get("steps")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AssertionError("job steps must be a list of mappings")
    return value


class WorkflowPolicyTest(unittest.TestCase):
    def assert_publisher_commands_are_exact(self, workflow: dict) -> None:
        build_steps = steps(workflow["jobs"]["build"])
        by_name = {item.get("name", ""): item for item in build_steps}
        self.assertEqual(
            by_name["Fetch immutable releases"]["run"],
            "python3 -m scripts.fetch_releases --manifest sources.json --destination downloads",
        )
        self.assertEqual(
            "python3 -m scripts.assemble_feed --manifest sources.json --downloads downloads --output pages",
            by_name["Assemble feed"]["run"],
        )
        self.assertEqual(
            by_name["Validate deployable inventory"]["run"],
            "python3 -m tests.inventory_test pages",
        )

    def test_ci_is_read_only_and_runs_every_local_test(self):
        workflow, text = load_workflow("test.yml")
        self.assertEqual(set(workflow["on"]), {"push", "pull_request"})
        self.assertEqual(workflow.get("permissions"), {"contents": "read"})
        self.assertNotIn("secrets.", text)
        self.assertNotRegex(text, r"(?m)^\s*(pages|id-token):")
        self.assertNotIn("deploy-pages", text)
        self.assertNotIn("upload-pages-artifact", text)
        commands = "\n".join(item.get("run", "") for item in steps(workflow["jobs"]["test"]))
        self.assertIn("python3 -m unittest discover", commands)
        self.assertIn("tests/*.sh", commands)
        self.assertIn("sh -n", commands)

    def test_pages_has_only_hourly_and_manual_triggers(self):
        workflow, _ = load_workflow("pages.yml")
        self.assertEqual(set(workflow["on"]), {"schedule", "workflow_dispatch"})
        schedules = workflow["on"]["schedule"]
        self.assertEqual(len(schedules), 1)
        cron = schedules[0]["cron"]
        fields = cron.split()
        self.assertEqual(len(fields), 5)
        self.assertEqual(fields[1:], ["*", "*", "*", "*"])
        self.assertRegex(fields[0], r"^[0-5]?[0-9]$")
        self.assertIn("concurrency", workflow)
        self.assertEqual(workflow.get("permissions"), {"contents": "read"})

    def test_pages_permissions_are_minimal_and_deploy_only(self):
        workflow, _ = load_workflow("pages.yml")
        jobs = workflow["jobs"]
        self.assertEqual(set(jobs), {"build", "deploy"})
        self.assertNotIn("permissions", jobs["build"])
        self.assertEqual(jobs["deploy"].get("permissions"), {
            "pages": "write",
            "id-token": "write",
        })
        self.assertEqual(jobs["deploy"].get("needs"), "build")
        self.assertEqual(jobs["deploy"].get("environment", {}).get("name"), "github-pages")

    def test_pages_pipeline_is_fail_closed_and_ordered(self):
        workflow, text = load_workflow("pages.yml")
        build_steps = steps(workflow["jobs"]["build"])
        names = [item.get("name", "") for item in build_steps]
        required = [
            "Check signing key",
            "Fetch immutable releases",
            "Assemble feed",
            "Build pinned usign",
            "Sign feed",
            "Verify signature with usign",
            "Validate deployable inventory",
            "Upload Pages artifact",
        ]
        for name in required:
            self.assertIn(name, names)
        self.assertEqual([names.index(name) for name in required], sorted(names.index(name) for name in required))

        check = build_steps[names.index("Check signing key")]
        self.assertEqual(check.get("env"), {
            "OPENWRT_FEED_USIGN_PRIVATE_KEY": "${{ secrets.OPENWRT_FEED_USIGN_PRIVATE_KEY }}"
        })
        self.assertRegex(check["run"], r'test -n "\$OPENWRT_FEED_USIGN_PRIVATE_KEY"')
        self.assertNotRegex(check["run"], r"(?m)^\s*(echo|printf).*OPENWRT_FEED_USIGN_PRIVATE_KEY")

        build = build_steps[names.index("Build pinned usign")]["run"]
        self.assertIn("https://git.openwrt.org/project/usign.git", build)
        self.assertEqual(build.count(USIGN_COMMIT), 1)
        self.assertIn("git -C", build)
        self.assertIn("fetch --depth 1 origin", build)
        self.assertIn("checkout --detach FETCH_HEAD", build)
        self.assertIn("cmake --build", build)

        sign = build_steps[names.index("Sign feed")]
        self.assertEqual(sign.get("env"), {
            "OPENWRT_FEED_USIGN_PRIVATE_KEY": "${{ secrets.OPENWRT_FEED_USIGN_PRIVATE_KEY }}"
        })
        self.assertIn("scripts/sign_feed.sh", sign["run"])
        verify = build_steps[names.index("Verify signature with usign")]["run"]
        for argument in ("-V", "pages/Packages", "pages/keithah-feed.pub", "pages/Packages.sig"):
            self.assertIn(argument, verify)
        self.assert_publisher_commands_are_exact(workflow)

        upload = build_steps[names.index("Upload Pages artifact")]
        self.assertEqual(upload.get("uses"), "actions/upload-pages-artifact@v5")
        self.assertEqual(upload.get("with", {}).get("path"), "pages")
        self.assertEqual(upload.get("with", {}).get("include-hidden-files"), "true")
        deploy_steps = steps(workflow["jobs"]["deploy"])
        self.assertEqual(len(deploy_steps), 1)
        self.assertEqual(deploy_steps[0].get("uses"), "actions/deploy-pages@v5")
        self.assertNotIn("continue-on-error", text)
        self.assertEqual(text.count("secrets.OPENWRT_FEED_USIGN_PRIVATE_KEY"), 2)
        fetch = build_steps[names.index("Fetch immutable releases")]
        self.assertEqual(fetch.get("env"), {"GH_TOKEN": "${{ github.token }}"})

    def test_publisher_command_policy_rejects_appended_arguments(self):
        workflow, _ = load_workflow("pages.yml")
        build_steps = steps(workflow["jobs"]["build"])
        assembler = next(item for item in build_steps if item.get("name") == "Assemble feed")
        assembler["run"] += " --unexpected"
        with self.assertRaises(AssertionError):
            self.assert_publisher_commands_are_exact(workflow)

    def test_current_official_action_majors_are_used(self):
        ci, _ = load_workflow("test.yml")
        pages, _ = load_workflow("pages.yml")
        used = [item.get("uses") for job in [*ci["jobs"].values(), *pages["jobs"].values()]
                for item in steps(job) if "uses" in item]
        self.assertIn("actions/checkout@v7", used)
        self.assertIn("actions/setup-python@v7", used)
        self.assertIn("actions/upload-pages-artifact@v5", used)
        self.assertIn("actions/deploy-pages@v5", used)
        self.assertTrue(all(re.fullmatch(r"actions/[a-z-]+@v[0-9]+", item) for item in used))

    def test_documentation_and_ignore_policy_are_explicit(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/keithah/openwrt-packages", readme)
        self.assertIn("https://keithah.github.io/openwrt-packages", readme)
        self.assertIn("aarch64_cortex-a53", readme)
        self.assertIn("f6c72c675c844b91", readme)
        for product in ("starwatch", "wattline", "ookla-speedtest-cli"):
            installer = f"install-{product}.sh"
            url = f"https://keithah.github.io/openwrt-packages/{installer}"
            self.assertIn(f"wget -qO- {url} | sh", readme)
            self.assertIn(f"curl -fsSL {url} | sh", readme)
        for repository in (
            "keithah/openwrt-starwatch",
            "keithah/openwrt-wattline",
            "keithah/openwrt-ookla-speedtest-cli",
        ):
            self.assertIn(f"https://github.com/{repository}", readme)
        for package in (
            "starwatchd", "luci-app-starwatch", "gl-app-starwatch", "wattlined",
            "wattline-bt", "wattline-rtl8761b", "luci-app-wattline",
            "gl-app-wattline", "ookla-speedtest-cli",
        ):
            self.assertIn(f"`{package}`", readme)
        for phrase in ("hourly", "manually", "fails the whole", "last verified", "no Ookla binary", "EULA"):
            self.assertIn(phrase, readme)

        ignored = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
        self.assertTrue({
            "downloads/", "pages/", "__pycache__/", "*.pyc", "*.ipk", "*.tgz",
            "*.tar", "*.tar.gz", "*.zip", "*.sec", "*.key", "*.pem", "*.priv",
            "*.private",
        } <= ignored)
        self.assertNotIn("*.pub", ignored)


if __name__ == "__main__":
    unittest.main()
