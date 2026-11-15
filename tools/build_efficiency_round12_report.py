"""Build a standalone report from measured round12 acceptance artifacts."""
import html
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/efficiency_round12_20261115'


def main():
    def read(path):return json.loads((OUT/path).read_text(encoding='utf-8-sig'))
    before,after=read('before.json'),read('after.json')
    old,new=read('iec104-before/results.json'),read('iec104-after/results.json')
    modbus,soe=read('modbus/results.json'),read('soe.json')
    assert all(x['passed'] for x in (before,after,old,new,modbus))
    assert all(x['exact'] for x in soe['pipelines'])
    testlog=(OUT/'regression-final.txt').read_text(encoding='utf-8-sig')
    assert not re.search(r'\d+ failed',testlog)
    tests=re.findall(r'\d+ passed[^\r\n]*',testlog)[-1]
    ui=json.loads((ROOT/'output/playwright/efficiency-round12/fixture-results.json').read_text())
    assert ui['passed']
    rows=[]
    def row(name,a,b):rows.append(f'<tr><td>{name}</td><td>{a:.3f}</td><td>{b:.3f}</td><td>{(b/a-1)*100:+.1f}%</td></tr>')
    row('无变化指令历史查询中位数',before['conditional']['median_ms'],after['conditional']['median_ms'])
    row('完整指令历史查询中位数',before['full']['median_ms'],after['full']['median_ms'])
    row('单条命令入库中位数',before['command_submit']['median_ms'],after['command_submit']['median_ms'])
    row('命令六次提交中位数',before['command_lifecycle']['median_ms'],after['command_lifecycle']['median_ms'])
    micro=''.join(rows);rows=[]
    for a,b in zip(old['pipelines'],new['pipelines']):
        for key,label in [('end_to_end_latency','变化至下游'),('command_receipt_latency','命令回执')]:
            row(f"IEC104 {a['channels']} 通道 {label}中位数",a[key]['median_ms'],b[key]['median_ms'])
        row(f"IEC104 {a['channels']} 通道 变化至下游 P95",a['end_to_end_latency']['p95_ms'],b['end_to_end_latency']['p95_ms'])
    pipelines=''.join(rows)
    data=dict(before=before,after=after,iec104_before=old,iec104_after=new,modbus=modbus,soe=soe)
    text=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SCDFE 第十二轮优化验证 · 2026-11-15</title>
