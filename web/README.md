# SCADA Web 管理接口

Python 标准库实现的本地管理服务，无需额外安装 Python 依赖。要求 Python 3.10 或更新版本、支持原生 dialog 和 BigInt 的现代浏览器。

## 启动

在任意目录运行：

```powershell
python -B D:\codex\scada\web\server.py --host 127.0.0.1 --port 8877
```

Windows 可运行 `D:\codex\scada\web\start.bat`；Linux/macOS 可运行
`sh web/start.sh`。也可以在项目目录直接执行上面的 Python 命令，两个平台相同。

浏览器打开 http://127.0.0.1:8877/，接口文档为 http://127.0.0.1:8877/api-docs。
默认仅监听本机。此版本没有登录和权限系统；若需要对外提供服务，应通过带身份认证和 HTTPS 的反向代理部署。

可选参数：

```powershell
python -B D:\codex\scada\web\server.py --port 8878 --db D:\test\scada.sqlite3 --schema-dir D:\codex\scada\dbdef\json
```

## 文件与数据

- `server.py`：HTTP 路由、输入检查、同源限制和静态文件服务。
- `service.py`：JSON 表结构加载、参数化 SQL、SQLite 事务和数据校验。
- `static/`：中文管理页面、字段编辑、API 说明。
- `test_server.py`、`test_protocol_seed_data.py`：HTTP 和协议测试数据回归测试。
- 表结构：`D:\codex\scada\dbdef\json\*.json`，共 19 表、335 个定义字段。
- 业务数据：`D:\codex\scada\db\scada.sqlite3`，首次启动建立空表；不会自动填充示例数据。
- SQLite 的 WAL/SHM 伴随文件也位于数据库目录。不要在运行期间只复制主文件作为备份；应使用 SQLite backup API 或在停服后备份。

19 张表（含运行进程监控表）使用与 JSON 一致的表名、列名和联合主键。RTU 与通道已经合并到 `fes_rtu_define`；`fes_scdfe_c_define` 的 `id=1` 行记录和控制 scdfe_c 主进程。协议参数表统一使用 `fes_channel_*_define` 命名。额外 `_version` 列用于本服务的乐观并发检查，`_scada_schema` 保存结构指纹。数据库必须使用当前 JSON schema；表结构不一致时直接报错，不做旧表、旧字段或旧名称迁移。

`fes_rtu_define` 还包含 `process_enable`、`process_status`、`process_pid`、`process_start_time`、`process_stop_time`、`process_heartbeat_time`、`process_exit_code` 和 `process_error`。其中 `process_enable` 是 RTU 自动通信标志：只有 `1` 才由 `scdfe_c.py` 自动启动并守护，`0` 时不参与自动通信、不受主程序守护，允许人工 `--rtu-id` 调试。状态码为 0 停止、1 启动中、2 运行、3 停止中、4 处理中、5 错误；时间字段为 Unix 毫秒时间戳。`process_pid` 保存对应通信线程的 native ID，停止后为 `0`。通信线程异常后仍自动恢复，但不再持久化恢复次数或恢复时间。

存储使用原生 SQLite（单文件多表），采用 WAL、每次操作独立连接、`BEGIN IMMEDIATE` 写事务、外键约束、30 秒 busy_timeout 和 mmap_size。此文件格式不是 `D:\codex\rtdb` 的每表独立 SQLite 封装格式；复用了它的 JSON 公共定义约定。旧 EMS 共享内存/二进制 RTDB 文件未直接接入。

## 页面操作

遥测、遥调常用列同时显示 `raw_value`（原始值）和 `value`（工程值）。遥测工程值由
`scdfe_c` 按 `raw_base + raw_scale * raw_value` 计算。遥测、遥信表已移除设备值、
设备状态、设备刷新时间及其 1/2/3 组字段；API 不返回或接受这些字段。

