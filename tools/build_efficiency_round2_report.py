"""Build the single-file Chinese performance and verification report."""
from pathlib import Path
from html import escape
import base64
import hashlib
import json
import re

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/scdfe_efficiency_20261112_round2'
runtime=json.loads((OUT/'runtime_comparison.json').read_text('utf-8'))
frontend=json.loads((OUT/'frontend_comparison.json').read_text('utf-8'))
regression=json.loads((OUT/'frontend_regression.json').read_text('utf-8'))
raw=(OUT/'pytest-full.txt').read_bytes()
pytest=raw.decode('utf-16') if raw.startswith(b'\xff\xfe') else raw.decode('utf-8-sig')
summary=re.findall(r'[^\r\n]*\d+ passed[^\r\n]*',pytest)[-1]
assert 'failed' not in summary.lower(), summary
for file,digest in runtime['current_sha256'].items():
    assert hashlib.sha256((ROOT/file).read_bytes()).hexdigest()==digest,file
names={
 'T source read: 200 RTUs x 50 points':'T 密集源点读取：200 RTU × 50 点',
 'T source read: 200 RTUs x 5 points':'T 小批源点读取：200 RTU × 5 点',
 'S IEC104: unchanged 4000 points after other RTU commit':'S IEC104：其他 RTU 提交后，本 RTU 4000 点无变化扫描',
 'C writer: 80 RTUs x 20 version-preserving updates':'C 写库：80 RTU × 20 点，保留逐消息版本',
 'CAN FD: 200 frames x 32 mapped signals':'CAN FD：200 帧 × 32 个映射信号',
 'S Modbus: prepare and publish 1000 points before start':'S Modbus：1000 点，启动前数据区更新',
 'S Modbus: publish 1000 points to active TCP server':'S Modbus：1000 点，运行中 TCP 服务更新',
}
rows=[]
for r in runtime['results']:
    before,after=r['before'],r['after']; reduction=r['reduction_percent']
    note=('基本持平（小幅波动）' if abs(reduction)<5 else
          f'耗时降低 {reduction:.1f}%' if reduction>0 else f'耗时增加 {-reduction:.1f}%')
    rows.append(f'<tr><td>{escape(names[r["name"]])}</td><td>{before["median_ms"]:.3f}</td><td>{after["median_ms"]:.3f}</td><td>{before["p95_ms"]:.3f} → {after["p95_ms"]:.3f}</td><td>{note}</td></tr>')
