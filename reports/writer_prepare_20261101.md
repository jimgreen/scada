# 写库参数准备优化效率报告（2026-11-01）

本轮在 `D:\codex\scada` 对批量写库热路径继续优化：单条消息、规范主键顺序且无重复点的快路径中，按表和主键字段组缓存已见键集合；逐点不再构造包含表名和字段名的重复 token，也不把已经是 tuple 的参数再次复制。异常边界同时保证停止消息在错误日志写入失败时仍被 `task_done()` 确认。

## CPU 参数准备对照

基线为提交 `3aca070`，只比较 `_prepare_batch_updates`；输入为同一份随机 1000 YC + 1000 YX 批消息，700 次循环，五轮交替顺序。SQL、参数、输入消息逐次核对一致。

| 指标 | 基线 | 优化后 | 变化 |
|---|---:|---:|---:|
| 5 轮中位数耗时 | 310.68 ms | 208.95 ms | 32.74% |
| 处理吞吐 | 4,506,211 点/秒 | 6,700,168 点/秒 | 48.69% |

该结果只测参数校验、合并和分组，不包含 SQLite、IPC 或提交；它证明优化减少了 CPU 准备工作，但不能单独代表现场端到端延迟。

## 6/60 RTU 链路对照

每站 1000 YC + 1000 YX，每站 20 周期，随机输入固定种子，真实 spawn 写库进程、IPC、SQLite 提交和排空均计时；每次逐点核验原始值、工程值、质量码、`_version=21`、行数、完整性和外键。

| 规模 | 基线 3 次（秒） | 优化后 3 次（秒） | 中位数变化 |
|---|---|---|---:|
| 6 RTU | 1.965, 5.335, 3.718 | 2.012, 5.085, 5.802 | -36.78% |
| 60 RTU | 43.803, 50.958, 11.530 | 48.592, 55.907, 10.640 | -10.93% |

本机链路计时受 Windows 调度和并发负载影响明显，单次差异远大于本轮微优化；6 RTU 中位数约慢 36.8%，60 RTU 中位数约慢 10.9%。因此本轮不宣称端到端吞吐改善，也不把异常单次结果作为性能保证。稳定可复现的收益仅限 CPU 参数准备阶段。

消息结构和原始/工程值语义未改变：每条 2000 点消息仍为 55771 字节，YC 原始值和工程值分别传输。

## 验证

- 参数准备：SQL 和参数相同，输入消息未被修改。
- 程序测试 104 项通过；之前的 Web 22 项回归已通过。
- Ruff、Python 编译、JavaScript 语法和 `git diff --check` 通过。
- 现场设备未接入；Windows 已实测，Linux 尚未实机复测。

## 复现

```powershell
Set-Location D:\codex\scada
.\.venv\Scripts\python.exe tools/benchmark_writer_prepare.py --baseline-ref 3aca070 --cycles 700 --repeat 5 --output reports/writer_prepare_20261101.json
.\.venv\Scripts\python.exe tools/benchmark_rtu_scale.py --pipeline-only --baseline-ref 3aca070 --replicas 1 10 --db-cycles 20 --repeat 3 --seed 20261101 --output reports/writer_prepare_pipeline_20261101.json
```

[CPU 原始结果](writer_prepare_20261101.json)  ·  [链路原始结果](writer_prepare_pipeline_20261101.json)
