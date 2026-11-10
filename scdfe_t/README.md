# scdfe_t 四遥转发

`scdfe_t` 将 `scdfe_c` 采集到的遥测（YC）和遥信（YX）按点映射提供给远方系统，并接收远方遥控（YK）和遥调（YT）命令。每个转发通道使用一个独立的操作系统进程，通道之间互不共享协议连接或运行状态。当前支持两种对外协议：IEC 60870-5-104（`iec104`）和 Modbus TCP（`modbus-tcp`）。

## 目录和入口

- `scdfe_t/__main__.py`：单个转发通道运行入口；
- `scdfe_t/`：转发运行时、协议适配和进程生命周期；
- `scdfe_t/web/`：转发通道的独立 Web 服务；
- `dbdef/json/fes_scdfe_t_define.json`：转发通道定义；
- `dbdef/json/fes_scdfe_t_point_define.json`：通道内四遥映射定义。

项目使用同一个 SQLite 数据库。运行前先用 `scdfe_c` 的数据库初始化流程创建/升级表，然后启动 Web 管理服务：

```powershell
Set-Location D:\codex\scada
.\.venv\Scripts\python.exe -B -m scdfe_t.web.server \
  --host 127.0.0.1 --port 8879 \
  --db D:\codex\scada\db\scada.sqlite3
```

Linux/macOS：

```sh
cd /opt/scada
.venv/bin/python -B -m scdfe_t.web.server \
  --host 127.0.0.1 --port 8879 \
  --db /var/lib/scada/scada.sqlite3
```

浏览器打开 `http://127.0.0.1:8879/`。服务只负责转发通道的配置、进程控制、点映射和日志查看；`scdfe_c` 的 RTU/四遥维护仍由 `scdfe_c` Web 服务提供。

## 建立一个转发通道

在 Web 的“转发通道”页新建一行，至少设置：

- `name`：操作员可读的通道名称；
- `protocol`：`iec104` 或 `modbus-tcp`；
- `bind_host`、`bind_port`：对外监听地址和端口；
- `common_address`、`unit_id`：IEC104 公共地址、Modbus 单元号；
- `scan_interval_ms`：读取源点值和质量的周期；`report_interval_ms`：IEC104 遥测、遥信的周期上送间隔，按 50 毫秒时钟向上对齐。例如设置 175 毫秒时，原生协议栈按 200 毫秒调度。Modbus 由对端主动读取寄存器。
- `process_enable`：是否允许 Web 启动/守护该通道。

每个通道对应一个 `channel_id`。多个通道可以使用不同协议、监听端口和点映射；监听端口不能重复。运行时修改通道名称、点名称、扫描周期、上送周期或保留时间，不会断开现有连接。修改正在使用的协议地址、监听端口或源点映射会重建服务，应在允许中断通信时操作。

## 四遥点映射

IEC104 公共地址范围为 1～65534，Modbus 从站地址仍为 1～247。创建和编辑转发通道时不能指定 PID、运行状态或心跳，这些字段由转发进程维护。

配置检查与源点扫描分别计时。即使原扫描周期为 60 秒，缩短周期后也会在下一次配置检查（默认约 1 秒）采用新周期；现有连接保留。长扫描间隔内仍更新心跳并响应停止指令。

在通道的“点映射”页逐条增加需要转发的点：

| 字段 | 说明 |
| --- | --- |
| `kind` | `yc`、`yx`、`yk` 或 `yt` |
| `source_rtu_id` | `scdfe_c` 中源 RTU 编号 |
| `source_pnt_no` | 源四遥表的点号；通过转发页面/API 新建时留空，沿用转发点号。需要重编号时可显式填写。 |
| `pnt_no` | 本转发通道内的唯一点序号 |
| `enabled` | `1` 才参与转发或命令接收 |
| `io_address`、`type_id` | IEC104 的 IOA、信息体类型；按通道实际配置填写 |
| `register_area`、`address` | Modbus TCP 的寄存器区和地址 |
| `data_type`、`byte_order`、`word_order` | Modbus 数据编码；按远方设备要求填写 |
| `scale`、`offset` | Modbus 数值工程换算 |
| `command_mode` | 遥控/遥调的命令模式，按协议和远方设备约定填写 |

