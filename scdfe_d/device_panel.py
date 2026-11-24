"""直接基于设备宽表读取实时量测与批量更新参数。"""

import math

from core.common import ApiError
from scdfe_d.measurement_layout import SUFFIXES
from scdfe_d.service import CATEGORIES, category_for


def device_catalog(store):
    """直接从设备宽表构建前端设备选择器目录（无需 device/d_device_members 表）。"""
    tables_out = []
    devices_out = []
    with store.connection() as con:
        con.execute("BEGIN")
        cur = con.cursor()
        cur.row_factory = None
        for table, _ in store._get_table_schemas(con):
            quoted = store._table_name(table)
            cat = category_for(table)
            rows = cur.execute(f"SELECT idx, name FROM {quoted} ORDER BY idx").fetchall()
            tables_out.append(dict(table_name=table, count=len(rows)))
            for idx, name in rows:
                did = store.device_id_for(table, idx, con=con)
                devices_out.append(
                    dict(
                        device_id=did,
                        name=name,
                        category=cat,
                        active=1,
                        tables=[dict(table=table, idx=idx)],
                    )
                )
    source_db = store.default_source_database()
    return dict(
        devices=devices_out,
        tables=tables_out,
        category_labels=CATEGORIES,
        sources=[source_db] if source_db else [],
    )


def _index(value):
    try:
        if type(value) not in (int, str):
            raise ValueError
        key = int(value)
        if not -(2**63) <= key < 2**63:
            raise ValueError
        return key
    except ValueError as exc:
        raise ApiError(400, "设备索引必须是 SQLite 整数") from exc


def _read_device(store, con, table, idx, *, live=False):
    if not isinstance(table, str) or not table:
        raise ApiError(400, "请选择设备表")
    idx = _index(idx)
    metrics = store.table_metrics(con, table)
    if metrics is None:
        raise ApiError(404, "所选设备表不存在")
    table_sql = store._table_name(table)
    wide = con.execute(f"SELECT * FROM {table_sql} WHERE idx=?", (idx,)).fetchone()
    if wide is None:
        raise ApiError(404, "所选设备在该表中不存在")

    did = store.device_id_for(table, idx, con=con)
    device_info = {
        "table": table,
        "idx": idx,
        "name": wide["name"],
        "device_id": did,
        "category": category_for(table),
        "active": 1,
    }
    source_db = store.default_source_database()
    items = []
    fields = wide.keys()[2:]
    step = len(SUFFIXES)
    for offset in range(0, len(fields), step):
        metric = fields[offset]
        fields_in_group = fields[offset : offset + step]
        if fields_in_group != [metric + suffix for suffix in SUFFIXES]:
            raise ApiError(409, "设备量测字段分组不完整")
        kind = next((k for m, k in metrics if m == metric), "yc")
        value, quality, previous, source_time, notuse, weight, rtu, point = (
            wide[field] for field in fields_in_group
        )
        mapped = rtu is not None and point is not None
        meta = store.runtime_meta_for(table, idx, metric) if mapped else None
        valid, reason, collected_at = store._infer_status(
            value, quality, source_time, notuse, rtu, point, meta
        )
        mid = store.measurement_id_for(table, idx, metric, con=con) if mapped else None
        item = dict(
            measurement_id=mid,
            metric=metric,
            kind=kind,
            name=metric,
            unit="",
            source_database=source_db if mapped else None,
            enabled=1 if mapped else None,
            collected_at=collected_at,
            valid=valid,
            reason=reason,
            value=value,
            quality=quality,
            previous_value=previous,
            source_time_ms=source_time,
            notuse=notuse,
            rtu_id=rtu,
            pnt_no=point,
        )
        if not live:
            item.update(
                version=store.version_for(table, idx, metric) if mapped else None,
                se_enabled=0,
                max_age_seconds=store._max_age_seconds if mapped else None,
                scale=1.0 if mapped else None,
                offset=0.0 if mapped else None,
                source_quality=(quality & 127) if (mapped and quality is not None) else None,
                weight=weight,
            )
        items.append(item)
    return dict(device=device_info, items=items)


