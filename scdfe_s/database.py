"""S Web 与独立协议运行器独占的模拟器 SQLite 存储。"""
from pathlib import Path

from core.workspace_paths import LEGACY_REPO_DB_DIR, default_database_path
from scdfe_c.web.service import DEFAULT_DB as SYSTEM_DB, SCHEMA_DIR, ScadaStore

DEFAULT_DB = default_database_path("s")
_LEGACY_SYSTEM_DB = LEGACY_REPO_DB_DIR / "scada.sqlite3"
_FORBIDDEN_BUSINESS_FILENAMES = frozenset({"scdfe_c.sqlite3"})


class SimulatorStore(ScadaStore):
    application_id = int.from_bytes(b"SDFS", "big")

    def __init__(self, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR, **kwargs):
        path = Path(db_path).resolve()
        for protected in (SYSTEM_DB.resolve(), _LEGACY_SYSTEM_DB.resolve()):
            if path == protected or (path.exists() and protected.exists() and path.samefile(protected)):
                raise ValueError("scdfe_s 不能打开 C/T 业务库；请使用独立的 scdfe_s.sqlite3")
        if path.name.lower() in _FORBIDDEN_BUSINESS_FILENAMES:
            raise ValueError("scdfe_s 不能打开或新建 C/T 业务库文件；请使用独立的 scdfe_s.sqlite3")
        super().__init__(path, schema_dir, **kwargs)
