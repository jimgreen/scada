from html import escape
import json
from pathlib import Path
import statistics

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'reports/scdfe_efficiency_20261112_round5'
data=json.loads((REPORT/'backend.json').read_text(encoding='utf-8'))
validation=json.loads((REPORT/'verification.json').read_text(encoding='utf-8'))
rows=[]
for r,label in zip(data['results'],['200 个源 RTU：批量新增 500 条转发映射','4 万源点：加载所选 RTU 的 200 个源点']):
    rows.append(f"<tr><td>{label}</td><td>{r['before_ms']:.3f} ms</td><td>{r['after_ms']:.3f} ms</td><td>{r['reduction_percent']:.1f}%</td></tr>")
for key,label,unit in [('scan_ms','T 扫描、发送提交及状态落库','ms'),('delivery_ms','T TCP 回环完整接收','ms'),('frames','160 点 IEC104 变化上送','帧'),('apdu_bytes','变化上送 APDU 数据量','B')]:
    a,b=[statistics.median(r[key] for r in data['wire'] if r['variant']==v) for v in ('before','after')]
    display=lambda n:f'{n:.3f}' if unit=='ms' else str(int(n))
    rows.append(f'<tr><td>{label}</td><td>{display(a)} {unit}</td><td>{display(b)} {unit}</td><td>{(1-b/a)*100:.1f}%</td></tr>')
