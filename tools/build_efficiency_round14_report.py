"""Standalone evidence report with failed runs retained alongside reruns."""
import html
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/efficiency_round14_20261115'

def main():
    def read(path):return json.loads((OUT/path).read_text(encoding='utf8'))
    data={name:read(name+'.json') for name in ('before','after','before-repeat','after-repeat','soe')}
    data.update(modbus_first=read('modbus/results.json'),modbus_repeat=read('modbus-repeat/results.json'),iec104=read('iec104/results.json'))
    data['first_command_diagnostics']=read('modbus/command-diagnostics-tcp-1ch.json')
    assert all(data[x]['passed'] for x in ('before','after','before-repeat','after-repeat','modbus_repeat','iec104'))
    assert all(x['exact'] for x in data['soe']['pipelines'])
    text=(OUT/'regression.txt').read_text(encoding='utf-8-sig');assert not re.search(r'\d+ failed',text)
    tests=re.findall(r'\d+ passed[^\r\n]*',text)[-1]
    ui=json.loads((ROOT/'output/playwright/efficiency-round14/fixture-results.json').read_text());assert ui['passed']
    rows=[]
    for suffix,label in (('','第一轮（旧→新）'),('-repeat','第二轮（新→旧）')):
        for a,b in zip(data['before'+suffix]['cases'],data['after'+suffix]['cases']):
            rows.append(f"<tr><td>{label}</td><td>{a['points']}</td><td>{a['selects']:.0f} → {b['selects']:.0f}</td><td>{a['median_ms']:.3f} → {b['median_ms']:.3f}</td><td>{a['p95_ms']:.3f} → {b['p95_ms']:.3f}</td></tr>")
    report=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SCDFE 第十四轮优化与验证</title>
