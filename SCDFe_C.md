# scdfe_c 通道守护采集程序

`scdfe_c.py` 是当前 SQLite/协议主站 API 的运行时调度层。它读取
`fes_rtu_define`，把一个 RTU 的一个通道作为一个独立通信线程，启动对应的
IEC104、Modbus TCP、Modbus RTU、CAN、BACnet/IP 或 OPC UA 主站。数据库批量写入
仍由独立写库进程执行。

## 启动

Windows：

```powershell
Set-Location D:\codex\scada
\.venv\Scripts\python.exe -m scdfe_c `
  --db D:\codex\scada\db\scada.sqlite3 `
  --log-dir D:\codex\scada\db\logs
```

Linux/macOS：

```sh
cd /opt/scada
.venv/bin/python -m scdfe_c \
  --db /var/lib/scada/scada.sqlite3 \
  --log-dir /var/log/scada
```

常用参数：

```text
--poll-interval 1       四遥采集周期，单位秒
--scan-interval 2       RTU/通道和点表定义扫描周期，单位秒
--retry-interval 5      连接失败后的重试周期，单位秒
--retention-hours 720   每个进程日志文件保留小时数
--max-scans N           测试或一次性检查时扫描 N 次后退出
```

正式运行时不应使用 `--max-scans`。主进程收到 Ctrl+C 或 SIGTERM 后会先通知
所有通信线程停止、等待退出，再结束批量写库进程。

## RTU 表进程控制字段

`fes_rtu_define` 增加了以下运行控制和状态字段。Web 页面或其他管理程序直接
修改这些字段即可控制对应的 RTU-通道通信线程；`scdfe_c` 每个扫描周期都会重新读取。

| 字段 | 用法 |
| --- | --- |
| `process_enable` | `1` 启用并保持通信线程，`0` 停止并保持停用 |
| `process_restart` | 写 `1` 请求一次重启；守护器消费后自动写回 `0` |
| `process_status` | `0` 停止、`1` 启动中、`2` 运行、`3` 停止中、`4` 重启中、`5` 错误 |
| `process_pid` | 该通信线程的 native ID；停止后为 `0` |
| `process_start_time` | 最近一次启动时间，Unix 毫秒时间戳 |
| `process_stop_time` | 最近一次停止时间，Unix 毫秒时间戳 |
| `process_restart_time` | 最近一次重启请求/执行时间，Unix 毫秒时间戳 |
| `process_heartbeat_time` | 通信线程最近一次成功循环时间，Unix 毫秒时间戳 |
| `process_exit_code` | 最近一次通信线程退出结果，正常停止为 `0`，异常停止为 `-1` |
| `process_restart_count` | 守护器执行的重启次数 |
| `process_error` | 最近一次启动、连接、采集或下发错误文本 |

例如，停止 RTU 3 的通信线程：

```powershell
$base = 'http://127.0.0.1:8877'
Invoke-RestMethod "$base/api/rtus/3" -Method Patch `
  -ContentType 'application/json' `
  -Body '{"process_enable":0}'
```

重新启用并请求重启：

```powershell
Invoke-RestMethod "$base/api/rtus/3" -Method Patch `
  -ContentType 'application/json' `
  -Body '{"process_enable":1,"process_restart":1}'
```

`process_restart` 是一次性命令位，不能用作持续启停开关；持续运行由
`process_enable` 控制。主调度线程写入启动/停止/重启过渡状态，通信线程写入线程 ID、
运行状态、心跳和连接错误。所有状态写入都使用短事务和 `_version` 递增，Web 的乐观
并发检查仍然有效。

## 运行行为

父进程每个扫描周期：

1. 遍历 `fes_rtu_define`，过滤出有 `channel_id`、`channel_type` 且规约已实现的通道；
2. 按 `process_enable` 决定是否保持该通道通信线程；
3. 消费 `process_restart=1`，为启用的通道停止旧进程并启动新进程；
4. 为每个启用的 `(rtu_id, channel_id)` 启动一个命名为 `scdfe-c-rtu-<rtu>-ch-<channel>` 的守护线程；
5. 通信线程异常退出时自动拉起，通道配置或四遥静态定义指纹变化时停止旧线程并重新加载；
6. RTU 被删除或通道字段被删除时停止对应通信线程；
7. 按配置的保留小时数定期清理各进程的过期小时日志。

