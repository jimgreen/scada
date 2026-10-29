"""OPC UA adapter tests using a local asyncua server."""

import asyncio
import socket
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from asyncua import Server, ua

from api import OpcUaMaster
from api.opc import OpcError, _LoopThread
from web.service import ScadaStore


def _tcp_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _OpcServer:
    def __init__(self):
        self.port = _tcp_port()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(15):
            raise RuntimeError("OPC UA test server did not start")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._setup())
        self.ready.set()
        self.loop.run_until_complete(self._wait())
        self.loop.run_until_complete(self.server.stop())
        self.loop.close()

    async def _setup(self):
        self.server = Server()
        await self.server.init()
        self.server.set_endpoint(f"opc.tcp://127.0.0.1:{self.port}/freeopcua/server/")
        namespace = await self.server.register_namespace("urn:scada:test")
        node_id = ua.NodeId("YC_1", namespace)
        self.node = await self.server.nodes.objects.add_variable(node_id, "YC_1", 12.5)
        await self.node.set_writable()
        await self.server.start()
        self.endpoint = f"opc.tcp://127.0.0.1:{self.port}/freeopcua/server/"
        self.node_id = f"ns={namespace};s=YC_1"

    async def _wait(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(0.01)

    def close(self):
        self.stop_event.set()
        self.thread.join(10)


class OpcTests(unittest.TestCase):
    def test_asyncio_thread_closes_and_failed_connect_does_not_leak_it(self):
        thread = _LoopThread("scada-opc-test")
        try:
            async def answer():
                return 7
            self.assertEqual(thread.call(answer), 7)
        finally:
            thread.close()
        self.assertFalse(thread.thread.is_alive())

        client = OpcUaMaster("opc.tcp://127.0.0.1:4840", timeout=0.2)

        async def fail_connect():
            raise RuntimeError("test connect failure")

        client._connect_backend = fail_connect
        with self.assertRaises(OpcError):
            client.connect()
        self.assertIsNone(client._loop_thread)

    def test_local_opcua_read_write_and_poll(self):
        server = _OpcServer()
        server.start()
        client = OpcUaMaster(server.endpoint, timeout=5, rtu_id=7)
        try:
            with client:
                self.assertEqual(client.read_node(server.node_id), 12.5)
                client.write_node(server.node_id, 44.0)
                self.assertEqual(client.read_node(server.node_id), 44.0)
                client.configure([{
                    "rtu_id": 7, "kind": "yc", "pnt_no": 1, "node_id": server.node_id,
                }])
                result = client.collect_once()[0]
                self.assertEqual((result.rtu_id, result.point_no, result.kind), (7, 1, "yc"))
                self.assertEqual(result.value, 44.0)
        finally:
            client.close()
            server.close()

    def test_fake_backend_and_opc_da_is_rejected(self):
        class Fake:
            def __init__(self):
                self.values = {"ns=2;s=YC_1": 1.0}

            def connect(self):
                return None

            def close(self):
                return None

            def read_node(self, node_id):
                return self.values[node_id]

            def write_node(self, node_id, value):
                self.values[node_id] = value
                return value

        fake = Fake()
        client = OpcUaMaster("opc.tcp://fake:4840", backend=fake)
        with client:
            client.configure([{"kind": "yc", "pnt_no": 1, "node_id": "ns=2;s=YC_1"}])
            self.assertEqual(client.collect_once()[0].value, 1.0)
            client.send_yt_point({"kind": "yt", "node_id": "ns=2;s=YC_1"}, 2.0)
            self.assertEqual(client.read_node("ns=2;s=YC_1"), 2.0)
        with self.assertRaises(ValueError):
            OpcUaMaster("Matrikon.OPC.Simulation.1")
        with self.assertRaises(OpcError):
            client.read_node("ns=2;s=YC_1")

    def test_master_point_number_commands_and_collect(self):
        class Fake:
            def __init__(self):
                self.values = {"ns=2;s=YC_1": 1.0, "ns=2;s=YK_1": False}
            def connect(self): pass
            def close(self): pass
            def read_node(self, node_id): return self.values[node_id]
            def write_node(self, node_id, value):
                self.values[node_id] = value
                return value
        master = OpcUaMaster("opc.tcp://fake:4840", rtu_id=7, backend=Fake())
        master.configure([
            {"rtu_id": 7, "kind": "yc", "pnt_no": 1, "node_id": "ns=2;s=YC_1"},
            {"rtu_id": 7, "kind": "yk", "pnt_no": 2, "node_id": "ns=2;s=YK_1"},
        ])
        with master:
            self.assertEqual(master.collect_once()[0].value, 1.0)
            master.send_yk_point(2, True)
            self.assertTrue(master.read_node("ns=2;s=YK_1"))

    def test_from_store_uses_current_channel_table_and_explicit_node_map(self):
        with TemporaryDirectory() as directory:
            store = ScadaStore(Path(directory) / "scada.sqlite3")
            store.create_row("fes_rtu_define", {
                "rtu_id": 8, "channel_type": "opc",
                "channel_id": 1, "chan_addr": "127.0.0.1", "chan_port": 4840,
            })
            store.create_row("fes_channel_opc_define", {
                "id": 1, "rtu_id": 8,
                "opc_name": "opc.tcp://127.0.0.1:4840",
            })
            store.create_row("fes_yc_define", {"rtu_id": 8, "pnt_no": 1, "name": "YC_1"})
            master = OpcUaMaster.from_store(
                store, 8, point_map={("yc", 1): "ns=2;s=YC_1"},
            )
            self.assertEqual(master._points[0]["node_id"], "ns=2;s=YC_1")


if __name__ == "__main__":
    unittest.main()
