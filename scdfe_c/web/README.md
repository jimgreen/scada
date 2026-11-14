# scdfe_c 管理 Web

`scdfe_c/web` 只提供 RTU、通道、四遥定义、协议辅助表、主进程、通信线程、数据库写进程和运行日志管理。服务端/主站通信测试页面与接口位于独立的 `scdfe_s/web`，默认端口为 8878。

## 操作归因与审计

### 通信业务明细

“运行日志”中选择 RTU 的通信日志，搜索 `业务事件`、`总召`、`突发/变位`、
`实时更新`、`指令`、`Modbus`、`CAN` 或 `SOE`。业务记录包含带时区的毫秒时刻、
组件、RTU/通道、批次和点号，不依赖“保存报文”开关。原始完整报文仍使用独立报文目录。

- 四遥：实际变化点记录原始值、工程值、前后质量与刷新时刻。稳定值心跳不重复记为变化。
  `已入写库队列`表示队列已接收；按 `写库批次` 查数据库写进程的 `写库事务已提交`，
  同时核对事务更新/未匹配行数。队列接收不代表数据库已经提交。
- IEC104：记录 STARTDT/STOPDT/TESTFR、总召激活/确认/返回/终止、周期/突发数据、
  CA、IOA、COT、收发序号、质量和 CP24/CP56 时标，指令记录选择位、执行、撤销及否定确认。
  CP24 不补造日期/小时；CP56 保留原始字节与无效标志，不推断设备时区。
- Modbus TCP/RTU：记录站号、功能码、零基地址、数量、通信尝试、原始位/寄存器值、
  响应耗时和异常码；TCP 记录库生成的事务标识。读响应本身没有地址，服务器日志须结合
  同连接请求解释 TID，不能把不同客户端的相同 TID 当成同一个请求。串口 CRC 由协议库校验，
  CRC 错误导致的无有效响应记为通信失败，不凭超时断言一定是 CRC 故障。
- CAN：记录帧 ID、标准/扩展格式、FD/RTR/BRS/ESI、DLC、数据、驱动时标、错误帧、
  驱动总线状态变化；配置的信号变化保留偏移/位宽/字节序/缩放和工程值。
  周期/回放记录定义编号、模式、周期、调度延迟及驱动调用耗时；请求记录匹配应答或超时。
  不将普通 CAN 数据擅自解释为 CANopen/J1939/UDS；ACK、CRC、错误计数和物理波形未由
  驱动提供时不能据此判定正常，`bus.state` 也不是物理层测量。
- SOE：分别记录事件入库、发送提交、APCI 确认推进游标和未确认重试。
  APCI 确认不等于对方业务数据库已提交；命令驱动返回也不代表物理动作完成。

同批变化点每 128 条合并写文件，不抽样丢弃变化明细；帧/请求记录会增加磁盘写入量，
按通道 `log_retention_hours` 保留。日志异常隔离于通信处理。新代码须重启相应通信进程
后生效；只刷新页面不能替换正在运行的通信代码，历史日志不会补写不存在的细节。

高频业务日志在生成处精简：进程、线程、组件、RTU、通道、规约集中记录在
`日志上下文 @编号` 中，后续明细用短 `@编号` 引用，每个小时文件各自包含定义。
同文件重启或归属变化会生成新编号；按 `日志上下文 @编号` 搜索可找到该条记录的完整归属。
模板只生成必要的单个分隔空格，不逐条重复清洗正文，点值、质量、时标和指令结果不抽样。
日志按“所见即所存”处理，低频故障详情中的原始标识和数据空白不被擅自删去。
不再用正则删除字段、压缩空白或裁剪首尾空格。仅把会拆分记录的换行字符显示为
`\r`、`\n` 等可见转义；S 的实时预览与文件使用同一正文，查询、导出不再改写正文。
页面关键词高亮只改变显示标记，每批编译一次匹配表达式；没有关键词时直接显示原文。
操作审计的密码/令牌脱敏仍在审计事件生成处执行，原始协议字节仍保存在报文 HEX 中。

完整业务日志的可重复性能对比：

```powershell
python tools/benchmark_business_logging.py --baseline-ref fb8561f --repeat 9 --output output/business_logging_performance.json
```

