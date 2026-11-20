# Web 文件根目录

`scdfe_c`、`scdfe_s`、`scdfe_t` 和 `scdfe_d` 使用同一个可配置的文件根目录，默认是仓库同级的 `workspace`，
本机为 `D:\codex\workspace`。页面中的数据库、文件和日志目录均相对于该根目录。

```powershell
python -m scdfe_c.web.server --port 8977 --workspace-root D:\codex\workspace
python -m scdfe_s.web.server --port 8978 --workspace-root D:\codex\workspace
python -m scdfe_t.web.server --port 8979 --workspace-root D:\codex\workspace
python -m scdfe_d.web.server --port 8980 --workspace-root D:\codex\workspace
```

`realtime/qinling` 为实时态，`simulation/qinling` 为独立复制的仿真态。
可以在“选择数据库”中浏览、校验、新建并选择根目录内的数据库。
各服务按监听地址和端口保存上次浏览的目录和已确认的数据库。重新打开选择器时恢复浏览目录，
重启服务时使用已确认的数据库。首次启动或需要临时指定数据库时，可加 `--db` 并传入根目录相对路径。
根目录没有上一级；绝对路径、`..` 和指向根目录外的目录链接均被后端拒绝。
服务启动前同样检查数据库和日志目录的实际位置。

`/api/database` 的 `database`、`relative_database`、`browse_directory` 和目录树条目的 `path` 均返回根目录相对路径。
切库请求中的 `expected_database` 使用 `/api/database` 返回的相对路径，以防其他页面切库后继续写入旧库。
旧版保存的 Web 服务目录相对路径在启动时按原基准解析，再检查是否处于新根目录内。
超出根目录的旧选择不会自动回退到其他数据库，需要通过 `--db` 指定正确路径。

验证用例：`tests.test_web_database_selection`、`tests.test_web_file_root`、`tests.test_web_workspace_root`。
