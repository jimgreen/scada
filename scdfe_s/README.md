# scdfe_s 主站/服务端通信测试服务

`scdfe_s` 集中提供 IEC104、Modbus TCP、Modbus RTU、CAN、BACnet/IP 和 OPC UA 的通信测试。
Web 服务只有一个站点，不同规约和角色使用独立页面；命令行入口每次只运行一个 RTU 通道。

目录职责：

- `scdfe_s/web/`：统一 Web 服务、API、静态页面和协议测试服务对象；
- `scdfe_s/run_iec104_server.py`：IEC104 服务端命令行入口；
- `scdfe_s/run_modbus_tcp_server.py`：Modbus TCP 主站/服务端命令行入口；
- `scdfe_s/run_modbus_rtu_server.py`：Modbus RTU 主站/服务端命令行入口；
- `scdfe_s/run_can_server.py`：CAN/CAN-FD 主站/服务端命令行入口；
- `client/`：主站/客户端通信类；
- `server/`：服务端/从站通信类。

数据库模型和 RTU、通道、四遥点表读取复用 `scdfe_c.web.service`，不把 RTU 自动守护和写库
进程控制混入通信测试服务。

## Web 服务

IEC104 的增量更新、全量更新与模拟指令先校验显式点并写入数据库，再更新站内值、向已连接客户端上送。全量更新中，一个点写入失败会回滚整批，不会发送这批值。如果数据库已经提交而网络发布失败，页面错误会明确区分这两种结果。

运行中保存服务配置时，先将新配置写入磁盘，再切换服务。文件写入失败时原连接继续工作；新服务启动失败时会尝试恢复原配置和原服务，并在页面显示结果。启动 Web 时若发现配置文件损坏，页面显示错误并允许人工修复，保存有效配置前不能启动该规约。运行中的服务固定使用启动时选定的 RTU，新增其他 RTU 不会改变现有连接的命令归属。

Windows：

```text
scdfe_s\web\start.bat --host 127.0.0.1 --port 8878
```

Linux/macOS：

```text
sh scdfe_s/web/start.sh --host 127.0.0.1 --port 8878
```

也可以直接运行：

```text
python -m scdfe_s.web.server --host 127.0.0.1 --port 8878
```

浏览器打开 `http://127.0.0.1:8878/`。首页提供以下独立页面：

- `/iec104`：IEC104 服务端、四遥更新和命令监控；
- `/modbus-tcp`、`/modbus-tcp-server`：Modbus TCP 主站、服务端；
- `/modbus-rtu`、`/modbus-rtu-server`：Modbus RTU 主站、服务端；
- `/can`、`/can-server`：CAN 主站、服务端；
- `/bacnet-server`、`/opc-server`：BACnet/IP、OPC UA 服务端。
- `/channels`：多 RTU 通道列表及通道内 YC/YX/YK/YT 测点定义的增删改查；
- `/processes`：各协议测试服务的独立启停和状态监控；
- `/communication`：连接数、测点数、通信状态和错误监控；
- `/logs`：按服务、日志类型和关键字查看运行日志。

每个页面使用同一套 `status/config/start/stop/poll/points/commands/logs/alerts` API，页面之间
不会共享运行中的传输对象；配置、日志和点表仍按 RTU 通道隔离。

通道和测点管理 API 使用当前数据库模型中的 RTU 作为通道记录：

```text
GET/POST/PATCH/DELETE /api/channels
GET/PATCH/DELETE       /api/channels/{rtu_id}
GET/POST                /api/channels/{rtu_id}/points/{yc|yx|yk|yt}
GET/PATCH/DELETE       /api/channels/{rtu_id}/points/{kind}/{pnt_no}
```

运行监控 API 为 `GET /api/monitor/processes`、`GET /api/monitor/communications`、
`GET /api/monitor/logs`，服务启停使用
`POST /api/monitor/communications/{service}/start|stop`。