1. 左侧“数据与定义”进入统一页面，通过“RTU 与通道 / 遥测 / 遥信 / 遥调 / 遥控 / 协议参数 / 辅助表”标签切换。在“RTU 与通道”中使用“新建 RTU”录入 RTU 基本参数和初始通道；已有 RTU 可通过“新建通道”入口选择后独立维护通道字段，再配置对应协议参数及四遥点。系统保持一 RTU 一通道。
2. RTU 行的“四遥”按钮进入其遥测标签；四遥、协议参数和含 `rtu_id` 的辅助表共享 RTU 过滤条件。
3. RTU 过滤框可以输入任意已存在的编号；建议列表显示前 500 个 RTU，并不是管理数量上限。留空显示全部。
4. 常用字段视图便于日常操作，“四遥数据”显示值、状态和相关时间字段，“全部字段”显示完整结构。编辑窗口始终提供全部字段。
5. “协议参数”标签内选择 IEC104、Modbus-TCP、Modbus-RTU、CAN、BACnet、OPC；“辅助表”标签内选择 MODBUS 通道/召唤/数据/遥调遥控映射、五防定义和下发信息表。两组分别记住当前选中的表，均在同一页面内查询与维护。RTU 行提供“RTU 参数”“通道维护”“通道参数”独立入口；IEC104、BACnet、OPC 参数表的内部 `id` 由服务端新建时自动分配。
6. 记录支持新增、查询、编辑、删除；搜索匹配文本字段及主键。默认每页 50 条，可选择 25、50、100、200、500 条；点击列标题在服务端排序。表结构按钮查看原始元数据。
7. 默认开启 5 秒自动刷新，详情、编辑或表结构窗口打开时暂停刷新。写入只提交实际修改的字段，保留未编辑的采集数据；同时用 `_version` 防止覆盖其他 Web 请求的更新。运行参数编辑中不会被自动刷新覆盖。
8. 双击记录或点击“详情”查看全部字段和完整长文本；编辑窗口支持按中文或英文字段名搜索。RTU 行可直接跳转四遥或该 RTU 的日志。
9. 左侧导航在各页面始终保留。表格表头、首列与操作列固定；宽表在自身容器中滚动，日志正文默认自动换行，窄屏自动改为上下布局。操作页面只显示操作、字段和实际状态；功能说明、教学提示及重复副标题已删除，进程执行信息仅在有操作或忙碌时显示。

**四遥数据编辑仅更新数据库记录，不发送现场遥控/遥调命令。** 页面“Web 已连接”仅表示 Web API 可访问，状态和时间字段显示数据库原值，不代表设备实时在线。

“进程监控”（`/#processes`）与通信监控并列，显示主进程、数据库写进程和已登记的 RTU 调试进程的真实 PID、父 PID、状态、心跳、启停时间、退出码、错误和日志入口。可对单个进程启动或停止；调试进程按 RTU 登记，启动前必须停用该 RTU 自动通信。写库独立停止会先暂停通信线程并排空队列；恢复写库后主进程按配置间隔恢复通信。一键启动顺序为主进程、写库就绪、自动通信及勾选的调试任务；一键停止顺序为调试任务、自动通信、排空写库队列、写库、主进程。每项操作显示实际阶段，超时会停在失败步骤并保留错误信息。停止全部会停止所有已登记调试任务（包括未勾选一键启动的任务）；Web 服务保持运行，以便再次启动和查看日志。



“通信监控”（`/#scdfe`）显示主进程 PID、状态、启动/停止时间、心跳、退出码和错误；RTU 线程支持搜索、状态筛选和分页，超过 500 个 RTU 也可完整浏览。顶部线程统计始终覆盖全库。可从每行查看完整错误、打开日志或启停自动通信；单 RTU 控制使用版本检查。

“运行日志”（`/#logs`）使用文件列表和正文双栏，支持“当前文件/全部匹配文件”范围、关键词、起止时间、进程、RTU、文件名和日志类型组合筛选。日志类型对应正文级别：正常（INFO）、告警（WARNING）、错误（ERROR）、过程（PROCESS）。起止时间使用浏览器本地时间并包含起止秒；“更早日志”按游标分批查找更早小时文件。结果可高亮关键词、分页查看、换行和导出；后端单次最多扫描 4 MB，超长日志行和轮转删除文件会安全跳过或要求刷新。主进程、通信线程、调试进程和数据库写进程仍各自每小时一个日志文件。

