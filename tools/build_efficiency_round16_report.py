"""Generate an offline report of display-write coalescing and acceptance."""
import html
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/efficiency_round16_20261115'


def main():
    def load(name):return json.loads((OUT/name).read_text(encoding='utf8'))
    data={name:load(name+'.json') for name in ('before','after','before-repeat','after-repeat','soe')}
    data.update(modbus=load('modbus/results.json'),iec104=load('iec104/results.json'))
    assert all(data[n]['passed'] for n in ('before','after','before-repeat','after-repeat','modbus','iec104'))
    assert all(p['exact'] for p in data['soe']['pipelines'])
    tests=(OUT/'regression-verified.txt').read_text(encoding='utf-8-sig')
    assert not re.search(r'\d+ failed',tests)
    data['tests']=re.findall(r'\d+ passed[^\r\n]*',tests)[-1]
    ui=[json.loads(x) for x in (OUT/'frontend.txt').read_text(encoding='utf-8-sig').splitlines() if x.startswith('{')]
    assert all(x['passed'] for x in ui);data['frontend']=ui
    rows=[]
    for suffix,order in [('', '旧→新'),('-repeat','新→旧')]:
        for a,b in zip(data['before'+suffix]['cases'],data['after'+suffix]['cases']):
            rows.append(f"<tr><td>{order}</td><td>{a['commands']} / {a['rtus']}</td><td>{a['commits'][0]} → {b['commits'][0]}</td><td>{a['batch']['median_ms']:.3f} → {b['batch']['median_ms']:.3f}</td><td>{a['batch']['p95_ms']:.3f} → {b['batch']['p95_ms']:.3f}</td><td>{a['response']['median_ms']:.3f} → {b['response']['median_ms']:.3f}</td></tr>")
    body=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SCDFE 第十六轮优化与验证</title>
