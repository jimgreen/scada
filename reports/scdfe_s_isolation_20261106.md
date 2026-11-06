# scdfe_s 独立数据库与三端联调验收 · 2026-11-06

正式地址为 `127.0.0.1`。三个 Web 已在原端口运行，S 的 Web、九个协议服务对象及独立规约启动器统一使用 S 专库。C/T 继续使用业务库。文件身份在 SQLite 内部持久化；即使改名或传入自定义路径，S 也拒绝已有的未标记业务库，C/T 拒绝 S 专库。

| 服务 | 原端口 | 已核对的数据库 |
|---|---:|---|
| scdfe_c | 8877 | `D:\codex\scada\db\scada.sqlite3` |
| scdfe_s | 8878 | `D:\codex\scada\db\scdfe_s.sqlite3`；`database_role=simulator` |
| scdfe_t | 8879 | 与 C 相同；原有 IEC 104 通道 1 仍在运行 |

## S 专库初始化

部署以业务库的只读快照为源，复制 62 个有效通道、YC 60,002、YX 60,002、YK 12,002、YT 12,002，以及对应规约参数和点映射。源库 RTU 1 没有通道号、规约或任何关联点，只在源库保留，未写入 S 专库。两库对照：19 张定义表、288,346 行，参与迁移的字段双向差集为空。进程 PID、命令时间戳、待执行命令和 T 转发表没有复制。目标存在时拒绝覆盖；有依赖数据的无通道 RTU 会中止迁移，不会静默丢点。[逐表迁移记录](scdfe_s_isolation_20261106/migration.json) · [现场核对](scdfe_s_isolation_20261106/live.json)

## 正式地址：Web 操作与规约实读

- Playwright 在正式 S 页面新建与 C 既有 RTU 同号的临时站（RTU 1），修改站名，再逐类新建、修改值和名称、删除 YC/YX/YK/YT 点，最后删除测试站。以正式 8877 API 对照，C 的同号记录所有字段及版本（5）均未变，四类测试点从未出现在 C；S 的测试记录已清理。[浏览器结果](scdfe_s_isolation_20261106/browser.json) · [四遥页面](scdfe_s_isolation_20261106/live_points.png)
- 通过 8878 启动既有 Modbus TCP 从站，向 1502、单元 3 发 FC03 并收到两个寄存器的响应，随后恢复原先的停止状态。向正式 T 通道的 2404 端口发送 IEC 104 总召，实收 YC 1 为 42.0，与 8877 查询到的 C 源点一致。T 原有通道进程在 Web 切换前后 PID 未变。[现场结果](scdfe_s_isolation_20261106/live.json)

## 同一组端口：真实端到端回环

在第二个回环地址 `127.0.0.2:8877/8878/8879` 上启动同一代码的三个 Web，分别使用临时 S 专库与 C/T 业务库，不占用正式地址。用相同 RTU 号、通道号和四遥点号验证两库并存，对每一类 S 点进行新增、修改、单点删除及删站级联，并逐字段复查 C 同号记录。再通过真实 Modbus TCP 连接验证 S → 两个 C 采集通道 → T 的 YC/YX，以及反向 YK/YT。T 收到原始值 8，按比例 2、偏移 10 转为 26；等待 C 的命令处理完成，从 S 的寄存器独立回读到 26。涵盖 S 主站单次采集和命令、C 的 Web 和 TCP 命令、两轮启停、按通道报文保存和 RX/TX 日志筛选。两项固定端口验收均通过，隔离测试运行时全部收尾。[原端口完整输出](scdfe_s_isolation_20261106/original_ports.txt)

## 回归与文档

- 项目测试目录 448 项：447 通过，1 项 Windows 符号链接权限测试跳过；Web 与样例库测试另有 36 项全部通过。合计 484 项，483 通过，1 跳过。[项目测试](scdfe_s_isolation_20261106/tests.txt) · [Web 测试](scdfe_s_isolation_20261106/regression.txt) · [迁移与身份保护专项](scdfe_s_isolation_20261106/migration_tests.txt)
- [HTML 使用说明](../docs/scdfe_usage_guide.html)说明两库职责、初始化命令、同号隔离验证、增删改的数据流与真实通信边界。[S 章节页面](scdfe_s_isolation_20261106/manual_isolation.png)

## 2026-11-07 · 手册规约与截图验收

- IEC 104、Modbus TCP、Modbus RTU、CAN/CAN FD、BACnet/IP、OPC UA 各自都有参数示例、点寻址方法及新增、查询、修改、删除步骤。[规约页面](scdfe_s_isolation_20261106/manual_protocol.png)
- 在隔离 C Web 的六个示例站上，逐一创建参数、重新查询、修改、删除并确认结果；随后对六种规约的点映射重复操作。按主键查询已删除的映射返回 404，本地 YC 点仍存在。[六组 CRUD 核对](scdfe_s_isolation_20261106/protocol_crud.json)
- 21 项服务操作及 24 项规约操作各配一张实际 Web 截图，45 张图均已嵌入 HTML。桌面 1280 像素和手机 390 像素下检查章节跳转和宽度；断网后逐张验证解码、放大、原始尺寸和关闭。58 个内部链接可定位，打印视图保留 45 张图。正文和截图不依赖外部资源，规约参考链接供联网查阅。[浏览器结果](scdfe_s_isolation_20261106/browser.json)

本次规约验收使用本机 TCP 对端。串口、物理 CAN 总线和现场设备不属于这次实机验证。