日志运行策略：主进程、数据库写进程和正常采集均按 30 秒输出周期摘要，异常和生命周期事件即时记录。主进程记录通信线程及写库进程状态，写库进程记录累计成功批次、输入消息/更新数和本地待写数量（不含跨进程队列）；无 RTU 或写库空闲时，运行中的主进程和写库进程仍各自生成每小时日志。主进程每 60 秒清理超过 `log_retention_hours` 的文件，默认保留 720 小时；写库独立暂停时主进程仍继续记录和清理。日志正文时间为本地 `HH:MM:SS`，文件名提供日期和小时，支持 Windows/Linux。

数据库写入策略：`scdfe_c` 启用后启动独立的 `scdfe_c-db-writer` 写库进程。通信线程通过有界
队列发送遥测、遥信、遥控/遥调状态和 RTU/线程状态更新，写库进程在队列连续空闲 100 ms 或累计 500 个
点更新时统一提交待写入数据。写库连接将 WAL 自动检查点设置为 10000 页，减少高频提交时的检查点开销；WAL 文件会在更大阈值后检查点，部署时应纳入磁盘空间监控。主进程停止或重载前会先通知通信线程退出并等待队列完成，避免通信线程直接争抢 SQLite 写锁。
生产消息在事务内保持采集消息顺序，扩大批量阈值时不会把多个采集周期合并成一次版本更新。

批量写入快路径按表和规范主键字段组复用已见键集合，减少每个点的临时对象；参数和 SQL 结果保持不变。
若写库异常时日志也无法写入，停止消息仍会被确认，避免队列排空等待卡住。该优化的 CPU 参数准备收益及
6/60 RTU 链路对照记录在 `reports/writer_prepare_20261101.md`；链路测量受 Windows 调度和系统负载影响，
报告不把不稳定的端到端差异宣称为吞吐保证。

## API

请求和响应采用 UTF-8 JSON；写请求要求 `Content-Type: application/json`，最大 2 MB。成功返回对象；错误返回 `{"error":{"code":"...","message":"...","details":...}}`。

| 资源 | 集合路径（GET、POST） | 单条路径（GET、PATCH、DELETE） |
| --- | --- | --- |
| RTU | `/api/rtus` | `/api/rtus/{rtu_id}` |
| 全部 RTU 的某类四遥点 | `/api/points/{kind}` | `/api/points/{kind}/{rtu_id},{pnt_no}` |
| 指定 RTU 的某类四遥点 | `/api/rtus/{rtu_id}/points/{kind}` | `/api/rtus/{rtu_id}/points/{kind}/{pnt_no}` |
| 任意已定义表 | `/api/tables/{table}/rows` | `/api/tables/{table}/rows/{key}` |

`kind` 为 `yc`（遥测）、`yx`（遥信）、`yt`（遥调）、`yk`（遥控）。泛型 `{key}` 按 JSON 中主键字段的顺序以逗号拼接，如下发信息表的 `cmd_time,is_yk,rtu_id,pnt_no`。作用域路径只允许单个点号，不能用复合键切换至另一个 RTU。

其他只读接口：

- `GET /api/health`：服务状态、数据文件路径。
- `GET /api/schemas`：全部结构定义。
- `GET /api/tables/{table}/schema`：单表结构。
- `GET /api/overview[?rtu_id=7]`：全部或指定 RTU 的数量概览。
- `GET /api/scdfe_c/status`：scdfe_c 主进程控制记录及 RTU-通道通信线程状态；支持 `limit`（1–500，默认 50）、`offset`、`q`、`process_status`、`process_enable`。响应含 `total/limit/offset` 与全库 `counts`（total/running/errors/automatic）；主进程使用 `item.process_pid`，每个 RTU 通信线程使用 `rtus[].thread_id`。
- `GET /api/scdfe_c/logs[?process=...&rtu_id=...]`：按进程、RTU 筛选按小时轮转的日志文件元数据。通信日志名为 `scdfe_c-rtu-7-YYYYMMDD-HH.log`，不含通道号；通道号保留在每条日志正文中。
- `GET /api/scdfe_c/logs/{filename}?tail=200`：安全读取指定日志尾部。日志正文时间为本地 `HH:MM:SS`；日期和小时来自文件名，不显示年月日、毫秒或时区。
- `GET /api/scdfe_c/logs/search`：按 `q`、`level`、`start`、`end`、`process`、`rtu_id`、`filename` 和 `file_query` 组合检索日志；每批返回最多 5000 条，使用 `next_cursor` 继续向前检索。

