"""为每种通信规约生成大规模 RTU、通道参数和四遥测试数据。

脚本一次性生成 IEC104、Modbus TCP、Modbus RTU、CAN、BACnet/IP 和 OPC UA 六类测试站，
每类默认生成 10 个 RTU；
已存在的测试站只补齐缺失记录，不覆盖用户修改：

* IEC104 参数表；
* Modbus TCP、Modbus RTU、CAN、BACnet/IP、OPC UA 五个测试站；
* 每个测试站 1000 个遥测、1000 个遥信、200 个遥控、200 个遥调；
* Modbus 的召唤、数据解析和遥控/遥调定义。

只增加带 ``TEST_PROTO_`` 标识的记录。重复执行不会覆盖用户修改；插入前会
使用 SQLite backup API 保存完整数据库快照，并在同一个事务中完成外键和完整性检查。
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .service import DEFAULT_DB, ScadaStore, quote_identifier


PREFIX = "TEST_PROTO"
RTU_TABLE = "fes_rtu_define"
POINT_TABLES = ("fes_yc_define", "fes_yx_define", "fes_yk_define", "fes_yt_define")
COUNTS = {"yc": 1000, "yx": 1000, "yk": 200, "yt": 200}
RTU_REPLICAS = 10

PROTOCOLS = (
    {
        "key": "iec104", "name": "IEC104扩展测试站",
        "channel": "127.0.0.1", "port": 25004,
        "parameter_table": "fes_channel_iec104_define",
        "rtu_code": "TEST_IEC104_RTU",
    },
    {
        "key": "modbus-tcp", "name": "ModbusTCP扩展测试站",
        "channel": "127.0.0.1", "port": 1503,
        "parameter_table": "fes_channel_modbus_tcp_define",
    },
    {
        "key": "modbus-rtu", "name": "ModbusRTU扩展测试站",
        "channel": "loop://", "port": None,
        "parameter_table": "fes_channel_modbus_rtu_define",
    },
    {
        "key": "can", "name": "CAN扩展测试站",
        "channel": "scada-protocol-can", "port": None,
        "parameter_table": "fes_channel_can_define",
    },
    {
        "key": "bacnet", "name": "BACnet扩展测试站",
        "channel": "127.0.0.1", "port": 47808,
        "parameter_table": "fes_channel_bacnet_define",
    },
    {
        "key": "opc", "name": "OPCUA扩展测试站",
        "channel": "opc.tcp://127.0.0.1:4840", "port": 4840,
        "parameter_table": "fes_channel_opc_define",
    },
)


def _key_token(spec: dict) -> str:
    """Return a stable code token unique within the protocol test data."""
    token = spec["key"].upper().replace("-", "_")
    index = int(spec.get("station_index", 1))
    return token if index == 1 else f"{token}_R{index:02d}"


def _base_rtu_code(spec: dict) -> str:
    return spec.get("rtu_code", f"{PREFIX}_{spec['key'].upper().replace('-', '_')}_RTU")


def expanded_protocols(replicas: int = RTU_REPLICAS) -> tuple[dict, ...]:
    """Expand each protocol fixture into deterministic RTU replicas."""
    if isinstance(replicas, bool) or not isinstance(replicas, int) or not 1 <= replicas <= 100:
        raise ValueError("replicas 必须为 1..100 的整数")
    expanded: list[dict] = []
    for base in PROTOCOLS:
        for index in range(1, replicas + 1):
            spec = dict(base)
            spec["station_index"] = index
            if index > 1:
                spec["name"] = f"{base['name']}{index:02d}"
                spec["rtu_code"] = f"{_base_rtu_code(base)}_R{index:02d}"
                if base["port"] is not None:
                    spec["port"] = int(base["port"]) + (2 if base["key"] == "bacnet" else 1) * (index - 1)
                if base["key"] == "opc":
                    spec["channel"] = f"opc.tcp://127.0.0.1:{spec['port']}"
                elif base["key"] == "can":
                    spec["channel"] = f"{base['channel']}-{index:02d}"
            expanded.append(spec)
    return tuple(expanded)


def _next_id(connection, table: str, pending: dict[str, int]) -> int:
    if table not in pending:
        current = connection.execute(
            f"SELECT COALESCE(MAX(id), 0) FROM {quote_identifier(table)}"
        ).fetchone()[0]
        pending[table] = int(current)
    pending[table] += 1
    return pending[table]


def _backup(db_path: Path) -> Path:
    folder = db_path.parent / "backups"
    folder.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = folder / f"{db_path.name}.before_protocol_test_data.{stamp}.{uuid4().hex[:8]}.bak"
    with path.open("xb"):
        pass
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(path)) as target:
            source.backup(target)
    return path


def _existing_by_code(connection, table: str, code: str):
    return connection.execute(
        f"SELECT * FROM {quote_identifier(table)} WHERE code=?", (code,)
    ).fetchone()


def _id_for_code(connection, table: str, code: str, ids: dict[str, int], indexes) -> int:
    index = indexes[table]
    existing = index["codes"].get(code)
    return (existing[index["primary_keys"].index("id")] if existing is not None
            else _next_id(connection, table, ids))


def _seed_indexes(connection, store: ScadaStore, tables: list[str]) -> dict[str, dict]:
    """Load primary keys and stable codes once for fast idempotent seeding."""
    indexes: dict[str, dict] = {}
    for table in dict.fromkeys(tables):
        public = store.catalog.public(table)
        primary_keys = tuple(public["primary_keys"])
        has_code = any(column["eng_name"] == "code" for column in public["columns"])
        selected = [*primary_keys]
        if has_code:
            selected.append("code")
        projection = ", ".join(quote_identifier(name) for name in selected)
        rows = connection.execute(
            f"SELECT {projection} FROM {quote_identifier(table)}"
        ).fetchall()
        indexes[table] = {
            "primary_keys": primary_keys,
            "keys": {tuple(row[name] for name in primary_keys) for row in rows},
            "codes": ({row["code"]: tuple(row[name] for name in primary_keys)
                       for row in rows if row["code"] is not None} if has_code else {}),
        }
    return indexes


def _add_record(store: ScadaStore, pending: list[tuple[str, dict]],
                table: str, values: dict, indexes: dict[str, dict]):
    values = store._normalise(table, values)
    index = indexes[table]
    key = tuple(values[key_name] for key_name in index["primary_keys"])
    if key in index["keys"]:
        return False
    code = values.get("code")
    if code:
        existing_key = index["codes"].get(code)
        if existing_key is not None:
            if existing_key != key:
                raise ValueError(f"{table}: 测试标识与现有主键冲突: {code}")
            return False
    pending.append((table, values))
    index["keys"].add(key)
    if code:
        index["codes"][code] = key
    return True


def _rtu_values(spec: dict, rtu_id: int, channel_id: int, now: int) -> dict:
    rtu_code = _base_rtu_code(spec)
    return {
        "rtu_id": rtu_id,
        "code": rtu_code,
        "rdfid": rtu_code,
        "name": spec["name"],
        "status": 0, "call_all_cycle": 30000, "refresh_trigger": 1000,
        "his_save": 0, "refresh_time": now,
        "process_enable": 0 if spec.get("station_index", 1) > 1 else 1,
        "channel_id": channel_id, "channel_type": spec["key"],
        "chan_addr": spec["channel"], "chan_port": spec["port"],
    }


def _point_values(spec: dict, rtu_id: int, kind: str, number: int,
                  now: int) -> dict:
    token = _key_token(spec)
    code = f"{PREFIX}_{token}_{kind.upper()}_{number:03d}"
    result = {
        "rtu_id": rtu_id, "pnt_no": number, "code": code, "rdfid": code,
        "name": f"{spec['key']}测试{kind.upper()}{number:03d}",
    }
    if kind == "yx":
        result.update(
            name_0="分位", name_1="合位", value=number % 2,
            status=0, refresh_time=now, value_dev=number % 2,
            status_dev=0, iflock=0, ifreverse=0, ifalarm=0, ifdata_curb=0,
            yk_pnt=number if number <= COUNTS["yk"] else None,
            yk_rtu=rtu_id if number <= COUNTS["yk"] else None,
        )
    elif kind == "yc":
        value = round(220.0 + number * 0.5, 3)
        result.update(
            raw_base=0.0, raw_scale=1.0, raw_value=value, value=value,
            value_dev=value, raw_status=0, status=0, status_dev=0,
            raw_min=-1000.0, eng_min=-1000.0, raw_max=1000.0, eng_max=1000.0,
            raw_time=now, refresh_time=now, refresh_time_dev=now,
            value_zone=0.01, time_zone=60, iflock=0, ifreverse=0,
            ifalarm=0, ifdata_curb=0,
            yt_pnt=number if number <= COUNTS["yt"] else None,
            yt_rtu=rtu_id if number <= COUNTS["yt"] else None,
        )
    elif kind == "yk":
        result.update(
            name_0="分闸", name_1="合闸", value=number % 2,
            max_wait_time=10000, icode=5, status=0,
            man_disable=0, auto_disable=0, cmd_time=0,
            cmd_handle_time=0, refresh_time=0,
        )
    else:
        value = float(number * 5)
        result.update(
            raw_base=0.0, raw_scale=1.0, raw_value=value, value=value,
            raw_min=-1000.0, eng_min=-1000.0, raw_max=1000.0, eng_max=1000.0,
            raw_status=0, status=0, is_step=0.0, step=0.0,
            max_wait_time=10000, icode=6, man_disable=0, auto_disable=0,
            cmd_time=0, cmd_handle_time=0, raw_time=0, refresh_time=0,
        )
    return result


def _parameter_values(spec: dict, rtu_id: int, identifier: int) -> dict:
    key = spec["key"]
    code = f"{PREFIX}_{_key_token(spec)}_PARAM"
    base = {"code": code, "rdfid": code, "name": f"{spec['name']}参数", "rtu_id": rtu_id}
    if key == "iec104":
        base.update(id=identifier, rtu_addr=1, ycbegin_addr=0x4001, yxbegin_addr=1,
                    ykbegin_addr=0x6001, ytbegin_addr=0x6201, yx_data_type=1,
                    yk_excute_type=1, yc_data_type=1, yt_excute_type=2,
                    call_alldata_time=30, if_autosend_alldata=1)
    elif key == "modbus-tcp":
        base.update(remote_host="127.0.0.1", remote_port=spec["port"], unit_id=3,
                    connect_timeout=1.0, response_timeout=1.0, retry_count=0, keep_alive=1)
    elif key == "modbus-rtu":
        base.update(serial_port="loop://", baudrate=9600, bytesize=8, parity="N",
                    stopbits=1, unit_id=4, response_timeout=1.0, retry_count=0)
    elif key == "can":
        base.update(interface="virtual", channel=spec["channel"], bitrate=500000,
                    data_bitrate=2000000, fd=0, receive_own_messages=0,
                    receive_timeout=0.2, retry_count=0, is_extended_id=0)
    elif key == "bacnet":
        base.update(id=identifier, bind_ip="127.0.0.1",
                    bind_port=int(spec["port"]) + 1, dev_id=1234 + int(spec.get("station_index", 1)) - 1)
    elif key == "opc":
        base.pop("rdfid", None)
        base.update(id=identifier, opc_interval=1000, opc_user="", opc_pwd="",
                    opc_domain="", opc_name=spec["channel"])
    return base


def _add_modbus_tables(connection, store: ScadaStore, spec: dict, rtu_id: int,
                       ids: dict[str, int], pending, indexes):
    key = spec["key"]
    prefix = f"{PREFIX}_{_key_token(spec)}"
    channel_code = f"{prefix}_CHANNEL"
    channel_id = _id_for_code(connection, "fes_channel_modbus_define", channel_code, ids, indexes)
    _add_record(store, pending, "fes_channel_modbus_define", {
        "id": channel_id, "rtu_id": rtu_id, "code": channel_code,
        "rdfid": channel_code, "name": f"{spec['name']}Modbus通道",
        "irtuaddr": 3 if key == "modbus-tcp" else 4,
    }, indexes)
    # Keep the initial YC/YX sequence numbers; additional YC frames use 2+.
    # Modbus limits FC3 to 125 registers and FC1 to 2000 coils per request.
    calls = [(0 if block == 0 else block + 1, 3, start, min(125, COUNTS["yc"] - start))
             for block, start in enumerate(range(0, COUNTS["yc"], 125))]
    calls.append((1, 1, 0, COUNTS["yx"]))
    for seq, function_code, start, count in calls:
        call_code = f"{prefix}_CALL_{seq}"
        call_id = _id_for_code(connection, "fes_channel_modbus_define_call", call_code, ids, indexes)
        _add_record(store, pending, "fes_channel_modbus_define_call", {
            "id": call_id, "rtu_id": rtu_id, "code": call_code,
            "name": f"{spec['name']}召唤{seq}", "irtuaddr": 3 if key == "modbus-tcp" else 4,
            "call_seq": seq, "iplace": start, "inum": count, "icode": function_code,
        }, indexes)
    for number in range(1, COUNTS["yc"] + 1):
        data_code = f"{prefix}_YC_DATA_{number:03d}"
        data_id = _id_for_code(connection, "fes_channel_modbus_define_data", data_code, ids, indexes)
        _add_record(store, pending, "fes_channel_modbus_define_data", {
            "id": data_id, "rtu_id": rtu_id, "code": data_code,
            "name": f"{spec['name']}遥测映射{number:03d}",
            "call_seq": 0 if number <= 125 else (number - 1) // 125 + 1,
            "if_yx": 0, "cnt": number, "trans_type": 2, "reg_addr": number - 1,
        }, indexes)
    for number in range(1, COUNTS["yx"] + 1):
        data_code = f"{prefix}_YX_DATA_{number:03d}"
        data_id = _id_for_code(connection, "fes_channel_modbus_define_data", data_code, ids, indexes)
        _add_record(store, pending, "fes_channel_modbus_define_data", {
            "id": data_id, "rtu_id": rtu_id, "code": data_code,
            "name": f"{spec['name']}遥信映射{number:03d}", "call_seq": 1,
            "if_yx": 1, "cnt": number, "trans_type": 8, "reg_addr": number - 1,
        }, indexes)
    for number in range(1, COUNTS["yk"] + 1):
        command_code = f"{prefix}_YK_CMD_{number:03d}"
        command_id = _id_for_code(connection, "fes_channel_modbus_define_ytyk", command_code, ids, indexes)
        _add_record(store, pending, "fes_channel_modbus_define_ytyk", {
            "id": command_id, "rtu_id": rtu_id, "code": command_code,
            "name": f"{spec['name']}遥控映射{number:03d}", "ifyk": 1, "cnt": number,
            "irtuaddr": 3 if key == "modbus-tcp" else 4, "reg_addr": number - 1,
            "icode": 5, "ytyk_type": 1,
        }, indexes)
    for number in range(1, COUNTS["yt"] + 1):
        command_code = f"{prefix}_YT_CMD_{number:03d}"
        command_id = _id_for_code(connection, "fes_channel_modbus_define_ytyk", command_code, ids, indexes)
        _add_record(store, pending, "fes_channel_modbus_define_ytyk", {
            "id": command_id, "rtu_id": rtu_id, "code": command_code,
            "name": f"{spec['name']}遥调映射{number:03d}", "ifyk": 0, "cnt": number,
            "irtuaddr": 3 if key == "modbus-tcp" else 4, "reg_addr": 200 + number,
            "icode": 6, "ytyk_type": 2,
        }, indexes)


def seed_protocol_test_data(store: ScadaStore, *, replicas: int = RTU_REPLICAS) -> dict:
    """在一个事务中增加各规约 RTU 副本的参数及四遥数据。"""
    specs = expanded_protocols(replicas)
    now = time.time_ns() // 1_000_000
    pending: list[tuple[str, dict]] = []
    ids: dict[str, int] = {}
    added = {"fes_rtu_define": 0, **{table: 0 for table in POINT_TABLES}}
    added.update({spec["parameter_table"]: 0 for spec in specs})
    for table in ("fes_channel_modbus_define", "fes_channel_modbus_define_call",
                  "fes_channel_modbus_define_data", "fes_channel_modbus_define_ytyk"):
        added[table] = 0
    backup = None
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            seed_tables = [RTU_TABLE, *POINT_TABLES,
                           *dict.fromkeys(spec["parameter_table"] for spec in specs),
                           "fes_channel_modbus_define", "fes_channel_modbus_define_call",
                           "fes_channel_modbus_define_data", "fes_channel_modbus_define_ytyk"]
            indexes = _seed_indexes(connection, store, seed_tables)
            next_rtu_id = connection.execute(
                'SELECT COALESCE(MAX(rtu_id), 0) FROM fes_rtu_define'
            ).fetchone()[0]
            next_channel_id = connection.execute(
                'SELECT COALESCE(MAX(channel_id), 0) FROM fes_rtu_define'
            ).fetchone()[0]
            for spec in specs:
                rtu_code = _base_rtu_code(spec)
                existing = _existing_by_code(connection, RTU_TABLE, rtu_code)
                if existing is None:
                    next_rtu_id += 1
                    next_channel_id += 1
                    rtu_id = next_rtu_id
                    channel_id = next_channel_id
                    _add_record(store, pending, RTU_TABLE,
                                _rtu_values(spec, rtu_id, channel_id, now), indexes)
                    added[RTU_TABLE] += 1
                else:
                    rtu_id = existing["rtu_id"]
                for kind, count in COUNTS.items():
                    table = f"fes_{kind}_define"
                    for number in range(1, count + 1):
                        if _add_record(store, pending, table,
                                       _point_values(spec, rtu_id, kind, number, now), indexes):
                            added[table] += 1
                parameter_code = f"{PREFIX}_{_key_token(spec)}_PARAM"
                parameter_existing = _existing_by_code(
                    connection, spec["parameter_table"], parameter_code
                )
                parameter_keys = store.catalog.public(spec["parameter_table"])["primary_keys"]
                if "id" in parameter_keys:
                    identifier = (
                        parameter_existing["id"] if parameter_existing is not None
                        else _next_id(connection, spec["parameter_table"], ids)
                    )
                else:
                    identifier = None
                if _add_record(store, pending, spec["parameter_table"],
                               _parameter_values(spec, rtu_id, identifier), indexes):
                    added[spec["parameter_table"]] += 1
                if spec["key"] in {"modbus-tcp", "modbus-rtu"}:
                    _add_modbus_tables(connection, store, spec, rtu_id, ids, pending, indexes)
            # Derive all insertion counts from the final pending batch. This also
            # keeps repeat execution at zero when every TEST_PROTO code exists.
            counts = Counter(table for table, _ in pending)
            added.update({table: counts[table] for table in added})
            if pending:
                backup = _backup(store.db_path)
                groups = {}
                for table, row in pending:
                    groups.setdefault((table, tuple(row)), []).append(tuple(row.values()))
                # RTU groups must precede dependent point/parameter groups.
                for (table, columns), rows in sorted(groups.items(), key=lambda item: item[0][0] != RTU_TABLE):
                    names = ", ".join(quote_identifier(key) for key in columns)
                    placeholders = ", ".join("?" for _ in columns)
                    connection.executemany(
                        f"INSERT INTO {quote_identifier(table)} ({names}) VALUES ({placeholders})", rows,
                    )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ValueError(f"外键检查失败: {[tuple(row) for row in violations]}")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(f"数据库完整性检查失败: {integrity}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    totals = {}
    with store.connection() as connection:
        for table in [RTU_TABLE, *POINT_TABLES,
                      *dict.fromkeys(spec["parameter_table"] for spec in specs),
                      "fes_channel_modbus_define", "fes_channel_modbus_define_call",
                      "fes_channel_modbus_define_data", "fes_channel_modbus_define_ytyk"]:
            totals[table] = connection.execute(
                f"SELECT COUNT(*) FROM {quote_identifier(table)}"
            ).fetchone()[0]
    return {
        "database": str(store.db_path), "backup": str(backup) if backup else None,
        "added": added, "totals": totals, "rtu_count": len(specs),
        "replicas_per_protocol": replicas,
        "points_per_rtu": COUNTS.copy(), "integrity_check": "ok",
        "foreign_key_violations": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--replicas", type=int, default=RTU_REPLICAS)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error(f"数据库不存在，请先通过 web.server 初始化: {args.db}")
    report = seed_protocol_test_data(ScadaStore(args.db), replicas=args.replicas)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
