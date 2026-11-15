# IEC104 纯 Python 实现与部署

自 2026-11-15 起，C/S/T 共用 `core/iec104_python`。协议核心由 Python 标准库的 socket、selectors、threading、struct 实现，不导入 c104、lib60870 或任何自建二进制模块。`client/iec104.py` 与 `server/iec104_server.py` 保留原业务接口；源码内的 `c104` 是新引擎的兼容别名，不是第三方包导入。

## 部署与升级

Windows/Linux 均使用 Python 3.11+，复制项目 Python 源码、Web 静态资源、配置及数据库定义，在实际运行环境执行：

```sh
python -m venv .venv
# Windows: .venv\Scripts\python.exe；Linux: .venv/bin/python
python -m pip install -r requirements.txt
python -c "from core.iec104_python import IMPLEMENTATION; print(IMPLEMENTATION)"
```

最后一条应输出 `python-stdlib`，应在项目根目录并使用实际服务解释器执行。IEC104 无须准备 .cpp/.h、CMake、编译器或平台专用 wheel。其他协议的设备驱动依然按设备要求安装，例如 CAN 驱动与串口权限。`vendor/c104` 只保留历史构建及性能复现资料，不参与新安装；已装的旧包也不会被运行代码使用。

升级前记录运行状态，正常停止 C 采集/写库/指令进程以及运行中的 S/T 协议服务，再重启 Web 并恢复原运行项。仅重启 Web 无法替换独立 C/T 进程已经加载的代码。可用 `SCADA_PYTHON` 指定子进程解释器。各端 `/api/protocol-capabilities` 应返回 IEC104 的 `implementation=python-stdlib`、`native_dependency=false`。

S 保留独立模拟库；C/T 共享业务库。HMI 编辑只写数据库，S 自行扫描、同步并上送。原有四遥字段、命令队列、SOE 事件和确认游标、连接历史及报文查询保持原契约。

## 实现范围与确认语义

- TCP 连接与重连、STARTDT/STOPDT/TESTFR、I/S/U 帧、15 位序号回绕、k/w 窗口、t1/t2/t3、分包/粘包和异常连接关闭。解析出多个目标地址时轮换重试。C 的 `test_fr()` 可主动执行 U 帧测试。
- 总召 ACTCON → 数据 → ACTTERM、周期和变位上送、质量变化、单点/双点/步位置及归一化/标度/浮点遥测；按 CA/类型批量编码并限制 ASDU 大小。当前响应站总召 QOI=20；没有配置分组总召映射。
- 遥控/遥调直接执行及选择后执行、撤销、限定词、命令确认和终止；选择绑定连接、CA/IOA、类型、OA、命令值及限定词并超时。失败返回否定确认，不自动重放命令；T 确认表示共享库接收命令，不等于现场已执行。
- CP24 与 CP56 类型。CP56 按 UTC 日历编码；CP24 按接收时刻最近的 UTC 小时补全，不能唯一恢复跨小时历史。控制命令不用 CP24。
- SOE CP56 单点/双点逐条保存，C 接收落库后才允许发出链路确认；S/T 等批次目标连接全部 APCI 确认后推进游标。连接断开或 STOPDT 时不推进未确认批次。整体为至少一次交付，确认与游标提交间崩溃仍可能重发；第三方接收端的 APCI 确认不证明其业务数据库已提交。
- 每个 Server/Client 一个 selector 网络线程，共享处理其连接；发送队列上限每连接 4 MiB，k 窗口防止无限在途发送。连接观察队列上限 32768 条，异步批量入库；日志溢出报告计数，不宣称观察日志无损。

## 验证方法

```sh
python -m pytest tests/test_iec104_python_wire.py tests/test_iec104_acceptance.py tests/test_iec104_command_contracts.py tests/test_iec104_soe_pipeline.py tests/test_connection_history.py -q
python tools/verify_iec104_pipeline.py --channels 1 4 --points 32 --rounds 20 --skip-comparison --output output/iec104-python-pipeline
python tools/benchmark_iec104_soe.py --events 128 --channels 1 4 --skip-db --output output/iec104-python-soe.json
```

独立报文测试覆盖时标字节、窗口阻塞/恢复、序号回绕、异常报文、超时、ASDU 分批；包含 `python -S` 子进程（禁用第三方包加载）的真实建链/总召。完整流水线使用临时 S 库、C/T 共享临时库及 TCP 端口，核对总召、四遥、质量、撤销、断线重连与周期上送。可选 `tests/test_iec104_reference_interop.py` 用已安装的旧 c104 进行双向互通；未安装时仅跳过这一参考库对照测试。

这是一份项目接口实现，未承诺完整 IEC104 标准认证或第三方 c104 的所有 API。当前实测环境为 Windows/Python 3.11；Linux 实机、物理设备与长期稳定性仍需各部署环境验收。

## 2026-11-15 SOE 对比

相同本机 TCP 和临时数据库负载，每通道连续提交 128 条 SOE，交替运行旧引擎与新引擎各三轮；统计从 S 事务提交到 C 落库、T 转发并取得下游 APCI 确认的耗时。两组全部逐条核对状态、质量和 CP56 原始字节一致。

| 通道数 / 总事件数 | 旧 c104 中位耗时 | 纯 Python 中位耗时 | 耗时变化 |
| --- | ---: | ---: | ---: |
| 1 / 128 | 146.348 ms | 147.010 ms | 增加 0.45%，基本持平 |
| 4 / 512 | 752.658 ms | 530.621 ms | 降低 29.50% |

对照代码来自提交 `5ed8168`，旧引擎为 `c104 2.2.1+scada.4`。批次、窗口与定时参数一致；期间原有工作区服务仍在运行，短样本受调度和数据库负载影响。这些数据衡量完整 SOE 链路，不等同纯报文编码速度，也不代表生产最大容量或长期稳定性。原始结果位于 `output/iec104_python_20261115/soe-{native,python}-{1,2,3}.json`，汇总为同目录 `comparison.json`。