脚本使用临时日志、隔离 SQLite WAL 数据库和本机 TCP，比较 1000 条变化明细、
6 个 RTU 共 3600 次点更新（采集入队、事务提交及日志）、Modbus TCP 读取和
10000 条业务日志的完整分页查询。固定信息移入上下文头后，须按 `@编号` 查询归属；
每条明细的点号、值、质量、事件时刻与批次仍保留。
前后均启用完整业务记录，交替运行并校验记录条数、内容和数据库最终值/版本；
JSON 保存全部样本、源码校验值和中位数。数据库案例采用带主键的最小点表，
不包含业务触发器或进程间队列传输，
本机 TCP 结果不等同于现场网络性能。日志分组写入后仍立即 flush；空变化批次不访问时钟或文件。
`node tools/benchmark_log_highlight.cjs` 可单独比较 5000 行关键词高亮的 CPU 耗时，
不含 HTTP 请求和浏览器布局时间。

日志检索在 C/S/T 共用实现中复用同次请求的日志头、文件日期和同日时钟转换；
缓存有容量限制，且不跨请求共享时区状态。仅由汉字（U+4E00–U+9FFF）和数字组成的
关键词先按 UTF-8 字节筛选，其余关键词保留 Unicode 大小写匹配。
筛选与导出始终保留原文；业务正文中引用“报文 RX/TX”不算报文记录，
历史 `[日志] 报文 RX/TX ...` 与新 `[报文]` 类型仍可正确筛选。

查询优化可用同一批临时日志进行独立比较（先保存修改前的查询源文件）：

```powershell
python tools/benchmark_log_query_batches.py --baseline-file before_log_query.py --repeat 9 --output output/log_query_batches/performance.json
```

脚本覆盖最新 20 条预览、12000 条明细、稀疏中文匹配、Unicode 匹配、未命中查询及
C/S/T 各 60 个 RTU/通道的日志。除了预览只取第一页，其余均读取全部分页。
两种实现读取完全相同的文件，校验文本、时间、顺序、游标和扫描计数，
生成 JSON 原始样本及单文件 HTML 报告；结果不包含 HTTP、浏览器绘制和现场通信耗时。

C/S/T 的 POST、PUT、PATCH、DELETE 请求统一生成服务端 `trace_id`，响应头返回
`X-Scada-Trace`。运行日志中选择“操作审计 · 进程 …”，打开“显示筛选”，按
`trace_id` 搜索即可查看请求到达、参数解析、数据库提交、业务返回和请求结束。
S/T 查看全站审计时先清除通信通道筛选。审计文件名为
`scdfe_c-operations-进程号-年月日-小时.log`（S/T 对应 `scdfe_s`、`scdfe_t`），
沿用运行日志目录的年月日小时子目录；C 使用配置的 `log_dir`，S/T 使用各自数据库旁的 `logs`。

每条记录包含带时区的毫秒时间、组件、进程/线程、真实 TCP 对端 IP/端口、HTTP
方法/路径、来源页面、客户端标签页和请求标记、最近按钮操作、操作对象、结果及耗时。
页面标记来自浏览器声明，最近按钮仅是排查线索；当前未登录的 Web 无法据此证明具体自然人身份。
不采信客户端提供的服务端追踪编号，不采信转发头代替实际连接地址。

新增、修改、删除及级联删除在事务成功提交后记录结果；单条修改保留前后值和字段名，
批量修改保留总数及最多 20 条样本，超过限制明确标记截断。回滚、版本冲突、参数错误、
来源检查失败、路径不存在均保留失败请求，不写“数据库已提交”。HTTP 202 表示已受理：
进程控制后台线程沿用原追踪号，另记任务编号、步骤及最终完成/失败。命令入库回执不等于
设备执行成功，可按 RTU、四遥类型、点号、`cmd_time` 对照 C 通信日志中的“命令时标”。
TCP 指令入口另外记录连接来源、认证结果、请求编号及受理结果，不记录认证令牌。

密码、令牌、Cookie、认证字段和 URL 凭据会脱敏，Referer/请求查询串不写入审计。
普通 GET/HEAD 轮询、周期采集和无审计上下文的批量写库不逐点写审计，也不额外查询旧值。
日志写入故障会在服务错误日志报告追踪号；已提交业务不被改报失败，避免客户端重复下发。
只看到“请求到达”而没有结果，须同时检查进程退出和日志写入错误，不能认定操作成功。

服务端改动需重启相应 Web/通信进程后加载，浏览器刷新后启用客户端来源标记。
外部工具直接修改 SQLite、操作系统强制终止进程，以及补丁加载前的旧日志不具备完整调用来源，
不能追溯补全。S 的自动重载/资源释放另记原因和代码调用位置；审计没有改变 C/T 共享业务库、
S 独立模拟库以及进程间只经数据库交互的边界。

## 通道与 RTU 定义

