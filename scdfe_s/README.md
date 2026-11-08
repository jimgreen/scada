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

## 并发更新与调试

- Modbus 从站的 Web 批量更新先校验整批点值，在一次 SQLite 事务中写入，再一次性发布到规约线程。校验或事务失败时不改寄存器；数据库已提交但规约发布未确认时，接口明确区分这两种状态。原始寄存器只接受 0 到 65535 的整数；YC、YT 指向同一保持寄存器时，同一批次必须填相同的值。
- IEC104 四遥页同时显示点号、信息体地址和类型标识。自动刷新更新监控值，保留尚未提交的配置字段；保存过程中继续输入的新值也保留为草稿。
- 在项目根目录使用项目虚拟环境运行 `python -m tools.soak_multichannel --minutes 30 --output output/soak_new`。输出目录必须是新目录。测试使用临时数据库、本机真实 TCP 连接和动态端口，并启动独立采集及转发进程。主采集服务使用主机互斥锁，请与占用该锁的服务或测试错开运行。逐分钟进度写入 `events.jsonl`，完成并清理后生成 `result.json`。

## 独立数据库

S 的 Web、IEC 104、Modbus TCP/RTU 和 CAN 命令行默认使用 `db/scdfe_s.sqlite3`。C 与 T 仍用 `db/scada.sqlite3`。两库可以复用 RTU 号、通道号和四遥点号；S 的增删改不会更新 C 的定义。两端交换值依靠实际规约连接。对照三个端口的 `/api/health`，S 还会显示 `database_role: simulator`。显式传入业务库会拒绝启动；C/T 也不能把 S 专库当成业务库。

直接启动 S 会创建空专库。若要使用业务库中的通道和测点作为测试起点，在目标文件尚不存在时执行：

```text
python -m scdfe_s.migrate --source db/scada.sqlite3 --destination db/scdfe_s.sqlite3
```

工具从源库的只读快照复制 RTU、四遥定义及其显示值、规约参数和映射，不复制进程状态、待执行命令或 T 转发配置。未配置通道且没有关联数据的 RTU 会列在报告中；若有子记录则中止，目标文件已存在时拒绝覆盖。此后两库独立维护，不会同步定义。S 的规约配置和日志位于专库旁；报文保存策略持久化在 S 独立数据库的 fes_rtu_define.packet_recording 字段。

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
- `/channels`：S 专库中的通道管理；`/points`：按 RTU 管理 YC/YX/YK/YT 点；
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

日志页 `/logs` 先用分页标签选择“运行日志”或“报文日志”，再选择通信通道。每个通道
独立生成 `scdfe_s-channel-{channel_id}-YYYYMMDD-HH.log`，左侧按年、月、日、小时展示
该通道的文件，右侧读取正文；运行日志只读 `logs/YYYY/MM/DD/HH/`，报文日志只读
`packets/YYYY/MM/DD/HH/`，两棵目录树不混写、不共用文件。可组合筛选 RX/TX、级别、关键字
和时间范围；支持历史分批检索、自动换行、跟随最新和导出本批结果。
文件目录接口为 `GET /api/log-files`，正文检索接口为 `GET /api/log-files/search`。
检索参数包括 `record=runtime|packet`（必须与所选标签一致）、`filename`（相对当前
`logs/` 或 `packets/` 目录的文件路径）、`channel_id`、`process`、`file_query`、`q`、`direction=RX|TX`、
`level=INFO|PACKET|WARNING|ERROR|PROCESS`、
`start/end`（带时区的 ISO 8601 时间）、`limit=1..5000` 和 `cursor`。
使用响应中的 `next_cursor` 继续向前检索；改变筛选条件后应清除游标。
这些接口只读取 `scdfe_s` 的日志，历史检索不受内存最近记录数量限制。
报文默认不写入文件。S 的 `fes_rtu_define.packet_recording` 是唯一的通道级开关，在“通道数据 → 编辑通道”中修改；未绑定 RTU 的独立模拟服务不保存报文，必须绑定到通道后按通道参数生效。客户端和服务端绑定同一通道时共用该字段。日志页只负责查看和筛选，不再提供第二个开关。

“通道数据 → 新增 / 编辑通道”中的“保存报文”是唯一的通道级开关。保存后约 1 秒内作用于该通道；通道列表和日志页只展示数据，不提供第二个修改入口。

开启保存后的新收发记录使用独立类型 `[报文]`（`PACKET`），写入 `packets/YYYY/MM/DD/HH/`，
保留 RX/TX 和十六进制原始字节；运行日志不再追加报文行。报文日志页可按通道、协议服务、
RX/TX 和关键字筛选，对应接口参数为 `GET /api/log-files/search?record=packet&channel_id=10&direction=RX`。
新报文记录保存收发时绑定的通道号，已停用或重新配置的通道也按原通道查询。报文查询只读取 `packets/YYYY/MM/DD/HH/`，不会把运行日志中的内容复制到报文目录。

## 点表刷新与批量更新

各规约的 `GET /api/<规约>/points` 默认返回完整点定义。仅用于显示四遥时，可传 `?view=table`，也可加 `kind=yc` 等类型过滤。IEC104 表格包含信息体地址和类型标识；未配置映射的点仍会列出。规约页面轮询只更新发生变化的单元格，可保留同一点的操作按钮和未保存的配置输入。

绑定 RTU 的 CAN、OPC UA 和 BACnet 服务端通过 `POST /api/<规约>/update/full` 批量更新时，先验证整批，再一次提交数据库。若数据库拒绝其中一个点，整批回滚，协议端不会发布这批值。提交后才向协议端逐点发布；如发布失败，接口会明确报告“数据库已写入”以及第一个未确认的点，可据此核查协议端再重试。未绑定 RTU 的独立模拟不写业务点表。

通信通道运行期间复用当前小时的通道日志文件句柄，每条记录写完即刷新，日志页可在服务运行中查看；停服时关闭句柄。报文是否写入仍由通道设置决定。

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
