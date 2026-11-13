"""Build an offline report from the measured S monitor results."""
from html import escape
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT/'reports/scdfe_efficiency_20261113_round7'
data = json.loads((REPORT/'backend.json').read_text(encoding='utf-8'))
verification = json.loads((REPORT/'verification.json').read_text(encoding='utf-8'))
rows = ''.join(f'<tr><td>{r["channels"]} 通道</td><td>{r["before_ms"]:.2f} ms</td><td>{r["after_ms"]:.2f} ms</td><td>{r["reduction_percent"]:.1f}%</td><td>{r["channels"]} → 1</td></tr>' for r in data['results'])
html = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,"><title>SCDFE 第七轮效率验证 · 2026-11-13</title><style>
body{margin:0;background:#eef3f7;color:#173246;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1050px;margin:auto;padding:28px}section,header{background:white;border:1px solid #d2e1e9;border-radius:12px;padding:24px;margin-bottom:20px}h1{font-size:29px;line-height:1.4}h2{font-size:22px}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #dce5eb;overflow-wrap:anywhere}a{color:#006e85}code,pre{background:#eaf2f6}pre{padding:12px;white-space:pre-wrap;overflow-wrap:anywhere}.note{border-left:5px solid #ba7527}.muted{color:#576e80}@media(max-width:650px){main{padding:10px}section,header{padding:14px}th,td{padding:5px;font-size:12px}h1{font-size:24px}}</style></head><body><main>
<header><p class="muted">D:\\codex\\scada · 2026-11-13（北京时间）· 基线 0b0f5d6</p><h1>多通道监控：批量提交与即时诊断</h1><p>S 端通信监控过去先读取数据库旧诊断，再逐通道刷新状态，导致同一响应中的收发计数和持久化诊断相差一轮。本轮先收集状态、一次提交，再读取已提交诊断，减少多 RTU 监控的数据库提交和重复关联查询。</p></header>
<section><h2>性能对比</h2><table><thead><tr><th>规模</th><th>修改前</th><th>修改后</th><th>耗时减少</th><th>每轮提交数</th></tr></thead><tbody>'''+rows+'''</tbody></table><p>真实本机 HTTP 通信监控接口；隔离 SQLite WAL 数据库；IEC104、Modbus TCP、CAN 服务混合配置并保持停止。两轮预热后交替执行新旧实现各 15 次，表中为中位数。前后使用相同数据库、连接池和动态变化的计数，比较包含序列化与本机 HTTP 往返。</p><p>每个通道额外的 RTU→通道查询被消除，使用一次共享关联查询。单通道结果受 Windows 调度和计时波动影响，3.3% 差异不作为稳定收益结论；300 通道的 300→1 次提交为确定的结构性减少。完整样本包含离群值，未剔除。</p></section>
<section><h2>正确性与并发</h2><ul><li>进程监控和通信监控共用批量状态发布；单 RTU 请求仍只处理所选 RTU，独立服务 status() 和启停调用保留原行为。</li><li>按固定顺序取得服务锁，再收集状态并进入数据库事务，提交后释放，避免旧监控快照覆盖较新的停止状态。事务开始后不再反向申请服务锁。</li><li>任意通道写入失败会回滚整批状态，并清除本次发布的限频缓存，使下一次刷新能够重试；接口返回 HTTP 503 与 runtime_snapshot_failed。</li><li>聚合监控查询只接受 GET；错误使用 POST 返回 405。通道启动和停止仍使用原有独立 POST 控制接口。</li></ul><p>批量期间会短暂持有所选服务的锁，因此启停和其他监控请求可能等待当前批次完成。本次验证覆盖停止操作与监控并发排序，尚未给出大量活动连接下的最大锁等待时间。</p></section>
<section><h2>页面操作</h2><p>S“通信监控”和“进程监控”照常刷新即可使用优化后的接口；通信测试页继续按当前 RTU 读取，不需修改通道或 RTU 定义。本轮不修改数据库结构，也不依赖页面编辑来驱动规约的数据同步。</p><pre>GET /api/monitor/communications
GET /api/monitor/communications?rtu_id=2
GET /api/monitor/processes</pre><p>没有并发状态变化时，通信响应中的 rx_count 与 runtime_status.rx_count 来自本轮刷新；若另一个启停或状态请求随后提交，持久化诊断可能反映更新的状态。</p></section>
<section><h2>验证与复现</h2><p>'''+escape(verification['summary'])+'''</p><p>'''+escape(verification['browser_summary'])+'''</p><pre>D:\\anaconda3\\python.exe -m pytest tests/test_efficiency_round7.py -q
D:\\anaconda3\\python.exe tools/benchmark_efficiency_round7.py
node tools/verify_efficiency_round7_web.cjs
D:\\anaconda3\\python.exe tools/build_efficiency_round7_report.py</pre><p>浏览器验证需要已启动 C/S/T Web 和可用的 Playwright；可用 PLAYWRIGHT_MODULE 指定本机模块位置。复现脚本只读取页面和 API，不操作现场通道。</p><p><a href="../reports/scdfe_efficiency_20261113_round7/backend.json">全部性能样本与源码校验值</a> · <a href="../reports/scdfe_efficiency_20261113_round7/verification.json">验证记录</a> · <a href="../reports/scdfe_efficiency_20261113_round7/browser_live.json">浏览器记录</a></p></section>
<section class="note"><h2>适用边界</h2><p>本轮测量的是监控接口与运行态数据库开销，不代表 IEC104、Modbus 或 CAN 的现场线缆吞吐同比例提高。规约测试使用本机 TCP 回环和 CAN virtual，总线硬件、串口、环境与长时间稳定性未在本轮验证。T 转发仍限 IEC104 与 Modbus TCP。其他已知能力限制见<a href="protocol_interface_updates.html">规约接口说明</a>。</p></section></main></body></html>'''
target = ROOT/'docs/scdfe_efficiency_round7_20261113.html'
target.write_text(html, encoding='utf-8')
print(target)
