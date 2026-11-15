"""Build one self-contained HTML report from round15 acceptance evidence."""
import html
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/efficiency_round15_20261115'


def main():
    def load(name):return json.loads((OUT/name).read_text(encoding='utf8'))
    data={n:load(n+'.json') for n in ('before','after','before-repeat','after-repeat','soe')}
    data.update(modbus=load('modbus/results.json'),modbus_final=load('modbus-final/results.json'),iec104=load('iec104/results.json'))
    assert all(data[n]['passed'] for n in ('before','after','before-repeat','after-repeat','modbus','modbus_final','iec104'))
    assert all(x['exact'] for x in data['soe']['pipelines'])
    tests=(OUT/'regression-final.txt').read_text(encoding='utf-8-sig')
    assert not re.search(r'\d+ failed',tests)
    result=re.findall(r'\d+ passed[^\r\n]*',tests)[-1]
    data['regression']=result
    data['frontend']=[json.loads(x) for x in (OUT/'frontend.txt').read_text(encoding='utf-8-sig').splitlines() if x.startswith('{')]
    assert all(x['passed'] for x in data['frontend'])
    rows=[]
    for suffix,label in [('', '第一轮：旧→新'),('-repeat','第二轮：新→旧')]:
        for key,title in [('read_under_write','慢写期间另一客户端读'),('write','写指令应答'),('idle_read','无慢写时读取')]:
            a,b=data['before'+suffix][key],data['after'+suffix][key]
            rows.append(f"<tr><td>{label}</td><td>{title}</td><td>{a['median_ms']:.3f} → {b['median_ms']:.3f}</td><td>{a['p95_ms']:.3f} → {b['p95_ms']:.3f}</td></tr>")
    report=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SCDFE 第十五轮优化与验证</title>
