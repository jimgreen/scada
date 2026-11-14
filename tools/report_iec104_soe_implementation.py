"""Build the self-contained SOE implementation/acceptance report from evidence."""
import hashlib
from html import escape
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
REPORTS=ROOT/'reports/iec104_soe_20261114'


def main():
    perf=json.loads((REPORTS/'implementation-performance-final.json').read_text(encoding='utf8'))
    burst=json.loads((REPORTS/'implementation-burst.json').read_text(encoding='utf8'))['pipelines'][0]
    qa_path=REPORTS/'web_qa.json'
    if not qa_path.exists(): qa_path=ROOT/'output/iec104_soe_ui_20261114/qa.json'
    qa=json.loads(qa_path.read_text(encoding='utf8'))
    suite=ET.parse(REPORTS/'implementation-final-regression.xml').getroot().find('testsuite')
    log_path=REPORTS/'implementation-final-regression.log'
    summary=f"{suite.attrib['tests']} 项测试记录（含子用例），0 失败"
    if log_path.exists():
        summary=re.search(r'\d+ passed, \d+ subtests passed.*',log_path.read_text(encoding='utf8')).group()
    wire=json.loads((REPORTS/'implementation-wire/wire_contracts.json').read_text(encoding='utf8'))
    assert int(suite.attrib['failures'])==int(suite.attrib['errors'])==0
    assert all(r['exact'] for r in perf['pipelines']) and burst['exact'] and wire['passed']
    assert all(not r['overflow'] and not r['errors'] for r in qa)
    rows=''.join(f"<tr><td>{r['channels']}</td><td>{r['expected']:,} / {r['expected']:,}</td><td>{r['events_per_second']:,.2f}</td><td>{r['commit_to_downstream_median_ms']:,.3f}</td><td>{r['commit_to_downstream_p95_ms']:,.3f}</td><td>{r['end_to_end_ack_ms']:,.3f}</td></tr>" for r in perf['pipelines'])
    db=perf['database_write'];one=db['one_event_per_asdu'];packed=db['packed_asdu']
    wheel=ROOT/'vendor/c104/c104-2.2.1+scada.3-cp311-cp311-win_amd64.whl'
    sha=hashlib.sha256(wheel.read_bytes()).hexdigest()
    html=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>IEC104 SOE 实现与验证 · 2026-11-14</title>
