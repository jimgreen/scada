# SCADA 服务端/从站 API（server）

`server/` 只存放可复用的服务端/从站类；通信主站和客户端类统一位于 `client/`，测试入口和 Web 服务统一位于 `scdfe_s/`。

## 服务端文件

| 文件 | 实现 |
| --- | --- |
| `iec104_server.py` | IEC 60870-5-104 服务端，支持四遥点、总召唤、主动上送和遥控遥调回调 |
| `modbus_server.py` | Modbus TCP/RTU 服务端基础实现和模拟器 |
| `modbus_tcp_server.py` | Modbus TCP 服务端适配 |
| `modbus_rtu_server.py` | Modbus RTU 服务端适配 |
| `can_server.py` | CAN/CAN-FD 服务端和虚拟总线模拟器 |
| `bacnet_server.py` | BACnet/IP 服务端 |
| `opc_server.py` | OPC UA 服务端 |
| `point_store.py` | 服务端点值存储 |

## Python 入口

```python
from server import IEC104Server, ModbusTcpServer, ModbusRtuServer

server = IEC104Server.from_store(store, rtu_id=7)
server.start()
try:
    server.update("yc", 1, 12.5)
finally:
    server.stop()
```

服务端测试的命令行入口在 `scdfe_s/run_iec104_server.py`，Web 测试服务在
`scdfe_s/web/server.py`，启动方式：

```text
python -m scdfe_s.run_iec104_server --rtu-id 7 --bind-ip 127.0.0.1 --port 2404
python -m scdfe_s.web.server --host 127.0.0.1 --port 8878
```

服务端类只负责协议监听、报文解析和点值/命令回调，不包含 Web 路由或 scdfe_c 守护进程控制。