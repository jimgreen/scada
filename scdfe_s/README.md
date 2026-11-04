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