主进程启用后还会启动一个独立的 `scdfe_c-db-writer` 批量写库进程。通信线程不直接
写 SQLite，而是把遥测、遥信、遥控/遥调状态和 RTU/线程状态更新投递到有界队列；同一采集周期的
遥测、遥信更新合并为一条批消息，写库进程每 200 ms 或累计 500 个点更新统一提交一次事务，
并按相同 SQL 更新形状使用 `executemany`。批消息按表和字段分组，公共字段名仅传一次，各点为参数元组；
写库进程在开启事务前完成校验、合并和参数准备，锁重试复用准备结果。旧的 `batch/updates` 队列格式已删除，
升级时整体重启 `scdfe_c`，确保通信线程和写库进程使用同一版本。写库进程独占并复用一个 SQLite 写连接，批次之间只提交
和重新开启事务。通信线程按 `worker_start_delay_ms`（默认 250 ms）
顺序拉起，避免多个协议栈同时初始化。主进程停止、重载或修改日志目录/运行参数时，
会先通知通信线程退出、等待队列完成，再停止写库进程。修改 `poll_interval_ms`、`retry_interval_ms`
或日志目录会平滑重载现有通信线程；`scan_interval_ms`、日志保留小时数和启动间隔在下一轮
调度中生效。批量事务连续失败时写库进程主动退出，由主进程重建写库进程并重新拉起通信线程，
避免静默丢失状态更新。

主进程控制记录位于 `fes_scdfe_c_define` 的 `id=1` 行。`process_enable` 控制主进程
是否工作，`process_restart` 是一次性全量重启请求；Web 写入这些字段后，主进程轮询消费。
该行还保存主进程 PID、状态、启动/停止/重启时间、心跳、退出码、重启次数、错误信息和运行参数。

程序支持 Windows 和 POSIX 系统：Windows 使用原生进程查询句柄检查 `scdfe_c` 是否存活，
不会用 `os.kill(pid, 0)` 误触发进程终止；POSIX 使用非破坏性的 `os.kill(pid, 0)` 探测。
文件锁、守护线程、SQLite WAL 和写库进程的启动/停止流程均按当前平台选择实现。

通信线程连接成功后持续调用主站的 `collect_once()`，把 `yc`、`yx` 返回值和质量/状态写回
`fes_yc_define`、`fes_yx_define`。遥测保留 `raw_value/raw_status/raw_time` 和
`value/status/refresh_time`：前者为通信采样原始数据，后者为工程运行数据，
工程值按 `raw_base + raw_scale * raw_value` 计算。比例配置每次连接/定义重载读取一次，
空配置默认基值 0、比例 1，显式比例 0 仍按 0 处理。非有限值或单点换算失败只记录该点错误。
遥信保留 `value/status/refresh_time`，保持开关量语义。
两表已删除 `value_dev/status_dev/refresh_time_dev` 及 `_1/_2/_3` 三组字段，
schema、数据类、Web 和物理 SQLite 同步删除，不提供旧字段兼容访问。
值和质量发生变化时立即写入；值和质量稳定时，默认每 5 秒写一次刷新时间心跳，避免稳定点
在每个采集周期重复产生完整数据库更新，同时保留数据新鲜度信息。
心跳只写 `refresh_time`，`raw_time` 保留最近一次写入原始值/质量的时间。
它读取 `fes_yk_define`、`fes_yt_define` 的命令时间和值；首次启动只下发满足
`cmd_time > cmd_handle_time` 的明确未处理命令，运行期间仅下发命令时间或值发生变化的命令。
通信线程复用自己的只读 SQLite 连接，通过命令点表 `_version` 汇总值判断是否有变化；
版本未变化时不再读取全部遥控、遥调行。成功后写入 `cmd_handle_time` 和状态，失败写入错误状态并记录错误日志。

实时值、质量、时间、命令处理时间和 `_version` 不参与定义指纹，因此正常采集不会导致
通道重启。点表增删、点号、协议地址、缩放、IEC104 地址、Modbus 解析定义、BACnet/OPC
通道参数等变化会改变指纹并自动热重载。

通信线程的主循环具有独立异常边界：连接、采集、点表解析、命令下发和状态写回异常只会
使当前线程进入错误状态并按重试周期重连；线程入口还会捕获未预期异常并记录独立日志，
主调度线程在下一次扫描发现线程退出后才重启该通道，不会终止其他通道线程。线程停止
超时不会强制杀死整个主进程，也不会立即启动同一通道的重复线程；该通道保持错误状态，
待旧线程真正退出后再由后续扫描恢复。

## 日志

默认目录为 `db/logs`。主进程、每个通信线程和数据库写进程各使用一个独立的按小时日志文件，格式为：

```text
scdfe_c-main-YYYYMMDD-HH.log
scdfe_c-rtu-2-ch-1-YYYYMMDD-HH.log
scdfe_c-db-writer-YYYYMMDD-HH.log
```

