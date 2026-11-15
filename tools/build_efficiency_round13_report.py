"""Standalone report including repeated, non-cherry-picked round13 results."""
import html
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'output/efficiency_round13_20261115'


def main():
    def read(name):return json.loads((OUT/name).read_text(encoding='utf-8-sig'))
    data={name:read(name+'.json') for name in ('history-before','history-after','feedback-before','feedback-after','writer-before','writer-after','soe')}
    data['modbus']=read('modbus/results.json')
    data['rtu-repeats']=read('rtu-repeats.json')
    for mode in ('before','after'):
        for repeat in (1,2):data[f'iec104-{mode}-{repeat}']=read(f'iec104-{mode}-{repeat}/results.json')
    assert all(x['passed'] for key,x in data.items() if key not in ('soe','modbus','rtu-repeats'))
    assert all(x['passed'] for x in data['modbus']['pipelines'])
    assert len(data['rtu-repeats'])==3 and all(x['passed'] for x in data['rtu-repeats'])
    assert all(x['exact'] for x in data['soe']['pipelines'])
    testlog=(OUT/'regression-final.txt').read_text(encoding='utf-8-sig')
    assert not re.search(r'\d+ failed',testlog)
    tests=re.findall(r'\d+ passed[^\r\n]*',testlog)[-1]
    ui=json.loads((ROOT/'output/playwright/efficiency-round13/fixture-results.json').read_text());assert ui['passed']
    rows=[]
    def row(label,a,b):rows.append(f'<tr><td>{label}</td><td>{a:.3f}</td><td>{b:.3f}</td><td>{(b/a-1)*100:+.1f}%</td></tr>')
    row('历史完整页 → 摘要页（100次，中位数）',data['history-before']['full']['median_ms'],data['history-after']['full']['median_ms'])
    row('100点无待核对命令时回采处理（100轮，中位数）',data['feedback-before']['median_ms'],data['feedback-after']['median_ms'])
    row('预置250ms队列年龄后的提交可见时间（12次，中位数）',data['writer-before']['median_ms'],data['writer-after']['median_ms'])
    row('指令六次FULL提交（60次，中位数）',data['history-before']['command_lifecycle']['median_ms'],data['history-after']['command_lifecycle']['median_ms'])
    micro=''.join(rows);rows=[]
    for repeat in (1,2):
        for a,b in zip(data[f'iec104-before-{repeat}']['pipelines'],data[f'iec104-after-{repeat}']['pipelines']):
            for percentile,label in [('median_ms','中位数'),('p95_ms','P95')]:
                row(f"第{repeat}轮 / {a['channels']}通道 / {label}",a['end_to_end_latency'][percentile],b['end_to_end_latency'][percentile])
    linkrows=''.join(rows)
    text=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SCDFE 第十三轮优化与验收 · 2026-11-15</title>
