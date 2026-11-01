# SCADA 协议主站 API

这里的每个协议主站接口只负责把 SCADA 点表字段映射到成熟 Python 库；TCP/串口帧、IEC104 链路状态机和 CAN 驱动由第三方库维护。公开入口只使用当前主站名称，不提供旧客户端名称或旧方法别名。

## 安装

在 Windows 或 Linux 的项目解释器中安装锁定版本（建议使用 Python 3.10+ 虚拟环境）：

```text
python -m pip install -r <项目目录>/requirements.txt
```

`c104` 2.2.1 提供 CPython 3.7–3.13 的 Windows x64 wheel，以及 Linux
manylinux x86_64/aarch64 wheel。换 Python 大版本、CPU 架构或发行版前应先确认
对应 wheel；没有匹配 wheel 时需要按 c104 上游文档准备 C++ 构建环境。其余适配器
使用跨平台 Python 包，但底层设备驱动仍由操作系统决定。

平台差异需要在部署配置中明确：Modbus RTU 在 Windows 使用 `COM3` 等串口名，Linux
通常使用 `/dev/ttyUSB0` 或 `/dev/ttyS0`；`python-can` 在 Linux 可使用 SocketCAN，
在 Windows 需安装并配置 PCAN、Vector 等厂商后端。BACnet/IP 和 OPC UA 的网络协议
本身跨平台，证书文件路径应使用当前系统的路径格式。这里的 OPC 接口是 OPC UA；
旧 OPC DA/COM 需要 Windows 专用后端，本项目不把 DA ProgID 当成跨平台 UA 地址。

## 接口文件

| 文件 | 第三方库 | 用途 |
| --- | --- | --- |
| `modbus_tcp.py` | `pyModbusTCP` | Modbus TCP 主站：读写线圈/寄存器、按 `fes_channel_modbus_define_*` 采集和遥调遥控 |
| `modbus_rtu.py` | `pymodbus[serial]` + `pyserial` | Modbus RTU 串口主站；串口参数必须由调用者显式指定 |
| `iec104.py` | `c104` | IEC 60870-5-104 主站链路、总召唤、四遥点注册和命令对象 |
| `iec104_server.py` | `c104` | IEC 60870-5-104 RTU 服务端、四遥站点、主动上送和命令回调 |
| `can.py` | `python-can` | 原始 CAN/CAN-FD 帧收发、过滤、周期发送 |
| `bacnet.py` | `bacpypes3` | BACnet/IP 客户端和主站：属性读写、四遥点轮询、遥控遥调 |
| `opc.py` | `asyncua` | OPC UA 客户端和主站：节点读写、四遥点轮询、遥控遥调 |

## Modbus TCP

```python
from api.modbus_tcp import ModbusTcpMaster

with ModbusTcpMaster("127.0.0.1", port=1502, unit_id=1, rtu_id=7) as master:
    registers = master.read_holding_registers(0, 2)
    master.write_single_coil(0, True)
```

地址使用线上零基地址。旧 `scdfe_c/CRtuModbusTcpEx.cpp` 的 `irtuaddr` 是从站地址，`rtu_id` 只用于点表筛选。

`ModbusTcpMaster` 是面向主站任务的入口，支持配置驱动采集和周期轮询：

```python
from api.modbus_tcp import ModbusTcpMaster

master = ModbusTcpMaster.from_channel(
    {"rtu_id": 7, "remote_host": "127.0.0.1", "remote_port": 1502,
     "irtuaddr": 1},
)
master.configure(modbus_calls, modbus_points, ytyk_definitions)
with master:
    values = master.collect_once()
    master.send_yk_point(1, True)
    master.send_yt_point(3, 12.5)
```

如果使用本项目的 `ScadaStore`，可以一次加载合并后的 RTU/通道记录、Modbus 召唤表、
数据点表和遥控遥调定义表。以下 RTU 7 为示例，需先按实际设备填写配置：

```python
from api.modbus_tcp import ModbusTcpMaster
from web.service import ScadaStore

master = ModbusTcpMaster.from_store(ScadaStore(), rtu_id=7)
with master:
    values = master.collect_once()
```

配置在一个只读事务中完整加载，不受 Web 列表每页500条限制。
RTU 的 `channel_type` 必须为 `modbus-tcp`；其他通道类型会被拒绝。
当前数据库约定每个 RTU 只有一个通道和一个 Modbus 参数定义；多个从站通过点表召唤行中的
`irtuaddr` 明确指定。

