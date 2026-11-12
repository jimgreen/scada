"""Compare cold/warm selected-RTU HTTP reads on private SQLite fixtures.

Run as ``python -m tools.benchmark_scoped_monitor --baseline b35268b``.
No transport is started and no production database is opened.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
from statistics import median
import subprocess
import tempfile
import threading
from time import perf_counter
from types import MethodType

from scdfe_s.web import server as web


def baseline_methods(ref):
    source = subprocess.check_output(
        ["git", "show", f"{ref}:scdfe_s/web/server.py"], encoding="utf-8"
    )
    tree = ast.parse(source)
    methods = {}
    for cls, name in (("ScadaHTTPServer", "channel_services"),
                      ("ScadaHandler", "_monitor_control")):
        parent = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
        method = next(n for n in parent.body if isinstance(n, ast.FunctionDef) and n.name == name)
        namespace = dict(vars(web))
        exec(compile(ast.Module(body=[method], type_ignores=[]), ref, "exec"), namespace)
        methods[name] = namespace[name]
    return methods


def run_case(root, stations, variant, baseline, repeat):
    httpd = web.make_server(port=0, db_path=root / "s.sqlite3")
    with httpd.store.connection() as con:
        con.execute("BEGIN")
        con.executemany(
            "INSERT INTO fes_rtu_define(rtu_id,channel_id,channel_type,name,process_enable) "
            "VALUES(?,?,'iec104',?,0)",
            [(i, i, f"station-{i}") for i in range(1, stations + 1)],
        )
        con.executemany(
            "INSERT INTO fes_channel_iec104_define(channel_id,remote_port) VALUES(?,?)",
            [(i, 24040 + i) for i in range(1, stations + 1)],
        )
        con.executemany(
            "INSERT INTO fes_yc_define(rtu_id,pnt_no,value) VALUES(?,1,42)",
            [(i,) for i in range(1, stations + 1)],
        )
        con.commit()
    original_monitor = web.ScadaHandler._monitor_control
    if variant == "before":
        httpd.channel_services = MethodType(
            lambda self, **kwargs: baseline["channel_services"](self), httpd
        )
        web.ScadaHandler._monitor_control = baseline["_monitor_control"]
    statements = []
    original_connection = httpd.store.connection

    @contextmanager
    def traced_connection():
        with original_connection() as con:
            con.set_trace_callback(statements.append)
            try:
                yield con
            finally:
                con.set_trace_callback(None)

    httpd.store.connection = traced_connection
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=30)
    times = []
    try:
        # Pre-connect so cold measurements isolate the first selected-RTU load.
        client.connect()
        for index in range(repeat + 1):
            statements.clear()
            start = perf_counter()
            client.request("GET", "/api/monitor/communications?rtu_id=1")
            response = client.getresponse()
            result = json.loads(response.read())
            times.append((perf_counter() - start) * 1000)
            assert response.status == 200 and result["total"] == 1
            item = result["items"][0]
            assert (item["rtu_id"], item["point_count"], item["running"]) == (1, 1, False)
            if index == 0:
                cold_selects = sum(s.startswith("SELECT") for s in statements)
                cold_services = len(httpd._channel_service_cache)
        return {"stations": stations, "variant": variant,
                "cold_ms": times[0], "warm_median_ms": median(times[1:]),
                "warm_samples_ms": times[1:], "cold_selects": cold_selects,
                "cold_service_objects": cold_services, "semantic_check": "passed"}
    finally:
        client.close()
        httpd.shutdown()
        thread.join(5)
        httpd.server_close()
        web.ScadaHandler._monitor_control = original_monitor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="b35268b")
    parser.add_argument("--stations", type=int, nargs="+", default=[30, 300])
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--repeat", type=int, default=15)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = baseline_methods(args.baseline)
    results = []
    with tempfile.TemporaryDirectory(prefix="scada-monitor-bench-") as temp:
        for count in args.stations:
            for round_no in range(args.rounds):
                for variant in (("before", "after") if round_no % 2 == 0 else ("after", "before")):
                    root = Path(temp) / f"{count}-{round_no}-{variant}"
                    result = run_case(root, count, variant, baseline, args.repeat)
                    result["round"] = round_no + 1
                    results.append(result)
    summary = []
    for count in args.stations:
        row = {"stations": count}
        for variant in ("before", "after"):
            group = [r for r in results if r["stations"] == count and r["variant"] == variant]
            row[variant] = {key: median(r[key] for r in group) for key in (
                "cold_ms", "warm_median_ms", "cold_selects", "cold_service_objects")}
        summary.append(row)
    report = {"baseline": args.baseline, "rounds": args.rounds, "repeat": args.repeat,
              "boundary": "private SQLite/WAL, stopped IEC104 stations, loopback HTTP; no field throughput claim",
              "summary": summary, "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