def read_device(store, table, idx, *, live=False):
    with store.connection() as con:
        con.execute("BEGIN")
        return _read_device(store, con, table, idx, live=live)


def _positive(value, label):
    try:
        number = float(value) if type(value) in (int, float) else math.nan
    except OverflowError:
        number = math.inf
    if not math.isfinite(number) or number <= 0:
        raise ApiError(400, f"{label}必须是有限的正数")
    return number


def _parameter_changes(changes, current):
    required = {"measurement_id", "version", "enabled", "notuse", "weight"}
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or not required <= change.keys() <= required | {
            "max_age_seconds"
        }:
            raise ApiError(400, "量测参数字段不完整或包含未知字段")
        mid, version = change["measurement_id"], change["version"]
        if type(mid) is not int or type(version) is not int or mid in seen:
            raise ApiError(400, "量测编号、版本无效或重复")
        seen.add(mid)
        row = current.get(mid)
        if row is None or row["rtu_id"] is None or row["pnt_no"] is None:
            raise ApiError(400, "只能修改当前设备已绑定量测的参数")
        if version != row["version"]:
            raise ApiError(409, "量测定义已修改，请刷新后重试", code="version_conflict")
        flags = {name: change[name] for name in ("enabled", "notuse")}
        if any(
            type(value) is not int or value not in (0, 1) for value in flags.values()
        ):
            raise ApiError(400, "启用和禁用标记必须为 0 或 1")
        # 纯设备宽表以 notuse 控制屏蔽；若前端取消 enabled 也映射为 notuse=1
        effective_notuse = 1 if (flags["notuse"] or not flags["enabled"]) else 0
        before = {
            "notuse": 1 if row["notuse"] else 0,
            "weight": float(row["weight"] if row["weight"] is not None else 1.0),
        }
        after = {
            "notuse": effective_notuse,
            "weight": _positive(change["weight"], "量测权重"),
        }
        if "max_age_seconds" in change:
            _positive(change["max_age_seconds"], "量测超时阈值")
        if before != after:
            yield mid, before, after


def update_parameters(store, payload):
    if set(payload) != {"table", "idx", "items"}:
        raise ApiError(400, "需要设备表、设备索引及量测参数")
    table, idx, items = payload["table"], payload["idx"], payload["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 500:
        raise ApiError(400, "一次保存须包含 1 至 500 项量测参数")
    with store.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        detail = _read_device(store, con, table, idx)
        current = {
            r["measurement_id"]: r
            for r in detail["items"]
            if r["measurement_id"] is not None
        }
        updates = list(_parameter_changes(items, current))
        physical_values = {}
        dev_idx = detail["device"]["idx"]
        for mid, before, after in updates:
            row = current[mid]
            metric = row["metric"]
            if before["notuse"] != after["notuse"]:
                physical_values[metric + "_notuse"] = after["notuse"]
                physical_values[metric + "_qual"] = (row["quality"] or 0) | 128
                store.set_runtime_meta(
                    table,
                    dev_idx,
                    metric,
                    valid=0,
                    reason="notuse" if after["notuse"] else "definition_changed",
                    collected_at=row["collected_at"],
                )
            if before["weight"] != after["weight"]:
                physical_values[metric + "_weight"] = after["weight"]
            store.bump_version(table, dev_idx, metric)
            store._audit(con, "measurement_parameters", mid, before, after)
        if physical_values:
            fields = ",".join(
                '"' + key.replace('"', '""') + '"=?' for key in physical_values
            )
            con.execute(
                f"UPDATE {store._table_name(table)} SET {fields} WHERE idx=?",
                (*physical_values.values(), dev_idx),
            )
        result = _read_device(store, con, table, idx)
    if updates:
        store.bump_config_epoch()
    return result
