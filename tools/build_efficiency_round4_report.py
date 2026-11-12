"""Build the self-contained HTML from this round's measured results."""
from html import escape
import json
from pathlib import Path
import statistics

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'reports/scdfe_efficiency_20261112_round4'
data=json.loads((REPORT/'backend.json').read_text(encoding='utf-8'))
validation=json.loads((REPORT/'verification.json').read_text(encoding='utf-8'))
rows=[]
for result in data['results']:
    name='200 RTU × 10 点批量写入并返回记录' if 'with receipts' in result['name'] else '不返回记录的批量写入（对照）'
    rows.append(f"<tr><td>{name}</td><td>{result['before']['median_ms']:.3f} ms</td><td>{result['after']['median_ms']:.3f} ms</td><td>{result['reduction_percent']:.1f}%</td></tr>")
wire={v:[r for r in data['wire'] if r['variant']==v] for v in ('before','after')}
for key,name,unit in [('frames','160 点变化上送 I 帧','帧'),('apdu_bytes','160 点变化上送 APDU 数据量','B'),('sync_ms','数据库扫描与发送提交耗时','ms'),('delivery_ms','TCP 回环完整接收耗时','ms')]:
    a,b=[statistics.median(row[key] for row in wire[v]) for v in ('before','after')]
    rows.append(f'<tr><td>{name}</td><td>{a:.3f} {unit}</td><td>{b:.3f} {unit}</td><td>{(1-b/a)*100:.1f}%</td></tr>')
html='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,"><title>SCDFE 第四轮效率验证 · 2026-11-12</title>
<style>body{margin:0;background:#eef3f7;color:#183044;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1080px;margin:auto;padding:30px}section,header{background:white;border:1px solid #d6e2ea;border-radius:12px;padding:24px;margin-bottom:20px}h1{font-size:29px;line-height:1.4}h2{font-size:22px}table{border-collapse:collapse;width:100%;table-layout:fixed}td,th{text-align:left;vertical-align:top;border-bottom:1px solid #dce5eb;padding:12px;overflow-wrap:anywhere}th:first-child{width:38%}code,pre{font-family:Consolas,monospace;background:#edf4f7}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:14px}a{color:#006b87}.note{border-left:5px solid #bd721d}.muted{color:#536d80}@media(max-width:650px){main{padding:10px}section,header{padding:14px}td,th{padding:7px;font-size:13px}h1{font-size:24px}}</style></head><body><main>
<header><p class="muted">D:\\codex\\scada · 2026-11-12 · 基线 a9f0978</p><h1>通信、数据库与页面刷新效率验证</h1><p>本轮集中处理 IEC104 多点变化上送、批量更新返回记录和 C/S/T 表格局部刷新。T 转发范围仍为 IEC104 与 Modbus TCP。</p></header>
<section><h2>同负载实测</h2><table><thead><tr><th>测试场景</th><th>修改前</th><th>修改后</th><th>下降比例</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table>
<p>数据库为同一临时 SQLite 文件及相同连接池配置，交替执行前后版本，各 21 次采样取中位数。2000 点返回记录的 SELECT 从 2000 次降至 5 次，两侧都仅提交一次事务。不返回记录的路径作为对照，未改变其实现。</p>
<p>IEC104 使用真实 c104/lib60870 与本机 TCP 回环，每次 80 遥测 + 80 遥信，其中各半使用 CP56 时间标签；每个版本 6 次，轮换先后顺序。发送提交耗时包括扫描、值转换、质量处理和 native 发送调用；完整接收耗时包含本机驱动调度与 TCP 传输。APDU 数据量仅计算 COT=3 的 I 帧，不包含 TCP/IP 头与链路确认帧。</p></section>
<section><h2>实现及正确性</h2><ul><li>S 数据库同步保持 50ms 检查间隔，将变化监视点按 native 信息体类型分组上送；底层按 ASDU 大小拆帧，值和质量同时更新，支持质量单独变化及 CP56 时间标签。</li><li>发送失败时保留待重发点，避免 native 内存已变而下一轮漏发。成功指底层发送接口接收批次，并非对端业务处理确认；若后续类型组失败，重试可能重复之前已接收的状态快照。</li><li>批量更新返回记录按表和主键分块查询，遵守 SQLite 参数上限，保留输入顺序、重复主键的最终版本、独立响应对象及整批回滚。此共用接口也用于 S 的 Modbus/CAN 批量写入返回值。</li><li>C/S 值变化时更新必要单元格，保留表格行和按钮焦点；S 编辑与删除按钮同步最新数据和版本。T 保留不变表头。</li></ul></section>
<section><h2>浏览器对比</h2><table><tr><th>50 行同时变值</th><th>修改前</th><th>修改后</th></tr><tr><td>C / S 新建表格行</td><td>各 50 行，按钮失焦</td><td>0 行，保留按钮焦点</td></tr><tr><td>T 新建表头</td><td>13 个</td><td>0 个</td></tr></table><p>采用 Chromium 加载仓库实际 HTML/JS，通过受控 API 响应比较 DOM 替换数量；该指标不等同于浏览器帧率或网络延迟。未降低刷新频率、设置持久值缓存或丢弃点值。</p></section>
<section><h2>验证结果</h2><p>'''+escape(validation['summary'])+'''</p><p>'''+escape(validation['frontend_summary'])+'''</p>
<p>覆盖多 RTU、多表、复合与单字段主键、低 SQLite 参数上限、重复键、触发器删除导致回滚；IEC104 混合类型、时间标签、质量、拆帧、总召唤及失败重发。旧规约和前端回归继续执行。</p>
<pre>D:\\anaconda3\\python.exe tools/benchmark_efficiency_round4.py
node tests/frontend_efficiency_round4.cjs
D:\\anaconda3\\python.exe tools/build_efficiency_round4_report.py</pre>
<p><a href="../reports/scdfe_efficiency_20261112_round4/backend.json">后端原始结果与源码校验值</a> · <a href="../reports/scdfe_efficiency_20261112_round4/frontend.json">浏览器对比</a> · <a href="../reports/scdfe_efficiency_20261112_round4/verification.json">验证记录</a></p></section>
<section class="note"><h2>适用边界</h2><p>这是本机临时数据库和 TCP 回环结果，不能代替现场多通道吞吐量、CAN 硬件总线、串口与长期稳定性验收。本轮没有修改 Modbus/CAN 编解码器，因此不主张这两类协议线速提高。IEC104 CP24、独立选择阶段及 CAN 高层协议栈等原有边界仍见<a href="protocol_interface_updates.html">接口使用说明</a>。轮询数据库得到的是已提交状态快照，不是每次中间变化的 SOE 日志。</p></section>
</main></body></html>'''
target=ROOT/'docs/scdfe_efficiency_round4_20261112.html'
target.write_text(html,encoding='utf-8'); print(target)