同一个通道的 `(kind, pnt_no)` 不能重复。YC/YX 只读 `scdfe_c` 的 `fes_yc_define` / `fes_yx_define`；YK/YT 下行命令写入 `fes_yk_define` / `fes_yt_define` 的 `value` 和 `cmd_time`，由 `scdfe_c` 现有通信线程继续完成实际设备下发。转发进程不直接操作 RTU 协议连接。

建议先确认源 RTU 的数据在更新，再启用转发。已启用的 IEC104 遥测/遥信源点被删除或值为空时，通道保留上次值并设置 IV 无效位；从未收到过有效值时用零值并标无效。源点恢复后按实际质量重新上送，即使数值与断开前相同也会更新。Modbus 无质量位，源点丢失时保留已有寄存器值。

## 命令行运行一个通道

Web 启动和停止是推荐方式。需要脚本或调试时，可直接运行一个通道：

```powershell
.\.venv\Scripts\python.exe -B -m scdfe_t \
  --db D:\codex\scada\db\scada.sqlite3 \
  --channel-id 1
```

`--channel-id` 必须是数据库中已定义的转发通道。进程读取通道协议、监听地址和点映射，不应在命令行重复传入协议参数。按 `Ctrl+C` 或收到 `SIGTERM` 时，进程先停止协议服务，再登记停止时间和退出状态。每个通道只能有一个运行实例；Web 启动前会检查数据库登记的 PID，异常退出的进程会标记为错误并允许重新启动。

## 扫描、点表与日志读取

同一 RTU 的连续源点使用复合主键范围查询，稀疏点只读映射中的点号；跨 RTU 或跨读取批次时，每轮扫描使用同一数据库快照。Modbus 把本轮变化的遥测、遥信交给服务线程发布，编码失败的点保留原值并在下次扫描重试；同批其他有效点仍可更新。IEC104 的总召和质量码变化继续按 IEC104 的语义处理。

四遥页面的通道选择框使用 `GET /api/trans/channels?view=selector`，只返回编号、编码、名称和规约。默认列表仍返回完整通道数据。刷新相同点映射时，界面保留原行和焦点；修改后使用新的版本号继续编辑。

日志目录和检索均可传 `channel_id`，例如 `GET /api/log-files/search?record=packet&channel_id=1&direction=RX`。
转发日志按所属通道的文件名过滤，报文文件名按所属通道过滤；通道标记由文件名和查询参数共同确定。大文件尾读只向前读取必要的块，最多 2 MB；
小文件仍显示准确的总行数。

## Web API

页面使用以下接口，便于脚本自动化：

```text
GET    /api/trans/channels
POST   /api/trans/channels
GET    /api/trans/channels/{channel_id}
PATCH  /api/trans/channels/{channel_id}
DELETE /api/trans/channels/{channel_id}
GET    /api/trans/channels/{channel_id}/points
POST   /api/trans/channels/{channel_id}/points
PATCH  /api/trans/channels/{channel_id}/points/{kind}/{pnt_no}
DELETE /api/trans/channels/{channel_id}/points/{kind}/{pnt_no}
GET    /api/trans/processes
POST   /api/trans/channels/{channel_id}/start|stop
GET    /api/trans/logs?channel_id=1&record=runtime
GET    /api/trans/log?filename=2026/11/04/08/scdfe_t-channel-1-20261104-08.log&record=runtime&tail=300
GET    /api/trans/log?filename=2026/11/04/08/scdfe_t-channel-1-20261104-08.log&record=packet&tail=300
GET    /api/health
```

请求体使用 `Content-Type: application/json`。删除通道会先停止对应进程，再在同一数据库事务中删除点映射和通道定义；页面会在删除前提示确认。运行状态字段（PID、心跳、退出码、错误等）由转发进程维护，不能通过普通通道编辑接口伪造。

## 进程监控和日志

