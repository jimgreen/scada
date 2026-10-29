"""为每种通信规约生成大规模 RTU、通道参数和四遥测试数据。

脚本一次性生成 IEC104、Modbus TCP、Modbus RTU、CAN、BACnet/IP 和 OPC UA 六个测试站；
已存在的测试站只补齐缺失记录，不覆盖用户修改：

* IEC104 参数表；
* Modbus TCP、Modbus RTU、CAN、BACnet/IP、OPC UA 五个测试站；
* 每个测试站 100 个遥测、100 个遥信、20 个遥控、20 个遥调；
* Modbus 的召唤、数据解析和遥控/遥调定义。

只增加带 ``TEST_PROTO_`` 标识的记录。重复执行不会覆盖用户修改；插入前会
使用 SQLite backup API 保存完整数据库快照，并在同一个事务中完成外键和完整性检查。
"""
from __future__ import annotations

import argparse
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
COUNTS = {"yc": 100, "yx": 100, "yk": 20, "yt": 20}

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


def _id_for_code(connection, table: str, code: str, ids: dict[str, int]) -> int:
    existing = _existing_by_code(connection, table, code)
    return existing["id"] if existing is not None else _next_id(connection, table, ids)


def _record(table: str, values: dict, store: ScadaStore):
    return store._normalise(table, values)


def _add_record(connection, store: ScadaStore, pending: list[tuple[str, dict]],
                table: str, values: dict, seen_codes: dict[str, set[str]]):
    values = _record(table, values, store)
    code = values.get("code")
    schema = store.catalog.public(table)
    primary_keys = schema["primary_keys"]
    if all(key in values for key in primary_keys):
        predicate = " AND ".join(f"{quote_identifier(key)}=?" for key in primary_keys)
        existing_key = connection.execute(
            f"SELECT 1 FROM {quote_identifier(table)} WHERE {predicate}",
            [values[key] for key in primary_keys],
        ).fetchone()
        if existing_key is not None:
            # Existing rows (including the first four IEC104 demo points) are
            # retained; only missing point numbers are added.
            return False
    if code and code in seen_codes.setdefault(table, set()):
        return False
    if code:
        existing = _existing_by_code(connection, table, code)
        if existing is not None:
            if any(existing[key] != values[key] for key in primary_keys):
                raise ValueError(f"{table}: 测试标识与现有主键冲突: {code}")
            # A repeated seed must not overwrite values edited by the user.
            seen_codes[table].add(code)
            return False
        seen_codes[table].add(code)
    pending.append((table, values))
    return True


def _rtu_values(spec: dict, rtu_id: int, channel_id: int, now: int) -> dict:
    rtu_code = spec.get("rtu_code", f"{PREFIX}_{spec['key'].upper().replace('-', '_')}_RTU")
    return {
        "rtu_id": rtu_id,
        "code": rtu_code,
        "rdfid": rtu_code,
        "name": spec["name"],
        "status": 0, "call_all_cycle": 30000, "refresh_trigger": 1000,
        "his_save": 0, "refresh_time": now,
        "channel_id": channel_id, "channel_type": spec["key"],
        "chan_addr": spec["channel"], "chan_port": spec["port"],
    }


def _point_values(spec: dict, rtu_id: int, kind: str, number: int,
                  now: int) -> dict:
    code = f"{PREFIX}_{spec['key'].upper().replace('-', '_')}_{kind.upper()}_{number:03d}"
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
    code = f"{PREFIX}_{key.upper().replace('-', '_')}_PARAM"
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
        base.update(id=identifier, bind_ip="127.0.0.1", bind_port=47809, dev_id=1234)
    elif key == "opc":
        base.pop("rdfid", None)
        base.update(id=identifier, opc_interval=1000, opc_user="", opc_pwd="",
                    opc_domain="", opc_name="opc.tcp://127.0.0.1:4840")
    return base


