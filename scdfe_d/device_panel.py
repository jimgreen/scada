"""Read live device quantities and update their configuration as one batch."""

import math

from scdfe_c.web.service import ApiError
from scdfe_d.measurement_layout import SUFFIXES
from scdfe_d.service import CATEGORIES, utc_now


def device_catalog(store):
    """The device pickers need model memberships, not model JSON or runtime aggregates."""
    with store.connection() as con:
        con.execute("BEGIN")
        tables = {
            row[0]: 0
            for row in con.execute(
                "SELECT table_name FROM d_device_tables ORDER BY table_name"
            )
        }
        devices = {}
        for row in con.execute("""SELECT d.device_id,d.name,d.category,d.active,b.table_name,b.idx
                FROM device d JOIN d_device_members b USING(device_id)
                ORDER BY d.device_id,b.table_name"""):
            device = devices.setdefault(
                row["device_id"],
                dict(
                    device_id=row["device_id"],
                    name=row["name"],
                    category=row["category"],
                    active=row["active"],
                    tables=[],
                ),
            )
            device["tables"].append(dict(table=row["table_name"], idx=row["idx"]))
            tables[row["table_name"]] += 1
        sources = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT source_database FROM scada_point_mapping ORDER BY source_database"
            )
        ]
    return dict(
        devices=list(devices.values()),
        tables=[dict(table_name=name, count=count) for name, count in tables.items()],
        category_labels=CATEGORIES,
        sources=sources,
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
    member = con.execute(
        """SELECT b.table_name AS 'table',b.idx,b.name,b.device_id,d.category,d.active
           FROM d_device_members b JOIN device d USING(device_id)
           WHERE b.table_name=? AND b.idx=?""",
        (table, idx),
    ).fetchone()
    if member is None:
        raise ApiError(404, "所选设备在该表中不存在")
    table_sql = store._table_name(table)
    wide = con.execute(f"SELECT * FROM {table_sql} WHERE idx=?", (idx,)).fetchone()
    if wide is None:
        raise ApiError(409, "设备量测表尚未同步")
    configuration_fields = (
        ""
        if live
        else """m.version,m.se_enabled,m.max_age_seconds,
           s.scale,s.offset,v.quality source_quality,"""
    )
    definitions = {
        row["metric"]: dict(row)
        for row in con.execute(
            f"""SELECT m.measurement_id,m.metric,m.kind,m.name,m.unit,
                      {configuration_fields}s.source_database,s.enabled,v.collected_at,
                      coalesce(v.valid,0) valid,coalesce(v.reason,'uncollected') reason
               FROM device_measurement m LEFT JOIN scada_point_mapping s USING(measurement_id)
               LEFT JOIN measurement_value v USING(measurement_id)
               WHERE m.device_id=? AND m.quantity_table=? AND m.catalog_active=1""",
            (member["device_id"], table),
        )
    }
    fields = wide.keys()[2:]
    types = None
    items = []
    for offset in range(0, len(fields), len(SUFFIXES)):
        metric = fields[offset]
        fields_in_group = fields[offset : offset + len(SUFFIXES)]
        if fields_in_group != [metric + suffix for suffix in SUFFIXES]:
            raise ApiError(409, "设备量测字段分组不完整")
        item = definitions.get(metric)
        if item is None:
            if types is None:
                types = {
                    row[1]: row[2]
                    for row in con.execute(f"PRAGMA table_info({table_sql})")
                }
            item = dict(
                measurement_id=None,
                metric=metric,
                kind="yx" if types[metric] == "INTEGER" else "yc",
                name=metric,
                unit="",
                source_database=None,
                enabled=None,
                collected_at=None,
                valid=0,
                reason="unmapped",
            )
            if not live:
                item.update(
                    version=None,
                    se_enabled=0,
                    max_age_seconds=None,
                    scale=None,
                    offset=None,
                    source_quality=None,
                )
        value, quality, previous, source_time, notuse, weight, rtu, point = (
            wide[field] for field in fields_in_group
        )
        item.update(
            value=value,
            quality=quality,
            previous_value=previous,
            source_time_ms=source_time,
            notuse=notuse,
            rtu_id=rtu,
            pnt_no=point,
        )
        if not live:
            item["weight"] = weight
        items.append(item)
    return dict(device=dict(member), items=items)


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
        if row is None or row["source_database"] is None:
            raise ApiError(400, "只能修改当前设备已绑定量测的参数")
        if version != row["version"]:
            raise ApiError(409, "量测定义已修改，请刷新后重试", code="version_conflict")
        flags = {name: change[name] for name in ("enabled", "notuse")}
        if any(
            type(value) is not int or value not in (0, 1) for value in flags.values()
        ):
            raise ApiError(400, "启用和禁用标记必须为 0 或 1")
        before = {
            key: row[key] for key in ("enabled", "notuse", "weight", "max_age_seconds")
        }
        after = dict(
            flags,
            weight=_positive(change["weight"], "量测权重"),
            max_age_seconds=_positive(
                change.get("max_age_seconds", row["max_age_seconds"]), "量测超时阈值"
            ),
        )
        if before != after:
            yield mid, version, before, after


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
        now = utc_now()
        physical_values = {}
        for mid, version, before, after in updates:
            metric = current[mid]["metric"]
            con.execute(
                """UPDATE device_measurement SET notuse=?,weight=?,max_age_seconds=?,
                   version=?,updated_at=? WHERE measurement_id=?""",
                (
                    after["notuse"],
                    after["weight"],
                    after["max_age_seconds"],
                    version + 1,
                    now,
                    mid,
                ),
            )
            con.execute(
                "UPDATE scada_point_mapping SET enabled=?,version=?,updated_at=? WHERE measurement_id=?",
                (after["enabled"], version + 1, now, mid),
            )
            for field in ("notuse", "weight"):
                if before[field] != after[field]:
                    physical_values[metric + "_" + field] = after[field]
            if any(
                before[key] != after[key]
                for key in ("notuse", "enabled", "max_age_seconds")
            ):
                reason = "definition_changed"
                if not after["enabled"]:
                    reason = "disabled"
                elif after["notuse"]:
                    reason = "notuse"
                con.execute(
                    "UPDATE measurement_value SET valid=0,reason=? WHERE measurement_id=?",
                    (reason, mid),
                )
                physical_values[metric + "_qual"] = (current[mid]["quality"] or 0) | 128
            store._audit(con, "measurement_parameters", mid, before, after)
        if physical_values:
            # The canonical parameter rows already hold the new values. Updating
            # just this device keeps its parameter columns in sync.
            fields = ",".join(
                '"' + key.replace('"', '""') + '"=?' for key in physical_values
            )
            con.execute(
                f"UPDATE {store._table_name(table)} SET {fields} WHERE idx=?",
                (*physical_values.values(), detail["device"]["idx"]),
            )
        return _read_device(store, con, table, idx)
