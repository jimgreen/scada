"""Render measured IEC104 results, including unresolved wire-contract failures."""
import hashlib
import html
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'reports/iec104_common_acceptance_20261113/recheck_01'


def table(headers,rows):
    def row(tag,values):return '<tr>'+''.join(f'<{tag}>{html.escape(str(v))}</{tag}>' for v in values)+'</tr>'
    return '<div class="scroll" tabindex="0"><table><thead>'+row('th',headers)+'</thead><tbody>'+''.join(row('td',r) for r in rows)+'</tbody></table></div>'


def main():
    result=json.loads((BASE/'final/results.json').read_text())
    scale=json.loads((BASE/'scale8/results.json').read_text())
    wire=json.loads((BASE/'final/wire_contracts.json').read_text(encoding='utf-8'))
    assert result['passed'] and scale['passed']
    suite=ET.parse(BASE/'final/regression.xml').getroot().find('testsuite')
    assert suite is not None and all(int(suite.get(k,'0'))==0 for k in ('errors','failures','skipped'))
    tests=len(suite.findall('testcase')); subtests=int(suite.get('tests'))-tests
    good=sum(c['passed'] for c in wire['checks']); bad=len(wire['checks'])-good
    pipelines=result['pipelines']+scale['pipelines']
    median=lambda p,k:p[k]['median_ms']
    latency=table(['通道 / 监视点数','更新批数','C 写库中位 / ms','端到端中位 / ms','端到端 P95 / ms','端到端最大 / ms'],[
        (f'{p["channels"]} / {p["channels"]*(p["yc_per_rtu"]+p["yx_per_rtu"])}',p['end_to_end_latency']['samples'],
         median(p,'c_commit_latency'),median(p,'end_to_end_latency'),p['end_to_end_latency']['p95_ms'],p['end_to_end_latency']['max_ms']) for p in pipelines])
    gi=table(['场景','每次总召点数','S 总召中位 / P95 (ms)','T 总召中位 / P95 (ms)','每端样本数'],[
        (f'{p["channels"]} 通道',p['yc_per_rtu']+p['yx_per_rtu'],
         f'{median(p,"s_gi")} / {p["s_gi"]["p95_ms"]}',f'{median(p,"t_gi")} / {p["t_gi"]["p95_ms"]}',p['s_gi']['samples']) for p in pipelines])
    commands=table(['场景','命令数','S 收到命令中位 / ms','C 回执入库中位 / P95 (ms)','S 重启→T 恢复 / ms'],[
        (f'{p["channels"]} 通道',p['command_latency']['samples'],median(p,'command_latency'),
         f'{median(p,"command_receipt_latency")} / {p["command_receipt_latency"]["p95_ms"]}',p['upstream_restart_recovery_ms']) for p in pipelines])
    single,batch=result['comparison']['single'],result['comparison']['batch']
    sw,bw=single['wire'][0],batch['wire'][0]
    efficiency=table(['512 个遥测点变化','逐点发送','批量 ASDU 发送'],[
        ('监视 I 帧数',sw['frames'],bw['frames']),('对应 APDU 字节',sw['bytes'],bw['bytes']),
        ('全点接收中位 / ms',median(single,'latency'),median(batch,'latency')),
        ('P95 / ms',single['latency']['p95_ms'],batch['latency']['p95_ms'])])
    features=table(['检查项','结果与范围'],[
        ('总召','通过：C 启动总召写库，S/T 每通道重复总召 5 次；ACTCON → COT=20 数据 → ACTTERM 顺序、对象数、CA/IOA、值及质量正确。'),
        ('变位 / 变化传送','修复后通过：直接改 S 数据库，S 自主上送 COT=3；每批全部点更新，等待下游完成后再发下一批，逐批核对对象总数和所有点值。'),
        ('质量变化与周期上送','通过：仅 IV/BL/恢复变化也到达 C/T；400 ms 稳态窗口没有重复变位；T 既有连接收到 COT=1 的完整周期数据。'),
        ('直接执行 / 自动选择执行','业务执行通过：YK/YT 直接执行及自动选择+执行均抵达 S 并产生 C 回执。选择确认、执行肯定确认可见；结束帧选择位存在下述兼容问题。'),
        ('撤销','不重复执行的业务保护通过，但标准确认检查未通过。不能把 C/S 同库回环成功等同于与第三方设备撤销互操作通过。'),
        ('链路控制','独立原始 TCP 对端验证 STARTDT、STOPDT、TESTFR 请求确认、停止期间无监视 I 帧、恢复后总召及原生 t3 保活。该探针直接测试 S/T 共用服务适配器。'),
        ('连接恢复与序号','通过：上游 S 重启后 C 自动重连与总召，T 获得最新值；下游重新连接总召也正确。捕获 APDU 校验 I/S/U 格式和 I 帧发送序号连续性，未覆盖全部恶意序号注入。'),
        ('信息类型与时间标签','单点、浮点遥测为完整链路负载；双点、步位置、归一化/标度、CP56 监视及时间命令由分层回归覆盖。没有证明源时间标签在 C/S/T 全链路无损透传。'),
    ])
    failures=table(['独立检查','结果','实际观察'],[
        (c['name'],'通过' if c['passed'] else '未通过',str(c.get('observed_cots',c.get('frames',c.get('error','符合本项断言'))))) for c in wire['checks']])
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    paths=[ROOT/p for p in ('client/iec104.py','server/iec104_server.py','scdfe_c/__init__.py','scdfe_s/web/iec104_service.py',
                           'scdfe_t/trans.py','core/command_store.py','vendor/c104/c104-2.2.1+scada.1.tar.gz',
                           'tools/verify_iec104_pipeline.py','tools/verify_iec104_wire_contracts.py','tests/test_iec104_receive_snapshots.py')]
    paths += [BASE/p for p in ('final/results.json','scale8/results.json','final/wire_contracts.json','final/regression.xml',
                               'diagnostic_4ch/failure-4ch-burst-32/state.json')]
    manifest=dict(git_head=head,source_state='HEAD plus uncommitted working tree',functional_tests=tests,subtests=subtests,
                  wire_checks_passed=good,wire_checks_failed=bad,full_wire_acceptance_passed=wire['passed'],
                  sha256={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
    (BASE/'final/manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    style='''*{box-sizing:border-box}body{margin:0;background:#f2f6f8;color:#193247;font:16px/1.8 "Microsoft YaHei",system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:28px 24px 50px}header,section{background:white;border:1px solid #dce5eb;border-radius:10px;padding:24px 28px;margin-bottom:18px}header{border-top:5px solid #a86414}h1{font-size:30px;line-height:1.4}h2{font-size:22px;margin:0 0 14px}p{margin:10px 0}.note{font-size:14px;color:#526a7c}.ok{background:#eef8f4;border-left:4px solid #228268;padding:14px 18px}.issue{background:#fff3e6;border-left:4px solid #be7928;padding:14px 18px}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;vertical-align:top;padding:10px 12px;border-bottom:1px solid #dde6ec}th{background:#edf4f7;white-space:nowrap}code{font:14px Consolas,monospace;background:#edf2f6;overflow-wrap:anywhere}pre{overflow:auto;background:#193247;color:white;padding:16px;font-size:13px;line-height:1.6;border-radius:8px}a{color:#086d9b;text-underline-offset:3px;overflow-wrap:anywhere}nav{display:flex;gap:12px 20px;flex-wrap:wrap}li{margin:8px 0}@media(max-width:700px){main{padding:12px 9px}header,section{padding:18px 14px}h1{font-size:25px}th,td{padding:8px}}@media print{body{background:white}main{max-width:none;padding:0}nav{display:none}}'''
    page=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>IEC104 复验、串值修复与运行效率 · 2026-11-13</title><style>{style}</style></head><body><main>
<header><p class="note">SCADA · 数据库解耦后 · 2026-11-13 · c104 {html.escape(result['c104'])}</p><h1>IEC104 常用功能复验<br>串值修复、报文兼容问题与运行效率</h1><div class="issue"><strong>结论：已修复一个实际数据正确性问题，但不能认定所有常用报文契约均通过。</strong>修复后 1/4/8 通道各 60 批更新通过；相关回归 {tests} 项、{subtests} 个子用例通过。独立原始 TCP 报文检查 {good} 项通过、{bad} 项未通过，未通过项没有被忽略或当作成功。</div><nav><a href="#fix">串值修复</a><a href="#issues">尚存问题</a><a href="#functions">功能范围</a><a href="#latency">运行效率</a><a href="#limits">验证边界</a><a href="#evidence">原始证据</a></nav></header>
<section id="fix"><h2>已修复：并发接收期间读取到其他点的值</h2><p>初次复验在 4 通道第 24 批超时；增加失败快照后，第二次在第 32 批复现。RTU 4 遥测点 4 的 S 数据库与线上 APDU 均为 <strong>4324</strong>，C 内存及数据库却为 <strong>4347</strong>，等于同帧点 27 的值；各采集线程没有通信错误。失败数据库、原始报文与状态记录均保留。</p><p>本地捆绑源码中，<code>DataPoint::getInfo()</code> 直接返回共享指针，而 <code>DataPoint::onReceive()</code> 会移动并替换该指针，二者没有同步。业务线程原来直接读取运行中的 <code>Point.info</code>，与原生接收线程发生并发访问；将报文按地址独立解码确认线上值正确后，问题定位在接收后点对象读取路径。</p><p><code>client/iec104.py</code> 现在由接收回调在该点的接收线程内复制值、质量和时间，保存为独立的不可变 Python 快照；业务 <code>value()</code>/<code>collect_once()</code> 读取这些快照。开始通信但尚未收到数据时返回无效状态，不把原生默认零值当成有效测量；旧点映射的回调不能覆盖新映射。</p><div class="ok">修复后，1/4/8 通道各完成 60 批全部点更新，无串值或监视数据超时。新增回归覆盖活跃连接不读取原生 info、未收到值时的无效状态，以及 4 客户端连续 32 批不同点值。原有双点、质量和 CP56 快照回归也通过。</div><p class="note">修改已保存并在隔离测试进程验证；本轮未重启生产 Web/C/S/T 服务。此修复保留最新值快照语义，没有新增 SOE 事件队列。</p></section>
<section id="issues"><h2>尚未修复的报文兼容问题</h2><p>使用独立编码的原始 TCP 对端进行检查，避免客户端和服务端使用同一个 c104 库时掩盖对称错误。对 YK 与 YT 各复现了以下三类问题：</p><ol><li><strong>S/T 共用服务适配器对撤销请求返回 COT=7。</strong>标准撤销确认的原因码是 9；当前库的 <code>Server::sendActivationConfirmation()</code> 对激活和撤销都填 7。成功/失败仍应由肯定或否定标志表达，而不是改用激活确认。原因码定义见 <a href="https://support.mz-automation.de/doc/lib60870/latest/group__COMMON.html">lib60870 官方 Common API</a>。</li><li><strong>C 端不接受独立对端的标准 COT=9。</strong>对端收到 COT=8 后回送同类型、同地址的肯定 COT=9，C 的撤销接口仍报“未被 c104 接受”。源码 <code>Connection::setCommandSuccess()</code> 的等待分支只把 COT=7 视为成功。</li><li><strong>选择执行结束帧保留 S/E=1。</strong>实际执行请求 S/E=0，ACTTERM 却从原选择 ASDU 克隆而来，保留选择位。独立检查按执行结束应对应执行请求验收，未通过。原生 <code>Server::unselect()</code> 使用保存的 selection.asdu 发结束帧。<a href="https://support.mz-automation.de/doc/lib60870/latest/group__IMASTER__CONNECTION.html">lib60870 官方说明</a>将 ACT_TERM 用于指示命令执行完成；这里不能把该结束帧当成设备动作完成证明。</li></ol>
<p>建议修复位置明确：c104 原生服务端区分 DEACTCON/ACTCON、客户端按发送原因等待相应确认，并将执行结束与选择释放分开。需要重新构建带版本号的本地依赖及重跑独立对端测试；本次没有修改二进制库，也没有用报文回调篡改日志或靠重复下发掩盖问题。</p>{failures}<p class="note">原始探针的进程退出码为 1，表示这些契约未通过。另一个完整链路结果中的 passed 仅代表该脚本既定的业务值、帧数量与顺序断言，不能覆盖此处的严格确认语义。</p></section>
<section id="functions"><h2>常用功能覆盖</h2>{features}<p>C/T 共享业务库，T 命令确认表示数据库受理；C 自主读库下发，再更新处理状态和 <code>cmd_handle_time</code>。S 保留独立模拟库。验收中 S 充当外部设备供 C 连接，未在 C/T 间引入报文或 IPC 依赖。</p><p>命令撤销没有再次提交 C 命令或刷新 S 处理时刻，这是业务保护结论；它不替代上面的 COT 与结束帧兼容验收。T 肯定响应也不表示现场执行器完成动作。</p></section>
<section id="latency"><h2>变位完整链路时延</h2>{latency}<p>从 S 数据库事务提交完成到 C 写库或所有 T 下游点可见计时；观察间隔 5 ms。每批修改全部监视点，等待该批完成后才提交下一批。每通道点数为 64 YC + 64 YX（1/4 通道）或 128 YC + 128 YX（8 通道），不含额外命令点。</p><p>S 扫描 50 ms、tick 100 ms；C 采集 50 ms；T 扫描/tick 50 ms；C 写库等待 100 ms、阈值 500 条；k=12、w=8。突发阶段 T 周期上送为 60000 ms，周期测试单独设为 200 ms。<strong>50 ms 配置不等于全链路 50 ms 刷新。</strong></p><h2>总召效率</h2>{gi}<p>请求发起至 ACTTERM 原始回调，每个 S/T 通道各重复 5 次。多通道列按各通道请求样本统计，不是同时完成所有通道总召的耗时。</p><h2>命令处理与故障恢复</h2>{commands}<p>每通道 YK/YT 各验证直接及自动选择执行；S 接收和 C 回执分开计时。每个负载场景只停止第 1 个 S 通道，然后验证自动重连与最新值恢复。P95 使用 nearest-rank；少量命令或总召样本的 P95 可能等于最大值。</p><h2>同量数据逐点发送与批量发送</h2>{efficiency}<p>同连接、同样 512 个遥测点，两种方式交替各 8 次并验证全部对象一致。监视 I 帧减少 {100*(1-bw['frames']/sw['frames']):.2f}%，对应 APDU 字节减少 {100*(1-bw['bytes']/sw['bytes']):.2f}%，中位接收耗时下降 {100*(1-median(batch,'latency')/median(single,'latency')):.2f}%。字节数不含 S/U 帧、TCP/IP 与链路层开销。</p><p class="note">这是当前实现中两种发送方式的对照，不是版本加速百分比。旧实现多通道已出现错误值，不能以其未完成的运行与修复后完整结果计算速度提升；本轮首先保证数据正确性。8 通道仍有约 0.74 秒的端到端 P95，后续优化需分阶段测量调度和写库等待。</p></section>
<section id="limits"><h2>其他验证边界</h2><p><strong>最新状态不等于逐条 SOE。</strong>独立用例在网上收到同点 21 次变位，采集返回 1 个最终快照；本次修复没有改变该语义。扫描间隔内的中间状态仍可能合并。</p><p>当前库仍不支持 CP24Time2a；支持自动“选择并执行”，没有单独“先选择、稍后执行”的高层接口。<code>max_asdu_length</code> 是诊断阈值而非强制组帧上限。回归验证 k/w/t1/t2/t3 配置与非法帧解析，但未完成所有异常序号、链路拥塞和 TCP 复位场景。</p><p>测试使用临时 S 与 C/T 数据库、本机随机端口、运行时线程及独立 C 批量写库进程。未测物理设备互操作、广域网延迟/丢包、完整生产监督器 CPU/内存曲线或长时间稳定性；不能据此宣称 IEC104 一致性认证通过。</p></section>
<section id="evidence"><h2>证据与复现</h2><p>工作目录 <code>D:\\codex\\scada</code>；基线 Git <code>{head[:12]}</code> 加未提交修改。依赖 c104 <code>{html.escape(result['c104'])}</code>。最终 1/4 通道开始于 <code>{html.escape(result['timestamp'])}</code>，8 通道开始于 <code>{html.escape(scale['timestamp'])}</code>；ISO 时间保留时区。</p><ul>
<li><a href="../reports/iec104_common_acceptance_20261113/recheck_01/final/results.json">修复后 1/4 通道、批量发送和快照边界结果</a>；<a href="../reports/iec104_common_acceptance_20261113/recheck_01/scale8/results.json">8 通道结果</a></li>
<li><a href="../reports/iec104_common_acceptance_20261113/recheck_01/final/wire_contracts.json">独立对端 16 项检查及完整 APDU</a>；<a href="../reports/iec104_common_acceptance_20261113/recheck_01/final/regression.xml">110 项回归 JUnit</a></li>
<li><a href="../reports/iec104_common_acceptance_20261113/recheck_01/diagnostic_4ch/failure-4ch-burst-32/state.json">修复前点值错误快照</a>；<a href="../reports/iec104_common_acceptance_20261113/recheck_01/diagnostic_4ch/failure-4ch-burst-32/wire.jsonl">对应原始报文</a>；<a href="../reports/iec104_common_acceptance_20261113/recheck_01/snapshot_before.xml">修复前回归失败记录</a></li>
<li><a href="../reports/iec104_common_acceptance_20261113/recheck_01/final/manifest.json">源码与证据 SHA-256 清单</a>；<a href="../tests/test_iec104_receive_snapshots.py">新增接收快照回归</a></li></ul>
<pre>Set-Location D:\\codex\\scada
&amp; D:\\anaconda3\\python.exe tools/verify_iec104_pipeline.py --rounds 60 --output reports/iec104_local_repeat
&amp; D:\\anaconda3\\python.exe tools/verify_iec104_pipeline.py --channels 8 --points 128 --rounds 60 --skip-comparison --output reports/iec104_local_repeat/scale8
&amp; D:\\anaconda3\\python.exe tools/verify_iec104_wire_contracts.py --output reports/iec104_local_repeat</pre><p class="note">性能测试顺序执行；原始报文探针当前会如实返回失败。报告正文和样式完全内嵌，证据及官方文档链接为可选补充。</p></section></main></body></html>'''
    destination=ROOT/'docs/iec104_recheck_20261113.html'
    destination.write_text(page,encoding='utf-8')
    print(json.dumps(dict(report=str(destination),tests=tests,subtests=subtests,wire_pass=good,wire_fail=bad),ensure_ascii=False))


if __name__=='__main__':main()
