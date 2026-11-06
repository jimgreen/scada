"""Measure supervisor DB operations, S page projections, T ownership checks and hourly log reads on private fixtures."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from threading import Thread
from time import perf_counter
from urllib.request import urlopen

from core.scdfe_control import InstanceLock
from scdfe_c import ScdfeSupervisor, discover_channels
from scdfe_c.web.log_query import search_logs
from scdfe_s.web.server import make_server
from scdfe_t.web.service import TransStore


def supervisor_cycle(root):
    obj = ScdfeSupervisor(root / "c.sqlite3")
    opens = 0
    original = obj.store._open_connection

    def counted(*args, **kwargs):
        nonlocal opens
        opens += 1
        return original(*args, **kwargs)

    obj.store._open_connection = counted
    elapsed = []

    def cycle():
        for _ in range(80):
            start = perf_counter()
            cfg = obj._control()
            assert cfg["id"] == 1
            assert not discover_channels(obj.store)
            obj._update_control_state(process_heartbeat_time=123)
            elapsed.append((perf_counter() - start) * 1000)
        obj._scan_count += 1

    obj.reconcile = cycle
    try:
        obj.run(max_scans=1)
        return dict(
            operations=len(elapsed),
            opens=opens,
            median_ms=round(median(elapsed), 4),
            idle_after_shutdown=len(obj.store._connection_pool),
        )
    finally:
        obj.store.close()


def web_reads(root):
    with make_server(port=0, db_path=root / "s.sqlite3") as web:
        with web.store.connection() as db:
            db.execute("BEGIN")
            for i in range(1, 501):
                db.execute(
                    "INSERT INTO fes_rtu_define(rtu_id, channel_id,channel_type,code,name) VALUES(?,?,'modbus-tcp',?,?)",
                    (i, i, f"RTU{i}", f"站{i}"),
                )
                db.execute(
                    "INSERT INTO fes_yc_define(rtu_id,pnt_no,value,name,code) VALUES(1,?,?,?,?)",
                    (i, i, f"遥测{i}", f"YC{i}"),
                )
            db.commit()
        th = Thread(target=web.serve_forever, daemon=True)
        th.start()
        try:
            result = {}
            for key, path, fields in [
                ("stations", "channels", "rtu_id,channel_type,name,code"),
                ("points", "channels/1/points/yc", "pnt_no,code,name,value"),
            ]:
                for narrow in [False, True]:
                    url = f"http://127.0.0.1:{web.server_port}/api/{path}?limit=500"
                    if narrow:
                        url += "&fields=" + fields
                    times = []
                    for i in range(12):
                        start = perf_counter()
                        with urlopen(url, timeout=10) as r:
                            data = r.read()
                        times.append((perf_counter() - start) * 1000)
                    rows = json.loads(data)["items"]
                    assert len(rows) == 500
                    view = [
                        {k: r[k] for k in set(fields.split(",")) | {"_version"}}
                        for r in rows
                    ]
                    digest = sha256(
                        json.dumps(view, sort_keys=True).encode()
                    ).hexdigest()
                    if not narrow:
                        full = digest
                    else:
                        assert digest == full
                    result[key + ("_view" if narrow else "_full")] = dict(
                        bytes=len(data), median_ms=round(median(times[1:]), 3)
                    )
            return result
        finally:
            web.shutdown()
            th.join()


def log_reads(root):
    path = root / "scdfe_c-main-20261106-10.log"
    with path.open("w", encoding="utf8") as out:
        for i in range(25000):
            out.write(f"10:00:00 [报文] 报文 RX 通道=1 #{i} HEX=01 02 03\n")
    results = {}
    for case, extra in [
        ("tail", {}),
        ("rare", {"q": "#5000 "}),
        ("many", {"limit": "1000"}),
    ]:
        params = (
            dict(filename=path.name, record="packet", direction="RX", limit="3") | extra
        )
        times = []
        for i in range(12):
            start = perf_counter()
            result = search_logs(root, params)
            times.append((perf_counter() - start) * 1000)
        assert result["items"]
        results[case] = dict(
            bytes=result["scanned_bytes"],
            median_ms=round(median(times[2:]), 3),
            rows=len(result["items"]),
            digest=sha256(
                json.dumps(result["items"], sort_keys=True).encode()
            ).hexdigest(),
        )
    return results


def monitor_reads(root):
    service = TransStore(root / "t.sqlite3", connection_pool_size=1)
    try:
        with service.store.connection() as con:
            con.executemany(
                "INSERT INTO fes_scdfe_t_define(channel_id,code,name,protocol,bind_port) VALUES(?,?,?,'modbus-tcp',?)",
                [(i, str(i), str(i), 10000 + i) for i in range(1, 501)],
            )
            con.commit()
        lease = InstanceLock(service.db_path, "trans-7")
        assert lease.acquire()
        try:
            times = []
            for _ in range(6):
                start = perf_counter()
                rows = service.processes()
                times.append((perf_counter() - start) * 1000)
                assert len(rows) == 500 and [
                    r["channel_id"] for r in rows if r["alive"]
                ] == [7]
            return dict(
                n=500,
                active=1,
                lease_files=len(list(root.glob("*.scdfe_c-trans-*.lock"))),
                first_ms=round(times[0], 2),
                repeat_median_ms=round(median(times[1:]), 2),
            )
        finally:
            lease.release()
    finally:
        service.store.close()


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("output", type=Path)
    output = cli.parse_args().output
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        result = dict(
            supervisor=supervisor_cycle(root),
            s_web=web_reads(root),
            logs=log_reads(root),
            monitor=monitor_reads(root),
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")
    print(json.dumps(result))
