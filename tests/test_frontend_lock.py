from __future__ import annotations

import asyncio
import unittest

from research_harness.frontend.lock import LockBusyError, SingleActiveRunLock


class LockTests(unittest.IsolatedAsyncioTestCase):
    async def test_acquire_records_holder(self) -> None:
        lock = SingleActiveRunLock()
        async with lock.acquire("thread_x", "grilling") as holder:
            self.assertTrue(lock.held())
            self.assertEqual(holder.thread_id, "thread_x")
            self.assertEqual(holder.phase, "grilling")
        self.assertFalse(lock.held())
        self.assertIsNone(lock.holder)

    async def test_second_acquire_refuses(self) -> None:
        lock = SingleActiveRunLock()
        async with lock.acquire("a", "grilling"):
            with self.assertRaises(LockBusyError):
                async with lock.acquire("b", "refine"):
                    self.fail("should not have acquired")

    async def test_release_after_exception(self) -> None:
        lock = SingleActiveRunLock()
        with self.assertRaises(RuntimeError):
            async with lock.acquire("a", "grilling"):
                raise RuntimeError("boom")
        # Lock should be released even after an exception inside the with-block.
        self.assertFalse(lock.held())
        async with lock.acquire("b", "refine"):
            self.assertTrue(lock.held())


if __name__ == "__main__":
    unittest.main()
