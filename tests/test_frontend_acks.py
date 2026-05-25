from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_harness.frontend import acks


class AcksTests(unittest.TestCase):
    def test_subscription_ack_starts_absent_and_can_be_granted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertFalse(acks.has_subscription_ack(repo))
            at = acks.grant_subscription_ack(repo)
            self.assertTrue(at)
            self.assertTrue(acks.has_subscription_ack(repo))
            # Idempotent — second grant keeps the same timestamp.
            again = acks.grant_subscription_ack(repo)
            self.assertEqual(at, again)

    def test_revoke_subscription_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            acks.grant_subscription_ack(repo)
            acks.revoke_subscription_ack(repo)
            self.assertFalse(acks.has_subscription_ack(repo))

    def test_full_auto_default_off_and_toggleable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertFalse(acks.full_auto_mode(repo))
            acks.set_full_auto_mode(repo, True)
            self.assertTrue(acks.full_auto_mode(repo))
            acks.set_full_auto_mode(repo, False)
            self.assertFalse(acks.full_auto_mode(repo))

    def test_requires_modal_respects_full_auto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.assertTrue(acks.requires_modal(repo))
            acks.set_full_auto_mode(repo, True)
            self.assertFalse(acks.requires_modal(repo))

    def test_execute_ack_record_validates_mode(self) -> None:
        record = acks.make_execute_ack_record("grilling", mode="manual")
        self.assertEqual(record["phase"], "grilling")
        self.assertEqual(record["mode"], "manual")
        self.assertIn("at", record)
        with self.assertRaises(ValueError):
            acks.make_execute_ack_record("grilling", mode="bogus")


if __name__ == "__main__":
    unittest.main()
