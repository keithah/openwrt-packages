from pathlib import Path
import shlex
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"


class WorkflowCliEntrypointTest(unittest.TestCase):
    def test_python_workflow_entrypoints_start_from_repository_root(self):
        workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["build"]["steps"]
        by_name = {step.get("name"): step for step in steps}

        for name in (
            "Fetch immutable releases",
            "Assemble feed",
            "Validate deployable inventory",
        ):
            with self.subTest(step=name):
                command = shlex.split(by_name[name]["run"])
                result = subprocess.run(
                    [*command[:3], "--help"] if command[1] == "-m" else [*command[:2], "--help"],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_shell_workflow_entrypoint_has_valid_syntax(self):
        result = subprocess.run(
            ["sh", "-n", "scripts/sign_feed.sh"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
