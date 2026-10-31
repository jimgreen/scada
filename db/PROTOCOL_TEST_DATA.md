# 协议 RTU 十倍扩容记录

2026-11-01 在 `D:\codex\scada\db\scada.sqlite3` 完成扩容，保留原浏览器站及 6 个协议站，新增 54 个协议 RTU。最终总 RTU 为 61，协议 RTU 为 60（每种协议 10）。每站四遥为 1000/1000/200/200。

| 数据 | 扩容前 | 新增 | 扩容后 |
| --- | ---: | ---: | ---: |
| 协议 RTU | 6 | 54 | 60 |
| 全部 RTU | 7 | 54 | 61 |
| 遥测 YC | 6000 | 54000 | 60000 |
| 遥信 YX | 6000 | 54000 | 60000 |
| 遥控 YK | 1200 | 10800 | 12000 |
| 遥调 YT | 1200 | 10800 | 12000 |
| 协议参数行（六表合计） | 6 | 54 | 60 |
| Modbus 召唤 | 4 | 176 | 180 |
| Modbus 解析点 | 4000 | 36000 | 40000 |
| Modbus 命令映射 | 800 | 7200 | 8000 |

已逐表对比并保留 19220 条原有行；逐站核验四遥数量。`PRAGMA integrity_check=ok`，`foreign_key_check=[]`。重复执行新增 0，备份为 null。

新增副本站默认停止；参数地址和稳定代码按副本区分。新 Modbus 召唤按 125 寄存器上限分帧。原有 RTU 3、4 的历史 Modbus 范围问题仍保留，详情见 [配置说明](TEST_DATA.md)。

扩容前备份：[scada.sqlite3.before_protocol_test_data.20261101_074816_691282.4e12c747.bak](D:/codex/scada/db/backups/scada.sqlite3.before_protocol_test_data.20261101_074816_691282.4e12c747.bak)。

[机器可读导入报告](protocol_test_data_report.json) 包含每站 RTU ID、协议、四遥计数、备份路径、保留行数量、配置校验结果及重复执行结果。完整复现命令和运行边界见 [测试数据说明](TEST_DATA.md)。