工作台默认先打开“通道定义”，再打开“RTU 定义”。通道目录通过
\`GET/POST/PATCH/DELETE /api/channels\` 管理，通道的复合身份是
\`(channel_type, channel_id)\`：先按规约类型选择对应的 \`fes_channel_*_define\`
表，再按 \`channel_id\` 定位记录。RTU 创建时同时提交 \`channel_type\` 和
\`channel_id\`，服务端按该复合键校验绑定。通道和 RTU 两页分别保存搜索、RTU
筛选、排序和分页状态。

## 概览与 RTU 选择

工作台顶部使用 `GET /api/overview?view=summary`，只统计 RTU 和四遥点表。不带 `view` 的 `GET /api/overview` 继续返回全部表计数和所有通道类型的数量。RTU 下拉框按页加载，超过 500 个站时也可选择后续站点。

规约配置行改属另一个 RTU 时，原站和新站都会收到定义变更。BACnet 和 OPC UA 采集主站用同一个数据库快照加载站、通道和测点映射；只有未配置映射的点才采用默认地址。

## 启动

Windows：

```powershell
Set-Location D:\codex\scada
.\.venv\Scripts\python.exe -B -m scdfe_c.web.server --host 127.0.0.1 --port 8877
```

或运行 `scdfe_c\web\start.bat`。Linux/macOS 使用 `sh scdfe_c/web/start.sh`。

浏览器打开 `http://127.0.0.1:8877/`，接口文档为 `/api-docs`。数据库默认使用
`D:\codex\scada\db\scada.sqlite3`，表结构来自 `D:\codex\scada\dbdef\json`。

## 管理范围

- 独立维护通道定义、RTU 定义、协议参数和辅助表；
- 遥测、遥信、遥控、遥调定义及在线命令下发；
- `scdfe_c` 自动守护、单 RTU 调试、主进程、数据库写进程和 TCP 指令服务进程控制；
- TCP 指令服务接收认证的 YK/YT JSON 指令，写库成功后由通信线程继续下发；
- 按年/月/日/小时分目录的进程日志查询、关键字/时段/类型筛选。

通信线程的启动、连接、重试、停止记录包含 RTU 编号/名称、通道类型/编号、连接目标、
主进程 PID 和线程 ID。IEC104 附公共地址 CA，Modbus 附站号（串口还包含波特率、
数据位、校验位、停止位），CAN 附接口及仲裁/数据波特率；OPC 端点不记录认证信息。
主进程日志可以通过关键词 `RTU编号=7` 或 `通道类型=iec104` 查找对应线程。
连接异常注明失败阶段、异常类型、连续失败次数及重试间隔，成功记录包含连接耗时。
启动日志表示线程已启动，连接成功需查看该 RTU 日志中的“通道连接成功”。
配置修改后，旧线程的停止日志保留旧连接目标，新线程记录新目标。连接上下文只在启动时
读取并缓存，不对每个采集周期、点值或报文追加数据库查询。新格式在重启 `scdfe_c`
通信主进程后生效；刷新或重启 Web 不会替换正在运行的通信代码，已有历史日志保持原文。

通信主站由 `client/` 导出的 `ModbusTcpMaster`、`ModbusRtuMaster`、`IEC104Master`、
`CanMaster`、`BacnetMaster` 和 `OpcUaMaster` 实现；`scdfe_c` 只负责按 RTU 配置调度这些主站并批量写库。

## 接口

```text
GET/PATCH /api/rtus/{rtu_id}/channel
GET/POST/PATCH/DELETE /api/rtus/{rtu_id}/channel/parameters
GET/POST /api/channels
GET/PATCH/DELETE /api/channels/{channel_type}/{channel_id}
GET/POST/PATCH/DELETE /api/channels/{channel_type}/{channel_id}/parameters
GET/POST/PATCH/DELETE /api/rtus/{rtu_id}/points/{yc|yx|yk|yt}
POST /api/rtus/{rtu_id}/commands/{yk|yt}/{pnt_no}
GET /api/processes
POST /api/processes/control
GET/PATCH /api/scdfe_c/status
PATCH /api/scdfe_c/control
GET/PATCH /api/scdfe_c/command-server
GET /api/scdfe_c/logs
GET /api/scdfe_c/logs/search
```

协议测试接口不在本服务中。启动独立测试 Web：

```powershell
.\.venv\Scripts\python.exe -B -m scdfe_s.web.server --host 127.0.0.1 --port 8878
```

然后打开 `http://127.0.0.1:8878/`，选择 IEC104、Modbus TCP、Modbus RTU、CAN、BACnet/IP
或 OPC UA 测试入口。
