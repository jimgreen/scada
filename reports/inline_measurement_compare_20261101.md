# 通信线程测量转换热点优化（2026-11-01）

本轮针对 `ChannelWorker._sync_measurements` 的逐点 CPU 热点做优化。基线是当前提交 `ab3c702` 的 `scdfe_c.py`，优化版本由同一工作树直接运行；正式数据库没有参与压测。

## 优化内容

- YC/YX 的质量码转换、YC 原始值到工程值转换和有限值检查在主循环内联，避免每个点调用 `_measurement_state` 及重复属性查找。未知点类型仍调用原校验方法，异常仍按单点隔离。
- 将 `previous[:3] != state` 改为三个元素的直接比较，避免每个已缓存点创建临时切片元组。
- 没有减少点更新、质量码、原始值、工程值、时间戳或版本号写入；消息结构和 `_preserve_versions` 语义不变。

## 热点证据

优化前的生产侧 cProfile（400 个采样周期、每周期 2000 点）显示：`_measurement_state` 累计约 0.58 s，`_measurement` 累计约 0.35 s，二者是逐点整理的主要 Python 热点；写库侧仍主要消耗在 SQLite `executemany` 和 `commit`，因此没有用不稳定的事务阈值改动替代本轮优化。

独立 CPU 微基准使用同一随机点流、同一消息形状，5 次中位数为 818.9 ms → 769.3 ms，耗时降低 6.1%。

## 真实管线对比

负载固定为每站 YC1000、YX1000、YK200、YT200，YC/YX 每周期全部变化，每站 20 个周期；6 RTU 覆盖六种协议各一站，60 RTU 为同一组合复制 10 次。每次运行包含通信线程、spawn 写库进程、队列序列化、SQLite 更新/提交和最终数据校验；每个规模和版本各运行 3 次，取中位数。

| RTU 数 | 指标 | 优化前中位数 | 优化后中位数 | 耗时变化 | 吞吐变化 |
|---:|---|---:|---:|---:|---:|
| 6 | 生产端入队 | 0.664 s | 0.644 s | 2.91% | 361,697 → 372,522 点/s |
| 6 | 完整排空 | 0.961 s | 0.948 s | 1.37% | 249,666 → 253,139 点/s |
| 60 | 生产端入队 | 8.227 s | 8.074 s | 1.85% | 291,732 → 297,235 点/s |
| 60 | 完整排空 | 8.570 s | 8.426 s | 1.69% | 280,036 → 284,838 点/s |

原始数据：[inline_measurement_compare_20261101.json](inline_measurement_compare_20261101.json)。每次运行均报告 `integrity_check=ok`、零外键违规、写库进程退出码 0，并逐行核对最终值、工程值、质量码和 `_version`。

## 验证边界

- 本轮测试为 Windows 10、Python 3.11、SQLite 3.53.1 的本机临时数据库与 IPC 压测；没有连接现场 IEC104、Modbus、CAN、BACnet 或 OPC 设备。
- 6 RTU 和 60 RTU 的中位数均改善，但 SQLite `executemany`/`commit` 仍是总耗时主体；磁盘缓存和线程调度会造成单次波动，表格数值用于同机同负载比较，不作为现场时延保证。
- 正式数据库 `db/scada.sqlite3` 未写入，本轮前后 SHA-256 均为 `554F2170D79C7164D23D0070C16AFCF231B5D21504CCC15D6A79625EF41AAA5D`。

## 复现

```powershell
Set-Location D:\codex\scada
.\.venv\Scripts\python.exe tools\benchmark_rtu_scale.py --pipeline-only --baseline-ref HEAD --replicas 1 10 --db-cycles 20 --repeat 3 --seed 20261101 --output reports\inline_measurement_compare_20261101.json
```
