"""Produce a self-contained SOE audit report, keeping failed requirements visible."""
from pathlib import Path
import html
import json
import re

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=ROOT/'reports/iec104_soe_20261114'


def table(headers,rows):
    return '<div class="table"><table><tr>'+''.join(f'<th>{html.escape(str(v))}</th>' for v in headers)+'</tr>'+''.join(
        '<tr>'+''.join(f'<td>{html.escape(str(v))}</td>' for v in row)+'</tr>' for row in rows)+'</table></div>'


def main():
    r=json.loads((EVIDENCE/'final/results.json').read_text(encoding='utf-8'))
    assert r['audit_completed'] and not r['soe_accepted']
    d,o,z=r['database'],r['overload'],r['timezone']
    log=(EVIDENCE/'regression.log').read_text(encoding='utf-8-sig')
    summary=re.search(r'\d+ passed[^\r\n]*',log)
    assert summary
    retry=(EVIDENCE/'supervisor_recheck.log').read_text(encoding='utf-8-sig')
    assert '2 passed' in retry and 'FAILED ' not in retry
    regression_text='广域回归首轮：2 failed, '+summary.group()+'；两项调度停止／重载测试单独复验：2 passed。首轮失败记录保留，未修改这两项测试或其业务代码。'
    passed=sum(c['passed'] for c in r['requirements']);failed=len(r['requirements'])-passed
    perf=table(['通道','事件数 / 接收数','吞吐 条/秒','发送→接收回调中位 ms','P95 ms','最大 ms'],[
        [p['channels'],f"{p['expected']} / {p['received']}",p['throughput_events_s'],
         p['send_to_callback_latency']['median_ms'],p['send_to_callback_latency']['p95_ms'],
         p['send_to_callback_latency']['max_ms']] for p in r['performance']])
    requirements=table(['专项验收条件','结论'],[
        [c['name'],'通过' if c['passed'] else '未通过'] for c in r['requirements']])
    data=table(['位置','输入','实测输出','判定'],[
        ['S 数据库→上送',f"{d['s_commits']} 次分别提交的变位",f"{d['s_spontaneous_events']} 条最终状态",'中间事件未保留'],
        ['C 接收→业务采集',f"{d['c_incoming_events']} 条真实 TCP 事件",f"{d['c_collected_snapshots']} 个当前值快照",'没有逐事件日志'],
        ['C/T 原始时标',d['source_time'],d['t_forwarded_time'],'源时标被替换'],
        ['延迟 ACK 超载',f"{o['submitted']} 条上送均被接受",f"{o['received']} 条收到，缺 {o['missing']} 条",'有限缓冲仍会静默覆盖'],
        ['T 重启后总召','读取已有业务库',f"{d['t_restart_samples']} 个当前值",'GI 正常；不存在历史事件恢复机制']])
    page=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IEC104 SOE 正确性与效率验收 · 2026-11-14</title><link rel="icon" href="data:,">