<style>:root{{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#253a4d;background:#f1f5f9}}*{{box-sizing:border-box}}body{{margin:0}}main{{max-width:1180px;margin:auto;padding:30px}}h1{{font-size:30px}}h2{{font-size:21px;margin-top:0}}p,li{{line-height:1.8}}section{{background:#fff;border:1px solid #dbe4ed;border-radius:12px;margin:20px 0;padding:22px}}.result{{border-left:5px solid #2b8264}}.boundary{{border-left:5px solid #bc882e}}.muted{{color:#536c80}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:11px;border-bottom:1px solid #e0e7ef;text-align:left;vertical-align:top}}th{{background:#edf3f9}}.numbers{{white-space:nowrap}}code{{font-family:Consolas,monospace;overflow-wrap:anywhere}}pre{{background:#edf3f9;padding:14px;overflow:auto}}a{{color:#146ba6}}nav{{display:flex;gap:18px;flex-wrap:wrap}}@media(max-width:600px){{main{{padding:14px}}section{{padding:15px}}h1{{font-size:25px}}th,td{{padding:8px}}}}</style></head><body><main>
<h1>IEC104 SOE：从最新值到逐次事件</h1><p class="muted">2026-11-14 · D:\\codex\\scada · Windows / CPython 3.11 / c104 2.2.1+scada.3</p>
<nav><a href="#result">验证结论</a><a href="#architecture">数据链路</a><a href="#performance">效率对比</a><a href="#usage">使用方法</a><a href="#limits">适用边界</a><a href="#evidence">证据与复现</a></nav>
<section class="result" id="result"><h2>本轮已实现并通过验证的功能</h2><p>S 将 CP56 单点/双点的每次值或品质变化与数据库更新一并提交；C 在接收回调中按 ASDU 事务记录事件；T 只读取 C/T 共享库，保留源时标、原始双点状态和品质，逐批转发并保存确认进度。最新值画面继续使用原有测点表。</p>
<p><strong>{escape(summary)}</strong>；另有 {len(wire['checks'])} 项独立原始报文检查通过。C/S/T 三个 SOE 页面在 1440px 和 390px 宽度均验证通过，筛选、分页、请求期间切换 RTU、重复读取不重复追加均正常。</p>
<div class="scroll"><table><tr><th>检查点</th><th>修改前</th><th>本轮结果</th></tr>
<tr><td>S 扫描窗口内快速变位</td><td>21 次提交合并为 1 次最新状态</td><td>21 次变位和 1 次品质变化全部逐条传输；停机期间另 7 次变化在重启后补发</td></tr>
<tr><td>C 连续接收</td><td>101 条事件只留下 1 个最新值快照</td><td>101 条分别落库；相同 IOA、相同报文再次发送仍保留为新的事件</td></tr>
<tr><td>T 源时标与双点状态</td><td>重建时标；双点 0/3 被业务布尔值折叠</td><td>CP56 原始 7 字节、毫秒、无效/替代/夏令时标志、双点 0/3、品质和 COT 保留；只重映射目标 CA/IOA</td></tr>
<tr><td>连接和确认</td><td>入队即视为已上送，没有持久游标</td><td>原批次所有接收连接确认后再保存游标；慢连接、断连、重启和游标提交失败均有回归测试</td></tr>
<tr><td>16,384 条突发</td><td>旧低优先级队列延迟 ACK 测试收到 8,452 条</td><td>数据库积压 + 有界发送路径完整落库并转发 {burst['c_persisted']:,} 条，耗时 {burst['end_to_end_ack_ms']/1000:.3f} 秒</td></tr></table></div>
<p class="muted">旧队列压力测试与新数据库链路采用不同注入方式，上表用于说明正确性变化，不用于计算端到端提速倍数。旧专项验收报告作为修改前证据保留。</p></section>
<section id="architecture"><h2>持久数据链路与数据库职责</h2>
<pre>S 独立数据库 → S 事件批次 → IEC104 TCP → C 接收回调
                                            ↓ 按 ASDU 提交
                                  C/T 共享业务库的 SOE 事件表
                                            ↓ 按已确认游标读取
                                     T → IEC104 TCP → 接收端</pre>
<p>新增内部表 <code>_scada_iec104_soe</code> 和 <code>_scada_iec104_soe_cursor</code>。事件表使用自增事件号，保存来源、通道、RTU、点号、CA、IOA、类型、原始状态、品质、COT、OA、测试/否定位、源时间、接收时间和 CP56 字节/标志。无指向当前点定义的外键，删除点定义不会级联删除历史。游标表保存消费者、已确认事件号和确认时刻。</p>
<p>S 专用触发器与点值事务保持原子性，不依赖 HMI 回调；重复写入相同值/品质、仅更新时间和已回滚事务不会增加事件。C 的普通最新值写入不会产生 S 事件。新增表由现有存储初始化事务创建，旧结构迁移工具已兼容，不重建原业务表。事件查询和转发使用来源/事件号以及来源/RTU/事件号索引。</p>
<p>每个发送通道最多一个在途批次，默认从数据库读取 128 条；原生 SOE 发送不经过会覆盖旧条目的低优先级队列。Python 轮询确认状态，不等待远端时阻塞其他通道；T 的单 RTU 来源使用索引过滤其他 RTU 的记录。</p></section>
<section id="performance"><h2>数据库写入与多通道效率</h2><p>相同 512 条事件、相同 SQLite FULL 同步策略、每种方式重复 3 次；对比逐事件 ASDU 与合并 ASDU 两种事务粒度。接收回调每个 ASDU 只提交一次，报文内的重复 IOA 仍逐条入表。</p>
<div class="scroll"><table class="numbers"><tr><th>方式</th><th>事件数</th><th>事务数</th><th>中位耗时 ms</th></tr><tr><td>每条事件一个 ASDU</td><td>512</td><td>{one['transactions']}</td><td>{one['median_ms']:,.3f}</td></tr><tr><td>按长度合并 ASDU</td><td>512</td><td>{packed['transactions']}</td><td>{packed['median_ms']:,.3f}</td></tr></table></div>
<p>该微基准事务数减少 {(1-packed['transactions']/one['transactions'])*100:.2f}%，中位写入耗时约为原来的 1/{db['speedup']:.2f}。这是同等持久化要求下的批量收益，不代表整个系统提速 {db['speedup']:.2f} 倍。</p>
<p>下面为真实 S 数据库 → C 落库 → T 转发 → 下游确认的完整链路实测。每通道 512 条事件；逐条检查数量、顺序、状态、品质和源时标。</p>
<div class="scroll"><table class="numbers"><tr><th>通道数</th><th>完整事件 / 预期</th><th>事件/秒</th><th>到下游中位 ms</th><th>到下游 P95 ms</th><th>全部确认 ms</th></tr>{rows}</table></div>
<p class="muted">使用临时数据库、本机 TCP，各适配器在同一测试进程内组合运行；w=1、t2=1 秒，S 50ms 扫描，T 在受控 5ms 循环中推进事件批次。延迟自 S 事务提交完成起算。生产默认 w/t2、磁盘负载、通道调度、独立进程及慢连接会改变延迟；这些短时测试不能替代长时间稳定性或硬件验收。多通道持久写入存在 SQLite 单写者竞争，通道数增加不保证吞吐线性增长。</p></section>
<section id="usage"><h2>配置与查看</h2><ol>
<li>在实际启动程序的 Python 环境安装 <code>requirements.txt</code>，确认 c104 为 <code>2.2.1+scada.3</code>，再重启相应服务。开发验收使用隔离环境，实际生效版本应核对服务启动环境。</li>
<li>S 仍使用独立模拟库；为遥信配置 IEC104 点映射：类型 30（CP56 单点）或 31（CP56 双点），也支持类型 1/3 配合 <code>time_tag=CP56Time2a</code>。仅开启普通无时标点不会自动变成完整 SOE 来源。</li>
<li>C 按对应类型接收；T 转发点选择相同单双点类型及源 RTU/点号。T 的其他来源及普通最新值转发保留现有行为，不能据此认定带有历史 SOE。</li>
<li>各 Web 工作台进入“SOE 事件”，或打开 <code>/soe</code>。可筛选 RTU、查看最新/最早记录、继续读取；表格显示原始状态、品质、源时间、接收时间和确认进度。页面最多保留 500 行，隐藏标签页暂停自动请求，数据库历史不会因页面裁剪而删除。</li>
<li>查询接口示例：<code>GET /api/soe?rtu_id=1&amp;after_id=100&amp;limit=100</code>；最新记录使用 <code>tail=1</code>，不能同时设置非零 after_id。接口为只读，limit 上限 1000。</li></ol></section>
<section id="limits" class="boundary"><h2>必须保留的功能边界</h2>
<p>实现的是有界发送、持久恢复的“至少一次”路径。对端 APCI 确认后、本地游标提交前崩溃，可能在重启时重复发送。APCI 确认不能证明外部接收端业务落库；C 事件落库失败会记录错误并使工作线程重建当前连接，但协议层确认不是数据库事务确认，因此不宣称任意故障下绝对无损或恰好一次。</p>
<p>游标按发送通道保存，批次以提交时的接收连接为确认范围；新加入连接不自动获得已经确认的历史。最慢的原接收连接会延迟该通道后续批次。总召仍提供当前值，不当作历史回放。改换源点或类型前应处理积压；历史类型与当前定义冲突时明确报错，不静默转换。</p>
<p>CP56 原始日历按 UTC 约定展示；若现场使用其他时区，应先统一约定，原始字节可追查。原生库普通 datetime API 的时区契约未整体改变。CP24Time2a 仍不支持；本轮 SOE 转发仅覆盖 CP56 单点/双点。事件自动清理未启用，归档必须保留未确认事件及游标。未进行实物设备、掉电持久性、长期连续运行验收。</p></section>
<section id="evidence"><h2>证据与复现</h2><p>最终回归零失败。首次扩展回归发现的旧迁移工具表目录冲突已修复；浏览器发现的 CSP 内联资源限制和多位数查询参数解析问题也已修复并补充测试。保留先前失败日志以便审查。</p>
<ul><li><a href="../reports/iec104_soe_20261114/implementation-final-regression.xml">最终回归结果 JUnit XML</a></li>
<li><a href="../reports/iec104_soe_20261114/implementation-performance-final.json">性能原始结果</a> / <a href="../reports/iec104_soe_20261114/implementation-burst.json">16,384 条突发结果</a></li>
<li><a href="../reports/iec104_soe_20261114/implementation-wire/wire_contracts.json">独立原始报文检查</a> / <a href="../reports/iec104_soe_20261114/web_qa.json">三端页面 QA</a></li>
<li><a href="iec104_soe_acceptance_20261114.html">修改前 SOE 专项验收</a> / <a href="../vendor/c104/README.md">原生补丁与构建说明</a></li></ul>
<pre>python -m pytest tests/test_iec104_soe_store.py tests/test_iec104_soe_pipeline.py tests/test_iec104_soe_api.py -q
python tools/benchmark_iec104_soe.py --output reports/soe-local.json
python tools/benchmark_iec104_soe.py --output reports/soe-burst-local.json --events 16384 --channels 1 --skip-db
python tools/verify_iec104_wire_contracts.py --output reports/soe-wire-local</pre>
<p>本轮隔离解释器：<code>output/iec104-native-20261114/venv/Scripts/python.exe</code>。原始源包经固定 SHA 校验后精确应用补丁；二次生成 sdist 的 SHA 相同。wheel SHA-256：<code>{sha}</code>。本报告记录开发验收阶段的结果；发布与重启状态以当次发布记录为准。</p></section>
</main></body></html>'''
    target=ROOT/'docs/iec104_soe_implementation_20261114.html'
    target.write_text(html,encoding='utf8')
    print(target)


if __name__=='__main__':main()
