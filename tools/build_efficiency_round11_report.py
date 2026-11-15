"""Build a standalone report from round 11 measurements, without inventing gains."""
import html
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/efficiency_round11_20261115'


def read(name):
    return json.loads((OUT/name).read_text(encoding='utf8'))


def main():
    before,after=read('t-before-initialized.json'),read('t-after-initialized.json')
    iec_before,iec_after=read('iec-before/results.json'),read('iec-after/results.json')
    modbus,soe=read('modbus/results.json'),read('soe.json')
    slow_before,slow_after=read('slow-before.json'),read('slow-after.json')
    assert all(x['passed'] for x in (before,after,iec_before,iec_after,modbus))
    assert all(x['exact'] for x in soe['pipelines'])
    assert slow_before['fast_received']==128 and slow_after['fast_received']==384
    results=(OUT/'regression-final.txt').read_text(encoding='utf8')
    test_summary=re.findall(r'\d+ passed[^\r\n]*',results)[-1]
    assert not re.search(r'\d+ failed',results)
    ui=json.loads((ROOT/'output/playwright/efficiency-round11/fixture-results.json').read_text())
    assert ui['passed']
    rows=[]
    def row(label,a,b):
        rows.append(f'<tr><td>{label}</td><td>{a:.3f}</td><td>{b:.3f}</td><td>{(b/a-1)*100:+.2f}%</td></tr>')
    for mode,label in [('idle','T 空闲刷新'),('sparse','T 单点变化刷新'),('dense','T 8000 点变化刷新')]:
        row(label,before[mode]['refresh']['median_ms'],after[mode]['refresh']['median_ms'])
    row('8000 点源库事务',before['dense']['source_write']['median_ms'],after['dense']['source_write']['median_ms'])
    row('T 8000 点变化刷新 P95',before['dense']['refresh']['p95_ms'],after['dense']['refresh']['p95_ms'])
    dense_rows=''.join(rows);rows=[]
    for a,b in zip(iec_before['pipelines'],iec_after['pipelines']):
        for key,label in [('end_to_end_latency','端到端变化'),('command_receipt_latency','指令回执')]:
            row(f"IEC104 {a['channels']} 通道 {label}中位数",a[key]['median_ms'],b[key]['median_ms'])
        row(f"IEC104 {a['channels']} 通道 端到端 P95",a['end_to_end_latency']['p95_ms'],b['end_to_end_latency']['p95_ms'])
    iec_rows=''.join(rows)
    soe_rows=''.join(f"<tr><td>{x['channels']}</td><td>{x['expected']}</td><td>{x['c_persisted']}</td><td>{sum(x['t_received'])}</td><td>{x['end_to_end_ack_ms']}</td></tr>" for x in soe['pipelines'])
    data=dict(t_before=before,t_after=after,null_timestamp_before=read('t-before.json'),null_timestamp_after=read('t-after.json'),
              iec_before=iec_before,iec_after=iec_after,modbus=modbus,soe=soe,slow_before=slow_before,slow_after=slow_after)
    text=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SCDFE 第十一轮优化验证 · 2026-11-15</title><style>
