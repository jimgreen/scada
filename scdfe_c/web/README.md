# scdfe_c 管理 Web

`scdfe_c/web` 只提供 RTU、通道、四遥定义、协议辅助表、主进程、通信线程、数据库写进程和运行日志管理。服务端/主站通信测试页面与接口位于独立的 `scdfe_s/web`，默认端口为 8878。

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

- RTU 与通道的一对一配置、协议参数和辅助表维护；
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