“进程监控”展示每个转发通道的状态、PID、启动时间、停止时间、心跳、退出码和最近错误；可以独立启动/停止，某个通道异常不会停止其他通道。运行日志按小时写入数据库旁的 `logs/YYYY/MM/DD/HH/`，报文日志按小时写入独立的 `packets/YYYY/MM/DD/HH/`；每个转发通道在两个目录各自使用对应文件，例如：

```text
logs/2026/11/04/08/scdfe_t-channel-1-20261104-08.log
packets/2026/11/04/08/scdfe_t-channel-1-20261104-08.log
```

不同通道的启停可以并行，同一通道的管理请求仍互斥。周期心跳只更新时间，不使正在编辑的定义失效；通道配置或启停状态变化仍更新版本。通道及点映射表单提交已读取的版本，遇到其他操作员先保存时会报告冲突，重新打开记录后再修改。

正文时间只有 `HH:MM:SS`，运行日志类型使用“日志/警告/错误/进程”，报文日志类型为“报文”。通道启动、保留期变更及运行中每小时，按最后修改时间分别清理本通道超过 `log_retention_hours` 的 `logs/` 和 `packets/` 小时文件。其他通道、其他服务以及不符合命名规则的文件不参与清理。
报文默认不写入文件。T 的 `fes_scdfe_t_define.packet_recording` 是唯一的通道级开关，在“通道数据 → 编辑通道”中修改；保存后约 1 秒内作用于该转发通道，不需要在日志页重复设置。日志页只负责查看和筛选。

“通道数据 → 编辑通道”中的“保存报文”是唯一的通道级开关。保存后约 1 秒内作用于该转发通道；通道列表和日志页只展示数据，不提供第二个修改入口。

开启保存后的新收发记录使用独立类型 `[报文]`（`PACKET`），只写入 `packets/`，保留 RX/TX 和十六进制原始字节；运行日志不会混入新报文。报文查询只读取 `packets/YYYY/MM/DD/HH/`，运行日志查询只读取 `logs/YYYY/MM/DD/HH/`。
日志页 `/logs.html` 先选择“运行日志”或“报文日志”标签，再在左侧按年、月、日、小时展示对应目录文件，右侧显示正文。可以按通道、文件名、RX/TX、关键词、时间范围和级别组合筛选，检索当前文件或全部匹配文件；
支持历史分批读取、自动换行、跟随最新和导出本批结果。

文件目录接口为 `GET /api/log-files`，正文检索接口为 `GET /api/log-files/search`。
检索参数包括 `filename`（相对当前 `logs/` 或 `packets/` 目录的文件路径）、`process=scdfe_t-channel-N`、
`file_query`、`q`、`record=packet|runtime`、`direction=RX|TX`、
`level=INFO|PACKET|WARNING|ERROR|PROCESS`、`start/end`（带时区的 ISO 8601 时间）、
`limit=1..5000` 和 `cursor`。使用响应中的 `next_cursor` 继续向前检索；
改变筛选条件后应清除游标。这些接口只读取 `scdfe_t` 日志，能检索较早记录而不局限于文件尾部。

## 本地回环验证

1. 在 `scdfe_c` 数据库中准备一个 RTU 和至少一个 YC、YX、YK、YT 点。
2. 建立一个 IEC104 或 Modbus TCP 转发通道，分别添加 YC/YX 上送映射和 YK/YT 下行映射。
3. 通过 Web 启动通道，在进程监控中确认 PID、心跳和日志均更新。
4. 使用对应协议测试客户端读取 YC/YX，核对值和质量码与 `fes_yc_define` / `fes_yx_define` 一致。
5. 使用客户端下发 YK/YT，确认 `fes_yk_define` / `fes_yt_define` 的 `value`、`cmd_time` 发生变化，随后由 `scdfe_c` 写库进程和通信线程完成设备侧下发。
6. 同时启动第二个转发通道，使用不同监听端口和点映射，确认两个进程的日志、心跳和数据互不串扰。

硬件串口、真实远方主站和实际现场设备不在本地回环验证范围内；现场联调前应按远方系统的 IEC104 类型标识/IOA 或 Modbus 寄存器字节序复核映射。
