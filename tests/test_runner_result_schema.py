from __future__ import annotations

import unittest

from research_harness.schemas.validator import validate_named_schema


class RunnerResultSchemaTests(unittest.TestCase):
    def test_runner_result_schema_accepts_completed_result(self) -> None:
        result = {
            "job_id": "job_1",
            "node_id": "n_demo_001",
            "status": "completed",
            "exit_code": 0,
            "elapsed_sec": 0.1,
            "timeout_sec": 60,
            "workspace": "/tmp/workspace",
            "source_files": ["/tmp/workspace/experiment.py"],
            "command": ["python", "-c", "print(1)"],
            "stdout_path": "/tmp/workspace/stdout.log",
            "stderr_path": "/tmp/workspace/stderr.log",
            "failure_record_candidate": None,
        }

        validate_named_schema("runner_result", result)


if __name__ == "__main__":
    unittest.main()