s=[r for r in frontend['measurements'] if r['module']=='S']
checks=len(frontend['checks'])+len(regression['checks'])
image=base64.b64encode((OUT/'s-paged-points.png').read_bytes()).decode('ascii')
sources='\n'.join(f'{f}  {digest}' for f,digest in runtime['current_sha256'].items())
html=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SCDFE 通信、数据库与界面效率验证 · 2026-11-12</title><link rel="icon" href="data:,"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef3f7;color:#213c50;font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1200px;margin:auto;padding:28px}}header,section{{background:white;border:1px solid #dce5ec;border-radius:12px;padding:26px;margin-bottom:20px}}h1{{line-height:1.35;font-size:30px}}h2{{font-size:23px}}small,.muted{{color:#526b7c}}a{{color:#007584}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;min-width:780px}}th,td{{padding:11px;text-align:left;border-bottom:1px solid #dce5ec}}th{{background:#edf6f7}}td:first-child{{max-width:440px}}code,pre{{font-family:Consolas,monospace}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;padding:16px;background:#eff4f7}}img{{width:100%;height:auto;border:1px solid #dce5ec}}.proof{{border-left:5px solid #087c83}}@media(max-width:700px){{main{{padding:10px}}section,header{{padding:16px}}h1{{font-size:25px}}}}</style></head><body><main>
<header><p class="muted">D:\\codex\\scada · 2026-11-12 · 第二轮效率优化</p><h1>通信、数据库与界面效率验证</h1><p>范围：C/S 的 IEC104、Modbus 与 CAN 热路径；T 继续仅支持 IEC104 与 Modbus TCP 转发。</p><nav><a href="#runtime">运行性能</a> · <a href="#web">页面交互</a> · <a href="#changes">修改与使用</a> · <a href="#proof">测试证据</a> · <a href="#method">方法与边界</a></nav></header>
<section id="runtime"><h2>同规模运行性能对比</h2><p>单位：毫秒；报告中位数与 P95。每项预热 3 次后采样 31 次；写库采用 41 组交替顺序的配对采样。基线是本轮开始时冻结的工作区文件，包含上一轮未提交修复。</p><div class="scroll"><table><thead><tr><th>场景</th><th>优化前中位数</th><th>优化后中位数</th><th>P95 前 → 后</th><th>结果</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p>T 根据源点密度选择查询方案：200 个 RTU 各取 5 点时，SELECT 从 200 次降为 1 次；每 RTU 50 点的密集场景保留直接范围查询。C 写库将相邻同形 SQL 的 executemany 从 80 次合为 1 次，保持原始写入次序与版本递增；小幅耗时变化不作为显著提速结论。</p>
<p>Modbus 的启动前与运行中结果分别列出。启动前去掉逐点遍历全部数据块；运行中保留服务事件循环的一次批量交接。两者均只编码一次。运行中另通过本机 TCP 读取首个浮点值验证线上的编码结果。</p></section>
<section id="web"><h2>页面交互与传输量</h2><div class="scroll"><table><tr><th>场景</th><th>优化前</th><th>优化后</th><th>验证</th></tr><tr><td>S：四遥各 10000 点，一次刷新</td><td>{s[0]['point_requests']} 次点表请求 / {s[0]['point_response_bytes']:,} B</td><td>{s[1]['point_requests']} 次 / {s[1]['point_response_bytes']:,} B</td><td>数据量减少 {(1-s[1]['point_response_bytes']/s[0]['point_response_bytes'])*100:.2f}%</td></tr><tr><td>C/T：三个并发进程刷新</td><td>各 3 次请求</td><td>各 1 次请求</td><td>写入完成后仍发起新的读取</td></tr></table></div><p>浏览器执行真实仓库 HTML/JavaScript，API 使用确定性拦截数据；这些是请求与响应体对比，不是实际部署网络时延。</p><details><summary>查看验证后的 S 页面（模拟 RTU 600）</summary><img alt="S 四遥页面显示实时值、状态、刷新时间及 RTU 600" src="data:image/png;base64,{image}"></details></section>
<section id="changes"><h2>修改与使用说明</h2><ul><li>S 通信测试页仍按四遥分页，每页 25 点；只读取各自当前页。相同 RTU 刷新保留页码，删除末页后自动回到有效页，切换 RTU 回到第一页。下拉框可读取超过 500 个 RTU。</li><li>C/T 进程页合并重叠读取。T 数据未变时保留表格节点，页面隐藏时暂停周期请求。写入成功后补一次新读取，旧请求不会覆盖新 RTU。</li><li>S IEC104 缓存数据库源样本，仅对变化的值/质量读取原生点。避免 float32 舍入使相同源值反复上送；失败时保留待重试状态，数据库依然由服务主动同步。</li><li>CAN 在配置时预计算字节范围、位掩码和浮点解码器；保留符号、缩放、偏移、质量位及短帧错误行为。</li><li>Modbus 修复列表型 SimData 的 count 使用：count 表示整段寄存器列表的重复次数。原来的重复扩大导致相邻 float32/float64 点重叠；现在按实际编码宽度定义，支持连续地址。</li><li>本轮不需要数据库表结构迁移。</li></ul></section>
<section id="proof" class="proof"><h2>验证结果</h2><p><strong>{escape(summary.strip())}</strong></p><p>两组浏览器检查合计 <strong>{checks}</strong> 项通过；覆盖分页、600 RTU、写后读取、晚到失败隔离、失败重试、日志刷新及规约表单。</p><p>新增回归覆盖跨 RTU 同点号、源点扇出、空值、SQLite 999 参数限制、质量变化与失败重试、CAN 解码一致性、连续浮点寄存器本机 TCP 读写、逐消息数据库版本。</p><p>T 长扫描停止用例在早期单项测试中曾超过 1 秒阈值；最终全量回归及额外连续 5 次复测通过。未定位到确定的阻塞根因，不能把后续通过解释为已修复该偶发时序现象；复测记录保存为 stop_latency_check.txt。</p><pre>D:\\anaconda3\\python.exe -m pytest tests scdfe_c/web/test_server.py scdfe_c/web/test_protocol_seed_data.py -q -rs
D:\\anaconda3\\python.exe tools/benchmark_efficiency_round2.py
node tests/frontend_efficiency_round2.cjs
node tests/frontend_ui_efficiency.cjs --output reports/scdfe_efficiency_20261112_round2/frontend_regression.json</pre><p class="muted">浏览器脚本使用已安装的 Playwright，可用 PLAYWRIGHT_MODULE 指定模块目录。详细原始数据：同目录 runtime_comparison.json、frontend_comparison.json、frontend_regression.json、pytest-full.txt。</p></section>
<section id="method"><h2>方法与验证边界</h2><p>{escape(runtime['python'].splitlines()[0])}；{escape(runtime['platform'])}。SQLite 使用临时文件和 WAL；测试结束关闭。IEC104 测量真实 c104 原生点对象，但该微基准不打开监听连接；CAN 测量本地编解码；通信正确性由回归中的本机 TCP/虚拟 CAN 测试补充。</p><p>本轮未做现场设备、多机网络、物理串口/CAN 总线或新增长期稳定性测试；上述热路径耗时不能直接等同于现场吞吐量。既有 CP24、CANopen/J1939/UDS 完整栈等能力边界没有因本轮优化而改变。</p><p>改动已保存在本地；未在本轮提交、推送或重启正在使用的 Web 服务。既有运行数据库和用户删除的文档保留原状。</p><details><summary>当前源文件 SHA256</summary><pre>{escape(sources)}</pre></details></section></main></body></html>'''
(OUT/'comparison.html').write_text(html,encoding='utf-8')
print(OUT/'comparison.html')
