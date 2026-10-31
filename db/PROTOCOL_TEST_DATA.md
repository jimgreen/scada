# 多规约大规模测试数据

运行以下命令可以补充所有规约测试站：

```powershell
Set-Location D:\codex\scada
.\.venv\Scripts\python.exe -m web.seed_protocol_test_data --db D:\codex\scada\db\scada.sqlite3
```

脚本只处理当前协议测试标识记录；已存在的测试站只补齐缺失内容。每个目标规约站均配置：

| 通信规约 | RTU | 参数表 | 四遥规模 |
| --- | --- | --- | --- |
| IEC104 | `TEST_IEC104_RTU`，RTU 2 | `fes_channel_iec104_define` | YC 1000、YX 1000、YK 200、YT 200 |
| Modbus TCP | `TEST_PROTO_MODBUS_TCP_RTU`，RTU 3 | `fes_channel_modbus_tcp_define`、Modbus 点表 | YC 1000、YX 1000、YK 200、YT 200 |
| Modbus RTU | `TEST_PROTO_MODBUS_RTU_RTU`，RTU 4 | `fes_channel_modbus_rtu_define`、Modbus 点表 | YC 1000、YX 1000、YK 200、YT 200 |
| CAN | `TEST_PROTO_CAN_RTU`，RTU 5 | `fes_channel_can_define` | YC 1000、YX 1000、YK 200、YT 200 |
| BACnet/IP | `TEST_PROTO_BACNET_RTU`，RTU 6 | `fes_channel_bacnet_define` | YC 1000、YX 1000、YK 200、YT 200 |
| OPC UA | `TEST_PROTO_OPC_RTU`，RTU 7 | `fes_channel_opc_define` | YC 1000、YX 1000、YK 200、YT 200 |

四遥数据使用各自 RTU 的联合主键 `(rtu_id, pnt_no)`。遥信 1–200 具备对应遥控关联，遥测 1–200 具备对应遥调关联；其余点用于批量采集、分页和点表边界测试。

Modbus TCP/RTU 另外生成：

- 2 个召唤定义，分别覆盖 1000 个保持寄存器和 1000 个线圈；
- 2000 个遥测解析点和 2000 个遥信解析点；
- 每站 200 个遥控、200 个遥调命令定义。

BACnet 点的 `object_type/instance`、OPC UA 点的 `node_id` 不在现有四遥表中，因此仍通过 `BacnetMaster.from_store(..., point_map=...)` 和 `OpcUaMaster.from_store(..., point_map=...)` 显式映射。数据库中的参数行保存绑定地址、设备号和 UA endpoint 等通道配置。

本次扩容导入结果：

- RTU 数量保持 7 个；六个协议测试站补齐至 1000/1000/200/200；
- `fes_yc_define` 总数 6000，`fes_yx_define` 总数 6000；
- `fes_yk_define` 总数 1200，`fes_yt_define` 总数 1200；
- Modbus 解析点总数 4000，遥控/遥调映射总数 800；
- IEC104、Modbus TCP、Modbus RTU、CAN、BACnet/IP、OPC UA 参数表均有测试记录；
- SQLite `integrity_check=ok`，`foreign_key_check` 为空；
- 备份文件：[scada.sqlite3.before_protocol_test_data.20261101_071809_218843.6062a100.bak](D:/codex/scada/db/backups/scada.sqlite3.before_protocol_test_data.20261101_071809_218843.6062a100.bak)。

重复执行不会覆盖已有值，也不会生成新的备份；临时数据库回归测试位于 [web/test_protocol_seed_data.py](D:/codex/scada/web/test_protocol_seed_data.py)。
