"""Read-only six-protocol configuration-load benchmark.

This measures the database portion of adapter ``from_store`` construction.
Both variants use one read transaction and materialise the same logical
configuration; ``wide`` selects every column, while ``narrow`` selects only
fields consumed by the corresponding adapter.
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import sys
import time
from pathlib import Path


POINT_TABLES = ("fes_yc_define", "fes_yx_define", "fes_yk_define", "fes_yt_define")
CASES = {
    "iec104": {
        "parameter": ("fes_channel_iec104_define", "rtu_id,rtu_addr,ycbegin_addr,yxbegin_addr,"
                       "ykbegin_addr,ytbegin_addr,yx_data_type,yk_excute_type,yc_data_type,"
                       "yt_excute_type,remote_host,remote_port"),
        "mapping": ("fes_channel_iec104_point_define", "rtu_id,kind,pnt_no,io_address,type_id,"
                    "report_ms,command_mode,qualifier,related_io_address,related_io_autoreturn"),
        "points": "pnt_no,value",
    },
    "modbus-tcp": {
        "parameter": ("fes_channel_modbus_tcp_define", "rtu_id,remote_host,remote_port,unit_id,"
                       "connect_timeout,response_timeout,retry_count,keep_alive"),
        "common": True,
        "points": "pnt_no",
    },
    "modbus-rtu": {
        "parameter": ("fes_channel_modbus_rtu_define", "rtu_id,serial_port,baudrate,bytesize,"
                       "parity,stopbits,unit_id,response_timeout,retry_count"),
        "common": True,
        "points": "pnt_no",
    },
    "can": {
        "parameter": ("fes_channel_can_define", "rtu_id,interface,channel,bitrate,data_bitrate,"
                       "fd,receive_own_messages"),
        "mapping": ("fes_channel_can_point_define", "rtu_id,kind,pnt_no,frame_id,tx_frame_id,"
                    "response_id,is_extended_id,is_fd,byte_offset,bit_offset,bit_length,value_type,"
                    "byte_order,scale,offset,quality_byte_offset,quality_mask,tx_data_hex,response_data_hex"),
        "points": "pnt_no",
    },
    "bacnet": {
        "parameter": ("fes_channel_bacnet_define", "rtu_id,bind_ip,bind_port,dev_id,remote_host,remote_port"),
        "mapping": ("fes_channel_bacnet_point_define", "rtu_id,kind,pnt_no,object_type,instance,"
                    "property_identifier,quality_property,priority,array_index"),
        "points": "pnt_no",
    },
    "opc": {
        "parameter": ("fes_channel_opc_define", "rtu_id,opc_user,opc_pwd,opc_domain,opc_name"),
        "mapping": ("fes_channel_opc_point_define", "rtu_id,kind,pnt_no,node_id,data_type,"
                    "value_attribute,quality_attribute,deadband"),
        "points": "pnt_no",
    },
}
POINT_JOIN = {
    "bacnet": (
        "fes_channel_bacnet_point_define",
        "m.object_type,m.instance,m.property_identifier,m.quality_property,m.priority,m.array_index",
    ),
    "opc": (
        "fes_channel_opc_point_define",
        "m.node_id,m.data_type,m.value_attribute,m.quality_attribute,m.deadband",
    ),
}
COMMON = {
    "fes_channel_modbus_define": "rtu_id,irtuaddr",
    "fes_channel_modbus_define_call": "rtu_id,irtuaddr,call_seq,iplace,inum,icode",
    "fes_channel_modbus_define_data": "rtu_id,call_seq,if_yx,cnt,trans_type,reg_addr,offset_bit,"
                                      "all_bit,if_reverse,twobytedata_byteseq,fourbytedata_byteseq",
    "fes_channel_modbus_define_ytyk": "rtu_id,ifyk,cnt,irtuaddr,reg_addr,icode,ytyk_type,ifreverse,"
                                       "twobytedata_byteseq,fourbytedata_byteseq,multi_para,special_yk_yccnt,"
                                       "special_yk_bit,send_ff_replace_01",
}


def load(connection, protocol, rtu_id, narrow):
    spec = CASES[protocol]
    connection.execute("BEGIN")
    projection = "rtu_id,channel_type" if narrow else "*"
    rows = [dict(row) for row in connection.execute(
        f"SELECT {projection} FROM fes_rtu_define WHERE rtu_id=?", (rtu_id,))]
    if spec.get("common"):
        for table, narrow_projection in COMMON.items():
            projection = narrow_projection if narrow else "*"
            rows.extend(dict(row) for row in connection.execute(
                f"SELECT {projection} FROM {table} WHERE rtu_id=? ORDER BY 1,2",
                (rtu_id,),
            ))
    parameter_table, parameter_projection = spec["parameter"]
    rows.extend(dict(row) for row in connection.execute(
        f"SELECT {parameter_projection if narrow else '*'} FROM {parameter_table} "
        "WHERE rtu_id=? ORDER BY 1 LIMIT 2", (rtu_id,)))
    if "mapping" in spec:
        mapping_table, mapping_projection = spec["mapping"]
        rows.extend(dict(row) for row in connection.execute(
            f"SELECT {mapping_projection if narrow else '*'} FROM {mapping_table} "
            "WHERE rtu_id=? ORDER BY kind,pnt_no", (rtu_id,)))
    if narrow and protocol in POINT_JOIN:
        mapping_table, mapping_projection = POINT_JOIN[protocol]
        rows.extend(dict(row) for row in connection.execute(
            f"""
            SELECT p.rtu_id, p.kind, p.pnt_no, {mapping_projection}
            FROM (
                SELECT rtu_id, 'yc' AS kind, pnt_no FROM fes_yc_define WHERE rtu_id=?
                UNION ALL
                SELECT rtu_id, 'yx' AS kind, pnt_no FROM fes_yx_define WHERE rtu_id=?
                UNION ALL
                SELECT rtu_id, 'yk' AS kind, pnt_no FROM fes_yk_define WHERE rtu_id=?
                UNION ALL
                SELECT rtu_id, 'yt' AS kind, pnt_no FROM fes_yt_define WHERE rtu_id=?
            ) AS p
            LEFT JOIN {mapping_table} AS m
              ON m.rtu_id=? AND m.kind=p.kind AND m.pnt_no=p.pnt_no
            ORDER BY CASE p.kind WHEN 'yc' THEN 1 WHEN 'yx' THEN 2
                                 WHEN 'yk' THEN 3 ELSE 4 END, p.pnt_no
            """, (rtu_id, rtu_id, rtu_id, rtu_id, rtu_id)))
    else:
        point_projection = spec["points"] if narrow else "*"
        for table in POINT_TABLES:
            rows.extend(dict(row) for row in connection.execute(
                f"SELECT {point_projection} FROM {table} WHERE rtu_id=? ORDER BY pnt_no", (rtu_id,)))
    connection.commit()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("db/scada.sqlite3"))
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = sqlite3.connect(args.db)
    base.row_factory = sqlite3.Row
    base.execute("PRAGMA query_only=ON")
    cases = []
    for protocol in CASES:
        row = base.execute(
            "SELECT rtu_id FROM fes_rtu_define WHERE channel_type=? ORDER BY rtu_id LIMIT 1",
            (protocol,),
        ).fetchone()
        if row is not None:
            cases.append((protocol, int(row[0])))
    base.close()
    if not cases:
        parser.error("database has no supported protocol RTUs")

    def connect():
        value = sqlite3.connect(args.db)
        value.row_factory = sqlite3.Row
        value.execute("PRAGMA query_only=ON")
        return value

    results = []
    for round_no in range(1, args.rounds + 1):
        for narrow in ((False, True) if round_no % 2 else (True, False)):
            for protocol, rtu_id in cases:
                started = time.perf_counter()
                sample = None
                for _ in range(args.repeat):
                    with connect() as connection:
                        sample = load(connection, protocol, rtu_id, narrow)
                elapsed = (time.perf_counter() - started) * 1000
                results.append({
                    "round": round_no, "protocol": protocol, "rtu_id": rtu_id,
                    "variant": "narrow" if narrow else "wide", "calls": args.repeat,
                    "elapsed_ms": elapsed, "per_call_ms": elapsed / args.repeat,
                    "rows": len(sample),
                })
    medians = {}
    for protocol, _ in cases:
        medians[protocol] = {}
        for variant in ("wide", "narrow"):
            selected = [row for row in results if row["protocol"] == protocol and row["variant"] == variant]
            medians[protocol][variant] = {
                "per_call_ms": statistics.median(row["per_call_ms"] for row in selected),
                "rows": statistics.median(row["rows"] for row in selected),
            }
        old = medians[protocol]["wide"]["per_call_ms"]
        new = medians[protocol]["narrow"]["per_call_ms"]
        medians[protocol]["reduction_percent"] = (old - new) / old * 100 if old else 0.0
    report = {
        "database": str(args.db.resolve()), "platform": platform.platform(), "python": sys.version,
        "method": "read-only formal DB; one transaction; SELECT * versus current adapter projections",
        "arguments": {"repeat": args.repeat, "rounds": args.rounds}, "cases": cases,
        "results": results, "medians": medians,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(medians, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