独立调试入口为 `python -m scdfe_c --rtu-id 7`，只接受 RTU 编号，通道信息从数据库自动读取。
先将目标 RTU 的 `process_enable` 设为 `0` 并等待通信线程停止，再启动调试；默认只采集和写回遥测/遥信，
加 `--send-commands` 才处理遥控/遥调。独立调试的小时日志以 `scdfe_c-debug-rtu-7` 为前缀，
可通过现有日志页面、`process` 或 `rtu_id` 过滤读取。同一服务器只允许一个自动守护主程序，
不同 RTU 的人工调试可以并行；主程序与调试进程共用每 RTU 一把服务器级锁，避免重复连接和状态覆盖。
互斥不受 `--db`、工作目录或登录用户影响。Windows 使用 Global 命名内核对象，Linux 使用抽象 UNIX
套接字；多个 Linux 容器需共享宿主机网络命名空间。升级时先停止全部旧版实例。
Web 启动请求遇到另一自动守护实例占用服务器时返回 HTTP 409（`process_already_running`），不修改本库控制配置。
完整命令、启停和日志配置见 [SCDFe_C.md](../SCDFe_C.md#指定-rtu-的独立调试进程)。

进程控制：`GET /api/processes` 返回进程列表、`busy` 和当前操作 `operation`；`POST /api/processes/control` 接受 `target`（`main`、`writer`、`debug:7`、`all`）与 `action`（`start`、`stop`）。调试任务可用 `action=configure`、`include_in_all=true/false` 设置是否参与一键启动。控制操作返回 HTTP 202，随后查询 `operation.status`（`running/completed/failed`）、`stage`、`steps` 和 `error`。并发控制返回 409；运行控制表只能由专用接口修改。

主进程控制：`PATCH /api/scdfe_c/control` 只接受 `process_enable`、`log_dir`、`log_retention_hours`、`scan_interval_ms`、`poll_interval_ms`、`retry_interval_ms` 和 `worker_start_delay_ms`；PID、状态、心跳、退出码等运行时字段只由 `scdfe_c` 写回。`log_retention_hours` 的范围为 1–87600，默认 720 小时。启停 `process_enable` 需单独提交，返回 HTTP 202 并进入相同的进程控制队列；配置和通信重载请求返回 HTTP 200。

修改 `poll_interval_ms`、`retry_interval_ms` 或 `log_dir` 后，守护进程会在下一次扫描中平滑重载现有通信线程；`worker_start_delay_ms` 用于后续顺序启动或热重载，`scan_interval_ms` 和日志保留小时数由主进程动态采用。批量写库事务连续失败时，写库进程会退出并由守护进程自动重建，同时重建受影响的通信线程。每个通信线程都有独立异常边界，单个通道的连接、采集或命令错误不会终止其他通信线程。

服务支持 Windows 和 POSIX 系统；Web 查询主进程存活状态时使用平台安全的非破坏性进程探测，Windows 不会通过 `os.kill(pid, 0)` 检查进程。

集合查询参数：`q`、`limit`（1–500，默认 25）、`offset`（默认 0）、`sort`（字段英文名）、`order`（asc/desc）；也可用字段名作为等值过滤条件，如 `?rtu_id=7&status=0`。返回 `items,total,limit,offset`。搜索中的 `%`、`_` 按字面值处理，不解释为 SQL 通配符。

新增 RTU 时必须同时填写 `channel_id`、`channel_type`；`status` 可选。RTU 表只保存站点、通道标识和运行控制状态，不保存地址、端口等连接字段。`channel_type` 支持 `iec104`、`modbus-rtu`、`modbus-tcp`、`can`、`bacnet`、`opc`、`custom`。通道字段也可通过 `/api/rtus/{rtu_id}/channel` 独立 PATCH；切换规约时如果旧协议参数仍存在，接口会返回 409，要求先删除旧参数。协议参数通过 `/api/rtus/{rtu_id}/channel/parameters` 管理，IEC104、BACnet、OPC 的内部 `id` 由服务端自动分配。数据库必须完整符合现行 JSON schema，缺少通道字段的数据库结构会直接被拒绝。点表新增只需要必填主键及所属 RTU，其他字段可选，未填写则保存 NULL。主键和 `rtu_id` 必须有效，RTU 编号大于 0，点号和 id 大于等于 0；整数在有符号 64 位范围内，浮点数必须有限。未知字段、重复 JSON 字段和非整数主键均被拒绝。

修改 RTU 的 `process_enable` 后，需运行 `scdfe_c.py` 守护进程才会启动或停止通信线程。`process_enable=0` 的 RTU 会被守护进程完全跳过，保留人工调试接管权；主进程控制接口会负责拉起或请求退出 `scdfe_c`，但不会在 HTTP 请求线程内建立协议连接；协议连接始终由主进程的通信线程或明确启动的 RTU 调试进程管理。

超出 JavaScript 安全整数范围的整数在响应中以十进制字符串返回；输入整数也可使用十进制字符串，避免 KEYID 精度丢失。API 时间字段保持原整数类型；界面将已知的运行时间、采集时间等毫秒时间戳格式化显示，完整记录详情同时保留原始数值。菜单名称保留来源定义，不虚构枚举值。

PATCH 提交需要改变的字段；数值传 `null` 可清空，文本传 `""` 可清空。主键不允许修改。单条结果返回 `_version` 和 ETag，可在 PATCH/DELETE 发送 `If-Match: "1"`。版本不匹配返回 409。若其他程序直接写 SQLite，需同时递增 `_version` 才能参与本服务的版本检查。

引用关系仅建立在明确的 `rtu_id` 上。数据库未定义语义的 `yc_id`、`dev_id`、点间链接等不被自动解释。删除仍有协议参数、点表或其他关联记录的 RTU 返回 409，并列出表名与数量，不会级联删除。

示例（命令实际执行将创建记录）：

```powershell
$base = 'http://127.0.0.1:8877'
Invoke-RestMethod "$base/api/rtus" -Method Post -ContentType 'application/json; charset=utf-8' -Body '{"rtu_id":7,"channel_id":70,"channel_type":"modbus-tcp","name":"Station 7"}'
Invoke-RestMethod "$base/api/rtus/7/channel/parameters" -Method Post -ContentType 'application/json; charset=utf-8' -Body '{"remote_host":"127.0.0.1","remote_port":502,"unit_id":1}'
Invoke-RestMethod "$base/api/rtus/7/points/yc" -Method Post -ContentType 'application/json; charset=utf-8' -Body '{"pnt_no":12,"name":"Voltage A","value":220.5}'
Invoke-RestMethod "$base/api/rtus/7/points/yc/12"
Invoke-RestMethod "$base/api/rtus/7/points/yc/12" -Method Patch -Headers @{'If-Match'='"1"'} -ContentType 'application/json' -Body '{"value":221.0}'
```

## 旧 monitor 对照

参考目录：`D:\OLD_D\ems\platform\source\scada\app\monitor`。

- `scdfe.cpp / InitRtuList`：RTU 下面关联通道，通过所属 RTU 匹配。
- `InitTable_yc/yx/yt/yk`：四遥类型切换；展示点号、名称、值、状态/时间等。
- `DoubleClickRtu`：选择 RTU 后切换四遥视图。
- `UpdateTable_*`、`OnPause`：数据刷新和暂停。
- 旧版 `fes_send_yk/fes_send_yt` 为设备命令发送，与本服务的数据 CRUD 分开；本次未实现发送、原始报文和 SCD 导入。

## 验证

```powershell
Set-Location D:\codex\scada
python -B -m unittest discover -s web -p "test*.py" -v
python -B -m unittest discover -s tests -p "test*.py" -v
node --check web/static/app.js
```

单元/HTTP 集成测试均在临时数据库执行，不向正式数据目录写测试记录。浏览器验收使用独立的 `output\playwright\ui-validation.sqlite3`，与 `db\scada.sqlite3` 分离。
