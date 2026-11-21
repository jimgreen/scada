"""以 dbdef/json/*.json 为唯一数据源（SSOT）校验与重新生成 dbdef/def/*.py。"""
from __future__ import annotations

import argparse
from dataclasses import MISSING, fields as dc_fields
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
JSON_DIR = ROOT / "dbdef" / "json"
DEF_DIR = ROOT / "dbdef" / "def"

_TYPE_ANNOTATIONS = {
    "int": "Optional[int]",
    "float": "Optional[float]",
    "str": "Optional[str]",
    "bool": "Optional[bool]",
}


def render_dataclass(schema: dict) -> str:
    """根据单张表的 JSON Schema 确定性生成对应的 Python dataclass 源码。"""
    head = schema["head"]
    eng_name = head["eng_name"]
    chn_name = head["chn_name"]
    columns = schema["columns"]
    pks = tuple(col["eng_name"] for col in columns if col.get("primary_key"))

    lines = [
        "# -*- coding: utf-8 -*-",
        '"""SCADA 数据库表定义（由 dbdef/json 自动生成，请勿手工修改）。',
        "",
        f"表名：{eng_name}",
        f"中文名：{chn_name}",
        '"""',
        "",
        "from dataclasses import dataclass, field",
        "from typing import ClassVar, Optional",
        "",
        "",
        "@dataclass",
        f"class {eng_name}:",
        f'    """{chn_name}。"""',
        "",
        f"    __tablename__: ClassVar[str] = {eng_name!r}",
        f"    __table_name_chn__: ClassVar[str] = {chn_name!r}",
        f"    __primary_keys__: ClassVar[tuple[str, ...]] = {pks!r}",
        "",
    ]

    for col in columns:
        name = col["eng_name"]
        ann = _TYPE_ANNOTATIONS.get(col["data_type"], "Optional[object]")
        default_val = col.get("default", None)
        lines.append(f"    {name}: {ann} = field(")
        lines.append(f"        default={default_val!r},")
        lines.append(f"        metadata={col!r},")
        lines.append("    )")
        lines.append("")

    lines.append(f"__all__ = [{eng_name!r}]")
    lines.append("")
    return "\n".join(lines)


def check_sync(json_dir: Path = JSON_DIR, def_dir: Path = DEF_DIR) -> list[str]:
    """检查 dbdef/def/*.py 与 dbdef/json/*.json 是否完全一致，返回差异列表。"""
    issues: list[str] = []
    json_files = {p.stem: p for p in sorted(json_dir.glob("*.json"))}
    def_files = {p.stem: p for p in sorted(def_dir.glob("*.py")) if not p.name.startswith("_")}

    for only_json in sorted(json_files.keys() - def_files.keys()):
        issues.append(f"缺少 Python 定义文件：{only_json}.py")
    for only_def in sorted(def_files.keys() - json_files.keys()):
        issues.append(f"多余 Python 定义文件（无对应 JSON）：{only_def}.py")

    for stem in sorted(json_files.keys() & def_files.keys()):
        schema = json.loads(json_files[stem].read_text(encoding="utf-8"))
        spec = importlib.util.spec_from_file_location(f"_dbdef_check_{stem}", def_files[stem])
        if spec is None or spec.loader is None:
            issues.append(f"{stem}: 无法加载模块")
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:
            issues.append(f"{stem}: 导入失败 {exc}")
            continue
        cls = getattr(mod, stem, None)
        if cls is None:
            issues.append(f"{stem}: 缺少同名 dataclass 类")
            continue
        py_fields = {f.name: f for f in dc_fields(cls)}
        json_cols = schema["columns"]
        if list(py_fields.keys()) != [c["eng_name"] for c in json_cols]:
            issues.append(f"{stem}: 字段列表或顺序不一致")
            continue
        for col in json_cols:
            name = col["eng_name"]
            f = py_fields[name]
            expected_default = col.get("default", None)
            actual_default = None if f.default is MISSING else f.default
            if actual_default != expected_default:
                issues.append(f"{stem}.{name}: 默认值漂移 py={actual_default!r} != json={expected_default!r}")
            if dict(f.metadata) != col:
                issues.append(f"{stem}.{name}: metadata 与 JSON 定义不一致")
    return issues


def sync_all(json_dir: Path = JSON_DIR, def_dir: Path = DEF_DIR) -> list[Path]:
    """按 dbdef/json/*.json 重新生成所有 dbdef/def/*.py 文件。"""
    def_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for json_file in sorted(json_dir.glob("*.json")):
        schema = json.loads(json_file.read_text(encoding="utf-8"))
        target = def_dir / f"{json_file.stem}.py"
        rendered = render_dataclass(schema)
        if not target.is_file() or target.read_text(encoding="utf-8") != rendered:
            target.write_text(rendered, encoding="utf-8", newline="\n")
            written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验或重新生成 dbdef/def/*.py（以 dbdef/json 为 SSOT）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="仅检查漂移（默认），存在漂移时返回非零退出码")
    mode.add_argument("--apply", action="store_true", help="按 JSON Schema 重新生成全部 dbdef/def/*.py")
    args = parser.parse_args(argv)

    if args.apply:
        written = sync_all()
        print(f"已同步更新 {len(written)} 个 dbdef/def/*.py 文件", flush=True)
        issues = check_sync()
        if issues:
            for item in issues[:20]:
                print("ERROR:", item, file=sys.stderr)
            return 1
        print("校验通过：全部 32 张表 dbdef/def 与 dbdef/json 100% 一致", flush=True)
        return 0

    issues = check_sync()
    if issues:
        print(f"发现 {len(issues)} 处漂移：", file=sys.stderr)
        for item in issues[:30]:
            print("  -", item, file=sys.stderr)
        return 1
    print("校验通过：dbdef/def 与 dbdef/json 完全一致", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