调用方提供的 `modbus_calls` / `modbus_points` 最小示例：

```python
modbus_calls = [dict(rtu_id=7, call_seq=0, irtuaddr=1,
                    iplace=0, inum=2, icode=3)]
modbus_points = [dict(rtu_id=7, call_seq=0, cnt=1, reg_addr=0,
                     trans_type=5, fourbytedata_byteseq=3, if_yx=0)]
ytyk_definitions = [
    dict(rtu_id=7, cnt=1, ifyk=1, irtuaddr=1, reg_addr=0, icode=5),
    dict(rtu_id=7, cnt=3, ifyk=0, irtuaddr=1, reg_addr=2, icode=16,
         ytyk_type=5, fourbytedata_byteseq=3),
]
```

这会把保持寄存器0和1解码为 ABCD 字节序的 float32，返回遥测点1。
`cnt` 对应四遥表的 `pnt_no`，`if_yx=1/0` 对应遥信/遥测。
配置会复制输入行并筛选当前 RTU，校验失败时保留上一次有效配置。
遥控和遥调允许点号相同；同一种类型重复点号则报错。

配置在一个只读事务中完整加载，不受 Web 列表每页500条限制。TCP/RTU 的协议专用参数分别从
`fes_channel_modbus_tcp_define` / `fes_channel_modbus_rtu_define` 读取；RTU 表只保存站点和通道标识，
连接地址、端口或串口名必须配置在对应协议参数表中。

`collect_once()` 使用 `fes_channel_modbus_define_call` 和 `fes_channel_modbus_define_data` 的
快照返回 `ModbusValue`，不会自动改写数据库。需要持续采集时传入回调和停止事件：

```python
import threading

stop_event = threading.Event()
with master:
    master.poll_loop(print, interval=1.0, stop_event=stop_event, max_cycles=10)
```

每轮采集与回调完成后等待 `interval` 秒。调用线程会阻塞至停止事件、指定次数
或异常，应用可自行放入工作线程；等待时可立即停止，进行中的请求受通信
`timeout` 限制。通信或回调异常会抛出，调用方可记录告警和安排下一次连接。

主站 API 不自动扫描从站、不自动重连、不自动重发写命令；连接、异常和重试策略
由部署层明确控制。`irtuaddr` 必须是1..247的从站地址，TCP 目标由协议参数表中的
`remote_host`/`remote_port` 决定。

功能码支持01/02/03/04读取、05/06单点写入、0F/10批量写入，使用标准 MBAP
Modbus TCP 帧。特殊寄存器位遥控通过 `send_yk_point(..., current_value=...)`
显式提供当前值；跨设备读改写不具备原子性。

```python
values = master.collect_once(calls, points)
master.send_yt_point(3, 12.5)
master.send_yk_point(1, True)
```

`collect_once` 不写数据库，返回 `ModbusValue`；失败会抛出 `ModbusError`，不会把无效值填成 0。特殊位遥控的读改写不是设备侧原子操作，调用者需要保证 `current_value` 的新鲜度。

## Modbus RTU

```python
from api.modbus_rtu import ModbusRtuMaster

with ModbusRtuMaster("COM3", baudrate=9600, bytesize=8,
                     parity="E", stopbits=1, unit_id=1) as master:
    value = master.read_input_registers(0, 1)
```

`ModbusRtuMaster` 与 `ModbusTcpMaster` 共用 `configure`、`collect_once`、
`poll_loop`、`send_yk_point` 和 `send_yt_point`。`from_store` 从合并后的 RTU/通道记录、
Modbus 召唤表和数据表加载配置，并要求 RTU `channel_type=modbus-rtu`。
协议专用表中的 `serial_port`（可为 `COM3` 或 `socket://host:port`）、`baudrate`、`bytesize`、`parity`、`stopbits`、`unit_id`、
`response_timeout` 会覆盖 RTU 通用记录的缺省值；直接构造时串口参数仍需显式给出。

```python
master = ModbusRtuMaster.from_store(
    ScadaStore(), rtu_id=7, baudrate=19200, bytesize=8,
    parity="O", stopbits=2, timeout=1.0,
)
with master:
    values = master.collect_once()
```

一个物理 RS-485 总线使用一个主站实例；每个召唤行的 `irtuaddr` 可访问不同
从站。RTU 帧和 CRC 由 `pymodbus` 处理，连接不会自动扫描、重连或重发写命令。