def _add_modbus_tables(connection, store: ScadaStore, spec: dict, rtu_id: int,
                       ids: dict[str, int], pending, seen_codes):
    key = spec["key"]
    prefix = f"{PREFIX}_{key.upper().replace('-', '_')}"
    channel_code = f"{prefix}_CHANNEL"
    channel_id = _id_for_code(connection, "fes_channel_modbus_define", channel_code, ids)
    _add_record(connection, store, pending, "fes_channel_modbus_define", {
        "id": channel_id, "rtu_id": rtu_id, "code": channel_code,
        "rdfid": channel_code, "name": f"{spec['name']}Modbus通道",
        "irtuaddr": 3 if key == "modbus-tcp" else 4,
    }, seen_codes)
    for seq, function_code, count in ((0, 3, 100), (1, 1, 100)):
        call_code = f"{prefix}_CALL_{seq}"
        call_id = _id_for_code(connection, "fes_channel_modbus_define_call", call_code, ids)
        _add_record(connection, store, pending, "fes_channel_modbus_define_call", {
            "id": call_id, "rtu_id": rtu_id, "code": call_code,
            "name": f"{spec['name']}召唤{seq}", "irtuaddr": 3 if key == "modbus-tcp" else 4,
            "call_seq": seq, "iplace": 0, "inum": count, "icode": function_code,
        }, seen_codes)
    for number in range(1, COUNTS["yc"] + 1):
        data_code = f"{prefix}_YC_DATA_{number:03d}"
        data_id = _id_for_code(connection, "fes_channel_modbus_define_data", data_code, ids)
        _add_record(connection, store, pending, "fes_channel_modbus_define_data", {
            "id": data_id, "rtu_id": rtu_id, "code": data_code,
            "name": f"{spec['name']}遥测映射{number:03d}", "call_seq": 0,
            "if_yx": 0, "cnt": number, "trans_type": 2, "reg_addr": number - 1,
        }, seen_codes)
    for number in range(1, COUNTS["yx"] + 1):
        data_code = f"{prefix}_YX_DATA_{number:03d}"
        data_id = _id_for_code(connection, "fes_channel_modbus_define_data", data_code, ids)
        _add_record(connection, store, pending, "fes_channel_modbus_define_data", {
            "id": data_id, "rtu_id": rtu_id, "code": data_code,
            "name": f"{spec['name']}遥信映射{number:03d}", "call_seq": 1,
            "if_yx": 1, "cnt": number, "trans_type": 8, "reg_addr": number - 1,
        }, seen_codes)
    for number in range(1, COUNTS["yk"] + 1):
        command_code = f"{prefix}_YK_CMD_{number:03d}"
        command_id = _id_for_code(connection, "fes_channel_modbus_define_ytyk", command_code, ids)
        _add_record(connection, store, pending, "fes_channel_modbus_define_ytyk", {
            "id": command_id, "rtu_id": rtu_id, "code": command_code,
            "name": f"{spec['name']}遥控映射{number:03d}", "ifyk": 1, "cnt": number,
            "irtuaddr": 3 if key == "modbus-tcp" else 4, "reg_addr": number - 1,
            "icode": 5, "ytyk_type": 1,
        }, seen_codes)
    for number in range(1, COUNTS["yt"] + 1):
        command_code = f"{prefix}_YT_CMD_{number:03d}"
        command_id = _id_for_code(connection, "fes_channel_modbus_define_ytyk", command_code, ids)
        _add_record(connection, store, pending, "fes_channel_modbus_define_ytyk", {
            "id": command_id, "rtu_id": rtu_id, "code": command_code,
            "name": f"{spec['name']}遥调映射{number:03d}", "ifyk": 0, "cnt": number,
            "irtuaddr": 3 if key == "modbus-tcp" else 4, "reg_addr": 200 + number,
            "icode": 6, "ytyk_type": 2,
        }, seen_codes)


def seed_protocol_test_data(store: ScadaStore) -> dict:
    """在一个事务中增加六个规约站的参数及四遥数据。"""
    now = time.time_ns() // 1_000_000
    pending: list[tuple[str, dict]] = []
    seen_codes: dict[str, set[str]] = {}
    ids: dict[str, int] = {}
    added = {"fes_rtu_define": 0, **{table: 0 for table in POINT_TABLES}}
    added.update({spec["parameter_table"]: 0 for spec in PROTOCOLS})
    for table in ("fes_channel_modbus_define", "fes_channel_modbus_define_call",
                  "fes_channel_modbus_define_data", "fes_channel_modbus_define_ytyk"):
        added[table] = 0
    backup = None
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            next_rtu_id = connection.execute(
                'SELECT COALESCE(MAX(rtu_id), 0) FROM fes_rtu_define'
            ).fetchone()[0]
            next_channel_id = connection.execute(
                'SELECT COALESCE(MAX(channel_id), 0) FROM fes_rtu_define'
            ).fetchone()[0]
            for spec in PROTOCOLS:
                rtu_code = spec.get(
                    "rtu_code",
                    f"{PREFIX}_{spec['key'].upper().replace('-', '_')}_RTU",
                )
                existing = _existing_by_code(connection, RTU_TABLE, rtu_code)
                if existing is None:
                    next_rtu_id += 1
                    next_channel_id += 1
                    rtu_id = next_rtu_id
                    channel_id = next_channel_id
                    _add_record(connection, store, pending, RTU_TABLE,
                                _rtu_values(spec, rtu_id, channel_id, now), seen_codes)
                    added[RTU_TABLE] += 1
                else:
                    rtu_id = existing["rtu_id"]
                for kind, count in COUNTS.items():
                    table = f"fes_{kind}_define"
                    for number in range(1, count + 1):
                        if _add_record(connection, store, pending, table,
                                       _point_values(spec, rtu_id, kind, number, now), seen_codes):
                            added[table] += 1
                parameter_code = f"{PREFIX}_{spec['key'].upper().replace('-', '_')}_PARAM"
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
                if _add_record(connection, store, pending, spec["parameter_table"],
                               _parameter_values(spec, rtu_id, identifier), seen_codes):
                    added[spec["parameter_table"]] += 1
                if spec["key"] in {"modbus-tcp", "modbus-rtu"}:
                    _add_modbus_tables(connection, store, spec, rtu_id, ids, pending, seen_codes)
            # Derive all insertion counts from the final pending batch. This also
            # keeps repeat execution at zero when every TEST_PROTO code exists.
            for table in added:
                added[table] = sum(1 for table_name, _ in pending if table_name == table)
            if pending:
                backup = _backup(store.db_path)
                for table, row in pending:
                    names = ", ".join(quote_identifier(key) for key in row)
                    placeholders = ", ".join("?" for _ in row)
                    connection.execute(
                        f"INSERT INTO {quote_identifier(table)} ({names}) VALUES ({placeholders})",
                        list(row.values()),
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
                      *[spec["parameter_table"] for spec in PROTOCOLS],
                      "fes_channel_modbus_define", "fes_channel_modbus_define_call",
                      "fes_channel_modbus_define_data", "fes_channel_modbus_define_ytyk"]:
            totals[table] = connection.execute(
                f"SELECT COUNT(*) FROM {quote_identifier(table)}"
            ).fetchone()[0]
    return {
        "database": str(store.db_path), "backup": str(backup) if backup else None,
        "added": added, "totals": totals, "rtu_count": len(PROTOCOLS),
        "points_per_rtu": COUNTS.copy(), "integrity_check": "ok",
        "foreign_key_violations": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error(f"数据库不存在，请先通过 web.server 初始化: {args.db}")
    print(json.dumps(seed_protocol_test_data(ScadaStore(args.db)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
