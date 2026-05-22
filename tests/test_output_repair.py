from __future__ import annotations

import json
import unittest

from research_harness.orchestrator.demo import _demo_node
from research_harness.workers.output_repair import OutputRepairError, parse_or_repair_json


class OutputRepairTests(unittest.TestCase):
    def _valid_report(self) -> dict[str, object]:
        node = _demo_node()
        return {
            "node_id": node["id"],
            "status": "completed",
            "claim_verdict_candidate": "supported",
            "metrics": {"x": 1},
            "baselines": {"naive": 0},
            "disproof_conditions_hit": [],
            "artifacts": [],
            "unexpected_observations": [],
            "failure_record_candidate": None,
        }

    def test_valid_json_passes_without_repair(self) -> None:
        report = self._valid_report()
        result = parse_or_repair_json(json.dumps(report), "worker_report")

        self.assertFalse(result.repaired)
        self.assertEqual(result.data["node_id"], report["node_id"])

    def test_markdown_fenced_json_is_format_repaired(self) -> None:
        report = self._valid_report()
        raw = "```json\n" + json.dumps(report) + "\n```"

        result = parse_or_repair_json(raw, "worker_report")

        self.assertTrue(result.repaired)
        self.assertEqual(result.note, "format_only_repair")

    def test_surrounding_prose_is_format_repaired(self) -> None:
        report = self._valid_report()
        raw = "Here is the result:\n" + json.dumps(report) + "\nDone."

        result = parse_or_repair_json(raw, "worker_report")

        self.assertTrue(result.repaired)
        self.assertEqual(result.data["status"], "completed")

    def test_schema_invalid_json_is_not_repaired_by_invention(self) -> None:
        report = self._valid_report()
        del report["metrics"]

        with self.assertRaisesRegex(OutputRepairError, "schema-invalid"):
            parse_or_repair_json(json.dumps(report), "worker_report")

    def test_multiple_json_objects_are_rejected(self) -> None:
        report = self._valid_report()
        raw = json.dumps(report) + "\n" + json.dumps(report)

        with self.assertRaisesRegex(OutputRepairError, "multiple JSON objects"):
            parse_or_repair_json(raw, "worker_report")


if __name__ == "__main__":
    unittest.main()