正常、告警、错误和过程信息统一写入所属进程的当前小时文件，级别保留在每行内容中，不再按级别拆分文件。每行含本地时间、级别、进程号、RTU、通道和消息。正常采集日志按 30 秒输出周期摘要，连接、告警、错误、定义变化和进程生命周期事件即时记录；日志采用每次追加后关闭文件的方式，不在多个进程间共享 handler。父进程按 `log_retention_hours` 清除旧文件，默认保留 720 小时；单个文件清理失败不会中断采集。

Web 页面选择“scdfe_c 守护进程”可查看主进程和各 RTU 通道状态，执行启动、停止和全量重启，
并按进程/小时列出和查看日志。对应接口为 `GET /api/scdfe_c/status`、
`PATCH /api/scdfe_c/control`、`GET /api/scdfe_c/logs` 和 `GET /api/scdfe_c/logs/{filename}`。

## 规约边界

- IEC104、Modbus TCP、Modbus RTU、BACnet/IP 和 OPC UA 通过各自主站 API 读写四遥点。
- BACnet/OPC 现有四遥表没有对象类型/实例或 NodeId 字段，守护程序为连续点号生成默认映射：
  BACnet 使用 `analog-value/binary-input/binary-output/analog-output`，OPC 使用
  `ns=2;s=YC_001`、`ns=2;s=YX_001` 等；现场系统应通过点映射扩展替换这些默认值。
- CAN 主站 API 当前是原始 CAN/CAN-FD 帧接口，没有统一的应用层点号、帧 ID、字节序和缩放定义。
  因此守护程序仍会启动 CAN 通道并接收原始帧，但没有应用层 `send_yk_point`/`send_yt_point`
  时会记录错误日志，不会猜测设备协议。接入 CAN 设备前应实现明确的帧解码和命令映射主站。

## 测试

已有数据库应先停止 Web 和 `scdfe_c`，执行一次字段迁移，再启动新版本：

```powershell
.\.venv\Scripts\python.exe tools/remove_device_values.py --db D:\codex\scada\db\scada.sqlite3
```

工具在 `db/backups` 创建 SQLite 完整备份，在事务中删除列并更新 schema 指纹；
校验剩余字段摘要、行数、外键及完整性，任何失败回滚。重复执行不修改数据、不生成备份。
额外指定 `--refresh-test-values` 才重置明确标识的协议样例值；保留点定义、命令时间，
跳过已下发或待处理的遥调命令。常规 seed 命令仍只补齐缺失记录，不覆盖用户编辑。

```powershell
Set-Location D:\codex\scada
\.venv\Scripts\python.exe -m unittest tests.test_scdfe_c -v
\.venv\Scripts\python.exe -m unittest discover -s tests
ruff check api web tests scdfe_c.py
\.venv\Scripts\python.exe -m compileall -q api web tests scdfe_c.py
```

`tests/test_scdfe_c.py` 使用假的主站验证定义发现、指纹隔离、批量实时写回、
变化命令、按进程小时日志清理和父进程的启动/停止；不会连接现场设备。当前 Windows
环境已执行协议回环和全套回归；Linux 分支通过平台模拟、静态检查和编译检查，尚未
在本机 Linux 内核中执行。

可以用下面的基准复测写库路径，固定模拟每站 1000 个遥测和 1000 个遥信：

```powershell
\.venv\Scripts\python.exe tools/benchmark_scdfe.py --cycles 1000 --db-cycles 20
```

输出同时给出旧的“每点一条队列消息”和当前批消息的队列条数、Python 序列化耗时，
命令轮询、定义指纹检查以及 SQLite 更新基准；实际结果会受操作系统、磁盘和 SQLite 缓存影响。


多 RTU 扩容基准：

```powershell
.\.venv\Scripts\python.exe tools/benchmark_rtu_scale.py --replicas 1 10 --cycles 1000 --db-cycles 20 --command-cycles 1000 --definition-cycles 10 --scan-cycles 1000 --output reports/rtu_scale_20261101.json
```

该工具独立生成 6/60 个 RTU，每站 YC1000/YX1000/YK200/YT200，分别记录串行阶段计时；随后用 6/60 个通信线程和真实独立写库进程测试变化值完整写回。排空并提交后才停止计时，逐点验证最终值和版本号。不连接现场设备，不改动正式数据库。默认定义扫描每站测 10 次，其余循环数见命令。

写库路径的聚焦对比可用下面的命令复测；工具会冻结优化前后的 `scdfe_c.py` 快照、校验父子进程哈希、串行执行并交替测试顺序。`--pipeline-only` 只计真实通信线程、跨进程队列、SQLite 提交和写库进程排空，`--repeat 3` 取三次中位数：

```powershell
.\.venv\Scripts\python.exe tools/benchmark_rtu_scale.py --pipeline-only --baseline-ref 19bcdc6 --replicas 1 10 --db-cycles 20 --repeat 3 --output reports/writer_packed_20261101.json
```

