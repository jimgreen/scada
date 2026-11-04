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

- `code`、`name`：操作员可读的通道标识；
- `protocol`：`iec104` 或 `modbus-tcp`；
- `bind_host`、`bind_port`：对外监听地址和端口；
- `common_address`、`unit_id`：IEC104 公共地址、Modbus 单元号；
- `scan_interval_ms`、`report_interval_ms`：采集/上送周期；
- `process_enable`：是否允许 Web 启动/守护该通道。

每个通道对应一个 `channel_id`。同一个数据库中的多个通道可以使用不同协议、监听端口和点映射；监听端口不能重复。修改协议、监听参数或映射前应先停止通道，保存后再启动，以避免旧连接继续使用旧定义。

## 四遥点映射

在通道的“点映射”页逐条增加需要转发的点：

| 字段 | 说明 |
| --- | --- |
| `kind` | `yc`、`yx`、`yk` 或 `yt` |
| `source_rtu_id` | `scdfe_c` 中源 RTU 编号 |
| `source_pnt_no` | 源四遥表的点号 |
| `pnt_no` | 本转发通道内的唯一点序号 |
| `enabled` | `1` 才参与转发或命令接收 |
| `io_address`、`type_id` | IEC104 的 IOA、信息体类型；按通道实际配置填写 |
| `register_area`、`address` | Modbus TCP 的寄存器区和地址 |
| `data_type`、`byte_order`、`word_order` | Modbus 数据编码；按远方设备要求填写 |
| `scale`、`offset` | Modbus 数值工程换算 |
| `command_mode` | 遥控/遥调的命令模式，按协议和远方设备约定填写 |

同一个通道的 `(kind, pnt_no)` 不能重复。YC/YX 只读 `scdfe_c` 的 `fes_yc_define` / `fes_yx_define`；YK/YT 下行命令写入 `fes_yk_define` / `fes_yt_define` 的 `value` 和 `cmd_time`，由 `scdfe_c` 现有通信线程继续完成实际设备下发。转发进程不直接操作 RTU 协议连接。

建议先启动 `scdfe_c` 并确认源 RTU 的四遥数据在更新，再启用转发通道；源点不存在、类型不匹配或映射未启用的点会记录错误并跳过，不影响同一通道的其他点。

## 命令行运行一个通道

Web 启动和停止是推荐方式。需要脚本或调试时，可直接运行一个通道：

```powershell
.\.venv\Scripts\python.exe -B -m scdfe_t \
  --db D:\codex\scada\db\scada.sqlite3 \
  --channel-id 1
```

`--channel-id` 必须是数据库中已定义的转发通道。进程读取通道协议、监听地址和点映射，不应在命令行重复传入协议参数。按 `Ctrl+C` 或收到 `SIGTERM` 时，进程先停止协议服务，再登记停止时间和退出状态。每个通道只能有一个运行实例；Web 启动前会检查数据库登记的 PID，异常退出的进程会标记为错误并允许重新启动。

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
GET    /api/trans/logs?channel_id=1
GET    /api/trans/log?filename=logs/2026/11/04/08/scdfe_t-channel-1-20261104-08.log&tail=300
GET    /api/health
```

请求体使用 `Content-Type: application/json`。删除通道会先停止对应进程，再在同一数据库事务中删除点映射和通道定义；页面会在删除前提示确认。运行状态字段（PID、心跳、退出码、错误等）由转发进程维护，不能通过普通通道编辑接口伪造。

## 进程监控和日志

“进程监控”展示每个转发通道的状态、PID、启动时间、停止时间、心跳、退出码和最近错误；可以独立启动/停止，某个通道异常不会停止其他通道。日志按小时写入数据库旁的 `logs/YYYY/MM/DD/HH/`，每个转发通道一个文件，例如：

```text
logs/2026/11/04/08/scdfe_t-channel-1-20261104-08.log
```

正文时间只有 `HH:MM:SS`，日志类型使用“日志/警告/错误/进程”。通道的 `log_retention_hours` 控制过期文件清理，日志页面支持按通道、关键词、时间和类型筛选并查看正文。

## 本地回环验证

1. 在 `scdfe_c` 数据库中准备一个 RTU 和至少一个 YC、YX、YK、YT 点。
2. 建立一个 IEC104 或 Modbus TCP 转发通道，分别添加 YC/YX 上送映射和 YK/YT 下行映射。
3. 通过 Web 启动通道，在进程监控中确认 PID、心跳和日志均更新。
4. 使用对应协议测试客户端读取 YC/YX，核对值和质量码与 `fes_yc_define` / `fes_yx_define` 一致。
5. 使用客户端下发 YK/YT，确认 `fes_yk_define` / `fes_yt_define` 的 `value`、`cmd_time` 发生变化，随后由 `scdfe_c` 写库进程和通信线程完成设备侧下发。
6. 同时启动第二个转发通道，使用不同监听端口和点映射，确认两个进程的日志、心跳和数据互不串扰。

硬件串口、真实远方主站和实际现场设备不在本地回环验证范围内；现场联调前应按远方系统的 IEC104 类型标识/IOA 或 Modbus 寄存器字节序复核映射。