<style>body{{max-width:1120px;margin:32px auto;padding:0 22px;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif;background:#f3f7fa;color:#203b4d}}section{{padding:24px;background:white;border:1px solid #dce5ec;border-radius:12px;margin:20px 0}}h1,h2{{color:#007e89}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid #dce5ec;padding:10px}}th{{background:#edf5f6}}.scroll{{overflow:auto}}.note{{border-left:4px solid #b98425;padding-left:15px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}</style></head><body>
<h1>SCDFE 第十五轮优化与验证</h1><p>2026-11-15 · 纯 Python · Modbus 多客户端慢写隔离与请求归属</p>
<section><h2>已定位并修复</h2><p>原 S/T Modbus 写回调在协议事件循环内同步执行，数据库慢提交会阻塞同服务其他客户端。原传输适配还依赖连接上共享的 last_pdu，且一次输入只推进一个完整请求，难以安全处理异步写与 TCP 粘包。基线测试分别复现其他客户端读取超时、粘包后续请求滞留和重复关闭事件循环报错，原始失败保留在 before-tests.txt。</p>
<p>当前实现每服务一个数据库回调工作线程；正常请求在同连接内顺序处理，写请求跨连接串行，其他连接可读取旧的已提交值。协议内存发布与应答仍在事件循环中完成，等待写回调成功；保持 S 独立库、C/T 共享业务库以及 T 只接 IEC104/Modbus TCP 的约束。</p>
<p>每条完整报文独立保存事务号、站号和接收时刻；处理分片及同批多条报文。非法功能码返回 01，已知功能的不可解码 PDU 返回 03，未知站号返回 0B；无效 MBAP 协议标识或长度关闭该连接。非法 PDU 不丢弃紧随其后的正常请求。保留原有日志、报文与客户端连接观察。</p>
<p>每连接最多 32 条、每服务最多 256 条请求（含执行中）。超限直接返回 06，使用对应事务号，不执行被拒绝指令。断开/停止时，未开始执行的排队请求被放弃并记原因，已开始的数据库操作等待完成；不自动补发。关闭等待超过 10 秒明确报尚未停止，重复关闭幂等。S 同步线程通过在途标记和代数校验，阻止旧数据库快照覆盖远程写入后的新值。</p></section>
<section><h2>前后性能对比</h2><div class="scroll"><table><tr><th>顺序</th><th>指标</th><th>中位耗时 ms</th><th>P95 ms</th></tr>{''.join(rows)}</table></div>
<p>固定写回调延迟 120 ms，两个真实回环 TCP 客户端，保持写应答不提前；每轮预热 2 次后测量 12 次，另测无慢写读取 100 次。12 样本 P95 按向上取整为最大值。使用修改前文件快照与当前源码，原始请求事务号和本机发送/接收时刻均内嵌保留。机器存在后台运行服务；第二轮期间有独立验收负载，不是独占机器容量测试。</p>
<p class="note">慢写期间其他连接读取延迟由约 121 ms 降至小于 1 ms，写指令仍需约 121 ms。该收益来自隔离，不表示 SQLite 提交加速；无慢写读取 P95 第二轮由 0.310 增至 0.327 ms，不宣称所有指标均提升。本轮变更不对实际设备端到端效率作倍数承诺。</p></section>
<section><h2>诊断与页面</h2><p>“运行诊断 → Modbus 请求调度”展示每通道已处理、待处理、峰值、忙拒绝及放弃计数，并保留最近 16 条写请求/异常记录。每条含事务号、站号、功能码、排队、执行、响应提交及总耗时。正常轮询只累计计数；沿用运行状态 JSON 快照，无新增业务表。S 的 engine 诊断与 T 的通道诊断均可通过 API 获取，即使没有其他阶段样本也显示。</p>
<p>响应提交表示本地发送调用返回，不能证明客户端收到或设备执行。连接断开、未发送和旧快照有明确区分。相同内容不替换 DOM，失败保留上次结果；HTML 转义、隐藏页暂停、请求防重叠继续有效。持久化快照沿用原节流机制，进程重启后统计窗口重新开始。</p></section>
<section><h2>正确性与证据范围</h2><p>{html.escape(result)}；Chromium 页面共 {sum(len(x['checks']) for x in data['frontend'])} 项检查通过。覆盖慢写隔离、成功应答时序、事务号/站号、分片/粘包、非法 PDU/MBAP、两层排队限额、跨客户端串行写、失败不改寄存器、断开与停机排空、S 数据库同步、诊断持久化 API 及既有命令恢复/连接历史/日志行为。</p>
<p>Modbus TCP 1/4 通道与 RTU socket:// 字节链路常用功能验收两次通过，含 01/02/03/04、05/06/15/16、地址偏移、越界/只读/部分浮点写异常、回读及 TCP 重连。最终一次在完整 MBAP 边界修改后执行。IEC104 1/4 通道总召、变位、质量变化、周期上送、选择/执行/撤销及直接执行回归通过。独立 SOE 验证共 {sum(x['expected'] for x in data['soe']['pipelines'])} 条事件，持久化和转发的状态、质量、原始时标及顺序一致，效率数据单独列入原始结果。</p>
<p>本轮未改 CAN 通信实现；RTU 使用软件字节链路，不代替 RS485 电气验收。未进行真实 CAN Bus-off、现场电磁干扰、硬件断电、Linux 实机或 24/72 小时长稳测试。</p>
<p class="note">本次 IEC104 四通道回归仍观察到约 885 ms 的 FULL 提交尖峰。此前偶发 Modbus 超时具体触发者尚不能由本轮受控隔离试验倒推；不将历史偶发超时宣布全部修复，也不降低持久化等级来隐藏延迟。</p>
<p>后续优先验证数据库提交尖峰与实际写入者/文件同步的关联、多通道长稳下队列上限和恢复策略、真实 CAN/串口异常恢复。源码基线、测试失败与修正后结果、性能数据及服务恢复证据位于 <code>output/efficiency_round15_20261115/</code>。</p>
<details><summary>完整原始结果（内嵌，可离线查看）</summary><pre>{html.escape(json.dumps(data,ensure_ascii=False,indent=2))}</pre></details></section></body></html>'''
    target=ROOT/'docs/scdfe_efficiency_20261115_round15.html';target.write_text(report,encoding='utf8');print(target)


if __name__=='__main__':main()