html='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,"><title>SCDFE 第五轮效率验证 · 2026-11-12</title><style>
body{margin:0;background:#eef3f7;color:#183044;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1080px;margin:auto;padding:30px}section,header{background:white;border:1px solid #d6e2ea;border-radius:12px;padding:24px;margin-bottom:20px}h1{font-size:29px;line-height:1.4}h2{font-size:22px}table{border-collapse:collapse;width:100%;table-layout:fixed}td,th{text-align:left;vertical-align:top;border-bottom:1px solid #dce5eb;padding:12px;overflow-wrap:anywhere}th:first-child{width:38%}code,pre{font-family:Consolas,monospace;background:#edf4f7}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:14px}a{color:#006b87}.note{border-left:5px solid #bd721d}.muted{color:#536d80}@media(max-width:650px){main{padding:10px}section,header{padding:14px}td,th{padding:7px;font-size:13px}h1{font-size:24px}}</style></head><body><main>
<header><p class="muted">D:\\codex\\scada · 2026-11-12 · 基线 c1e9509</p><h1>转发通信、多 RTU 映射与交互效率</h1><p>本轮优化 T 的 IEC104 批量变化上送、来自多 RTU 的批量映射写入，以及 C/S/T 的 RTU 和源点选择。T 仍只提供 IEC104 与 Modbus TCP 转发入口。</p></header>
<section><h2>相同负载实测</h2><table><thead><tr><th>场景</th><th>修改前</th><th>修改后</th><th>耗时/数量下降</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table>
<p>数据库对比使用同一临时 SQLite 文件和同样的连接池，前后交替执行各 21 次，取中位数；映射表清理在计时之外。500 条映射的读取查询从 1002 次降至 5 次，仍在单个事务内提交。</p>
<p>源点详情对比保持返回的点列表一致：新界面显式请求 include_rtus=0，省去本次不需要的全局 RTU 点数统计。默认 API 响应仍同时包含 RTU 目录。这里的性能收益适用于详情加载，不表示所有目录查询都变快。</p>
<p>真实 T 转发回环含 80 个源 RTU、160 个监视点（80 遥测、80 遥信，各半含 CP56 时标），使用 c104/lib60870 和本机 TCP，每侧 6 次采样。验证每次所有值和质量到达、ASDU 长度、运行态落库和总召唤。APDU 字节数仅计 COT=3 的 I 帧，不含 TCP/IP 头、确认帧；完整接收延迟仍受驱动调度周期影响。</p></section>
<section><h2>变化与操作方式</h2><ul><li>T 把变化点按 IEC 信息体类型批量发送；坏点和发送失败的类型组单独记录错误，成功点更新转发时间，失败点下轮重试。无对端时更新站内缓存，供后续总召读取。源点缺失时保持上次值并置无效质量。</li><li>批量新增映射只检查固定次数的通道定义，按相同字段批量 INSERT；分块读取新记录，保留自动点号分配、混合四遥类型、输入顺序及版本。任一插入失败会整批回滚。</li><li>C 的重叠 RTU 目录读取合并，保存完成后的读取仍会跟随在旧请求之后。C/S 下拉列表内容未变时保留已有选项和当前选择。</li><li>T 快速切换源 RTU、类型，或者关闭后重新打开编辑器时，迟到响应和旧错误无法覆盖新选择。源点加载期间禁用旧源点，保存前要求当前源点已加载完成。</li></ul>
<pre>GET /api/trans/source-catalog?kind=yc
# 保留原有响应：RTU 目录 + 指定 RTU 的源点
GET /api/trans/source-catalog?kind=yc&amp;rtu_id=7
# 已加载目录的前端只取当前源点
GET /api/trans/source-catalog?kind=yc&amp;rtu_id=7&amp;include_rtus=0</pre></section>
<section><h2>浏览器对比</h2><table><tr><th>场景</th><th>修改前</th><th>修改后</th></tr><tr><td>C 同时读取 RTU 目录 5 次</td><td>5 次请求</td><td>1 次请求</td></tr><tr><td>C / S 相同 RTU 列表刷新</td><td>重建全部选项</td><td>保留选项 DOM</td></tr><tr><td>T 切到 RTU 2 后 RTU 1 响应迟到</td><td>源点列表被旧响应覆盖</td><td>保持 RTU 2 源点</td></tr></table><p>Chromium 加载仓库实际 HTML/JS，受控 API 注入响应乱序、错误和保存请求；测试也验证最新 RTU 和所选源点进入批量保存请求。</p></section>
<section><h2>验证结果与复现</h2><p>'''+escape(validation['summary'])+'''</p><p>'''+escape(validation['frontend_summary'])+'''</p><pre>D:\\anaconda3\\python.exe tools/benchmark_efficiency_round5.py
node tests/frontend_efficiency_round5.cjs
D:\\anaconda3\\python.exe tools/build_efficiency_round5_report.py</pre><p><a href="../reports/scdfe_efficiency_20261112_round5/backend.json">性能原始采样与源码 SHA256</a> · <a href="../reports/scdfe_efficiency_20261112_round5/frontend.json">浏览器对比</a> · <a href="../reports/scdfe_efficiency_20261112_round5/verification.json">验证记录</a></p></section>
<section><h2>停止测试的核查</h2><p>首次完整回归有 1 项停止时限失败：线程栈显示通信循环已经结束，线程正在关闭 SQLite 最后一个连接；重复探针测得其中一次关闭耗时 1406 ms。SQLite 在最后一个 WAL 连接关闭时可能进行检查点与文件清理，参见<a href="https://www.sqlite.org/wal.html">SQLite 官方 WAL 说明</a>。</p><p>实际 T Web 使用持续连接池。测试现保留同样生命周期的观察连接，仍要求 60 秒扫描间隔下 1 秒内退出，并在测试后关闭全部连接；连续 30 次通过。此处修改了测试条件，没有放宽停止断言或改变数据库持久化策略。上述证据不能说明操作系统文件收尾始终小于 1 秒。</p><p>验证完成日期与完整回归结果见验证记录；本轮开发及性能采样始于 2026-11-12。</p></section>
<section class="note"><h2>适用边界</h2><p>以上是本机数据库和 TCP 回环结果。本轮未更改 Modbus/CAN 编解码器，不主张其现场总线吞吐量提升。批量映射维护适用于 T 的两种转发协议和不同协议采集到的源 RTU。原有 CP24、独立选择阶段、CAN 高层协议栈与硬件验收边界仍见<a href="protocol_interface_updates.html">规约接口说明</a>。发送成功表示原生接口接受上送，不等同对端业务确认；分组失败后的重试可能重复已送达的状态快照。</p></section></main></body></html>'''
target=ROOT/'docs/scdfe_efficiency_round5_20261112.html';target.write_text(html,encoding='utf-8');print(target)
