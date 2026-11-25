"""直接基于设备宽表读取实时量测与批量更新参数。"""

import math

from core.common import ApiError
from scdfe_d.measurement_layout import QUAL_OK, QUAL_OVER_MAX, QUAL_UNDER_MIN, SUFFIXES
from scdfe_d.service import (
    CATEGORIES,
    _check_limit_status,
    _is_valid_point_config,
    _parse_limit_number,
    category_for,
)


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
    available = set(wide.keys())
    for metric, kind in metrics:
        fields_in_group = [metric + suffix for suffix in SUFFIXES]
        if any(field not in available for field in fields_in_group):
            raise ApiError(409, "设备量测字段分组不完整")
        (
            value,
            quality,
            previous,
            source_time,
            notuse,
            weight,
            rtu,
            point,
            max_value,
            min_value,
        ) = (wide[field] for field in fields_in_group)
        mapped = _is_valid_point_config(rtu, point)
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
            max_value=max_value,
            min_value=min_value,
        )
        if not live:
            item.update(
                version=store.version_for(table, idx, metric) if mapped else None,
                se_enabled=0,
                max_age_seconds=store._max_age_seconds if mapped else None,
                scale=1.0 if mapped else None,
                offset=0.0 if mapped else None,
                source_quality=quality if (mapped and quality is not None) else None,
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


def _optional_limit(value, label):
    if value is None or value == "":
        return None
    parsed = _parse_limit_number(value)
    if parsed is None:
        raise ApiError(400, f"{label}必须是有限数值或留空")
    return parsed


def _parameter_changes(changes, current):
    required = {"measurement_id", "version", "enabled", "notuse", "weight"}
    optional = {"max_age_seconds", "max_value", "min_value"}
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or not required <= change.keys() <= required | optional:
            raise ApiError(400, "量测参数字段不完整或包含未知字段")
        mid, version = change["measurement_id"], change["version"]
        if type(mid) is not int or type(version) is not int or mid in seen:
            raise ApiError(400, "量测编号、版本无效或重复")
        seen.add(mid)
        row = current.get(mid)
        if row is None or not _is_valid_point_config(row["rtu_id"], row["pnt_no"]):
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
            "max_value": row.get("max_value"),
            "min_value": row.get("min_value"),
        }
        after = {
            "notuse": effective_notuse,
            "weight": _positive(change["weight"], "量测权重"),
            "max_value": (
                _optional_limit(change["max_value"], "量测上限")
                if "max_value" in change
                else row.get("max_value")
            ),
            "min_value": (
                _optional_limit(change["min_value"], "量测下限")
                if "min_value" in change
                else row.get("min_value")
            ),
        }
        if (
            ("max_value" in change or "min_value" in change)
            and after["max_value"] is not None
            and after["min_value"] is not None
            and after["max_value"] <= after["min_value"]
        ):
            raise ApiError(400, f"{row['metric']}：量测上限必须大于下限，无法修改")
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
        pending_meta = []
        dev_idx = detail["device"]["idx"]
        for mid, before, after in updates:
            row = current[mid]
            metric = row["metric"]
            if before["notuse"] != after["notuse"]:
                physical_values[metric + "_notuse"] = after["notuse"]
                pending_meta.append(
                    (
                        metric,
                        0,
                        "notuse" if after["notuse"] else "definition_changed",
                        row["collected_at"],
                    )
                )
            if before["weight"] != after["weight"]:
                physical_values[metric + "_weight"] = after["weight"]
            limits_changed = False
            if before["max_value"] != after["max_value"]:
                physical_values[metric + "_max"] = after["max_value"]
                limits_changed = True
            if before["min_value"] != after["min_value"]:
                physical_values[metric + "_min"] = after["min_value"]
                limits_changed = True
            # 若仅修改上下限且当前通信正常，立即依据现有实测值重算质量码，确保页面与库内一致
            if (
                limits_changed
                and row.get("quality") in (QUAL_OK, QUAL_OVER_MAX, QUAL_UNDER_MIN)
                and row.get("value") is not None
            ):
                limit_hit = _check_limit_status(
                    row["value"], after["max_value"], after["min_value"]
                )
                if limit_hit is not None:
                    new_qual, new_reason = limit_hit
                    new_valid = 0
                else:
                    new_qual = QUAL_OK
                    new_reason = "notuse" if after["notuse"] else ""
                    new_valid = 0 if after["notuse"] else 1
                physical_values[metric + "_qual"] = new_qual
                pending_meta.append((metric, new_valid, new_reason, row["collected_at"]))
        if physical_values:
            fields = ",".join(
                '"' + key.replace('"', '""') + '"=?' for key in physical_values
            )
            con.execute(
                f"UPDATE {store._table_name(table)} SET {fields} WHERE idx=?",
                (*physical_values.values(), dev_idx),
            )
    # 仅在 SQLite 事务提交成功后更新内存版本号、运行时状态与变更审计，避免回滚脏状态
    if updates:
        for metric, valid_flag, reason_text, collected_at in pending_meta:
            store.set_runtime_meta(
                table,
                dev_idx,
                metric,
                valid=valid_flag,
                reason=reason_text,
                collected_at=collected_at,
            )
        for mid, before, after in updates:
            row = current[mid]
            store.bump_version(table, dev_idx, row["metric"])
            store._audit(None, "measurement_parameters", mid, before, after)
        store.bump_config_epoch()
    with store.connection() as con:
        con.execute("BEGIN")
        return _read_device(store, con, table, idx)
