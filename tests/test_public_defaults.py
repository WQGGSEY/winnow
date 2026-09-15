"""Keep distributed defaults usable without the maintainer's machine or data."""

import json
from pathlib import Path
import unittest


class PublicDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = json.loads(
            (Path(__file__).resolve().parents[1] / "settings.json").read_text(
                encoding="utf-8"
            )
        )

    def test_default_runner_uses_path_instead_of_private_executables(self):
        self.assertEqual(self.settings["runtime"]["runner_executable_overrides"], {})

    def test_default_resources_do_not_advertise_operator_private_assets(self):
        runtime = self.settings["runtime"]
        self.assertEqual(runtime["runner_capabilities"], ["local_runner", "cpu"])
        self.assertEqual(self.settings["data_adapters"]["registered"], [])
        self.assertEqual(runtime["llm_orchestrator"]["mcp"]["server_env"], {})


if __name__ == "__main__":
    unittest.main()
