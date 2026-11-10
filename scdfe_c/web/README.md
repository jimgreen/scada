# scdfe_c 管理 Web

`scdfe_c/web` 只提供 RTU、通道、四遥定义、协议辅助表、主进程、通信线程、数据库写进程和运行日志管理。服务端/主站通信测试页面与接口位于独立的 `scdfe_s/web`，默认端口为 8878。

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