<style>*{{box-sizing:border-box}}body{{margin:0;background:#edf3f7;color:#173246;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1140px;margin:auto;padding:28px 22px}}header,section{{padding:25px;background:white;border:1px solid #d8e3eb;border-radius:12px;margin-bottom:18px}}header{{border-top:6px solid #ae3c32}}h1{{font-size:30px;line-height:1.4}}h2{{font-size:22px;margin:0 0 14px}}h3{{font-size:18px}}.muted{{color:#52697a}}.failure{{border-left:5px solid #ae3c32;background:#fff5f1;padding:15px}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}}.card{{padding:15px;background:#eef5f7;border-radius:8px}}.card strong{{font-size:25px;display:block}}nav{{display:flex;gap:15px;flex-wrap:wrap}}a{{color:#006982}}.table{{overflow-x:auto;margin:16px 0}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:11px 9px;text-align:left;border-bottom:1px solid #dbe4eb;vertical-align:top;min-width:85px;overflow-wrap:anywhere}}th{{background:#edf5f7}}code,pre{{font:14px/1.65 Consolas,monospace;background:#edf3f6;overflow-wrap:anywhere}}pre{{white-space:pre-wrap;padding:16px}}li{{margin:8px 0}}footer{{padding:12px;color:#52697a}}@media(max-width:650px){{main{{padding:10px}}header,section{{padding:17px}}h1{{font-size:25px}}.cards{{grid-template-columns:1fr}}.table::before{{content:'表格可左右滑动';display:block;color:#52697a;font-size:12px;margin-bottom:5px}}}}</style></head><body><main>
<header><p class="muted">D:\\codex\\scada · 本机 TCP／临时数据库 · 2026-11-14</p><h1>IEC104 SOE 正确性与效率验收</h1>
<p class="failure"><strong>结论：完整 SOE 功能未通过验收。</strong>带时标报文可以收发，但当前系统保存的是最新状态。逐条事件持久化、源时标和时间标志保留、可恢复的事件转发尚未形成完整链路。</p>
<div class="cards"><div class="card"><strong>{passed} 通过 / {failed} 未通过</strong>专项验收条件</div><div class="card"><strong>66,560 条</strong>受控负载完整接收</div><div class="card"><strong>{o['missing']} 条缺失</strong>16384 条延迟 ACK 超载</div></div>
<p>已修复 C 点表类型被通道默认值覆盖的问题；新增回归与自动验收工具。通过的软件回归不代表 SOE 功能通过。本轮没有新增事件业务表，没有切换生产服务。</p>
<nav><a href="#correctness">正确性</a><a href="#loss">事件丢失位置</a><a href="#performance">效率</a><a href="#repair">修复与后续</a><a href="#reproduce">复现证据</a></nav></header>
<section id="correctness"><h2>正确性覆盖与结果</h2>{requirements}
<p>报文层覆盖 M_SP_TB_1（30）、M_DP_TB_1（31），单点 0/1、双点 0/1/2/3，BL/SB/NT/IV 质量位，毫秒分辨率、闰日跨日、同毫秒不同变位、源时钟回退以及接收线序。独立解码器同时验证 SQ 顺序地址、CA/IOA 和 COT，独立 TCP 对端注入时间标志及 COT=20。</p>
<p>普通总召与变位事件需要区分。业务快照没有 COT 和原始双点状态字段，双点 0/3 都会映射为 None 并增加无效质量；这一处理适用于布尔状态画面，但不足以保存完整 SOE 原始记录。</p>
<h3>时间接口边界</h3><p>本机把 <code>{z['requested_utc']}</code> 直接交给原生 Information 构造器时，线上时间为 <code>{z['wire_time']}</code>，偏移 <strong>{z['offset_seconds']/3600:g} 小时</strong>。返回值为无时区的本地时间。此项测试的是原生 API，不是用户在 HMI 输入事件时间的流程。</p>
<p>pybind 的 chrono 转换会忽略输入 datetime 时区，并返回本地 naive datetime；这与绑定源码和公开文档一致。<a href="https://pybind11.readthedocs.io/en/stable/advanced/cast/chrono.html">pybind11 时间转换说明</a>。CP56 本身没有时区标识，工程必须约定 UTC 或地方时；本报告独立对端约定 UTC，受控发送夹具显式转换到原生要求的本地 naive 输入。此转换仅在测试夹具中，未伪装成产品时间修复。</p>
<p>时间 IV、SU、替代标志和测点质量是不同字段。原始帧能携带这些位，但当前业务快照未暴露它们。<a href="https://support.mz-automation.de/doc/lib60870/latest/group__COMMON.html">lib60870 信息对象与 CP56 API</a>。</p></section>
<section id="loss"><h2>事件在哪一步被合并或丢失</h2>{data}
<p>S 测试使用现有扫描锁暂停一次扫描，在此期间完成 21 次独立提交；释放后由 S 自主扫描上送。C 测试让 101 条真实报文先到达，再运行一次真实 collect_once 与 ChannelWorker 写库。T 使用真实共享数据库和 refresh 方法。这里控制扫描窗口以确定性复现合并，不把人工暂停时间当作正常运行延迟。</p>
<p>同点连续变化在 C 的最新值字典中覆盖；写库只更新四遥当前值和本地刷新时间，T 又从该快照生成报文。源码与当前表结构未发现持久化事件日志及消费游标。T 重启后总召只返回当前值属于正常 GI 行为；缺少历史恢复的依据是没有事件存储和重放机制，不能要求总召本身回放全部历史。</p>
<p>超载测试先发送 16384 条短报文，再开始接收和 ACK，等待流量排空后统计，无重复收到事件。原生发送调用均返回接受，缓冲溢出未向调用者返回逐事件失败。1024 是按最大 ASDU 分配缓冲的容量参数；小帧可以容纳更多条，不能把它理解为严格的 1024 条上限，也不能保证无限事件无损。</p></section>
<section id="performance"><h2>带时标报文收发效率</h2>{perf}
<p>每通道 2 个遥信点，单点／双点交替；每批每通道 512 条，10 批。每条都校验点身份、值、质量、毫秒源时标和顺序，不以最终值正确替代事件计数。每批接收完整后再发送下一批；k=12、w=8，服务端 tick=50 ms。</p>
<p class="failure">以上是 S 适配层→TCP→C 接收回调的效率，包含本机驱动、时间转换及测试观察回调开销。C/S/T 的持久化 SOE 链路尚不存在，因此逐事件落库吞吐、无损转发端到端时延与重启恢复耗时均为<strong>无法有效评估</strong>，没有用最终状态刷新时间冒充这些指标。</p>
<p>测试在同一 Python 进程中运行多组本机通信线程，不是独立主机容量认证；不包含现场网络、长时间稳定性、磁盘满、进程崩溃、持续超载或持久化队列恢复。超载实验单独报告，不计入成功吞吐。</p></section>
<section id="repair"><h2>已修复与仍需补齐的内容</h2>
<p><strong>已修复：</strong>IEC104Master.from_store 原来总是填入通道默认 data_type，覆盖点映射 type_id；带时标 30/31/32 以及普通双点／步位置信息可能被注册成单点。现在优先使用显式点类型，保留调用者覆盖和未映射时的默认值。修复前回归复现 5 个类型失败，修复后实际 S 数据库→C 接收链路通过。</p>
<p><strong>能力接口：</strong>C/S/T 的 <code>/api/protocol-capabilities</code> 新增 <code>monitoring_semantics=latest_point_snapshot</code>、<code>soe_event_log=false</code> 和 <code>soe_lossless_forwarding=false</code>，避免把 spontaneous=true 理解为完整 SOE。</p>
<p><strong>代码回归：</strong>{html.escape(regression_text)} 类型映射、SOE 解码、原有 IEC104 收发及能力 API 检查通过；专项验收仍有 {failed} 项未通过。回归通过项和复验结果不能掩盖 SOE 功能缺口。</p>
<ol><li>S 独立库需要追加式事件记录，值／质量修改与事件生成同事务提交；保留事件序号、源时间、原始状态与时间标志。</li><li>C 接收需要独立事件队列，批量写入共享业务库，保留 COT、CA、IOA、类型、原始 CP56 和接收序号；不能按点号去重覆盖事件。</li><li>T 从事件日志按消费游标读取并转发，跨重启保存进度，明确重传／去重、确认和离线保留策略；C/T 继续只经数据库交互。</li><li>建立队列容量、溢出告警和背压策略，分别测线上吞吐、持久化吞吐、端到端延迟及恢复能力；最新值表继续服务现有画面。</li></ol>
<p>上述四项是后续实现清单，本轮未宣称已经完成；也未要求 GI 充当事件历史查询。</p></section>
<section id="reproduce"><h2>复现与证据</h2><pre>output/iec104-native-20261114/venv/Scripts/python.exe tools/verify_iec104_soe.py --channels 1 4 8 --events 512 --rounds 10 --output reports/iec104_soe_20261114/reproduce

# 退出码 0：SOE 全部通过；1：验收完成但有功能未通过；2：测试本身中断。
# 当前预期是退出码 1，不能将其隐藏为通过。</pre>
<p>完整结果：<code>reports/iec104_soe_20261114/final/results.json</code>；包含原始时标探针、独立对端 APDU、队列超载 APDU 和数据库边界结果。另有 final.log、type_before.xml、type_after.xml、regression.xml／regression.log、supervisor_recheck.xml／log。</p>
<p>验证原生库：<code>{r['c104']}</code>；扩展 SHA-256：<code>{r['native_sha256']}</code>。业务库使用临时目录，不修改正在运行的 C/T 共享库或 S 独立库。文档日期按用户时区 Pacific/Auckland，JSON 保留 UTC 采样时间。</p></section>
<footer>单文件 HTML，正文、数据与样式均已内嵌，可离线阅读。普通 IEC104 接收正确性与完整 SOE 能力分别评价。</footer></main></body></html>'''
    path=ROOT/'docs/iec104_soe_acceptance_20261114.html';path.write_text(page,encoding='utf-8')
    print(path);print(summary.group());print(f'SOE: {passed} passed, {failed} failed')


if __name__=='__main__':main()