<style>body{{max-width:1120px;margin:32px auto;padding:0 22px;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif;background:#f3f7fa;color:#203b4d}}section{{padding:24px;background:white;border:1px solid #dce5ec;border-radius:12px;margin:20px 0}}h1,h2{{color:#007e89}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid #dce5ec;padding:10px}}th{{background:#edf5f6}}.scroll{{overflow:auto}}.note{{border-left:4px solid #b98425;padding-left:15px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}</style></head><body>
<h1>SCDFE 第十四轮优化与验证</h1><p>2026-11-15 · 纯 Python · S 独立数据库；C/T 共享数据库；T 入口保持 IEC104 与 Modbus TCP。</p>
<section><h2>本轮落地</h2><p>批量遥控、遥调改为按 RTU 与点类型分组读取，400 点一块限制 SQL 参数数，RTU 类型每批分块查询。全部点在同一写事务内完成校验，随后分类型执行更新，并再次读取完整回执以保留触发器更新字段和输入顺序。末点越限、闭锁、非法地址或待处理槽位冲突仍导致整批回滚；FULL 持久化不变。</p>
<p>新增 T 指令入库分段诊断：连接获取、参数设置、写锁、读取校验、更新及回执、FULL 提交、参数恢复、总耗时。保存最近 256 个阶段样本的分位数，以及每服务最近 16 次超过 100 ms 或失败请求；关联前 8 个 RTU/点/命令时标，不记录命令值。沿用运行诊断 JSON 快照持久化，无需新建业务表。诊断异常不能把已提交命令变成失败。</p>
<p>运行诊断页显示累计请求数、未提交数、失败阶段及慢请求关联。相同慢批次、指令表和配置检查内容不再重复替换 DOM；隐藏页暂停、请求防重叠和失败保留上次结果仍保留。人工结案接口收到错误类型的阶段字段时返回参数错误。</p></section>
<section><h2>前后性能对比</h2><div class="scroll"><table><tr><th>轮次</th><th>每批点数</th><th>SELECT 数</th><th>中位耗时 ms</th><th>P95 ms</th></tr>{''.join(rows)}</table></div>
<p>每场景预热 3 次后测量 12 次，临时 SQLite 文件、1/100/1000 点、最多 10 RTU，保留实际触发器与 FULL 提交。P95 在此小样本中为最大值；不是现场吞吐或长稳容量。背景运行服务及其他任务未停止，非独占机器。</p>
<p class="note">可以确认的是批量查询工作量减少：100 点 300→5 次，1000 点 3000→41 次。耗时两轮有改善也有退化，单点没有减少查询，未确认整体稳定提速。完整数值全部保留，不只呈现有利的一轮。</p></section>
<section><h2>超时归因：新增证据与边界</h2><p>首次 Modbus TCP 单通道运行在 FC15 收到响应前超过客户端 500 ms 超时；该次 T 指令入库诊断显示总耗时约 20.491 ms，写锁约 0.035 ms、校验 0.841 ms、更新 4.992 ms、提交 9.570 ms，且已提交。这排除了将该次超时整体解释为慢入库，但尚未确定响应链路的具体阻塞位置。它不能证明上一轮约 776 ms 入库的原因，两次事件必须分别看待。</p>
<p>相同代码和 500 ms 门槛的第二次 TCP 1/4 通道及 RTU socket:// 完整验收通过；首次失败保留在原始数据中。报文验证工具现在无论成功失败都保存观测报文与命令分段快照，便于后续对齐。超时不等于设备未执行，不自动重发旧指令，也未通过增大超时隐藏问题。</p>
<p>IEC104 回归中也捕获到一个不同的慢请求：T 通道 4 入库总计约 263.379 ms，其中 FULL 提交约 262.166 ms、写锁约 0.017 ms。此记录表明该次耗时集中在提交阶段，不能当成锁竞争；提交可能涉及文件同步或检查点，当前证据仍不足以进一步判定。正常指令持久化等级未降低。</p>
<p>运行诊断中的“已提交”仅指数据库已经接受指令，不代表设备执行或客户端已收到响应。该诊断测量从 DatabaseCommandWriter 调用到释放连接，未覆盖协议解析、之前的请求排队、日志与网络发送。慢写库批次和慢指令请求为两种独立观察，不能直接相加。</p></section>
<section><h2>正确性与交付</h2><p>{html.escape(tests)}；隔离 Chromium 页面检查 {len(ui['checks'])} 项通过。覆盖跨 RTU/类型批量请求、超过 400 点分块、输入顺序、完整回执、最后一项失败整批回滚、并发槽位保护、FULL 提交、诊断异常不改变提交结果、锁等待和失败分段、诊断保留上限及持久化 HTTP 读取。</p>
<p>IEC104 1/4 通道常用功能回归；SOE 共 {sum(x['expected'] for x in data['soe']['pipelines'])} 条事件，状态、质量、原始 CP56 和顺序一致。Modbus 成功复测覆盖 01/02/03/04、05/06/15/16、地址偏移、异常响应、回读与断线恢复；RTU 使用 socket:// 字节传输，不代表 RS485 物理验收。没有真实 CAN/串口、Linux 实机、硬件断电或 24/72 小时运行验收。</p>
<p>源码前置快照、原始失败及复测、性能数据和备份恢复记录保存在 <code>output/efficiency_round14_20261115/</code>。使用说明同步更新，服务按加载前快照恢复；没有向运行站点发送验收控制，也没有提交或推送 Git。</p>
<p>下一步优先项：对齐客户端发送、服务接收、入库完成和实际发送响应的同一次请求；验证协议服务在慢写事务下的多客户端隔离；继续进行多通道持续运行测试。当前证据不足以将偶发超时宣布修复。</p>
<details><summary>全部原始结果（内嵌）</summary><pre>{html.escape(json.dumps(data,ensure_ascii=False,indent=2))}</pre></details></section></body></html>'''
    path=ROOT/'docs/scdfe_efficiency_20261115_round14.html';path.write_text(report,encoding='utf8');print(path)

if __name__=='__main__':main()
