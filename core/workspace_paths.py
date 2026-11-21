"""统一的工作区三参数数据库路径解析与目录发现模块。

数据库路径遵循统一结构：
    <workspace_root>/<runtime_name>/<scene_name>/sqldb/scada/<role_db>.sqlite3

其中：
- ``workspace_root``：工作区根目录（默认 ``D:\\codex\\workspace``，环境变量 ``SCDFE_WORKSPACE_ROOT``）
- ``runtime_name``：运行态名称，如 ``realtime`` / ``simulation``（默认 ``realtime``，环境变量 ``SCDFE_RUNTIME_NAME``）
- ``scene_name``：场景名称，如 ``qinling``（默认 ``qinling``，环境变量 ``SCDFE_SCENE_NAME``）
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat


REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_REPO_DB_DIR = REPO_ROOT / "db"

DEFAULT_WORKSPACE_ROOT = Path(__file__).resolve().parents[2] / "workspace"
DEFAULT_RUNTIME_NAME = "realtime"
DEFAULT_SCENE_NAME = "qinling"
SQLDB_RELATIVE_PARTS = ("sqldb", "scada")

ENV_WORKSPACE_ROOT = "SCDFE_WORKSPACE_ROOT"
ENV_RUNTIME_NAME = "SCDFE_RUNTIME_NAME"
ENV_SCENE_NAME = "SCDFE_SCENE_NAME"

ROLE_DB_FILENAMES = {
    "c": "scdfe_c.sqlite3",
    "t": "scdfe_c.sqlite3",
    "s": "scdfe_s.sqlite3",
    "d": "scdfe_d.sqlite3",
}

_INVALID_SEGMENT_CHARS = set('/\\\x00<>:"|?*')


def validate_segment(value: str, label: str) -> str:
    """校验运行态名称或场景名称为单个安全目录名，禁止路径穿越。"""
    if not isinstance(value, str):
        raise ValueError(f"{label}必须为非空字符串")
    text = value.strip()
    if (
        not text
        or text != value
        or text in {".", ".."}
        or text.startswith(".")
        or text.endswith((" ", "."))
        or any(ch in _INVALID_SEGMENT_CHARS for ch in text)
    ):
        raise ValueError(f"{label}无效：{value!r}；仅允许单级合法目录名称")
    return text


def resolve_workspace_root(workspace_root: str | Path | None = None) -> Path:
    """解析工作区根目录，优先级：显式参数 > 环境变量 > 默认值。"""
    raw = workspace_root
    if raw is None:
        env_value = os.environ.get(ENV_WORKSPACE_ROOT, "").strip()
        raw = Path(env_value) if env_value else DEFAULT_WORKSPACE_ROOT
    return Path(raw).resolve()


def resolve_runtime_name(runtime_name: str | None = None) -> str:
    """解析运行态名称，优先级：显式参数 > 环境变量 > 默认值。"""
    if runtime_name is not None:
        return validate_segment(runtime_name, "运行态名称")
    env_value = os.environ.get(ENV_RUNTIME_NAME, "").strip()
    return validate_segment(env_value or DEFAULT_RUNTIME_NAME, "运行态名称")


def resolve_scene_name(scene_name: str | None = None) -> str:
    """解析场景名称，优先级：显式参数 > 环境变量 > 默认值。"""
    if scene_name is not None:
        return validate_segment(scene_name, "场景名称")
    env_value = os.environ.get(ENV_SCENE_NAME, "").strip()
    return validate_segment(env_value or DEFAULT_SCENE_NAME, "场景名称")


def database_directory(
    workspace_root: str | Path | None = None,
    runtime_name: str | None = None,
    scene_name: str | None = None,
) -> Path:
    """组合返回 `<workspace_root>/<runtime_name>/<scene_name>/sqldb/scada` 目录。"""
    root = resolve_workspace_root(workspace_root)
    runtime = resolve_runtime_name(runtime_name)
    scene = resolve_scene_name(scene_name)
    directory = (root / runtime / scene / Path(*SQLDB_RELATIVE_PARTS)).resolve()
    if not directory.is_relative_to(root):
        raise ValueError("数据库目录超出工作区根目录范围")
    return directory


def default_database_path(
    role: str,
    workspace_root: str | Path | None = None,
    runtime_name: str | None = None,
    scene_name: str | None = None,
) -> Path:
    """返回指定角色（c/s/t/d）在三参数下的默认 SQLite 文件路径。"""
    if role not in ROLE_DB_FILENAMES:
        raise ValueError(f"不支持的服务角色：{role!r}")
    return database_directory(workspace_root, runtime_name, scene_name) / ROLE_DB_FILENAMES[role]


def add_workspace_arguments(parser: argparse.ArgumentParser) -> None:
    """为 CLI / Web 解析器统一注册工作区根目录、运行态名称、场景名称三个参数。"""
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help=f"工作区根目录（环境变量 {ENV_WORKSPACE_ROOT}，默认 {DEFAULT_WORKSPACE_ROOT}）",
    )
    parser.add_argument(
        "--runtime-name",
        "--runtime",
        dest="runtime_name",
        default=None,
        help=f"运行态名称，如 realtime/simulation（环境变量 {ENV_RUNTIME_NAME}，默认 {DEFAULT_RUNTIME_NAME}）",
    )
    parser.add_argument(
        "--scene-name",
        "--scene",
        dest="scene_name",
        default=None,
        help=f"场景名称，如 qinling（环境变量 {ENV_SCENE_NAME}，默认 {DEFAULT_SCENE_NAME}）",
    )


def resolve_cli_database(
    role: str,
    explicit_db: str | Path | None = None,
    *,
    workspace_root: str | Path | None = None,
    runtime_name: str | None = None,
    scene_name: str | None = None,
) -> tuple[Path, Path]:
    """解析 CLI 入口的工作区根目录与数据库路径。

    - 若未显式传 ``--db``，自动定位到三参数默认库路径；
    - 若传入相对 ``--db`` 且指定了工作区根目录（或处于常规用法），相对工作区根目录解析；
    - 若传入绝对 ``--db``，按该路径解析（保留测试及独立调试兼容性）。
    """
    root = resolve_workspace_root(workspace_root)
    if explicit_db is None:
        return root, default_database_path(role, root, runtime_name, scene_name)
    candidate = Path(explicit_db)
    if candidate.is_absolute():
        return root, candidate.resolve()
    if workspace_root is not None:
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("数据库路径不能超出工作区根目录")
        return root, resolved
    return root, candidate.resolve()


def infer_workspace_context(db_path: str | Path, workspace_root: str | Path) -> dict[str, str | None]:
    """根据数据库相对路径反推运行态名称、场景名称与 sqldb 目录相对路径。"""
    root = Path(workspace_root).resolve()
    try:
        rel = Path(db_path).resolve().relative_to(root)
    except (ValueError, OSError, RuntimeError):
        return {"runtime_name": None, "scene_name": None, "sqldb_directory": None}
    parts = rel.parts
    if len(parts) >= 5 and parts[2] == SQLDB_RELATIVE_PARTS[0] and parts[3] == SQLDB_RELATIVE_PARTS[1]:
        return {
            "runtime_name": parts[0],
            "scene_name": parts[1],
            "sqldb_directory": "/".join(parts[:4]),
        }
    if len(parts) >= 3:
        return {
            "runtime_name": parts[0],
            "scene_name": parts[1],
            "sqldb_directory": f"{parts[0]}/{parts[1]}/{'/'.join(SQLDB_RELATIVE_PARTS)}",
        }
    return {"runtime_name": None, "scene_name": None, "sqldb_directory": None}


def _safe_subdirs(directory: Path) -> list[str]:
    """列出目录下的直接常规子目录名（排除隐藏目录与软链/Junction）。"""
    if not directory.is_dir():
        return []
    names: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith(".") or entry.name in {"node_modules", "__pycache__"}:
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.is_symlink():
                        continue
                    if os.name == "nt":
                        attrs = entry.stat(follow_symlinks=False).st_file_attributes
                        if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                            continue
                    names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return []
    names.sort(key=str.lower)
    return names


def list_runtimes(workspace_root: str | Path) -> list[str]:
    """扫描工作区根目录下的可用运行态目录列表。"""
    return _safe_subdirs(Path(workspace_root).resolve())


def list_scenes(workspace_root: str | Path, runtime_name: str) -> list[str]:
    """扫描指定运行态目录下的可用场景目录列表。"""
    root = Path(workspace_root).resolve()
    try:
        runtime = validate_segment(runtime_name, "运行态名称")
    except ValueError:
        return []
    target = (root / runtime).resolve()
    if not target.is_relative_to(root):
        return []
    return _safe_subdirs(target)