2026-11-01 第 2 轮优化采用分组参数消息和事务前准备，对照上一版 `19bcdc6` 当场复测：
6 站完整写回中位数从 1.936 秒降至 1.178 秒，60 站从 22.913 秒降至 14.779 秒，
吞吐分别提高 64.4% 和 55.0%。每条 2000 点消息从 171703 字节降至 96790 字节。
这是一组新的同条件对照，不与上一轮其他时段的绝对吞吐直接计算加速比。
逐点校验和完整计时口径见 [第 2 轮效率报告](reports/writer_packed_20261101.md)；
[上一轮报告](reports/writer_optimization_20261101.md)保留作历史记录。
本机 SQLite/IPC 压测未包含设备通信时延，Linux 尚未实机复测。

第 3 轮继续优化通信线程的测量写回：`_sync_measurements` 直接生成 YC/YX 分组参数，避免逐点更新字典的创建、复制和再次转换；变化值、心跳和重复点的最后值语义保持不变，队列入队失败时缓存不会提前更新。对照上一版 `260a7f3`，6 个 RTU 完整写回中位数从 1.387 秒降至 1.021 秒，60 个 RTU 从 17.057 秒降至 11.088 秒，吞吐分别提高 35.9% 和 53.8%。详细逐点核验和三次原始结果见 [第 3 轮效率报告](reports/measurement_direct_20261101.md)；[原始数据](reports/measurement_direct_20261101.json)。

第 4 轮优化独立写库进程的参数准备：单条消息、规范主键顺序且无重复键时，直接按 SQL 分组使用参数元组，省去逐点合并字典和二次分组；重复键、部分字段覆盖与多消息批次继续使用通用合并路径，两条路径共享元数据校验。对照 `71444c2`，最终代码每种规模、每个版本共测七次：6 RTU 中位数 0.923 → 0.897 秒，60 RTU 8.859 → 8.368 秒，吞吐分别提高 2.90% 和 5.87%。全部 28 次运行、3696 万次点更新均通过最终值、状态、版本与完整性核验。结果存在波动，包含优化后更慢的单次运行；不表示最坏延迟保证。完整原始结果、测试与复现命令见 [第 4 轮效率报告](reports/writer_fastpath_20261101.md)。本轮未改动 SQLite 持久性设置，未连接现场设备，Linux 尚未实机复测。

本轮负载使用逐点、逐站独立随机流，固定种子使对照可复现。YC 原始值和工程值分别保持多样性，工程值按测试配置 `2 + 1.1 * raw_value` 计算；YC/YX 质量码随机取 0–3，且每点相邻周期质量不同。质量码只有 4 种，允许不同测点使用相同质量码，不能解释为所有点质量码唯一。实时协议数据不注入随机值。

删除设备字段后的性能对照使用同一 schema、相同换算和输入，只比较遥测质量/时间参数的独立绑定与复用绑定：

```powershell
.\.venv\Scripts\python.exe tools/benchmark_rtu_scale.py --pipeline-only --compare-bindings --replicas 1 10 --db-cycles 20 --repeat 3 --seed 20261101 --output reports/raw_engineering_binding_20261101.json
```

`--compare-bindings` 从当前源码生成仅修改 YC 参数布局的基线，冻结两份源码并记录 SHA-256；父进程和 spawn 写库进程使用相同快照。它与 `--baseline-ref` 互斥。旧字段结构下的历史报告不能直接用于计算本轮收益。最新结果及测量边界见 [原始值/工程值效率报告](reports/raw_engineering_binding_20261101.md)。

本轮继续优化独立写库进程的参数准备：按表和规范主键字段组缓存已见键集合，逐点不再构造重复的表名/字段名 token，也不复制已经是 tuple 的行参数；停止消息的确认放入 `finally`，错误日志无法写入时也不会使队列排空卡住。基线为 `3aca070`，相同随机 1000 YC + 1000 YX 批消息的 `_prepare_batch_updates` 五轮中位数从 310.68 ms 降至 208.95 ms，参数准备耗时减少 32.74%，吞吐提高 48.69%。该微基准不包含 IPC/SQLite，故不把它等同于端到端收益。

同条件 6/60 RTU 链路复测包含较大 Windows 调度波动：6 RTU 三次为基线 1.965/5.335/3.718 秒、优化后 2.012/5.085/5.802 秒；60 RTU 三次为基线 43.803/50.958/11.530 秒、优化后 48.592/55.907/10.640 秒。中位数没有稳定提速，本轮不宣称端到端吞吐改善；每次仍通过 240000/2400000 点更新的原始值、工程值、质量、版本和 SQLite 完整性校验。完整数据见 [本轮效率报告](reports/writer_prepare_20261101.md)。
