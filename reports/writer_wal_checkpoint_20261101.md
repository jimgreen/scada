# SQLite WAL 检查点热点优化报告（2026-11-01）

本轮基线为提交 `12db87a`，保持写库队列空闲周期 **100 ms**、事务阈值 **500 个点更新** 和消息合并语义不变，针对上一轮剖析发现的 SQLite 提交热点优化。最终保留的改动只在写库进程专属连接上设置 `PRAGMA wal_autocheckpoint=10000`，其他 Web、通信线程连接不改变默认检查点策略。基准运行采用等价的字面量配置，最终源码将其提取为 `BATCH_WRITER_WAL_AUTOCHECKPOINT` 常量。

## 热点证据

上一轮 60 RTU 剖析中，写库进程有 1201 次提交；SQLite `commit` 累计 2.881 s，`executemany` 累计 4.375 s，队列读取累计 3.174 s。说明参数准备已经不是第一热点，频繁 WAL 检查点和提交固定开销值得单独验证。

本轮在相同 60 RTU / 20 周期输入下重新做 cProfile 对照。剖析只用于定位热点，绝对总耗时不用于性能结论：

| 指标 | 基线 | WAL 检查点优化 | 变化 |
|---|---:|---:|---:|
| `commit` 累计耗时 | 2.375 s | 1.543 s | **减少 35.03%** |
| `commit` 调用次数 | 1201 | 1201 | 语义未改为跨消息合并 |
| `executemany` 累计耗时 | 3.678 s | 4.446 s | 剖析下有波动，不宣称改善 |
| 参数准备累计耗时 | 0.846 s | 0.991 s | 剖析下有波动，不宣称改善 |

优化没有改变 `_version` 递增次数、消息顺序或每次事务中的 SQL 逻辑；只把检查点触发阈值交给写库连接控制。较大的 WAL 阈值会延后检查点，因此部署环境需要监控 WAL 文件占用。

## 无剖析完整链路对照

使用固定随机种子 `20261101`，每站 YC1000、YX1000、YK200、YT200，分别测试 6 和 60 RTU；每站 20 个周期，每周期 2000 个 YC/YX 更新。计时包含通信线程、spawn 写库进程、跨进程队列、SQLite 更新、提交和排空；初始化和最终逐点校验不计时。每种规模、每个版本各运行三次并交替顺序。

| 规模 / 更新数 | 基线三次（ms） | 优化后三次（ms） | 基线中位数 | 优化后中位数 | 耗时变化 | 吞吐变化 |
|---|---|---|---:|---:|---:|---:|
| 6 RTU / 24 万 | 1033.84, 962.78, 972.13 | 973.76, 946.31, 954.07 | 972.13 | 954.07 | **减少 1.86%** | **提高 1.89%** |
| 60 RTU / 240 万 | 9514.58, 9418.63, 9318.86 | 8665.95, 8519.49, 8612.51 | 9418.63 | 8612.51 | **减少 8.56%** | **提高 9.36%** |

12 次运行全部通过：最终原始值、工程值、质量码、时间、版本号、行数、SQLite `integrity_check` 和外键检查；每次写库进程退出码均为 0。优化后每条 2000 点消息序列化大小仍为 55790 字节，没有增加 IPC 消息体积。

## 未采用的候选

曾测试把事务阈值从 500 提高到 5000 或 3000 点，并增加消息顺序执行以维持 `_version`。直接跨消息合并会把 20 个周期的版本号错误压成 16，已拒绝；修正为同一事务内按消息顺序执行后，5000 点在 60 RTU 中位数比基线慢约 11.22%，3000 点也出现明显长尾，因此没有采用事务阈值修改。当前仍为 500 点，保证低延迟和版本语义。

## 回归与复现

- `tests.test_scdfe_c`、`tests.test_scdfe_control`：**51 项通过**；本轮新增 WAL 阈值默认值断言。
- Ruff、Python compileall、`git diff --check` 通过。
- 测试中的故意异常回溯仍是既有异常隔离测试输出，不表示写库失败；测试最终返回成功。
- Windows 已实测；Linux 尚未实机复测；未连接现场设备；正式 `db/scada.sqlite3` 的已有本地修改未纳入基准或提交。

```powershell
Set-Location D:\codex\scada
.\.venv\Scripts\python.exe tools\benchmark_rtu_scale.py --pipeline-only --baseline-ref 12db87a --replicas 1 10 --db-cycles 20 --repeat 3 --seed 20261101 --output reports/writer_wal_checkpoint_compare_20261101.json
.\.venv\Scripts\python.exe tools\benchmark_rtu_scale.py --pipeline-only --baseline-ref 12db87a --replicas 10 --db-cycles 20 --repeat 1 --profile-writer --seed 20261101 --output reports/writer_wal_checkpoint_profile_20261101.json
```

[完整链路原始结果](writer_wal_checkpoint_compare_20261101.json) · [剖析原始结果](writer_wal_checkpoint_profile_20261101.json) · [汇总数据](writer_wal_checkpoint_summary_20261101.json) · [5000 点阈值候选](writer_batch5000_compare_20261101.json) · [3000 点阈值候选](writer_batch3000_compare_20261101.json)