## IEC104 主站

```python
from api import IEC104Master

with IEC104Master("10.0.0.20", common_address=1,
                  ycbegin_addr=0x4001, yxbegin_addr=0x01,
                  ykbegin_addr=0x6001, ytbegin_addr=0x6201) as master:
    master.configure([
        {"kind": "yc", "pnt_no": 1, "data_type": 1},
        {"kind": "yx", "pnt_no": 1, "data_type": 1},
    ])
    master.general_interrogation()
    current = master.collect_once()
```

点号到信息体地址沿用旧工程的 `start + point_no - 1`。`data_type=1/2/3` 对应 IEC104 浮点/标度/归一化配置，遥信 `1/2` 对应单点/双点。`c104` 自己的后台线程负责接收；`collect_once()` 只读取已接收的点，不阻塞等待某个报文。
`IEC104Master` 同时提供 `from_channel`、`from_store`、`collect_once`、`poll_loop`、
`send_yk_point` 和 `send_yt_point`。`from_store` 读取当前 `fes_channel_iec104_define`
参数表，四遥点的 IEC104 类型可通过 `point_map` 显式指定。

## CAN / CAN-FD

```python
from api.can import CanMaster

with CanMaster(interface="pcan", channel="PCAN_USBBUS1", bitrate=500000) as master:
    master.send(0x123, b"\x01\x02")
    frame = master.recv(timeout=1.0)
```

`CanMaster.request` 提供显式请求/应答事务：

```python
response = master.request(
    0x100, b"\x02", response_id=0x180, timeout=0.5,
    matcher=lambda frame: frame.data[:1] == b"\x02",
)
```

超时抛出 `CanTimeout`，不自动重发；未匹配帧会保留在主站接收队列中，之后
仍可由 `recv` 或 `collect_once` 读取。`poll_loop` 提供批量接收回调和停止事件，
`send_periodic` 提供 python-can 的周期发送任务。`CanMaster` 不假设 CANopen、
J1939 或其他应用层协议，应用层 ID、字节序、信号缩放和请求序号由设备协议配置。
调试可使用 `interface="virtual"`；现场使用 `socketcan`、`pcan`、`vector`、
`kvaser` 等 python-can 支持的接口。CAN-FD 使用 `fd=True`，最多 64 字节。
从 `ScadaStore` 构造时使用 `CanMaster.from_store(store, rtu_id)`，接口、通道、
仲裁波特率和 CAN-FD 参数由当前 `fes_channel_can_define` 读取；CAN 原始帧没有统一的四遥点表，
请求/应答的帧 ID 和数据布局由调用方显式指定。

## IEC104 服务端

服务端采用 `c104.Server`，只在显式调用 `start()` 或进入 `with` 代码块时监听端口。下面示例创建一个公共地址为 1 的站点，注册遥信、遥测和遥控，并处理遥控回调：

```python
from api.iec104_server import IEC104Server

def on_yk(command):
    print(command.kind, command.point_no, command.value,
          "select=" + str(command.selected))
    return True  # 返回 False 或 c104.ResponseState.FAILURE 会拒绝命令

server = IEC104Server(ip="0.0.0.0", port=2404, common_address=1)
server.add_point("yx", 1, value=False)
server.add_point("yc", 1, data_type=1, value=12.5)
server.add_point("yk", 1, value=False, command_handler=on_yk)

with server:
    server.update("yx", 1, True)       # 按遥信点配置主动上送
    server.update("yc", 1, 13.0)       # 按遥测点配置主动上送
```

服务端 API 的主要方法为：

- `add_point(kind, point_no, ...)`：注册 `yx`、`yc`、`yk`、`yt` 点；`data_type` 沿用客户端 API 的 1/2/3 映射。
- `update(kind, point_no, value, cause="SPONTANEOUS")`：更新并主动上送遥信或遥测。
- `transmit_many(kind, {point_no: value})`：使用 `c104.Batch` 批量上送同一类型的信息体。
- `set_command_handler("yk" 或 "yt", point_no, handler)`：注册命令回调，回调参数为 `IEC104Command`。
- `signal_initialized()`：发送站点初始化完成通知。

`IEC104Command` 包含点类型、点号、值、选择/执行标志、传送原因、源地址和接收时间。命令回调异常会被转换为 IEC104 失败响应，并保存在 `last_error` 中，避免异常逃逸到 c104 后台线程。

