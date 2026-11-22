import time
import unittest

from tests import test_scdfe_d as fixture


class CollectionIntervalTests(unittest.TestCase):
    setUp = fixture.DeviceTests.setUp
    server = fixture.DeviceHTTPTests.server
    call = staticmethod(fixture.DeviceHTTPTests.call)

    def _wait_for_batch(self, store, minimum, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            last = store.overview()["last_refresh"]
            if last is not None and last["refresh_id"] >= minimum:
                return last["refresh_id"]
            time.sleep(0.05)
        self.fail("collector did not reach the requested batch in time")

    def _save(self, base, seconds):
        return self.call(
            base, "/api/collector/interval", "PUT", {"interval_seconds": seconds}
        )

    def test_default_bounds_and_live_rescheduling(self):
        server, base = self.server()
        self.assertEqual(self.call(base, "/api/overview")[1]["interval_seconds"], 5)
        for invalid in (0, 61, 1.5, "5", True, None):
            self.assertEqual(self._save(base, invalid)[0], 400)
        self.assertEqual(self._save(base, 60)[1]["interval_seconds"], 60)
        self.assertEqual(self.call(base, "/api/collector/start", "POST", {})[0], 200)
        previous = self._wait_for_batch(server.store, 1)
        worker = server.collector._thread
        status, saved = self._save(base, 1)
        self.assertEqual(
            (status, saved["interval_seconds"], saved["running"]), (200, 1, True)
        )
        self._wait_for_batch(server.store, previous + 1, seconds=4)
        self.assertIs(server.collector._thread, worker)
        self.call(base, "/api/collector/stop", "POST", {})
        self.assertEqual(server.store.overview()["interval_seconds"], 1)
