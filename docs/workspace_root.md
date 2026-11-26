# 工作区三参数与数据库统一路径规范

`scdfe_c`、`scdfe_s`、`scdfe_t` 与 `scdfe_d` 统一采用三参数定位数据库目录：

```text
<workspace_root>\<runtime_name>\<scene_name>\sqldb\scada\
├── scdfe_c.db   # scdfe_c 与 scdfe_t 共享业务库
├── scdfe_s.db   # scdfe_s 独立模拟器库
└── scdfe_d.db   # scdfe_d 设备历史采集库
```

## 参数说明（均由用户指定）

| 参数 | CLI 选项 | 环境变量 | 默认值 |
| :--- | :--- | :--- | :--- |
| 根目录 | `--workspace-root` | `SCDFE_WORKSPACE_ROOT` | `D:\codex\workspace` |
| 运行态名称 | `--runtime-name` / `--runtime` | `SCDFE_RUNTIME_NAME` | `realtime` |
| 场景名称 | `--scene-name` / `--scene` | `SCDFE_SCENE_NAME` | `qinling` |

示例启动命令：

```powershell
python -m scdfe_c.web.server --port 8977 --workspace-root D:\codex\workspace --runtime-name realtime --scene-name qinling
python -m scdfe_s.web.server --port 8978 --workspace-root D:\codex\workspace --runtime-name realtime --scene-name qinling
python -m scdfe_t.web.server --port 8979 --workspace-root D:\codex\workspace --runtime-name realtime --scene-name qinling
python -m scdfe_d.web.server --port 8980 --workspace-root D:\codex\workspace --runtime-name realtime --scene-name qinling
```

## 关键行为约定

1. **偏好文件隔离**：Web 选库偏好保存在 `<workspace_root>\.scdfe\web_database_<role>_<hash>.json`，不再写入代码仓库 `db/` 目录。
2. **显式三参数优先**：启动时显式传入 `--runtime-name` 或 `--scene-name` 时，优先定位到该运行态与场景下的 `sqldb/scada` 默认库；亦可通过 `--db` 指定根目录内任意相对路径。
3. **选择器下拉联动**：Web“选择数据库”弹窗提供“运行态 / 场景”下拉框，自动联动定位 `<runtime>/<scene>/sqldb/scada/<role_db>.db`，同时保留目录树浏览、校验和新建能力。
4. **安全边界**：根目录没有上一级；绝对路径、`..` 和指向根目录外的目录链接均被后端拒绝。
5. **一键安全迁移**：使用 `python -m tools.migrate_db_workspace --workspace-root D:\codex\workspace --runtime-name realtime --scene-name qinling --apply --overwrite --clean-legacy` 将仓库 `db/` 中的历史数据库通过 SQLite 在线备份与 `VACUUM` 迁移至目标目录并完成全量校验。

验证用例：`tests.test_workspace_paths`、`tests.test_web_database_selection`、`tests.test_web_file_root`、`tests.test_web_workspace_root`。
