"""S Web and standalone protocol runners own one SQLite database."""
from pathlib import Path

from scdfe_c.web.service import DEFAULT_DB as SYSTEM_DB, SCHEMA_DIR, ScadaStore

DEFAULT_DB = SYSTEM_DB.with_name("scdfe_s.sqlite3")


class SimulatorStore(ScadaStore):
    application_id = int.from_bytes(b"SDFS", "big")

    def __init__(self, db_path=DEFAULT_DB, schema_dir=SCHEMA_DIR, **kwargs):
        path = Path(db_path).resolve()
        if path == SYSTEM_DB.resolve() or (path.exists() and SYSTEM_DB.exists() and path.samefile(SYSTEM_DB)):
            raise ValueError("scdfe_s 不能打开 C/T 业务库；请使用独立的 scdfe_s.sqlite3")
        super().__init__(path, schema_dir, **kwargs)
