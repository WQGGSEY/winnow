from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from research_harness.local_preflight import run_preflight


REPO_ROOT = Path(__file__).resolve().parents[1]


class LocalPreflightTests(unittest.TestCase):
    def test_run_preflight_returns_passed_summary(self) -> None:
        result = run_preflight(REPO_ROOT)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["settings_backend"], "mock")
        self.assertEqual(result["runner_result_status"], "completed")
        self.assertGreaterEqual(result["promotion_critic_count"], 1)
        self.assertGreaterEqual(result["rebuttal_critic_count"], 1)

    def test_preflight_module_cli_outputs_json(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "research_harness.local_preflight"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "passed")


if __name__ == "__main__":
    unittest.main()