body{{max-width:1120px;margin:32px auto;padding:0 24px;background:#f3f7fa;color:#193649;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}}section{{background:white;border:1px solid #dce5eb;border-radius:12px;padding:24px;margin:20px 0}}h1,h2{{color:#007f89}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid #dce5eb;padding:10px}}th{{background:#eff7f8}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}code{{color:#006873}}.note{{border-left:4px solid #bd8e31;padding-left:14px}}
</style></head><body><h1>SCDFE 第十一轮优化验证</h1><p>2026-11-15 · 纯 Python · S 独立数据库，C/T 共享业务库 · 修改前工作区快照对照</p>
<section><h2>功能与正确性</h2><p>修复刷新时间为 NULL 的遥测/遥信点值或质量变化后不更新时间的问题；自动刷新时间只维护一次变化索引。T 根据变化比例选择变化点或整组读取，保持删除、空值、映射边界及失败重试语义。SOE 不使用最新值合并。</p>
<p>C/S/T 新增“指令追踪”：入库、派发、IEC104 选择/确认、执行请求及结果阶段持久化；C/T 共用记录，S 保存接收记录。同一毫秒多个 S 接收指令独立保留。派发状态在设备 I/O 前提交，未知结果不自动重发；已确认但点表结果丢失时恢复状态，避免重复执行。驱动明确返回 False 按失败处理。</p>
<p class="note">IEC104 激活确认、Modbus 响应和驱动调用完成均不代表设备实际动作完成。当前未关联回采点，页面明确显示“未关联回采验证”。S 选择/撤销查业务日志；升级前历史不补造，指令表暂不自动清理；不确定命令需人工核对，未提供自动重发。</p>
<p>接口 <code>GET /api/command-traces</code> 支持 RTU/点/阶段过滤与命令编号游标分页。最新页 5 秒刷新，隐藏与历史页暂停，单请求防重叠，未变化行保留 DOM，错误保留已有数据。</p></section>
<section><h2>批量变化与数据库写入</h2><table><tr><th>场景</th><th>之前 ms</th><th>之后 ms</th><th>耗时变化</th></tr>{dense_rows}</table>
<p>16 RTU × 1000 点，共 8000 YC + 8000 YX；1 个 T 通道映射全部点；每场景 40 次。执行实际 T 刷新、IEC104 本地缓存发布与状态写库，没有 TCP 下游。首末点值逐轮断言。前后刷新时间初值都设为 0，保证同等更新时间工作量。原运行服务保留，未并行运行其他回归负载。</p>
<p class="note">空闲耗时和密集刷新 P95 未改善；尾部延迟仍需进一步定位。另存 NULL 初值对照：旧版漏更新时间，不能把它与修复后的写入成本解释为等功能对比。全部结果与样本数随报告附带，局部收益不代表生产最大容量。</p></section>
<section><h2>SOE 慢连接隔离</h2><p>真实 TCP 同时连接正常客户端和不发送确认的客户端，预置 384 条事件，128 条一批，受控 5 ms 泵送。在 2 秒观察窗内，旧版正常客户端只收到 128 条后等待慢端；新版正常客户端收到全部 384 条，完成时间 {slow_after['complete_ms']:.3f} ms，顺序正确。两版持久化游标均保留为 0，未跳过慢端未确认事件。</p>
<p>额外回归覆盖慢端断线、新连接补发全部缺口；逐条核对状态、质量、CP56 字节，正常存活连接不重复补发该缺口。运行诊断显示各连接进度，日志记录连接与对端。数据库保存保守共享确认下限，各活动连接独立推进；重启或重连可能重复投递，不承诺恰好一次。</p>
<table><tr><th>完整链路通道数</th><th>源事件</th><th>C 入库</th><th>T 下游</th><th>提交至确认 ms</th></tr>{soe_rows}</table>
<p>完整链路为 S 库→TCP→C 事件库→T 游标→TCP 下游；w=1、t2=1，S 50 ms tick、T 受控 5 ms 泵送。上表为当前版实测，不是前后性能比较。</p></section>
<section><h2>IEC104 与 Modbus 链路验证</h2><table><tr><th>场景</th><th>之前 ms</th><th>之后 ms</th><th>耗时变化</th></tr>{iec_rows}</table>
<p>IEC104 前后均为 1/4 通道、每通道 64 YC + 64 YX、12 批变化、真实 C 写库子进程。覆盖总召开始/数据/结束、地址隔离、变位、质量变化、周期、遥控遥调直接执行与选择执行、撤销、断线恢复及帧序号。端到端统计采用同一批变化，5 ms 查询包含观察延迟，各阶段分位数不能相加。指令回执增加持久化阶段记录，性能代价如实列出。</p>
<p>Modbus TCP 1/4 通道、每通道 64 YC + 64 YX、8 轮变化验收通过，含常用读写功能、异常与重连。RTU 使用生产串口适配器的 socket:// 字节传输验证。没有真实串口/CAN 物理硬件、Linux 实机或长时间浸泡验收。</p></section>
<section><h2>测试、部署与复现</h2><p>最终回归：{html.escape(test_summary)}；指令页面真实 Chromium 交互 {len(ui['checks'])} 项通过；运行诊断原有 13 项浏览器检查通过。修正测试临时库的 RTU 锁隔离，避免测试 RTU 7/8 与本机业务进程争抢全局锁；对应失败在修改前快照中也复现，生产锁语义保持不变。</p>
<p>页面入口：各 Web 的 <code>/command-traces</code>、<code>/diagnostics</code>。测试只使用临时库、随机本机端口；未向运行中的站点发送测试控制指令。部署前备份数据库，按快照恢复此前运行的服务集合；实际健康检查和恢复凭据保存在 <code>output/efficiency_round11_20261115/release/</code>。本轮没有 Git 提交或远端推送。</p>
<p>脚本：<code>tools/benchmark_efficiency_round10.py --initialized-timestamps</code>、<code>tools/benchmark_soe_slow_peer.py</code>、<code>tools/verify_iec104_pipeline.py</code>、<code>tools/verify_modbus_pipeline.py</code>、<code>tools/benchmark_iec104_soe.py</code>、<code>tests/test_efficiency_round11.py</code>。修改前完整代码快照和两种时标初值结果均保存在本轮 output 目录。</p>
<details><summary>原始测量数据</summary><pre>{html.escape(json.dumps(data,ensure_ascii=False,indent=2))}</pre></details></section></body></html>'''
    target=ROOT/'docs/scdfe_efficiency_20261115_round11.html'
    target.write_text(text,encoding='utf8');print(target)


if __name__=='__main__':main()
