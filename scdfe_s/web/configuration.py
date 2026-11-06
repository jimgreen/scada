"""Durable Web configuration changes for the in-process protocol services."""
import json
import os
from pathlib import Path
import tempfile


def write_config(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name+".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_config(service, previous, candidate, adopt, error_type, *, active):
    # Caller owns the lifecycle lock. Never tear down a good connection for
    # an unchanged configuration or a failed file write.
    if previous == candidate and not service._config_error and service.config_path.is_file():
        return service.status()
    try:
        service._persist_config(candidate)
    except (OSError, ValueError) as exc:
        message = f"保存配置失败: {exc}"
        service._record("ERROR", message, alert=True)
        raise error_type(message) from exc
    was_running = active  # a disconnected engine can still own a reconnect loop
    try:
        if was_running and previous != candidate:
            service.stop()
        adopt(candidate)
        if was_running and previous != candidate:
            service.start()
    except Exception as failure:
        errors = []
        adopt(previous)
        try:
            service._persist_config(previous)
        except (OSError, ValueError) as exc:
            errors.append(f"原配置写回磁盘失败: {exc}")
        if was_running and not service.running:
            try:
                service.start()
            except Exception as exc:
                errors.append(f"原服务恢复失败: {exc}")
        recovery = "；".join(errors) if errors else "已恢复原配置"
        message = f"新配置未生效: {failure}；{recovery}"
        service._record("ERROR", message, alert=True)
        raise error_type(message) from failure
    if service._config_error:
        service._last_error = None
    service._config_error = None
    service._record("INFO", "服务配置已更新")
    return service.status()
