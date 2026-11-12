"""Build the standalone HTML from measured results and completed test logs."""
from html import escape
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT/'reports/scdfe_efficiency_20261112_round3'
backend = json.loads((REPORT/'backend.json').read_text('utf-8'))
frontend = json.loads((REPORT/'frontend.json').read_text('utf-8'))
regression = json.loads((REPORT/'frontend_regression.json').read_text('utf-8'))
log = (REPORT/'pytest-full.txt').read_text('utf-8-sig')
summary = re.search(r'(\d+ passed, \d+ skipped, \d+ subtests passed in [^=\r\n]+)', log)
if not summary:
    raise SystemExit('A completed passing full regression log is required')
for name, expected in backend['source_sha256_lf'].items():
    if hashlib.sha256((ROOT/name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != expected:
        raise SystemExit('Benchmark source changed: '+name)
verification = dict(summary=summary.group(1).strip(), frontend_checks=len(frontend['checks']),
                    frontend_regression_checks=len(regression['checks']),
                    full_test_command='python -m pytest tests scdfe_c/web/test_server.py scdfe_c/web/test_protocol_seed_data.py -vv -rs -o faulthandler_timeout=30',
                    scope='Temporary databases, protocol loopback tests and intercepted browser APIs; no hardware acceptance claim.')
(REPORT/'verification.json').write_text(json.dumps(verification,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
labels = {
    '200 RTU single-row reads':'200 个 RTU，各读取一条遥测记录',
    '200 RTUs x 10 points atomic write':'200 个 RTU × 10 点，整批写库',
    'T runtime persistence 2000 points':'T 端 2000 点实时状态落库',
    'CAN decode 200 frames x 2 signals':'CAN：200 帧 × 2 信号',
    'CAN decode 200 frames x 32 signals':'CAN：200 帧 × 32 信号',
}
rows = []
for result in backend['results']:
    reduction = result['reduction_percent']
    conclusion = f'下降 {reduction:.2f}%' if reduction > 0 else f'基本持平，上升 {-reduction:.2f}%'
    rows.append(f"<tr><td>{escape(labels[result['name']])}</td><td>{result['before']['median_ms']:.3f} ms</td><td>{result['after']['median_ms']:.3f} ms</td><td>{conclusion}</td></tr>")
html = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SCDFE 通信、数据库与界面效率对比 · 第三轮</title><link rel="icon" href="data:,">
<style>body{font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#eef3f7;color:#203c50}main{max-width:1100px;margin:auto;padding:24px}header,section{padding:24px;margin-bottom:18px;background:#fff;border:1px solid #d9e4eb;border-radius:10px}h1{font-size:28px;line-height:1.4}h2{font-size:22px}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{padding:10px;border-bottom:1px solid #d9e4eb;text-align:left;vertical-align:top;overflow-wrap:anywhere}code,pre{font-family:Consolas,monospace;overflow-wrap:anywhere}pre{white-space:pre-wrap;background:#edf5f6;padding:12px}a{color:#007b86}.boundary{border-left:5px solid #aa7029}@media(max-width:650px){main{padding:8px}header,section{padding:14px}h1{font-size:24px}}</style></head><body><main>
<header><p>D:\\codex\\scada · 2026-11-12</p><h1>通信、数据库与界面效率对比</h1><p>本轮以已发布提交 <code>1d21c03</code> 为基线，继续优化共用数据库接口、CAN 信号解码、T 实时状态写入和三端页面请求。保留所有点值、质量、刷新时间及命令处理语义。</p></header>
<section id="performance"><h2>本机性能对比</h2><table><tr><th>场景</th><th>修改前中位耗时</th><th>修改后中位耗时</th><th>变化</th></tr>''' + ''.join(rows) + '''</table>
<p>逐点读取、T 状态落库和 CAN 各交替采样 31 组；2000 点批量写库采样 15 组。前后版本都使用同一环境与相同数据，交替测量顺序。数据库读写使用真实 ScadaStore、临时 SQLite 文件和相同大小的连接池。</p>
<p>T 状态落库测试使用包含实时字段和主键的最小 SQLite/WAL 表，单独测量运行态写入循环；CAN 测试使用实际编解码器及内存帧，不连接总线硬件。以上数据不是现场吞吐量。稀疏 CAN 按实测单列展示，避免用密集信号场景代替全部情况。</p></section>
<section><h2>通信与多 RTU 数据库</h2><ul>
<li>批量更新复用字段元数据，仅校验本次变更的字段；相邻且 SQL 相同的更新合并为 executemany。2000 点同结构更新的 Python SQL 调用由 2000 次降到 1 次，前后仍都只提交一个事务。</li>
<li>重复主键按原顺序更新，版本号逐次增加；缺失点或后续写入失败整批回滚。需要合并旧行进行校验的规约配置继续逐行校验，不跳过约束。</li>
<li>单条宽记录查询复用按位置转换的逻辑，保留字段顺序独立性、布尔值、NULL、字节内容和版本号。</li>
<li>T 的 IEC104/Modbus TCP 转发点运行状态按相同字段结构批量落库，保留刷新时间、最后转发时间及失败重试，不增加定义版本。</li>
<li>CAN 密集信号每帧按所需字节序转换完整报文一次，同帧信号共用结果。64 信号混合类型用例中，整数转换由 48 次减少到最多 2 次；有符号值、偏移、缩放、质量及短帧错误保持一致。稀疏帧保留直接解码路径。</li>
<li>补齐 Modbus 位段定义校验：offset_bit 与 all_bit 之和超过 16 时，保存接口明确拒绝。</li></ul></section>
<section><h2>C / S / T 页面交互</h2><table><tr><th>场景</th><th>修改前</th><th>修改后</th></tr>
<tr><td>C 同范围表格并发刷新</td><td>5 次查询</td><td>1 次查询</td></tr>
<tr><td>S 四遥页连续刷新</td><td>5 次目录查询</td><td>1 次目录查询</td></tr>
<tr><td>T 通道选择列表连续刷新</td><td>5 次查询</td><td>1 次查询</td></tr>
<tr><td>C 切换“全部字段”</td><td>只重绘旧投影，部分字段缺失</td><td>按所选视图重新查询并显示完整字段</td></tr></table>
<p>合并仅发生在同一范围的重叠读取期间。保存后仍会补发最新读取；切换 RTU、通道或分页时，旧响应不能覆盖新范围。S 四遥页数据不变时保留现有表格行，减少重绘和焦点变化。未增加持久数据缓存，也未削减数据上送或刷新频率。</p></section>
<section><h2>验证与复现</h2><p><strong>''' + escape(verification['summary']) + '''</strong>。跳过项为 Windows 符号链接权限。</p><p>新增浏览器检查 ''' + str(verification['frontend_checks']) + ''' 项、既有浏览器回归 ''' + str(verification['frontend_regression_checks']) + ''' 项通过；覆盖重复读取合并、保存后读取、范围切换、完整字段与 DOM 保留。</p>
<pre>D:\\anaconda3\\python.exe tools/benchmark_efficiency_round3.py
node tests/frontend_efficiency_round3.cjs
D:\\anaconda3\\python.exe tools/build_efficiency_round3_report.py</pre>
<p>浏览器脚本通过 PLAYWRIGHT_MODULE 指定已安装的 Playwright。<a href="../reports/scdfe_efficiency_20261112_round3/backend.json">后端原始采样与源码校验值</a> · <a href="../reports/scdfe_efficiency_20261112_round3/frontend.json">浏览器对比</a> · <a href="../reports/scdfe_efficiency_20261112_round3/verification.json">验证记录</a></p></section>
<section class="boundary"><h2>范围与边界</h2><p>本轮未扩展现场规约能力，IEC104 CP24、独立选择阶段、CAN 高层协议栈及物理层验收等原有未完成项仍见 <a href="protocol_interface_updates.html">规约接口使用说明</a>。T 转发入口仍仅 IEC104 与 Modbus TCP。</p><p>优化共用读写路径可供多种规约使用，但本轮没有把本机数据库或 CAN 解码耗时推断成 IEC104/Modbus 线速提升。运行数据库和原有文档删除不纳入代码提交。</p></section>
</main></body></html>'''
target = ROOT/'docs/scdfe_efficiency_round3_20261112.html'
target.write_text(html,encoding='utf-8')
print(target)
print(json.dumps(verification,ensure_ascii=False))
