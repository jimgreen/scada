# RTU、通道及四遥测试数据

项目中的协议测试数据由一个脚本一次性生成：

```powershell
Set-Location D:\codex\scada
\.venv\Scripts\python.exe -m web.seed_protocol_test_data --db D:\codex\scada\db\scada.sqlite3
```

脚本一次性建立 IEC104、Modbus TCP、Modbus RTU、CAN、BACnet/IP 和 OPC UA 测试站，并只增加缺失的测试记录；重复执行会保留已有值、名称和版本号，不覆盖用户修改。

## 最终数据规模

每个协议测试站均配置 **1000 个遥测（YC）、1000 个遥信（YX）、200 个遥控（YK）和 200 个遥调（YT）**，为原规模的 10 倍：

| 通信规约 | 测试站标识 | RTU | 通道/参数表 |
| --- | --- | ---: | --- |
| IEC104 | `TEST_IEC104_RTU` | 2 | `fes_channel_iec104_define` |
| Modbus TCP | `TEST_PROTO_MODBUS_TCP_RTU` | 3 | `fes_channel_modbus_tcp_define`、Modbus 点表 |
| Modbus RTU | `TEST_PROTO_MODBUS_RTU_RTU` | 4 | `fes_channel_modbus_rtu_define`、Modbus 点表 |
| CAN | `TEST_PROTO_CAN_RTU` | 5 | `fes_channel_can_define` |
| BACnet/IP | `TEST_PROTO_BACNET_RTU` | 6 | `fes_channel_bacnet_define` |
| OPC UA | `TEST_PROTO_OPC_RTU` | 7 | `fes_channel_opc_define` |

RTU 1“浏览器站”保留为原有业务数据，不作为协议测试站。六个协议测试站合计新增 6000 个 YC、6000 个 YX、1200 个 YK 和 1200 个 YT；连同 RTU 1 的原有数据，实际总数以数据库查询为准。每个协议测试站的四遥点使用该 RTU 的联合主键 `(rtu_id, pnt_no)`；前 200 个遥信具备对应遥控关联，前 200 个遥测具备对应遥调关联，其余点用于批量采集、分页和边界测试。

RTU 表中的通信线程控制字段已同步迁移：现有站点默认 `process_enable=1`、`process_restart=0`，运行状态字段初始为停止状态。通过 [scdfe_c.py](../scdfe_c.py) 可按 RTU 表中的启停/重启标志控制每个 RTU-通道通信线程，并回写宿主 PID、状态、启动/停止/重启时间、心跳、退出结果、重启次数和错误信息。

## 通道和协议数据

- IEC104 参数行包含公共地址及 YC/YX/YK/YT 起始 IOA，覆盖 2400 个四遥点。
- Modbus TCP 和 Modbus RTU 各生成 2 个召唤定义、2000 个数据解析点和 400 个遥控/遥调命令定义。
- CAN 测试站保存虚拟接口、仲裁波特率、CAN-FD 和过滤相关参数，四遥数据用于站点和点表联调；原始 CAN 帧的 ID、字节布局和信号缩放由应用协议显式定义。
- BACnet/IP 保存本地绑定地址、端口和设备号；BACnet 对象类型和实例号通过 `BacnetMaster.from_store(..., point_map=...)` 显式映射。
- OPC UA 保存 `opc.tcp://127.0.0.1:4840`、刷新周期和登录字段；NodeId 通过 `OpcUaMaster.from_store(..., point_map=...)` 显式映射。

数据库中的 BACnet/OPC 四遥表没有新增专用对象字段，因此点映射仍由主站 API 的 `point_map` 提供，避免改动现有四遥表结构和已有业务数据。

## 安全导入和核验

每次真正新增数据前，脚本使用 SQLite backup API 在 `db/backups/` 保存完整数据库快照；所有记录在一个事务中提交，并执行 SQLite 完整性检查和外键检查。没有新增记录时不会生成新备份。

本次实际导入结果和备份路径见 [PROTOCOL_TEST_DATA.md](PROTOCOL_TEST_DATA.md) 以及 [protocol_test_data_report.json](protocol_test_data_report.json)。当前数据库已核验：`PRAGMA integrity_check` 为 `ok`，`PRAGMA foreign_key_check` 无记录。重复运行脚本新增 0 条记录，且不生成新备份。

协议测试数据只用于页面展示、筛选、编辑、点表分页和后续模拟联调；导入本身不会启动协议客户端或服务端。RTU 和通道状态为初始占位值，不能据此认定现场通信已经连接。

## 相关测试

```powershell
Set-Location D:\codex\scada
\.venv\Scripts\python.exe -m unittest discover -s tests
\.venv\Scripts\python.exe -m unittest web.test_server web.test_protocol_seed_data
ruff check api web tests
\.venv\Scripts\python.exe -m compileall -q api web tests
```

协议扩展脚本的专门回归测试位于 [web/test_protocol_seed_data.py](../web/test_protocol_seed_data.py)。
