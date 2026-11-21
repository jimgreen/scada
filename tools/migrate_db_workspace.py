"""将 SCADA 数据库统一由仓库 db/ 迁移至三参数工作区目录的工具。

目标目录格式：
    <workspace_root>/<runtime_name>/<scene_name>/sqldb/scada/

核心保障：
1. 迁移前检查源库与目标库是否持有活跃 InstanceLock，运行中直接中止；
2. 使用 SQLite 在线备份 API（backup）复制数据，安全合并 WAL，不损伤源库；
3. 若目标库已存在且允许覆盖，先备份至目标同级 `backups/<timestamp>/`；
4. 迁移后修正库内相对路径（如 `fes_scdfe_c_define.log_dir`），可选 VACUUM 回收空闲页并全量校验；
5. 全部迁移验证通过后，可按需清理旧仓库 `db/*.bak`、陈旧 `.lock`、过期测试库及根目录 `.tmp_*`。
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from core.database_validation import validate_database
from core.scdfe_control import InstanceLock
from core.workspace_paths import (
    LEGACY_REPO_DB_DIR,
    REPO_ROOT,
    add_workspace_arguments,
    database_directory,
    resolve_runtime_name,
    resolve_scene_name,
    resolve_workspace_root,
)
from scdfe_c.web.service import SCHEMA_DIR, SchemaCatalog


_SOURCE_MAPPINGS = (
    # (角色, 候选源文件名元组, 目标文件名)
    ("c", ("scada.sqlite3", "scdfe_c.sqlite3"), "scdfe_c.sqlite3"),
    ("s", ("scdfe_s.sqlite3",), "scdfe_s.sqlite3"),
    ("d", ("scdfe_d.sqlite3",), "scdfe_d.sqlite3"),
)

_LOCK_ROLES = (
    "main",
    "writer",
    "command-server",
    "process-control",
    "device-collector",
    "packet-log-c",
    "packet-log-s",
    "packet-log-t",
)


def _active_locks(db_path: Path) -> list[str]:
    """检查指定数据库文件上当前是否存在活跃的进程锁。"""
    held: list[str] = []
    if not db_path.parent.is_dir():
        return held
    roles = list(_LOCK_ROLES)
    prefix = f"{db_path.name}.scdfe_c-"
    for candidate in db_path.parent.glob(f"{prefix}*.lock"):
        role = candidate.name[len(prefix):-5]
        if role and role not in roles:
            roles.append(role)
    for role in roles:
        if InstanceLock.held(db_path, role):
            held.append(role)
    return held


def _find_source(source_dir: Path, candidates: tuple[str, ...]) -> Path | None:
    for name in candidates:
        candidate = source_dir / name
        if candidate.is_file():
            return candidate
    return None


def _backup_sqlite(source: Path, destination: Path) -> None:
    """使用 SQLite online backup API 完整复制数据库并合并 WAL。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(source), timeout=10)) as src:
        with closing(sqlite3.connect(str(destination), timeout=10)) as dst:
            src.backup(dst)
            dst.commit()


def _normalize_migrated_db(db_path: Path, role: str, *, vacuum: bool) -> list[str]:
    """修正迁移后库内的相对路径并按需执行 VACUUM 与 WAL 模式确认。"""
    notes: list[str] = []
    with closing(sqlite3.connect(str(db_path), timeout=10)) as con:
        if role == "c":
            has_table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fes_scdfe_c_define'"
            ).fetchone()
            if has_table:
                row = con.execute("SELECT log_dir FROM fes_scdfe_c_define WHERE id=1").fetchone()
                if row and row[0] and (Path(row[0]).is_absolute() or ".." in str(row[0])):
                    con.execute("UPDATE fes_scdfe_c_define SET log_dir='logs' WHERE id=1")
                    notes.append(f"已重置 fes_scdfe_c_define.log_dir: {row[0]!r} -> 'logs'")
                con.commit()
        if vacuum:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("VACUUM")
            notes.append("已完成 VACUUM 回收空闲页")
        con.execute("PRAGMA journal_mode=WAL")
        con.commit()
    return notes


def _validate_target(db_path: Path, role: str, catalog: SchemaCatalog) -> dict:
    if role == "d":
        from scdfe_d.service import validate_database as validate_device_database
        return validate_device_database(db_path)
    return validate_database(db_path, role, catalog)


