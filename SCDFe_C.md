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
并按相同 SQL 更新形状使用 `executemany`。写库进程独占并复用一个 SQLite 写连接，批次之间只提交
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
`fes_yc_define`、`fes_yx_define`，同时更新 `raw_*`、`value*`、`status*` 和刷新时间字段。
值和质量发生变化时立即写入；值和质量稳定时，默认每 5 秒写一次刷新时间心跳，避免稳定点
在每个采集周期重复产生完整数据库更新，同时保留数据新鲜度信息。
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

可以用下面的基准复测写库路径，固定模拟每站 100 个遥测和 100 个遥信：

```powershell
\.venv\Scripts\python.exe tools/benchmark_scdfe.py --cycles 1000 --db-cycles 20
```

输出同时给出旧的“每点一条队列消息”和当前批消息的队列条数、Python 序列化耗时，
命令轮询、定义指纹检查以及 SQLite 更新基准；实际结果会受操作系统、磁盘和 SQLite 缓存影响。
