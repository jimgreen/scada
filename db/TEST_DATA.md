# RTU、通道及四遥测试数据

本次把 **6 个协议测试 RTU 扩为 60 个**：IEC104、Modbus TCP、Modbus RTU、CAN、BACnet/IP、OPC UA 各 10 个。保留 RTU 1 浏览器站，正式数据库总记录数为 **61**。该扩容口径针对参与通信的协议测试站，不复制浏览器站。

每站保持 YC1000、YX1000、YK200、YT200；全库合计 YC60000、YX60000、YK12000、YT12000，共 144000 个四遥点。

| 通信规约 | 站数 | 当前本地 RTU ID |
| --- | ---: | --- |
| iec104 | 10 | 2, 8, 9, 10, 11, 12, 13, 14, 15, 16 |
| modbus-tcp | 10 | 3, 17, 18, 19, 20, 21, 22, 23, 24, 25 |
| modbus-rtu | 10 | 4, 26, 27, 28, 29, 30, 31, 32, 33, 34 |
| can | 10 | 5, 35, 36, 37, 38, 39, 40, 41, 42, 43 |
| bacnet | 10 | 6, 44, 45, 46, 47, 48, 49, 50, 51, 52 |
| opc | 10 | 7, 53, 54, 55, 56, 57, 58, 59, 60, 61 |

## 导入和重复执行

```powershell
Set-Location D:\codex\scada
.\.venv\Scripts\python.exe -m web.seed_protocol_test_data --db D:\codex\scada\db\scada.sqlite3 --replicas 10
```

`--replicas` 表示每种协议的目标站数，默认 10。指定较小值只补齐相应目标站，不删除既有站。`--replicas 1` 可在空临时数据库生成原规模的 6 个站。新 RTU 从当前最大 RTU ID 和 channel_id 后分配；用稳定 `code`（原站代码加 `_R02`…`_R10`）复用已有行。参数、四遥和 Modbus 映射代码也携带副本编号。

插入前使用 SQLite backup API 创建完整快照，在一个事务内提交并验证外键和完整性。已有行的值、定义、控制标志和 `_version` 保持原样；无新增时 `added=0`、`backup=null`。扩容回归对比原有所有表行，并逐站检查数量，不只比较全库总数。

新增 54 个 RTU 默认 `process_enable=0`；准备好对应的模拟服务端或设备后，可从 Web 的 RTU 控制界面启用。原有 6 个站的启停标志保持原样。导入本身不启动服务。

## 协议配置

- 新 Modbus TCP/RTU 站每站 8 个 FC3 召唤（每个 125 个寄存器）和 1 个 FC1 召唤（1000 个线圈），配套 2000 个解析点、400 个遥控/遥调映射；全库 20 个 Modbus 站共 180 个召唤、40000 个解析点和 8000 个命令映射。
- IEC104 每站使用独立本地端口；公共地址和四遥 IOA 按站内配置解释。
- OPC UA 的 RTU 地址与参数表 `opc_name` 使用同一站的端点，副本使用不同本地端口。
- BACnet 的客户端绑定端口与远端端口分别分配，副本之间不重叠。
- CAN 使用独立虚拟通道名；Modbus RTU 的 `loop://` 为测试占位配置。
- BACnet 对象映射、OPC NodeId、CAN 应用层帧映射仍应按目标设备配置。数据导入和配置校验不代表真实设备通信已建立。

原有 RTU 3、4 的 Modbus 召唤范围仍为 100 点，而此前四遥扩容已将解析点增至 1000，构造主站时会报 `reg_addr 必须是 0..99 的整数，实际为 100`。此次保留原有行，未覆盖这些配置；新增 18 个 Modbus 站均通过无网络访问的完整配置校验。正式数据库原站的问题不影响独立基准数据库，其测试配置全部按当前合法批次生成。

当前核验和备份见 [导入报告](protocol_test_data_report.json) 与 [扩容记录](PROTOCOL_TEST_DATA.md)。

## 测试与效率对比

```powershell
.\.venv\Scripts\python.exe -m unittest web.test_protocol_seed_data -q
.\.venv\Scripts\python.exe tools/benchmark_rtu_scale.py --replicas 1 10 --cycles 1000 --db-cycles 20 --command-cycles 1000 --definition-cycles 10 --scan-cycles 1000 --output reports/rtu_scale_20261101.json
```

基准分别建立 6 和 60 个 RTU 的临时数据库；采集同步、命令检查、定义扫描为串行阶段计时；完整写回阶段为每 RTU 一个线程和一个独立 `spawn` 写库进程。所有测量点每周期变化，计时包括队列传输与提交后的排空等待，逐行检查最终值和版本号。测试不会连接真实设备。
