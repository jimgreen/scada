"""Build the self-contained HTML from recorded measurements and validation."""
from html import escape
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT/'reports/scdfe_efficiency_20261113_round6'
data = json.loads((REPORT/'backend.json').read_text(encoding='utf-8'))
verification = json.loads((REPORT/'verification.json').read_text(encoding='utf-8'))
labels = ['50 通道诊断查询', '5000 通道诊断查询', '500 通道状态写入（同一事务）', 'Modbus FC15：1968 线圈 TCP 往返']
rows = ''.join(f'<tr><td>{label}</td><td>{row["before_ms"]:.3f} ms</td><td>{row["after_ms"]:.3f} ms</td><td>{row["reduction_percent"]:.1f}%</td></tr>' for label, row in zip(labels, data['results']))
html = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,"><title>SCDFE 第六轮效率验证 · 2026-11-13</title><style>
body{margin:0;background:#eef3f7;color:#173246;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1050px;margin:auto;padding:28px}section,header{background:white;border:1px solid #d2e1e9;border-radius:12px;padding:24px;margin-bottom:20px}h1{font-size:29px;line-height:1.4}h2{font-size:22px}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{text-align:left;vertical-align:top;padding:12px;border-bottom:1px solid #dce5eb;overflow-wrap:anywhere}th:first-child{width:38%}a{color:#006e85}code,pre{background:#eaf2f6}pre{padding:12px;white-space:pre-wrap;overflow-wrap:anywhere}.note{border-left:5px solid #ba7527}.muted{color:#576e80}@media(max-width:650px){main{padding:10px}section,header{padding:14px}th,td{padding:7px;font-size:13px}h1{font-size:24px}}</style></head><body><main>
<header><p class="muted">D:\\codex\\scada · 2026-11-13 · 基线 4f9aed9</p><h1>批量命令、通道诊断与监控页刷新</h1><p>本轮消除大批量 Modbus 命令与诊断通道列表的重复比较，减少所有规约共用的运行态写入开销，并改善 C/S/T 进程监控和 S 通信监控的刷新行为。没有增加业务表或变更现场通信参数。</p></header>
<section><h2>相同负载对比</h2><table><thead><tr><th>场景</th><th>修改前</th><th>修改后</th><th>耗时下降</th></tr></thead><tbody>'''+rows+'''</tbody></table><p>每个场景先预热 2 轮，再交替采样 21 轮，取中位数。历史实现直接来自 Git 提交 4f9aed9。数据库使用同一个临时 SQLite 文件、连接池和配置；不把建表、填充数据或启动监听计入计时。</p><p>诊断查询的输入列表包含每个通道两次，用于模拟多个 RTU 引用同一通道；返回集合、顺序及诊断内容逐项一致。小范围查询变化可能处于计时噪声内，不据此宣称普遍加速。写入场景两侧均由调用方开启一次事务并写入 500 个通道；本轮减少的是重复构造 SQL 的 Python 开销。</p><p>Modbus 场景使用真实 pymodbus 客户端、服务器和本机 TCP 连接，FC15 每次写入 1968 个线圈；检查全部回调次数和最终读回值。回调在内存中确认，没有 writer IPC、现场设备执行或串口传输。该结果体现协议分派开销，不代表现场全链路同比例加速。</p></section>
<section><h2>实现与正确性</h2><ul><li>Modbus TCP 转发按点身份去重，每个点仅分派一次，保持首次遇到的地址顺序。仍先验证整个请求范围，非法尾地址、跨越只读测点、截断宽寄存器均在任何命令回调之前拒绝。</li><li>多通道诊断使用集合去重，按当前 SQLite 参数上限分块。跨分块读取在同一个只读事务中完成，保持快照一致性；下一次查询仍能读取新提交的状态。保留服务过滤、全局排序、空选择和单通道查询行为。</li><li>共用状态写入复用固定 UPSERT 文本，保留限频、版本递增、失败回滚，以及调用方事务的提交/回滚边界。IEC104、Modbus、CAN 等状态发布路径均使用这一接口。</li></ul></section>
<section><h2>页面交互</h2><table><tr><th>场景</th><th>修改前</th><th>修改后</th></tr><tr><td>S 连续触发 5 次监控刷新</td><td>10 次 API 请求</td><td>2 次 API 请求</td></tr><tr><td>C 心跳刷新</td><td>重建 4 个统计节点</td><td>保留 4 个统计节点</td></tr><tr><td>S 50 行心跳刷新</td><td>重建 50 个操作按钮，焦点丢失</td><td>保留 50 个按钮及焦点</td></tr><tr><td>T 50 行心跳刷新</td><td>重建 100 个操作按钮，焦点丢失</td><td>保留 100 个按钮及焦点</td></tr></table><p>S 合并相同范围的在途读取，启动/停止完成后仍重新取最新状态。S/T 只替换变化单元格；调整行顺序后按钮绑定更新，空列表和恢复数据均经过浏览器验证。异常提示仍可重试。</p></section>
<section><h2>验证与复现</h2><p>'''+escape(verification['summary'])+'''</p><p>'''+escape(verification['frontend_summary'])+'''</p><pre>D:\\anaconda3\\python.exe tools/benchmark_efficiency_round6.py
node tests/frontend_efficiency_round6.cjs
D:\\anaconda3\\python.exe tools/build_efficiency_round6_report.py</pre><p><a href="../reports/scdfe_efficiency_20261113_round6/backend.json">原始采样与源码校验值</a> · <a href="../reports/scdfe_efficiency_20261113_round6/frontend.json">浏览器对比</a> · <a href="../reports/scdfe_efficiency_20261113_round6/verification.json">验证记录</a></p></section>
<section class="note"><h2>适用边界</h2><p>T 转发入口仍限 IEC104 和 Modbus TCP。本轮没有更改 IEC104/CAN 的线缆组帧或物理层，也没有新增 CAN 或 Modbus RTU 转发入口。CAN 硬件、串口、高低温、干扰和长时间稳定性须另行验证。已有协议能力及限制见<a href="protocol_interface_updates.html">规约接口说明</a>。</p></section></main></body></html>'''
target = ROOT/'docs/scdfe_efficiency_round6_20261113.html'
target.write_text(html, encoding='utf-8')
print(target)