<style>body{{max-width:1120px;margin:32px auto;padding:0 22px;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif;background:#f3f7fa;color:#203b4d}}section{{padding:24px;background:white;border:1px solid #dce5ec;border-radius:12px;margin:20px 0}}h1,h2{{color:#007e89}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid #dce5ec;padding:10px}}th{{background:#edf5f6}}.scroll{{overflow:auto}}.note{{border-left:4px solid #b98425;padding-left:15px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}</style></head><body>
<h1>SCDFE 第十六轮优化与验证</h1><p>2026-11-15 · 纯 Python · 保留 S 独立库、C/T 共享业务库及数据库解耦</p>
<section><h2>定位与改进</h2><p>T 原先在源库指令 FULL 提交后，立即逐请求再写一次四遥显示状态。扫描线程与协议/SOE 回调可能同时刷新同一状态表；日志写入异常还可能从已提交之后的回调抛出，导致协议否定应答。四项定向测试在修改前分别复现了同步附加写入、提交后日志异常、并发旧快照覆盖风险和每请求两次提交。</p>
<p>指令确认仍以源库 FULL 提交为准；显示状态和确认业务日志由扫描或约 100 ms 的独立定时任务刷新。最新值按点与字段合并，只有一个显示批次进入 SQL；数据库提交期间可继续缓存新状态，成功后只移除本批已保存字段。显示写锁忙时立即返回并保留状态，不在协议回调里等待第二个事务，也不把显示状态失败解释为控制失败。</p>
<p>相同字段集采用同一 executemany 批次，保留定义版本号。临时 busy_timeout 设置在退出时恢复；失败整批回滚并重试最新状态。业务日志异常不阻止显示保存；部分写入结果未知的日志不自动重放，诊断计数标记未确认。正常退出尝试保存剩余状态，长期锁冲突仍可能留下未保存显示快照；持久化指令追踪保持完整。</p>
<p>SOE 确认回调只排入 forward_time 显示更新，逐条事件存储、时间标签、转发顺序与确认游标不改为快照。C/S 通信实现和业务表结构未在本轮调整。T 的规约入口仍是 IEC104 与 Modbus TCP。</p></section>
<section><h2>前后性能</h2><div class="scroll"><table><tr><th>运行顺序</th><th>独立请求数 / RTU 数</th><th>数据库提交次数</th><th>整组耗时中位 ms</th><th>整组耗时 P95 ms</th><th>单请求回调中位 ms</th></tr>{''.join(rows)}</table></div>
<p>每种规模预热 2 组、测量 8 组，临时 SQLite WAL 数据库、真实 FULL 指令提交，组末保存显示状态；保持每条独立指令的命令时标和追踪条数。各轮为相反顺序，机器有正常运行服务及独立验收负载，非独占机器。未进行设备通信；“单请求回调”不包含客户端网络往返，8 样本 P95 为最大值。</p>
<p class="note">100 条时从 200 次提交降为 101 次，减少的是显示快照事务，100 次指令 FULL 提交全部保留。25 条由 50 次降为 26 次；单条仍为 2 次，完整落库耗时可能变差。实际运行中若扫描插入突发请求之间，会形成多个显示批次，不能保证现场始终达到一次合并。</p></section>
<section><h2>诊断与交互</h2><p>运行诊断新增“转发显示状态写库”，显示待保存点数、等待时长、已保存批次和点次、失败/锁冲突、未确认日志条数、最近错误；同时提供显示写锁、SQL、提交和写库总耗时阶段。正常轮询取既有运行状态 JSON，不新增表，不触发额外检查点。相同表格内容保留 DOM，错误文本转义，刷新失败保留既有数据，过期快照明确显示。</p>
<p>100 ms 是目标调度间隔，磁盘、日志与线程调度仍会引入延迟。直接嵌入使用 TransRuntime.command 的调用方应运行调度循环，或调用 refresh/_flush_point_runtime 以刷新显示；协议成功应答含义仍是源库已接受，不是现场已执行。强制终止可能丢失尚未保存的显示状态及尚未写出的业务日志，不能据此认为源库指令不存在。</p></section>
<section><h2>正确性与限制</h2><p>{html.escape(data['tests'])}；Chromium 页面 {sum(len(x['checks']) for x in ui)} 项检查通过。覆盖已提交指令与观察日志解耦、并发刷新序列、突发合并、整批回滚、定义版本保持、锁冲突即时返回及恢复原连接参数、长扫描周期下的独立刷新、运行诊断 API、原有 Modbus 请求归属/限额与控制恢复逻辑。测试期间发现一处“收到网络响应即假定计数已更新”的测试竞态，改用事件循环屏障读取诊断后复测；原始失败保留。</p>
<p>Modbus TCP 1/4 通道和 RTU socket:// 常用功能链路通过；IEC104 1/4 通道总召、变位、质量、周期、遥控/遥调选择执行、直接执行、撤销与重连通过。SOE 单独验收 {sum(p['expected'] for p in data['soe']['pipelines'])} 条，持久化及转发的状态、质量、原始时标、顺序一致；效率数据见原始结果。</p>
<p class="note">本轮降低重复写入与竞争，没有证明此前约 885 ms FULL 提交尖峰的磁盘级根因已消除。仍需进一步关联文件同步、检查点和并发写入者，再做多通道长稳及真实 CAN/RS485 故障恢复。没有硬件断电、Linux 实机或 24/72 小时验收。</p>
<p>源码基线、失败及最终测试、前后性能、协议与 SOE 原始结果保存在 <code>output/efficiency_round16_20261115/</code>。使用说明同步更新，未向运行站点发送测试控制。</p>
<details><summary>完整原始数据（内嵌）</summary><pre>{html.escape(json.dumps(data,ensure_ascii=False,indent=2))}</pre></details></section></body></html>'''
    path=ROOT/'docs/scdfe_efficiency_20261115_round16.html';path.write_text(body,encoding='utf8');print(path)


if __name__=='__main__':main()