日志页 `/logs` 左侧按年、月、日、小时展示数据库旁 `logs/` 中的文件，右侧读取正文。
可选择协议服务、当前文件或全部匹配文件，并组合筛选报文/运行日志、RX/TX、级别、
关键字和时间范围；支持历史分批检索、自动换行、跟随最新和导出本批结果。
文件目录接口为 `GET /api/log-files`，正文检索接口为 `GET /api/log-files/search`。
检索参数包括 `filename`（相对 `logs/` 的文件路径）、`process`、`file_query`、`q`、
`record=packet|runtime`、`direction=RX|TX`、`level=INFO|PACKET|WARNING|ERROR|PROCESS`、
`start/end`（带时区的 ISO 8601 时间）、`limit=1..5000` 和 `cursor`。
使用响应中的 `next_cursor` 继续向前检索；改变筛选条件后应清除游标。
这些接口只读取 `scdfe_s` 的日志，历史检索不受内存最近记录数量限制。
报文默认不写入文件。在“运行日志”的“报文保存范围”中选择具体通道，再切换“保存报文（调试）”，只影响该通道；“默认设置”影响未单独设置的通道，“恢复使用默认设置”清除当前通道的单独设置。设置持久化在数据库旁的 `<数据库文件名>.scdfe_s.packet-log.json`，约 1 秒内生效，无需重启协议服务。`GET/PUT /api/packet-recording` 管理默认值，`GET/PUT /api/packet-recording/channels/<channel_id>` 管理通道设置；PUT 请求体为 `{"enabled":true}` / `{"enabled":false}`，通道接口可用 `{"enabled":null}` 恢复默认。通道编号来自服务启动时绑定的 RTU 的 `channel_id`；未绑定 RTU 的独立模拟服务使用默认设置。同一通道的客户端和服务端共用该通道设置，三套应用之间互不影响。普通日志、历史文件和界面即时通信记录不受影响。

“通道数据”列表也提供“报文保存”列：显示每个通道的实际开启/关闭状态及“沿用默认/单独设置”，可直接切换或恢复默认，与运行日志页使用同一设置。页面每 5 秒刷新保存状态，保存失败会恢复原状态并显示错误。

开启保存后的新收发记录使用独立类型 `[报文]`（`PACKET`），保留 RX/TX 和十六进制原始字节。
运行日志的“报文通道号”可与协议服务、RX/TX、关键字同时筛选；对应接口参数为 `GET /api/log-files/search?channel_id=10&direction=RX`。新报文记录保存收发时绑定的通道号，已停用或重新配置的通道也按原通道查询。旧报文或未绑定通道的记录仍可在不指定通道号时查看。
历史 `[日志] 报文 ...` 仍可通过 `record=packet` 检索，文件原文和类型保持原样。

## 命令行通道入口

IEC104 服务端：

```text
python -m scdfe_s.run_iec104_server --rtu-id 7 --bind-ip 127.0.0.1 --port 2404
python scdfe_s/run_iec104_server.py --rtu-id 7 --bind-ip 127.0.0.1 --port 2404
```

Modbus TCP：

```text
python -m scdfe_s.run_modbus_tcp_server --rtu-id 3 --mode master
python -m scdfe_s.run_modbus_tcp_server --rtu-id 3 --mode server --listen-port 1503
```

Modbus RTU：

```text
python -m scdfe_s.run_modbus_rtu_server --rtu-id 4 --mode master
python -m scdfe_s.run_modbus_rtu_server --rtu-id 4 --mode server --simulation
```

CAN/CAN-FD：

```text
python -m scdfe_s.run_can_server --rtu-id 5 --mode master
python -m scdfe_s.run_can_server --rtu-id 5 --mode server --simulation
```

三个新增入口都支持：

- `--db`、`--schema-dir`：覆盖数据库和 JSON 表定义目录；
- `--rtu-id`：指定唯一运行通道；
- `--mode master|server`：选择主站或服务端；
- `--poll-interval`：采集/快照周期，默认 1 秒；
- `--max-cycles`：运行指定周期后退出，适合自动化测试；
- `--simulation`/`--real`：服务端选择内存模拟或真实端口/串口。

协议参数默认从指定 RTU 的参数表读取，显式命令行参数只覆盖本次启动配置。收到 Ctrl+C
或 SIGTERM 后，入口会先停止协议对象，再退出进程。