<style>body{{max-width:1120px;margin:32px auto;padding:0 20px;background:#f3f7fa;color:#193649;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}}section{{background:white;padding:24px;margin:20px 0;border:1px solid #dce6ed;border-radius:12px}}h1,h2{{color:#007b88}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #dce6ed}}th{{background:#edf6f7}}.scroll{{overflow:auto}}.note{{border-left:4px solid #b78930;padding-left:14px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}code{{color:#006878}}</style></head><body>
<h1>SCDFE 第十三轮优化与验收</h1><p>2026-11-15 · 纯 Python · S 独立库 / C、T 共享业务库 · T 仅 IEC104 / Modbus TCP</p>
<section><h2>本轮完成内容</h2><p>写库批次期限从消息实际入队时刻计算，排队时间计入等待预算，避免积压消息出队后再等完整周期。到期时最多预取 64 条已经排队的消息，并遵守批次上限；不会越过排空或停止标记。锁冲突保留批次，失败时不确认排空成功，正常停止仍等待前面的事务提交。</p>
<p>新增最近 16 个慢批次记录（入队至提交至少 100 ms），关联时间、RTU、消息/更新数、排队、锁等待、退避、SQL、提交及回执日志耗时。按原诊断快照频率保存，不逐批写日志。各列来自同一批次，不能把不同阶段的分位数相加；排队包含合批、调度等因素，提交可能包含检查点，没有直接认定根因。</p>
<p>回采只订阅尚在等待验证的命令目标，通过数据库版本提示及时刷新，不再为只有配置、没有待验证命令的点持续写样本。确认、超时或人工结案后撤销订阅；冻结规则语义不变。只验证最新实际接收样本，短暂状态仍可能在扫描之间被覆盖，本轮未实现回采事件序列或稳定持续时间判断。</p>
<p>指令页面使用摘要接口，详情、完整时序、错误、回采和核对内容按需读取；原完整接口默认行为保留。增加 RTU 换通道后的缓存失效。人工核对提交前重新读取详情，并继续进行服务端版本检查。</p></section>
<section><h2>不确定指令的人工结案</h2><p>在“指令追踪 → 详情 / 结案”处理派发、选择、选择确认或执行请求后结果未知的命令。必须先停止原派发进程并核实现场；填写操作人声明、依据，勾选只结案不重发。C/T 接口在 FULL 事务中复核原进程已退出、阶段和命令时标一致、旧命令仍占用待处理槽位，随后释放该槽位并保存结案时间/人员/依据。</p>
<p>原规约阶段和完成时刻保持不变，点状态保留未确认/失败语义（3）；不伪造成功，不发送控制。后续控制须通过原控制入口独立提交新命令，获得新时标。已结案、原进程仍活着、槽位被后续命令替换或阶段发生变化时拒绝操作。S 仅保存接收记录，不提供派发结案接口。</p>
<p class="note">这是保守的进程级限制：只停止一个通信线程、原采集进程仍活着时不能结案；停止共享采集进程会影响其余通道，应由操作者安排。缺少原进程标识的旧记录不自动释放。操作人是填写者声明，另有请求来源审计，不等于登录认证。人工结案不会降低正常命令 FULL 持久化要求。</p></section>
<section><h2>受控性能对比</h2><div class="scroll"><table><tr><th>场景</th><th>之前 ms</th><th>之后 ms</th><th>耗时变化</th></tr>{micro}</table></div>
<p>历史数据预置 5000 条、每页 50 条，预热 5 次后统计 100 次：响应体 {data['history-before']['full']['body_bytes']} → {data['history-after']['full']['body_bytes']} 字节；304 无变化路径仍保留。回采场景 100 点 × 100 轮、没有待核对命令，写入样本从 {data['feedback-before']['sample_rows_submitted']} 行降为 {data['feedback-after']['sample_rows_submitted']} 行。</p>
<p>队列实验使用真实写库子进程，将输入时间戳人工设为已等待 250 ms，批次周期 200 ms，通过 2 ms 查询观察提交；测量的是此后追加的提交可见时间，不包含人工预置的 250 ms，也不是现场端到端吞吐。结果证明不再额外等待一个完整周期。正常指令 FULL 提交本轮未优化，耗时变化如表记录。</p></section>
<section><h2>真实 IEC104 链路：两轮完整结果</h2><div class="scroll"><table><tr><th>场景</th><th>之前 ms</th><th>之后 ms</th><th>耗时变化</th></tr>{linkrows}</table></div>
<p>顺序执行旧版第一轮 → 新版第一轮 → 旧版第二轮 → 新版第二轮。每轮 1/4 通道、每通道 64 YC + 64 YX、40 批变化；每种通道规模、每个版本共 80 批。S→TCP→C 写库子进程→业务库→T→TCP 下游。原运行服务和其他本机任务保持运行，没有并行执行其他基准或测试负载；这不是独占机器环境。</p>
<p class="note">轮间波动明显，旧版四通道中位数也从约 1.25 秒变为约 203 ms；新版既有改善也有退化。不能将单轮差值或合并结果全归因于修改，暂不宣称整体稳定提速。已确认的代码路径是队列年龄未计入批次期限，不能据此追认上一轮每个延迟尖峰的原因。</p>
<p>当前版第一轮完整运行中的写锁最大等待约 450 ms（单通道）/435 ms（四通道），提交最大约 3.3/5.1 ms；这些窗口含总召、控制、断线恢复等阶段，不是只统计变化批次。说明本次采样中确有锁等待尖峰，不能把所有慢提交解释为检查点；还需关联具体写入者和操作。完整分段快照、同批次记录和报文均随原始结果保留。</p></section>
<section><h2>正确性与交付</h2><p>最终回归：{html.escape(tests)}。指令页面隔离 Chromium 验证 {len(ui['checks'])} 项通过，运行诊断原有 13 项检查通过。包括回采立即订阅/退出、无命令不写样本、原数据迁移、活进程拒绝结案、重复结案拒绝、旧命令不重放、新命令新时标、C/S/T 实际 HTTP、缓存换通道失效、排空标记顺序及失败队列计数。</p>
<p>IEC104 两轮均覆盖总召、质量变化、变位、周期、遥控/遥调直接与选择执行、撤销、断线恢复和地址隔离。Modbus TCP 1/4 通道常用功能通过。SOE 1/4 通道各 128 条共 {sum(x['expected'] for x in data['soe']['pipelines'])} 事件，逐条核对状态、质量、原始 CP56 和顺序一致。没有真实 CAN/串口、Linux 实机、硬件断电或 24/72 小时运行验收。</p>
<p class="note">保留的失败：首次 Modbus RTU socket:// 验收在 T 的 FC05 回应处超过客户端 500 ms 超时。日志显示请求写源库到已入库约 776 ms，随后 C 已下发、S 已接收、处理结果已入库；不能把接收超时理解为未执行，更不能自动补发。保持同样代码、500 ms 超时及负载，独立复测 3 次均通过，未因此删除首次失败或放宽门槛。该次入库内部哪个阶段慢尚未取证，不宣称已经解决，也不承诺所有响应小于 500 ms。</p>
<p>新增接口：<code>GET /api/command-traces?view=summary</code>、<code>GET /api/command-traces/{{id}}</code>、<code>POST /api/command-traces/{{id}}/resolve</code>。详情携带结案阻止原因，服务端提交时再次核实。回采规则与原审计入口沿用。</p>
<p>修改前工作区快照、测试、性能数据、数据库备份及恢复凭据在 <code>output/efficiency_round13_20261115/</code>；脚本为 <code>tools/benchmark_efficiency_round13.py</code>、<code>tools/benchmark_efficiency_round12.py --summary</code>、<code>tests/test_efficiency_round13.py</code>、<code>tests/frontend_efficiency_round13.cjs</code>。按快照恢复此前运行服务；没有对现场建立回采规则或发送测试指令，没有提交/推送 Git。</p>
<details><summary>全部原始测量数据（内嵌）</summary><pre>{html.escape(json.dumps(data,ensure_ascii=False,indent=2))}</pre></details></section></body></html>'''
    target=ROOT/'docs/scdfe_efficiency_20261115_round13.html';target.write_text(text,encoding='utf8');print(target)


if __name__=='__main__':main()
