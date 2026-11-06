"""Real three-Web acceptance on private databases and loopback sockets.

The s simulator has its own database, so a source Web update cannot make the
c/t assertions pass by writing directly into their acquisition database.
Runtime processes are started/stopped through their public Web APIs.
Run serially with other tests that claim the host-wide scdfe_c main lease.
"""
from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pymodbus.client import ModbusTcpClient

from core.process_registry import PROCESS_TABLE
from core.scdfe_control import CONTROL_TABLE, ServerInstanceLock
from scdfe_c.command_server import CommandClient
from scdfe_c.web.server import make_server as c_web
from scdfe_s.web.server import make_server as s_web
from scdfe_t.web.server import make_server as t_web


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ThreeWebInteractionTests(unittest.TestCase):
    SOURCE = 37101  # Same RTU, channel and point numbers as C, in a different database.
    RTUS = (37101, 37102)
    WEB_HOST = "127.0.0.1"
    WEB_PORTS = {}  # An acceptance runner may assign fixed ports for a maintenance window.

    def setUp(self):
        if ServerInstanceLock.held():
            self.skipTest("host scdfe_c main instance is occupied; run this acceptance test serially")
        self.temp = tempfile.TemporaryDirectory(prefix="scada-three-web-")
        self.root = Path(self.temp.name)
        self.webs = {}
        self.threads = []
        self.clients = []
        self.channel_created = False
        self.addCleanup(self._cleanup)
        for name, factory, database in (
            ("c", c_web, self.root / "system" / "scada.sqlite3"),
            ("s", s_web, self.root / "source" / "scdfe_s.sqlite3"),
            ("t", t_web, self.root / "system" / "scada.sqlite3"),
        ):
            web = self.webs[name] = factory(self.WEB_HOST, self.WEB_PORTS.get(name, 0), db_path=database)
            thread = threading.Thread(target=web.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.webs["c"].process_controller.timeout = 20
        self.source_port, self.forward_port, self.command_port = free_port(), free_port(), free_port()
        self.row_id = 0

    def api(self, name, path, method="GET", body=None, *, status=200, headers=None):
        request = Request(
            f"http://{self.WEB_HOST}:{self.webs[name].server_port}{path}", method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            response = urlopen(request, timeout=25)
        except HTTPError as exc:
            response = exc
        with response:
            result = json.loads(response.read())
            self.assertEqual(response.status, status, (name, method, path, result))
            return result

    def wait_for(self, predicate, *, timeout=12, label="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.05)
        diagnostics = {}
        if "c" in self.webs:
            store = self.webs["c"].store
            with store.connection() as connection:
                for table in ("fes_rtu_define", "fes_yc_define", PROCESS_TABLE):
                    diagnostics[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
        diagnostics["logs"] = {
            str(path.relative_to(self.root)): path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
            for path in self.root.rglob("*.log")
        }
        self.fail(f"timed out waiting for {label}: {json.dumps(diagnostics, ensure_ascii=False)}")

    def control(self, target, action):
        self.api("c", "/api/processes/control", "POST", {"target": target, "action": action}, status=202)
        def completed():
            value = self.api("c", "/api/processes")
            return value if not value["busy"] else None
        snapshot = self.wait_for(
            completed,
            timeout=25, label=f"{target} {action}",
        )
        self.assertEqual(snapshot["operation"]["status"], "completed", snapshot["operation"])
        return snapshot

    def _table(self, web, table, values):
        return self.api(web, f"/api/tables/{table}/rows", "POST", values, status=201)["item"]

    def _numbered(self, web, table, values):
        self.row_id += 1
        return self._table(web, table, {"id": self.row_id, **values})

    def _modbus_definitions(self, web, rtu, *, yc_addresses, yx=True, yk_addresses=(), yt_address=None):
        self._numbered(web, "fes_channel_modbus_define", {"rtu_id": rtu, "irtuaddr": 1})
        self._table(web, "fes_channel_modbus_tcp_define", {
            "rtu_id": rtu, "remote_host": "127.0.0.1", "remote_port": self.source_port,
            "unit_id": 1, "response_timeout": .5, "connect_timeout": .5,
        })
        self._numbered(web, "fes_channel_modbus_define_call", {
            "rtu_id": rtu, "call_seq": 0, "irtuaddr": 1,
            "iplace": min(yc_addresses.values()),
            "inum": max(yc_addresses.values()) - min(yc_addresses.values()) + 1, "icode": 3,
        })
        for point, address in yc_addresses.items():
            self._numbered(web, "fes_channel_modbus_define_data", {
                "rtu_id": rtu, "call_seq": 0, "cnt": point, "reg_addr": address,
                "if_yx": 0, "trans_type": 2,
            })
        if yx:
            self._numbered(web, "fes_channel_modbus_define_call", {
                "rtu_id": rtu, "call_seq": 1, "irtuaddr": 1, "iplace": 0, "inum": 1, "icode": 2,
            })
            self._numbered(web, "fes_channel_modbus_define_data", {
                "rtu_id": rtu, "call_seq": 1, "cnt": 1, "reg_addr": 0, "if_yx": 1, "trans_type": 8,
            })
        for point, address in dict(yk_addresses).items():
            self._numbered(web, "fes_channel_modbus_define_ytyk", {
                "rtu_id": rtu, "cnt": point, "ifyk": 1, "irtuaddr": 1, "reg_addr": address, "icode": 5,
            })
        if yt_address is not None:
            point, address = yt_address
            self._numbered(web, "fes_channel_modbus_define_ytyk", {
                "rtu_id": rtu, "cnt": point, "ifyk": 0, "irtuaddr": 1, "reg_addr": address,
                "icode": 6, "ytyk_type": 2, "multi_para": 1,
            })

    def _create_source(self):
        self.api("s", "/api/channels", "POST", {
            "rtu_id": self.SOURCE, "channel_id": self.SOURCE + 1000, "channel_type": "modbus-tcp",
            "name": "Acceptance source", "process_enable": 0,
        }, status=201)
        for kind, point in (("yc", 1), ("yc", 2), ("yx", 1), ("yk", 3), ("yk", 4), ("yt", 5)):
            self.api("s", f"/api/channels/{self.SOURCE}/points/{kind}", "POST", {
                "pnt_no": point, "name": f"Source {kind} {point}", "value": 0,
            }, status=201)
        self._modbus_definitions("s", self.SOURCE, yc_addresses={1: 0, 2: 1},
                                 yk_addresses={3: 2, 4: 3}, yt_address=(5, 4))
        self.api("s", "/api/modbus-tcp-server/config", "PATCH", {
            "rtu_id": self.SOURCE, "bind_ip": "127.0.0.1", "listen_port": self.source_port,
            "unit_id": 1,
        })
        self.assertTrue(self.api("s", "/api/modbus-tcp-server/start", "POST", {})["item"]["running"])
        for kind, point, value in (("yc", 1, 12), ("yc", 2, 31), ("yx", 1, 1), ("yt", 5, 24)):
            self.source_update(kind, point, value)

    def source_update(self, kind, point, value):
        self.api("s", "/api/modbus-tcp-server/update/incremental", "POST", {
            "kind": kind, "pnt_no": point, "value": value,
        })

    def _create_acquisition(self):
        self.api("c", "/api/scdfe_c/control", "PATCH", {
            "scan_interval_ms": 100, "poll_interval_ms": 50, "retry_interval_ms": 100,
            "worker_start_delay_ms": 0,
        })
        self.api("c", "/api/scdfe_c/command-server", "PATCH", {
            "bind_host": "127.0.0.1", "bind_port": self.command_port, "auth_token": "acceptance-only-token",
        })
        for index, rtu in enumerate(self.RTUS):
            self.api("c", "/api/rtus", "POST", {
                "rtu_id": rtu, "channel_id": rtu + 1000, "channel_type": "modbus-tcp",
                "name": f"Acceptance RTU {index + 1}", "process_enable": 1,
            }, status=201)
            for kind in ("yc", "yx", "yk", "yt"):
                self.api("c", f"/api/rtus/{rtu}/points/{kind}", "POST", {
                    "pnt_no": 1, "name": f"{kind} one", "value": 24 if kind == "yt" else 0,
                    **({"cmd_time": 0, "cmd_handle_time": 0} if kind in {"yk", "yt"} else {}),
                }, status=201)
            self._modbus_definitions("c", rtu, yc_addresses={1: index},
                                     yk_addresses={1: index + 2}, yt_address=(1, 4 + index))

    def _create_forwarder(self):
        self.api("t", "/api/trans/channels", "POST", {
            "channel_id": 1, "code": "ACCEPTANCE", "name": "Acceptance forwarder",
            "protocol": "modbus-tcp", "bind_host": "127.0.0.1", "bind_port": self.forward_port,
            "unit_id": 1, "scan_interval_ms": 50,
        }, status=201)
        self.channel_created = True
        for index, rtu in enumerate(self.RTUS):
            for kind, area, dtype in (("yc", "input", "uint16"), ("yk", "coil", "bool")):
                self.api("t", "/api/trans/channels/1/points", "POST", {
                    "pnt_no": index + 1, "kind": kind, "source_rtu_id": rtu, "source_pnt_no": 1,
                    "register_area": area, "address": index, "data_type": dtype,
                }, status=201)
        for kind, area, address, dtype, extra in (
            ("yx", "discrete", 0, "bool", {}),
            ("yt", "holding", 10, "uint16", {"scale": 2, "offset": 10}),
        ):
            self.api("t", "/api/trans/channels/1/points", "POST", {
                "pnt_no": 1, "kind": kind, "source_rtu_id": self.RTUS[0], "source_pnt_no": 1,
                "register_area": area, "address": address, "data_type": dtype, **extra,
            }, status=201)

    def c_point(self, rtu, kind):
        return self.api("c", f"/api/rtus/{rtu}/points/{kind}/1")["item"]

    def handled(self, rtu, kind, expected):
        def check():
            row = self.c_point(rtu, kind)
            return row if (row["value"] == expected and row["status"] == 1
                           and (row["cmd_time"] or 0) > 0
                           and row["cmd_handle_time"] >= row["cmd_time"]) else None
        return self.wait_for(check, label=f"{rtu} {kind} acknowledgment")

    def client(self, port):
        client = ModbusTcpClient("127.0.0.1", port=port, timeout=.5, retries=0)
        self.clients.append(client)
        self.wait_for(client.connect, label=f"listener {port}")
        return client

    def test_same_number_crud_is_independent_for_all_four_point_types(self):
        cdb = self.api("c", "/api/health")["database"]
        self.assertEqual(cdb, self.api("t", "/api/health")["database"])
        self.assertNotEqual(cdb, self.api("s", "/api/health")["database"])
        kinds = ("yc", "yx", "yk", "yt")
        rtu = {"rtu_id": self.SOURCE, "channel_id": self.SOURCE+1000, "channel_type": "modbus-tcp"}
        base = f"/api/rtus/{self.SOURCE}"
        other = f"/api/channels/{self.SOURCE}"
        self.api("c", "/api/rtus", "POST", dict(rtu, name="C"), status=201)
        before = {}
        for kind in kinds:
            before[kind] = self.api("c", base+"/points/"+kind, "POST",
                                    dict(pnt_no=1,name="C-"+kind,value=0),status=201)["item"]
        before_rtu = self.api("c", base)["item"]
        self.api("s", other, status=404)
        self.api("s", "/api/channels", "POST", dict(rtu, name="S"), status=201)
        self.api("s", other, "PATCH", {"name": "S-edit"})
        for kind in kinds:
            points = other+"/points/"+kind
            self.api("s", points, "POST", dict(pnt_no=1,name="S",value=0), status=201)
            self.api("s", points+"/1", "PATCH", dict(name="S-edit",value=1))
            self.assertEqual(self.api("c", base+"/points/"+kind+"/1")["item"],before[kind])
            self.api("s", points+"/1", "DELETE")
            self.api("s", points, "POST", dict(pnt_no=1,name="S-again"), status=201)
        self.api("s", other, "DELETE")
        self.api("s", other, status=404)
        self.assertEqual(self.api("c", base)["item"],before_rtu)
        for kind in kinds:
            self.assertEqual(self.api("c", base+"/points/"+kind+"/1")["item"],before[kind])

    def test_all_three_webs_acquisition_forwarding_commands_and_restart(self):
        for family in ('c','s','t'):
            self.assertFalse(self.api(family, '/api/packet-recording')['enabled'])
            self.assertTrue(self.api(family, '/api/packet-recording', 'PUT', {'enabled':True})['enabled'])
        self._create_source()
        self._create_acquisition()
        self._create_forwarder()
        self.control("main", "start")
        self.wait_for(lambda: self.c_point(self.RTUS[0], "yc")["value"] == 12, label="first acquisition")
        self.wait_for(lambda: self.c_point(self.RTUS[1], "yc")["value"] == 31, label="second acquisition")
        self.wait_for(lambda: self.c_point(self.RTUS[0], "yx")["value"] == 1, label="binary acquisition")
        self.control("command-server", "start")
        self.api("t", "/api/trans/channels/1/start", "POST", {})
        source, remote = self.client(self.source_port), self.client(self.forward_port)
        self.assertEqual(remote.read_input_registers(0, count=2, device_id=1).registers, [12, 31])
        self.assertTrue(remote.read_discrete_inputs(0, count=1, device_id=1).bits[0])
        self.assertEqual(remote.read_holding_registers(10, count=1, device_id=1).registers, [7])

        self.assertFalse(remote.write_coil(0, True, device_id=1).isError())
        self.handled(self.RTUS[0], "yk", 1)
        self.assertEqual(source.read_coils(2, count=2, device_id=1).bits[:2], [True, False])
        self.assertEqual(self.c_point(self.RTUS[1], "yk")["cmd_time"], 0)
        self.assertFalse(remote.write_coil(1, True, device_id=1).isError())
        self.handled(self.RTUS[1], "yk", 1)
        self.assertEqual(source.read_coils(2, count=2, device_id=1).bits[:2], [True, True])
        self.assertFalse(remote.write_register(10, 8, device_id=1).isError())
        self.handled(self.RTUS[0], "yt", 26)
        self.assertEqual(source.read_holding_registers(4, count=1, device_id=1).registers, [26])

        self.source_update("yc", 1, 42)
        self.source_update("yx", 1, 0)
        self.wait_for(lambda: self.c_point(self.RTUS[0], "yc")["value"] == 42, label="source change acquisition")
        self.wait_for(lambda: remote.read_input_registers(0, count=1, device_id=1).registers == [42],
                      label="source change forwarding")
        self.wait_for(lambda: not remote.read_discrete_inputs(0, count=1, device_id=1).bits[0],
                      label="binary change forwarding")
        connection = remote.socket
        deadline, reads = time.monotonic() + 3.3, 0
        while time.monotonic() < deadline:
            response = remote.read_input_registers(0, count=2, device_id=1)
            self.assertFalse(response.isError(), response)
            self.assertEqual(response.registers, [42, 31])
            self.assertIs(remote.socket, connection, "unchanged configuration replaced the live TCP connection")
            reads += 1
            time.sleep(.05)
        self.assertGreaterEqual(reads, 20)
        process = self.api("t", "/api/trans/processes")["items"][0]
        self.assertTrue(process["alive"])
        self.assertEqual(process["process_status"], 2)
        self.assertFalse(process["process_error"])

        # The separate s master must load mappings and expose freshly sampled
        # values, not just echo its own source-definition database.
        self.api("s", "/api/modbus-tcp/config", "PATCH", {
            "rtu_id": self.SOURCE, "remote_host": "127.0.0.1", "remote_port": self.source_port,
            "unit_id": 1, "response_timeout": .5, "connect_timeout": .5,
        })
        self.api("s", "/api/modbus-tcp/start", "POST", {})
        self.assertFalse(source.write_register(0, 47, device_id=1).isError())
        sampled = self.api("s", "/api/modbus-tcp/poll", "POST", {})["item"]
        self.assertGreaterEqual(sampled["count"], 3)
        displayed = self.api("s", "/api/modbus-tcp/points?kind=yc")["items"]
        self.assertEqual(next(row for row in displayed if row["pnt_no"] == 1)["value"], 47)
        self.api("s", "/api/modbus-tcp/commands/yk/3", "POST", {"value": 0})
        self.assertEqual(source.read_coils(2, count=2, device_id=1).bits[:2], [False, True])
        self.api("s", "/api/modbus-tcp/commands/yt/5", "POST", {"value": 35})
        self.assertEqual(source.read_holding_registers(4, count=1, device_id=1).registers, [35])

        # Verify the independently managed command endpoint can stop and
        # restart while the acquisition supervisor remains running.
        main_pid = next(row["process_pid"] for row in self.api("c", "/api/processes")["items"]
                        if row["process_key"] == "main")
        self.control("command-server", "stop")
        self.control("command-server", "start")
        command = CommandClient(self.root / "system" / "scada.sqlite3")
        self.assertTrue(command.submit_command(self.RTUS[0], "yk", 1, 1)["ok"])
        self.handled(self.RTUS[0], "yk", 1)
        self.assertTrue(source.read_coils(2, count=1, device_id=1).bits[0])
        row = self.c_point(self.RTUS[0], "yt")
        self.api("c", f"/api/rtus/{self.RTUS[0]}/commands/yt/1", "POST", {"value": 38},
                 status=202, headers={"If-Match": str(row["_version"])})
        self.handled(self.RTUS[0], "yt", 38)
        self.assertEqual(source.read_holding_registers(4, count=1, device_id=1).registers, [38])
        self.assertEqual(next(row["process_pid"] for row in self.api("c", "/api/processes")["items"]
                              if row["process_key"] == "main"), main_pid)

        # Switch off while all three ends are exchanging real frames. Existing
        # connections and live values must continue, without new packet lines.
        for family in ('c','s','t'):
            self.api(family, '/api/packet-recording', 'PUT', {'enabled':False})
        time.sleep(1.2)  # Remote runtimes reload settings at most once a second.
        def packet_count():
            return sum(path.read_text(encoding='utf-8').count('[报文]') for path in self.root.rglob('*.log'))
        count=packet_count()
        connection=remote.socket
        self.source_update('yc',1,48)
        self.wait_for(lambda:self.c_point(self.RTUS[0],'yc')['value']==48,label='acquisition while packet recording off')
        self.wait_for(lambda:remote.read_input_registers(0,count=1,device_id=1).registers==[48],label='forwarding while packet recording off')
        self.assertIs(remote.socket,connection)
        self.assertEqual(packet_count(),count)

        # Enable only the selected channels while all service defaults remain
        # off. RTU IDs deliberately differ from the channel policy keys.
        def process_packets(process):
            return sum(p.read_text(encoding='utf-8').count('[报文]')
                       for p in self.root.rglob(f'{process}-*.log'))
        processes = [f'scdfe_c-rtu-{rid}' for rid in self.RTUS] + [
            'scdfe_s-modbus-tcp-server', 'scdfe_s-modbus-tcp', 'scdfe_t-channel-1']
        before = [process_packets(name) for name in processes]
        for family, cid in [('c', self.RTUS[0]+1000), ('s', self.SOURCE+1000), ('t', 1)]:
            self.api(family, f'/api/packet-recording/channels/{cid}', 'PUT', {'enabled': True})
            self.assertFalse(self.api(family, '/api/packet-recording')['enabled'])
        time.sleep(1.2)
        self.api('s', '/api/modbus-tcp/poll', 'POST', {})
        self.assertEqual(remote.read_input_registers(0, count=1, device_id=1).registers, [48])
        for index in (0,2,3,4):
            self.wait_for(lambda i=index: process_packets(processes[i]) > before[i], label=processes[index]+' channel recording')
        self.assertEqual(process_packets(processes[1]), before[1], 'second acquisition channel leaked packets')
        self.assertIs(remote.socket, connection)

        self.api('c', f'/api/packet-recording/channels/{self.RTUS[0]+1000}', 'PUT', {'enabled': False})
        self.api('c', f'/api/packet-recording/channels/{self.RTUS[1]+1000}', 'PUT', {'enabled': True})
        time.sleep(1.2)
        first, second = [process_packets(name) for name in processes[:2]]
        self.wait_for(lambda: process_packets(processes[1]) > second, label='second channel independently enabled')
        self.assertEqual(process_packets(processes[0]), first, 'first channel did not stop recording')
        for family, cid in [('c', self.RTUS[0]+1000), ('c', self.RTUS[1]+1000), ('s', self.SOURCE+1000), ('t', 1)]:
            result = self.api(family, f'/api/packet-recording/channels/{cid}', 'PUT', {'enabled': None})
            self.assertIsNone(result['override'])
            self.assertFalse(result['enabled'])
        time.sleep(1.2)
        count = packet_count()
        self.api('s', '/api/modbus-tcp/poll', 'POST', {})
        self.assertEqual(remote.read_input_registers(0, count=1, device_id=1).registers, [48])
        self.assertEqual(packet_count(), count)
        self.source_update('yc',1,47)
        self.wait_for(lambda:self.c_point(self.RTUS[0],'yc')['value']==47,label='restore source value')
        self.api("t", "/api/trans/channels/1/stop", "POST", {})
        self.assertFalse(self.api("t", "/api/trans/processes")["items"][0]["alive"])
        # Real protocol traffic must be durable at all three ends, not merely
        # represented by a poll-summary or an in-memory Web event.
        for root, process in (("system", f"scdfe_c-rtu-{self.RTUS[0]}"),
                              ("source", "scdfe_s-modbus-tcp-server"),
                              ("source", "scdfe_s-modbus-tcp"),
                              ("system", "scdfe_t-channel-1")):
            files = list((self.root / root / "logs").rglob(f"{process}-*.log"))
            self.assertTrue(files, process)
            content = "\n".join(path.read_text(encoding="utf-8") for path in files)
            self.assertIn("报文 RX", content, process)
            self.assertIn("报文 TX", content, process)
            self.assertIn("HEX=", content, process)
        self.assertTrue(self.api("s", "/api/monitor/logs?keyword=HEX%3D")["items"])
        self.api("t", "/api/trans/channels/1/start", "POST", {})
        remote.close()
        remote = self.client(self.forward_port)
        self.wait_for(lambda: remote.read_input_registers(0, count=1, device_id=1).registers == [47],
                      label="forwarder restart")
        self.api("t", "/api/trans/channels/1/stop", "POST", {})
        self.control("all", "stop")
        self.assertFalse(any(row["alive"] for row in self.api("c", "/api/processes")["items"]))
        for slug in ("modbus-tcp", "modbus-tcp-server"):
            self.assertFalse(self.api("s", f"/api/{slug}/stop", "POST", {})["item"]["running"])

        # A second complete start cycle must acquire fresh listeners/leases
        # and resume acquisition, not merely leave stale RUNNING indicators.
        source.close()
        self.api("s", "/api/modbus-tcp-server/start", "POST", {})
        self.source_update("yc", 1, 53)
        self.source_update("yc", 2, 61)
        self.source_update("yx", 1, 1)
        self.control("writer", "start")
        self.wait_for(lambda: self.c_point(self.RTUS[0], "yc")["value"] == 53, label="acquisition restart")
        self.wait_for(lambda: self.c_point(self.RTUS[1], "yc")["value"] == 61, label="second RTU restart")
        self.api("t", "/api/trans/channels/1/start", "POST", {})
        remote.close()
        remote = self.client(self.forward_port)
        self.assertEqual(remote.read_input_registers(0, count=2, device_id=1).registers, [53, 61])
        self.api("s", "/api/modbus-tcp/start", "POST", {})
        self.api("s", "/api/modbus-tcp/poll", "POST", {})
        restarted_points = self.api("s", "/api/modbus-tcp/points?kind=yc")["items"]
        self.assertEqual(next(row for row in restarted_points if row["pnt_no"] == 1)["value"], 53)
        self.api("t", "/api/trans/channels/1/stop", "POST", {})
        self.control("all", "stop")
        for slug in ("modbus-tcp", "modbus-tcp-server"):
            self.api("s", f"/api/{slug}/stop", "POST", {})
        for name in ("c", "s", "t"):
            self.api(name, "/api/health")
        self.assertFalse(any(row["alive"] for row in self.api("c", "/api/processes")["items"]))
        self.assertFalse(self.api("t", "/api/trans/processes")["items"][0]["alive"])
        print(json.dumps({
            "acceptance": "three-web-modbus", "source_database_isolated": True,
            "rtus": len(self.RTUS), "stable_read_seconds": 3.3,
            "stable_reads": reads, "stable_read_failures": 0, "same_tcp_socket": True,
            "yc_yx_changes_propagated": True, "same_source_point_yk_routing": True,
            "yt_scale_2_offset_10_raw8_engineering26": True,
            "s_mapped_master_poll_and_commands": True, "c_http_and_tcp_commands": True,
            "independent_command_restart": True, "complete_start_stop_cycles": 2,
            "all_runtime_processes_stopped": True,
        }), flush=True)

    def _cleanup(self):
        errors = []
        def attempt(action):
            try:
                action()
            except Exception as exc:
                errors.append(repr(exc))
        for client in self.clients:
            attempt(client.close)
        if self.channel_created and "t" in self.webs:
            trans = self.webs["t"].trans_store
            attempt(lambda: trans.stop(1))
            for process in list(trans._children.values()):
                if process.poll() is None:
                    attempt(process.terminate)
                attempt(lambda process=process: process.wait(timeout=5))
        if "c" in self.webs:
            controller = self.webs["c"].process_controller
            if controller._thread is not None:
                controller._thread.join(25)
            def cooperative_stop():
                with controller.store.connection() as connection:
                    connection.execute(f"UPDATE {CONTROL_TABLE} SET process_enable=0")
                    connection.execute(f"UPDATE {PROCESS_TABLE} SET process_enable=0")
                    connection.execute("UPDATE fes_scdfe_command_define SET process_enable=0")
                    connection.commit()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and any(row["alive"] for row in controller.snapshot()["items"]):
                    time.sleep(.1)
            attempt(cooperative_stop)
            # Only handles created by this test's controller are eligible for
            # emergency cleanup. Never terminate a PID obtained from SQLite.
            for process in list(controller._children.values()):
                if process.poll() is None:
                    attempt(process.terminate)
                attempt(lambda process=process: process.wait(timeout=5))
            attempt(lambda: self.assertFalse(any(row["alive"] for row in controller.snapshot()["items"]),
                                             "acceptance cleanup left a runtime process alive"))
        for web in self.webs.values():
            attempt(web.shutdown)
            attempt(web.server_close)
        for thread in self.threads:
            thread.join(3)
        attempt(self.temp.cleanup)
        self.assertFalse(errors, errors)


if __name__ == "__main__":
    unittest.main()
