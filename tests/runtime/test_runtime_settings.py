from __future__ import annotations

import unittest

from time_tl.runtime.services import RuntimeConfiguredAlertWorker


class Reader:
    def __init__(self, result: tuple[int, int] | Exception) -> None:
        self.result = result
        self.calls = 0

    async def read(self) -> tuple[int, int]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RuntimeAlertSettingsTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, reader: Reader) -> RuntimeConfiguredAlertWorker:
        return RuntimeConfiguredAlertWorker(
            provider=object(),
            repository=object(),
            lease=object(),
            poll_interval_seconds=15,
            requests_per_minute=5,
            monotonic=lambda: 0.0,
            jitter=lambda _low, _high: 0.0,
            runtime_settings=reader,
            stale_after_seconds=120,
        )

    async def test_valid_pair_is_applied_between_cycles(self) -> None:
        reader = Reader((30, 180))
        worker = self.worker(reader)
        await worker._refresh_runtime_settings()
        self.assertEqual(30.0, worker.poll_interval_seconds)
        self.assertEqual(180, worker.stale_after_seconds)
        self.assertEqual(1, reader.calls)

    async def test_unavailable_settings_keep_last_safe_values(self) -> None:
        reader = Reader(RuntimeError("database unavailable"))
        worker = self.worker(reader)
        await worker._refresh_runtime_settings()
        self.assertEqual(15.0, worker.poll_interval_seconds)
        self.assertEqual(120, worker.stale_after_seconds)


if __name__ == "__main__":
    unittest.main()