def migrate_workspace_databases(
    *,
    source_dir: Path,
    workspace_root: Path,
    runtime_name: str,
    scene_name: str,
    apply: bool = False,
    overwrite: bool = False,
    vacuum: bool = True,
    clean_legacy: bool = False,
    repo_root: Path = REPO_ROOT,
) -> dict:
    """执行（或预演）三参数工作区数据库迁移。"""
    source_dir = Path(source_dir).resolve()
    root = resolve_workspace_root(workspace_root)
    runtime = resolve_runtime_name(runtime_name)
    scene = resolve_scene_name(scene_name)
    target_dir = database_directory(root, runtime, scene)
    catalog = SchemaCatalog(SCHEMA_DIR)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    report: dict = {
        "mode": "apply" if apply else "dry_run",
        "source_dir": str(source_dir),
        "workspace_root": str(root),
        "runtime_name": runtime,
        "scene_name": scene,
        "target_dir": str(target_dir),
        "databases": [],
        "cleaned_files": [],
        "freed_bytes": 0,
    }

    plan: list[tuple[str, Path, Path]] = []
    for role, candidates, target_name in _SOURCE_MAPPINGS:
        src = _find_source(source_dir, candidates)
        if src is None:
            continue
        dst = target_dir / target_name
        if src.resolve() == dst.resolve():
            raise ValueError(f"源库与目标库相同，无需迁移：{src}")
        src_locks = _active_locks(src)
        if src_locks:
            raise RuntimeError(f"源库 {src} 存在运行中服务锁：{', '.join(src_locks)}，请先停止服务")
        if dst.exists():
            dst_locks = _active_locks(dst)
            if dst_locks:
                raise RuntimeError(f"目标库 {dst} 存在运行中服务锁：{', '.join(dst_locks)}，请先停止服务")
            if apply and not overwrite:
                raise FileExistsError(f"目标库已存在：{dst}；如需备份后覆盖请追加 --overwrite")
        plan.append((role, src, dst))

    if not plan:
        raise FileNotFoundError(f"源目录 {source_dir} 中未找到可迁移的 SCADA 数据库文件")

    for role, src, dst in plan:
        entry = {
            "role": role,
            "source": str(src),
            "target": str(dst),
            "source_bytes": src.stat().st_size,
            "target_exists": dst.exists(),
            "backup": None,
            "notes": [],
        }
        if not apply:
            report["databases"].append(entry)
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target_dir, prefix=".migrate-") as tmp_dir:
            staged = Path(tmp_dir) / dst.name
            _backup_sqlite(src, staged)
            entry["notes"].extend(_normalize_migrated_db(staged, role, vacuum=vacuum))
            validation = _validate_target(staged, role, catalog)
            if not validation.get("valid"):
                raise RuntimeError(f"迁移校验失败（{dst.name}）：{validation.get('message')}")
            entry["validation"] = validation

            if dst.exists():
                backup_dir = target_dir / "backups" / timestamp
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_path = backup_dir / dst.name
                _backup_sqlite(dst, backup_path)
                entry["backup"] = str(backup_path)

            for suffix in ("-wal", "-shm"):
                Path(str(dst) + suffix).unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(staged) + suffix).unlink(missing_ok=True)
            staged.replace(dst)
            entry["target_bytes"] = dst.stat().st_size
            report["databases"].append(entry)

    if clean_legacy:
        candidates_to_clean: list[Path] = []
        if source_dir.is_dir():
            candidates_to_clean.extend(sorted(source_dir.glob("*.bak")))
            candidates_to_clean.extend(sorted(source_dir.glob("*.lock")))
            candidates_to_clean.extend(sorted(source_dir.glob("web_database_*.json")))
            test_db = source_dir / "test_protocol_schema.sqlite3"
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(test_db) + suffix)
                if p.is_file():
                    candidates_to_clean.append(p)
        for tmp_script in sorted(repo_root.glob(".tmp_*")):
            if tmp_script.is_file():
                candidates_to_clean.append(tmp_script)

        seen: set[Path] = set()
        for item in candidates_to_clean:
            resolved = item.resolve()
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            size = resolved.stat().st_size
            if apply:
                try:
                    resolved.unlink()
                except OSError:
                    continue
            report["cleaned_files"].append(str(resolved))
            report["freed_bytes"] += size

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="将 SCADA 数据库统一迁移至 <workspace_root>/<runtime_name>/<scene_name>/sqldb/scada"
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=LEGACY_REPO_DB_DIR,
        help=f"源数据库目录（默认 {LEGACY_REPO_DB_DIR}）",
    )
    add_workspace_arguments(parser)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="仅预演迁移计划（默认）")
    mode.add_argument("--apply", action="store_true", help="正式执行数据库迁移")
    parser.add_argument("--overwrite", action="store_true", help="目标已存在时先备份再覆盖")
    parser.add_argument("--no-vacuum", action="store_true", help="跳过迁移后的 VACUUM 空间回收")
    parser.add_argument(
        "--clean-legacy",
        action="store_true",
        help="迁移校验通过后清理旧目录 .bak/.lock/旧偏好/过期测试库及根目录 .tmp_*",
    )
    args = parser.parse_args(argv)
    report = migrate_workspace_databases(
        source_dir=args.source_dir,
        workspace_root=resolve_workspace_root(args.workspace_root),
        runtime_name=resolve_runtime_name(args.runtime_name),
        scene_name=resolve_scene_name(args.scene_name),
        apply=bool(args.apply),
        overwrite=bool(args.overwrite),
        vacuum=not args.no_vacuum,
        clean_legacy=bool(args.clean_legacy),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
