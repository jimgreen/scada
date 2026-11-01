"""BACnet/IP adapter tests; the protocol test uses two local bacpypes3 apps."""

import asyncio
import socket
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bacpypes3.ipv4.app import NormalApplication
from bacpypes3.local.analog import AnalogValueObject
from bacpypes3.local.device import DeviceObject
from bacpypes3.pdu import IPv4Address

from api import BacnetMaster
from api.bacnet import BacnetError, _LoopThread
from web.service import ScadaStore


def _udp_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _BacnetServer:
    def __init__(self):
        self.port = _udp_port()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("BACnet test server did not start")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._setup())
        self.ready.set()
        self.loop.run_until_complete(self._wait())
        self.app.close()
        self.loop.close()

    async def _setup(self):
        device = DeviceObject(objectIdentifier=("device", 1234), objectName="test-device")
        self.app = NormalApplication(device, IPv4Address(f"127.0.0.1:{self.port}"))
        self.value = AnalogValueObject(
            objectIdentifier=("analog-value", 1), objectName="AV_1", presentValue=12.5
        )
        self.app.add_object(self.value)

    async def _wait(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(0.01)

    def close(self):
        self.stop_event.set()
        self.thread.join(5)


class BacnetTests(unittest.TestCase):
    def test_asyncio_thread_closes_and_failed_connect_does_not_leak_it(self):
        thread = _LoopThread("scada-bacnet-test")
        try:
            async def answer():
                return 7
            self.assertEqual(thread.call(answer), 7)
        finally:
            thread.close()
        self.assertFalse(thread.thread.is_alive())

        client = BacnetMaster("127.0.0.1", timeout=0.2)

        async def fail_connect():
            raise RuntimeError("test connect failure")

        client._connect_backend = fail_connect
        with self.assertRaises(BacnetError):
            client.connect()
        self.assertIsNone(client._loop_thread)

    def test_local_bacnet_ip_read_write_and_point_poll(self):
        server = _BacnetServer()
        server.start()
        client = BacnetMaster(
            "127.0.0.1", remote_port=server.port, local_port=_udp_port(), timeout=2,
            rtu_id=7,
        )
        try:
            with client:
                self.assertEqual(client.read_property("analog-value", 1), 12.5)
                client.write_property("analog-value", 1, 33.25)
                self.assertEqual(client.read_property("analog-value", 1), 33.25)
                client.configure([{
                    "rtu_id": 7, "kind": "yc", "pnt_no": 1,
                    "object_type": "analog-value", "instance": 1,
                }])
                value = client.collect_once()[0]
                self.assertEqual((value.rtu_id, value.point_no, value.kind), (7, 1, "yc"))
                self.assertEqual(value.value, 33.25)
        finally:
            client.close()
            server.close()

    def test_fake_backend_and_validation(self):
        class Fake:
            def __init__(self):
                self.values = {("analog-value", 1): 3.0}

            def connect(self):
                return None

            def close(self):
                return None

            def read_property(self, object_type, instance, property_identifier, **kwargs):
                return self.values[(object_type, instance)]

            def write_property(self, object_type, instance, value, property_identifier, **kwargs):
                self.values[(object_type, instance)] = value

        fake = Fake()
        client = BacnetMaster("127.0.0.1", backend=fake)
        with client:
            client.configure([{
                "kind": "yc", "pnt_no": 1,
                "object_type": "analog-value", "instance": 1,
            }])
            self.assertEqual(client.collect_once()[0].value, 3.0)
            client.send_yt_point({
                "kind": "yt", "object_type": "analog-value", "instance": 1,
            }, 4.0)
            self.assertEqual(client.read_property("analog-value", 1), 4.0)
        with self.assertRaises(BacnetError):
            client.read_property("analog-value", 1)
        with self.assertRaises(ValueError):
            BacnetMaster("127.0.0.1", backend=fake).configure([{
                "kind": "yx", "pnt_no": 1,
                "object_type": "analog-value", "instance": 1,
            }])

    def test_master_point_number_commands_and_collect(self):
        class Fake:
            def __init__(self):
                self.values = {("analog-value", 1): 3.0, ("binary-output", 2): 0}
            def connect(self): pass
            def close(self): pass
            def read_property(self, object_type, instance, property_identifier, **kwargs):
                return self.values[(object_type, instance)]
            def write_property(self, object_type, instance, value, property_identifier, **kwargs):
                self.values[(object_type, instance)] = value
        master = BacnetMaster("127.0.0.1", rtu_id=7, backend=Fake())
        master.configure([
            {"rtu_id": 7, "kind": "yc", "pnt_no": 1,
             "object_type": "analog-value", "instance": 1},
            {"rtu_id": 7, "kind": "yk", "pnt_no": 2,
             "object_type": "binary-output", "instance": 2},
        ])
        with master:
            self.assertEqual(master.collect_once()[0].value, 3.0)
            master.send_yk_point(2, True)
            self.assertEqual(master.read_property("binary-output", 2), True)

    def test_from_store_uses_current_channel_table_and_explicit_point_map(self):
        with TemporaryDirectory() as directory:
            store = ScadaStore(Path(directory) / "scada.sqlite3")
            store.create_row("fes_rtu_define", {
                "rtu_id": 7, "channel_type": "bacnet",
                "channel_id": 1,
            })
            store.create_row("fes_channel_bacnet_define", {
                "id": 1, "rtu_id": 7, "bind_ip": "127.0.0.1",
                "bind_port": 47809, "dev_id": 1001,
            })
            store.create_row("fes_yc_define", {"rtu_id": 7, "pnt_no": 1, "name": "AI_1"})
            master = BacnetMaster.from_store(
                store, 7, point_map={("yc", 1): ("analog-value", 1)},
            )
            self.assertEqual(master._points[0]["object_type"], "analog-value")


if __name__ == "__main__":
    unittest.main()
