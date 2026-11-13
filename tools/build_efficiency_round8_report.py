"""Build the standalone report from the recorded backend and browser evidence."""
from html import escape
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'reports/scdfe_efficiency_20261113_round8'
backend=json.loads((REPORT/'backend.json').read_text(encoding='utf-8'))
verification=json.loads((REPORT/'verification.json').read_text(encoding='utf-8'))
rows=''.join(f'<tr><td>{r["rtus"]} RTU / {r["points"]:,} 点</td><td>{r["before_ms"]:.4f} ms</td><td>{r["after_ms"]:.4f} ms</td><td>{r["reduction_percent"]:.1f}%</td></tr>' for r in backend['results'])
html='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,"><title>SCDFE 第八轮效率验证 · 2026-11-13</title><style>
body{margin:0;background:#eef3f7;color:#173246;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1050px;margin:auto;padding:28px}header,section{background:white;border:1px solid #d2e1e9;border-radius:12px;padding:24px;margin-bottom:20px}h1{font-size:29px;line-height:1.4}h2{font-size:22px}table{border-collapse:collapse;table-layout:fixed;width:100%}th,td{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #dce5eb;overflow-wrap:anywhere}a{color:#006e85}code,pre{background:#eaf2f6}pre{padding:12px;white-space:pre-wrap;overflow-wrap:anywhere}.note{border-left:5px solid #ba7527}.muted{color:#576e80}@media(max-width:650px){main{padding:10px}header,section{padding:14px}th,td{padding:6px;font-size:12px}h1{font-size:24px}}</style></head><body><main>
<header><p class="muted">D:\\codex\\scada · 2026-11-13（北京时间）· 基线 3e36657</p><h1>转发监控完整显示与多 RTU 目录查询</h1><p>补齐 T 通信监控遗漏的配置字段，修复源点目录统计与列表可能不一致的问题，并降低多 RTU 源点查询与动态表格更新开销。</p></header>
<section><h2>本轮修复</h2><p><code>GET /api/trans/status</code> 现在返回页面需要的监听主机、端口、扫描周期、IEC104 公共地址、Modbus 站号、全量上送周期和日志保留小时数。“通信监控”显示监听地址与扫描周期，“通道数据”的详情区显示所选通道的完整常用配置。配置来自通道定义，运行状态继续来自诊断快照。</p><p>源点目录先按 RTU 统计测点数量，再关联 RTU 名称和规约，避免对每个测点重复关联 RTU。一次请求同时获取 RTU 统计和选中源点时使用同一只读事务；已验证其他写入者在两次读取之间新增点，当前响应保持一致，下一次查询可见新点。</p><p>四遥分类、排序、空名称、没有测点的 RTU 排除规则和 <code>include_rtus=0</code> 行为保持一致；没有新增索引或业务表。</p></section>
<section><h2>数据库查询性能</h2><table><thead><tr><th>规模</th><th>修改前</th><th>修改后</th><th>耗时减少</th></tr></thead><tbody>'''+rows+'''</tbody></table><p>测量 <code>TransStore.source_catalog('yc')</code> 的 RTU 目录统计，不包括 HTTP 往返和源点明细加载。新旧函数在同一个隔离 SQLite WAL 数据库、同一连接池运行；三轮预热后交替采样各 21 次，表中为中位数。响应内容逐项一致，所有采样保留。10 点小规模结果接近计时噪声，不作为稳定提升结论。</p></section>
<section><h2>动态表格交互</h2><table><thead><tr><th>50 行更新场景</th><th>修改前</th><th>修改后</th></tr></thead><tbody><tr><td>T 通道数据：心跳、计数与版本变化</td><td>替换 50 行，按钮焦点丢失</td><td>保留 50 行，更新变化单元格和删除版本号，焦点保留</td></tr><tr><td>T 通信监控：计数、时间与诊断变化</td><td>替换 50 行，链接焦点丢失</td><td>保留 50 行，更新变化单元格，焦点保留</td></tr></tbody></table><p>通道数量或顺序变化时重新绑定行，防止操作目标错位；空结果、读取失败后恢复、下拉选择高亮和删除使用最新版本均通过浏览器断言。这里量化的是 DOM 替换数量，不代表整页加载耗时降到零。</p></section>
<section><h2>验证结果</h2><p>'''+escape(verification['summary'])+'''</p><p>'''+escape(verification['browser_summary'])+'''</p><p>'''+escape(verification['live_summary'])+'''</p><p>'''+escape(verification['timing_note'])+'''</p><pre>D:\\anaconda3\\python.exe -m pytest tests/test_efficiency_round8.py tests/test_review_t_web.py tests/test_efficiency_round5.py tests/test_scdfe_t.py tests/test_review_t_runtime.py -q
D:\\anaconda3\\python.exe tools/benchmark_efficiency_round8.py
node tests/frontend_efficiency_round8.cjs
D:\\anaconda3\\python.exe tools/build_efficiency_round8_report.py</pre><p><a href="../reports/scdfe_efficiency_20261113_round8/backend.json">原始性能样本</a> · <a href="../reports/scdfe_efficiency_20261113_round8/frontend.json">浏览器对比</a> · <a href="../reports/scdfe_efficiency_20261113_round8/verification.json">完整验证记录</a></p></section>
<section class="note"><h2>验证边界</h2><p>本轮主要改善 T 人机监控及读取 C 数据库的多 RTU 源点目录；没有改变 IEC104、Modbus、CAN 的报文处理或现场链路速率。T 入口仍限 IEC104 与 Modbus TCP。实际 Web 检查只读取配置和已有运行状态，没有启动现场通道或发送控制命令；串口、CAN 硬件和长时间稳定性不在本轮验收范围内。其他能力限制见<a href="protocol_interface_updates.html">规约接口使用说明</a>。</p></section></main></body></html>'''
target=ROOT/'docs/scdfe_efficiency_round8_20261113.html'
target.write_text(html,encoding='utf-8');print(target)