## BACnet/IP 主站

BACnet 使用 `bacpypes3` 的 BACnet/IP 栈。对象类型和实例号必须明确配置，不能
仅凭中文名称猜测对象：

```python
from api import BacnetMaster

points = [
    {"rtu_id": 7, "kind": "yc", "pnt_no": 1,
     "object_type": "analog-value", "instance": 1},
    {"rtu_id": 7, "kind": "yx", "pnt_no": 1,
     "object_type": "binary-input", "instance": 2},
    {"rtu_id": 7, "kind": "yk", "pnt_no": 1,
     "object_type": "binary-output", "instance": 3},
]
master = BacnetMaster("10.0.0.20", remote_port=47808,
                      local_ip="0.0.0.0", local_port=47809,
                      device_id=1001, rtu_id=7)
master.configure(points)
with master:
    values = master.collect_once()
    master.send_yk_point(1, True)
```

`read_property`/`write_property` 可直接访问任意标准对象属性；`collect_once` 返回
`BacnetValue`，`poll_loop` 的回调在调用线程中执行。`send_yk_point` 和
`send_yt_point` 按配置点号选择对象并写入 `presentValue`。从 `ScadaStore` 构造时，
使用 `BacnetMaster.from_store(store, rtu_id, point_map=...)`，例如：

```python
point_map = {
    ("yc", 1): ("analog-value", 1),
    ("yx", 1): ("binary-input", 2),
}
master = BacnetMaster.from_store(ScadaStore(), 7, point_map=point_map)
```

`fes_channel_bacnet_define` 保存本端绑定地址、远端设备实例号和 BACnet 通道；现有四遥
表没有 BACnet 对象类型/实例字段，因此 `point_map` 是必要的显式映射。BACnet
设备实例号用于配置校验和设备语义，实际 BACnet/IP 请求同时需要远端 IP 地址。

## OPC UA 主站

OPC 主站 API 使用 `asyncua` 实现 OPC UA。endpoint 必须是 `opc.tcp://...`、
`opc.https://...` 或 `https://...`，点表使用完整 NodeId：

```python
from api import OpcUaMaster

points = [
    {"rtu_id": 7, "kind": "yc", "pnt_no": 1,
     "node_id": "ns=2;s=YC_001"},
    {"rtu_id": 7, "kind": "yk", "pnt_no": 1,
     "node_id": "ns=2;s=YK_001"},
]
master = OpcUaMaster("opc.tcp://10.0.0.30:4840", rtu_id=7,
                     username="user", password="password")
master.configure(points)
with master:
    values = master.collect_once()
    master.send_yk_point(1, True)
```

`read_node`/`write_node` 访问单个 UA 节点，`read_node_data` 可同时取得值和状态码，
`read_nodes`/`write_nodes` 访问多个节点，`collect_once` 返回 `OpcValue`（含状态码和 UTC 接收时间），并提供与其他
主站一致的 `poll_loop`、`send_yk_point`、`send_yt_point`。从数据库构造时可用：

```python
master = OpcUaMaster.from_store(
    ScadaStore(), 7,
    point_map={("yc", 1): "ns=2;s=YC_001", ("yk", 1): "ns=2;s=YK_001"},
)
```

旧 `scdfe_c` 的 `opc_name` 是 OPC DA/COM ProgID；本 API 不把 ProgID 冒充 OPC UA。
传入 `Matrikon.OPC.Simulation.1` 等 DA 名称会直接报错。若必须连接 OPC DA，需
另行增加 Windows COM 后端并单独验证其位数、DCOM 权限和服务器 ProgID。

## 来源与边界

地址默认值、Modbus 类型/字节序、IEC104 四遥起始地址和命令类型来自 `D:\OLD_D\ems\platform\source\scada\app\scdfe_c` 与 `D:\codex\scada\dbdef`。目前已完成库适配、离线模拟和本机 Modbus TCP、Modbus RTU、CAN virtual、IEC104、BACnet/IP、OPC UA 回环通信验证；这些回环验证在当前 Windows 环境完成，Linux 分支已通过静态检查和平台模拟测试，尚未在本机 Linux 内核上现场执行。实际设备的从站地址、串口参数、CAN ID、BACnet 对象、OPC UA NodeId 和安全策略仍需现场核对。旧工程 OPC DA/COM 尚未由本 API 实现。
