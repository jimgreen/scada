# 多规约大规模测试数据

运行以下命令可以补充所有规约测试站：

```powershell
Set-Location D:\codex\scada
.\.venv\Scripts\python.exe -m web.seed_protocol_test_data --db D:\codex\scada\db\scada.sqlite3
```

脚本只处理当前协议测试标识记录；已存在的测试站只补齐缺失内容。每个目标规约站均配置：

| 通信规约 | RTU | 参数表 | 四遥规模 |
| --- | --- | --- | --- |
| IEC104 | `TEST_IEC104_RTU`，RTU 2 | `fes_channel_iec104_define` | YC 100、YX 100、YK 20、YT 20 |
| Modbus TCP | `TEST_PROTO_MODBUS_TCP_RTU`，RTU 3 | `fes_channel_modbus_tcp_define`、Modbus 点表 | YC 100、YX 100、YK 20、YT 20 |
| Modbus RTU | `TEST_PROTO_MODBUS_RTU_RTU`，RTU 4 | `fes_channel_modbus_rtu_define`、Modbus 点表 | YC 100、YX 100、YK 20、YT 20 |
| CAN | `TEST_PROTO_CAN_RTU`，RTU 5 | `fes_channel_can_define` | YC 100、YX 100、YK 20、YT 20 |
| BACnet/IP | `TEST_PROTO_BACNET_RTU`，RTU 6 | `fes_channel_bacnet_define` | YC 100、YX 100、YK 20、YT 20 |
| OPC UA | `TEST_PROTO_OPC_RTU`，RTU 7 | `fes_channel_opc_define` | YC 100、YX 100、YK 20、YT 20 |

四遥数据使用各自 RTU 的联合主键 `(rtu_id, pnt_no)`。遥信 1–20 具备对应遥控关联，遥测 1–20 具备对应遥调关联；其余点用于批量采集、分页和点表边界测试。

Modbus TCP/RTU 另外生成：

- 2 个召唤定义，分别覆盖 100 个保持寄存器和 100 个线圈；
- 200 个遥测解析点和 200 个遥信解析点；
- 每站 20 个遥控、20 个遥调命令定义。

BACnet 点的 `object_type/instance`、OPC UA 点的 `node_id` 不在现有四遥表中，因此仍通过 `BacnetMaster.from_store(..., point_map=...)` 和 `OpcUaMaster.from_store(..., point_map=...)` 显式映射。数据库中的参数行保存绑定地址、设备号和 UA endpoint 等通道配置。

本次实际导入结果：

- 新增 RTU 5 个；IEC104 测试站补齐至 100/100/20/20；
- `fes_yc_define` 总数 600，`fes_yx_define` 总数 600；
- `fes_yk_define` 总数 120，`fes_yt_define` 总数 120；
- IEC104、Modbus TCP、Modbus RTU、CAN、BACnet/IP、OPC UA 参数表均有测试记录；
- SQLite `integrity_check=ok`，`foreign_key_check` 为空；
- 备份文件：[scada.sqlite3.before_protocol_test_data.20261029_073759_960702.b7b54d9f.bak](D:/codex/scada/db/backups/scada.sqlite3.before_protocol_test_data.20261029_073759_960702.b7b54d9f.bak)。

重复执行不会覆盖已有值，也不会生成新的备份；临时数据库回归测试位于 [web/test_protocol_seed_data.py](D:/codex/scada/web/test_protocol_seed_data.py)。
