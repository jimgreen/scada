# scdfe_d 设备量测

`scdfe_d` 读取 `scdfe_c` 采集业务库的遥测、遥信点，将数据关联到设备属性。
源业务库只读；设备定义、映射及量测结果写入独立的 `scdfe_d.sqlite3`。

## 设备表

按设备模型库的表名创建 SQLite **实体表**，例如 `ACGenerator`、`DCGenerator`、
`ACLoad`、`DCLoad`、`ACBreak`、`DCBreak`、`ACBranch`、`DCBranch`、`ACNode`、
`DCNode`、`ACStorageGen`、`DCStorageGen`、`DCACConverter`、`DCDCConverter`。

每张设备表只保留 `idx`、`name` 和量测字段，不复制额定容量、连接节点、控制方式等模型参数。
模型扩展表保留自己的 `idx`；没有 `name` 的发电机扩展表使用其关联发电机的名称。
通用功率、电压、电流等量测放在所属发电机基础表。SOC、风速、组件温度等专属量测只放在对应扩展表，同一映射落在一张表。

以有功量测为例：

| 字段 | 含义 |
| --- | --- |
| `p` | 当前采集值，经比例和偏移换算 |
| `p_qual` | 源质量字；不可用时附加无效位 `0x80` |
| `p_pre` | 上一次源采样的量测值；首次采集为空；重复轮询同一源时标/值不推进 |
| `p_time` | 源时标，Unix 毫秒，不用本次轮询时间替代 |
| `p_notuse` | 人工禁用标记，0 使用、1 禁用 |
| `p_weight` | 正数权重 |
| `p_rtu` | 源 RTU 编号 |
| `p_pnt` | 源点号 |

其他属性使用独立的同样 8 个字段。储能包括 `soc`；变流器保留 `ac_`/`dc_` 或
`i_`/`j_` 各侧功率、电压、电流属性。现有目录的电压属性沿用 `u`、`uab` 等名称。
遥信的位置、运行、故障等属性同样有各自的 8 个字段。

`device_measurement.kind` 区分 YC/YX，`scada_point_mapping.source_database` 记录源库，
因此相同 RTU/点号的遥测和遥信不会混用。Web 保存映射会同步更新实体表中的
`*_rtu`、`*_pnt`、`*_notuse`、`*_weight`；这些列通过 SQLite 触发器与映射定义联动，
直接 SQL 修改也会参与后续采集。更换来源后清除旧来源的值和前次值。

`device`、`device_measurement`、`scada_point_mapping` 等是内部管理表，负责类型、单位、
来源、比例偏移、超时、版本和修改记录。RTDB 内置目录只登记设备量测表。

## 启动

在 `D:\codex\scada` 运行：

```powershell
.venv\Scripts\python.exe -B -m scdfe_d.web.server --port 8980 --workspace-root D:\codex\workspace --db realtime/qinling/sqldb/scada/scdfe_d.sqlite3 --collect
```

访问 `http://127.0.0.1:8980/`。省略 `--collect` 时只启动 Web，在页面启动采集。
在 Web 中确认数据库后，重启时省略 `--db` 即可沿用已保存的数据库。
默认采集周期为 5 秒。可在「设备采集」页输入 1–60 的整数并保存。周期写入当前设备库，运行中调整会重新安排下一批，重启后继续沿用。独立后台也会读取已保存的周期。

```powershell
.venv\Scripts\python.exe -B -m scdfe_d --workspace-root D:\codex\workspace --db realtime/qinling/sqldb/scada/scdfe_d.sqlite3
```

追加 `--interval 5` 可在启动时指定并保存周期。追加 `--once` 仅采集一次。同一设备库只允许一个采集实例。

Web 的「量测定义」页可按设备和名称查找量测，浏览源库并新增、修改或删除点表映射。
「量测查询」按设备表和设备查看实时值、上次采样值、源时标与质量。
「参数配置」可对同一设备批量保存量测启用状态、人工禁用、权重和超时阈值。
切换这三个页面或刷新时保留当前设备。参数页可勾选量测并批量设置，使用「仅看待保存」核对草稿后整批保存。
数据库选择器支持新建目录和空设备库；所有 Web 文件路径相对于配置的工作区根目录，
禁止越界。采集中不能切换设备库或导入模型。

首次导入模型可追加：

```text
--model realtime/qinling/sqldb/model/devices.sqlite3
```

导入保留模型设备标识和已有映射，不将模型设定值当作实时量测。新设备需在映射页
添加属性和源点。秦岭现有库已保留 1,839 个 YC/YX 映射。氢系统模型设备预建了压力、流量、运行等量测字段组，当前源点未绑定，量测为 NULL 并带无效质量。

## 数据语义与接口

每轮从 `scdfe_c` 只读取得源点快照，在同一事务内批量更新量测记录和发生变化的设备字段。未变化的设备字段保持原值，采集批次照常记录。
周期任务复用自己的 SQLite 写连接，每批提交后释放事务。停止采集时关闭连接，单批失败后回滚并重连。
质量异常、过时、无时标、未来时标、源库不可用、缺失点、停用映射和人工禁用都
显示不可用原因。源丢失时保留最后读到的值和时标，同时置无效位；消费方应检查质量。
本模块保存当前值和前次值，不承担 SOE 事件历史。

- `GET /api/overview`：采集状态、计数及最近批次。
- `GET /api/device-tables`：设备实体表目录。
- `GET /api/device-catalog`：界面初始化所需的设备、所属设备表、类型和源库。
- `GET /api/devices`：设备与所属表。
- `GET /api/devices/{id}/values`：该设备基础表的完整 8 字段量测记录。
- `GET /api/device-measurements?table=ACGenerator&idx=1`：按设备表及模型索引查看量测和实时值。
  实时轮询可追加 `view=live`，响应只包含查询页需要的字段。
- `PUT /api/device-parameters`：按 `table` 和 `idx` 一次保存该设备的量测参数。`items` 中每项携带 `measurement_id`、`version`、`enabled`、`notuse`、`weight`，可选 `max_age_seconds`。
- `GET /api/measurements?table=ACGenerator&kind=yc&q=p`：分页属性及映射。
- `POST /api/measurements`、`PUT/DELETE /api/measurements/{id}`：属性映射维护；更新、删除要求版本号。
- `GET /api/source-points?source_database=...&kind=yc&q=SOC`：只读源点搜索。
- `POST /api/collector/start`、`stop`、`once`：采集控制。
- `PUT /api/collector/interval`：保存周期，请求体如 `{"interval_seconds":5}`。
- `POST /api/model/import`：请求体 `{"path":"根目录相对路径"}`。
- `GET /api/history`：最近 100 个采集批次和修改记录。

采集批次保留最近约 1,000 批，仍被当前值引用的批次不会删除。

## 验证

```powershell
.venv\Scripts\python.exe -B -m unittest tests.test_scdfe_d tests.test_web_database_selection tests.test_web_file_root -q
```

测试覆盖前次值、YC/YX 区分、质量/超时/源库丢失、比例权重、映射版本、独占采集、
模型分类实体表及扩展索引、映射字段驱动采集、根目录限制、数据库校验与切换。