<style>body{{max-width:1100px;margin:32px auto;padding:0 20px;background:#f2f6f9;color:#193649;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}}section{{background:white;padding:24px;margin:20px 0;border:1px solid #dbe6ed;border-radius:12px}}h1,h2{{color:#007c88}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #dbe6ed}}th{{background:#eaf4f5}}.scroll{{overflow:auto}}.note{{border-left:4px solid #b78a35;padding-left:15px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}code{{color:#006873}}</style></head><body>
<h1>SCDFE 第十二轮优化验证</h1><p>2026-11-15 · 纯 Python · S 独立数据库，C/T 共享业务库 · 修改前工作区快照对比</p>
<section><h2>指令提交与回采验证</h2><p>C/T 命令提交、C 派发及关键阶段记录使用 SQLite WAL + synchronous=FULL；事务结束恢复连接的原同步设置。测量值的高频写库仍沿用原策略。派发标记在设备 I/O 前提交，不确定结果不自动重发。模拟进程在派发提交后直接退出，重新打开数据库后不会再次认领该命令。</p>
<p>新增显式回采关联：遥控关联遥信，遥调关联遥测，可跨 RTU；支持比例、偏移、绝对偏差和超时。遥控限定同向或反向。规则在派发时冻结，后续修改不改变已派发命令的判断。仅采纳执行请求之后、超时之前实际接收且质量有效的新样本；缓存重复读取、人机修改点值及刷新时间不作为接收证据。页面分别显示规约确认和回采结果。</p>
<p class="note">回采符合预期只证明收到的状态符合规则，不证明物理动作因果。毫秒时标下与执行同毫秒的样本保守排除。该机制按最新样本核对，可能漏掉两次扫描之间的短暂状态；SOE 仍走独立事件链路。没有规则则不推断成功，也不会自动为现场点配置关联。接收进程停止时无法产生新样本，超时状态在命令进程恢复检查后更新。</p>
<p>新增人工核对记录，保存结论、说明、填写人声明、时间和请求来源；采用版本检查避免覆盖并发修改。保存不会发送命令、改写规约确认或解除待处理命令。填写人声明不是登录身份认证。</p>
<p>FULL 提交的前提仍是操作系统和存储正确履行同步请求；本轮验证了配置、事务与进程退出恢复，没有做硬件断电试验。S 独立接收记录的提交策略未在本轮改为 FULL。</p></section>
<section><h2>界面与数据库诊断</h2><p>指令追踪支持 RTU 名称、点名、阶段和时间范围筛选；配置弹窗按名称选点，超过 1000 点可继续按名称或点号查找。回采和核对信息同页查看。最新页 5 秒刷新，历史页、隐藏页和弹窗期间暂停；请求不重叠，错误保留已显示数据。未变化时通过 ETag 返回 304，避免宽表连接、JSON 序列化和重复 DOM 更新；点名修改会失效缓存。</p>
<p>运行诊断新增写锁等待、SQL 更新、事务提交分段统计，显示锁冲突数、累计退避、WAL 文件大小、自动检查点阈值及待写消息数。锁冲突仍保留原批次等待重试。自动检查点可能包含在提交耗时中；没有独立测量检查点，不能只凭 WAL 大小判断故障。</p>
<p>接口：<code>GET /api/command-traces</code>、<code>GET /api/command-options</code>、<code>GET/PUT /api/command-feedback</code>、<code>POST /api/command-traces/{{id}}/review</code>、<code>GET /api/diagnostics</code>。C/T 配置回采；S 仅显示独立接收记录和人工核对。</p></section>
<section><h2>同机性能对比</h2><div class="scroll"><table><tr><th>场景</th><th>之前 ms</th><th>之后 ms</th><th>耗时变化</th></tr>{micro}</table></div>
<p>临时数据库预置 5000 条历史，每页 50 条；HTTP 预热 5 次后统计 100 次，命令预热 5 次后统计 60 次。命令六次提交包括入库、派发、选择、选择确认、执行和确认，不包含设备通信。原运行服务继续运行，没有同时执行其他基准测试。</p>
<p>重复查询响应体 {before['conditional']['body_bytes']} → {after['conditional']['body_bytes']} 字节（仍有 HTTP 响应头）；完整查询因增加回采和核对字段，响应体 {before['full']['body_bytes']} → {after['full']['body_bytes']} 字节。FULL 同步显著增加命令提交耗时，本轮是可靠性与查询效率改进，不能声称所有路径更快。</p>
<div class="scroll"><table><tr><th>真实 TCP 链路</th><th>之前 ms</th><th>之后 ms</th><th>耗时变化</th></tr>{pipelines}</table></div>
<p>IEC104 1/4 通道，每通道 64 YC + 64 YX，12 批变化。S→TCP→C 写库子进程→共享业务库→T→TCP 下游。覆盖总召、地址隔离、变位与质量变化、周期、遥控遥调直接/选择执行、撤销和断线恢复。前后均从本轮修改前快照/当前代码启动；短时结果有调度噪声，不等于容量或长期稳定性，分段分位数不能相加。</p></section>
<section><h2>验收与使用范围</h2><p>后端最终回归：{html.escape(tests)}。指令页面真实 Chromium 隔离测试 {len(ui['checks'])} 项通过，覆盖配置保存、人工核对、历史分页、条件查询、名称转义与错误保留。真实 IEC104 控制与新遥信回采关联通过。数据库旧指令迁移、并发版本冲突、质量异常、旧样本、超时样本、锁冲突批次保留分别验证。</p>
<p>Modbus TCP 1/4 通道、64 YC + 64 YX、8 轮常用读写与重连验收通过；RTU 使用 socket:// 字节传输验证生产串口适配器。SOE 1/4 通道各 128 事件，逐条核对 C 入库及 T 下游的状态、质量、CP56 字节与顺序：{sum(x['expected'] for x in soe['pipelines'])} 条全部一致。未替代真实 CAN/串口硬件、Linux 实机、断电与长时间稳定性验收。</p>
<p>部署前 SQLite 备份，排空 C 写库后重启 Web，恢复此前活动的服务集合；发布凭据见本轮 output/release。页面 <code>/command-traces</code> 与 <code>/diagnostics</code>。没有向运行站点发送测试命令，没有提交或推送 Git。测试与基准脚本保存在 <code>tests/test_efficiency_round12.py</code>、<code>tests/frontend_efficiency_round12.cjs</code>、<code>tools/benchmark_efficiency_round12.py</code>；原始结果与修改前快照在 <code>output/efficiency_round12_20261115/</code>。</p>
<details><summary>原始性能与链路数据（内嵌）</summary><pre>{html.escape(json.dumps(data,ensure_ascii=False,indent=2))}</pre></details></section></body></html>'''
    target=ROOT/'docs/scdfe_efficiency_20261115_round12.html';target.write_text(text,encoding='utf8');print(target)


if __name__=='__main__':main()
