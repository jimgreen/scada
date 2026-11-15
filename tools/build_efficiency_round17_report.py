"""Build the self-contained status sampling acceptance report."""
import html
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/efficiency_round17_20261115'


def main():
    data={name:json.loads((OUT/(name+'.json')).read_text(encoding='utf8'))
          for name in ('before','after','before-repeat','after-repeat','soe')}
    for name in ('modbus','iec104'):
        data[name]=json.loads((OUT/name/'results.json').read_text(encoding='utf8'))
    assert all(data[name]['passed'] for name in ('before','after','before-repeat','after-repeat','modbus','iec104'))
    assert all(p['exact'] for p in data['soe']['pipelines'])
    tests=(OUT/'regression.txt').read_text(encoding='utf-8-sig')
    lock=(OUT/'lock-order.txt').read_text(encoding='utf-8-sig')
    assert not re.search(r'\d+ failed',tests+lock)
    data['tests']=re.findall(r'\d+ passed[^\r\n]*',tests)[-1]
    data['lock_test']=re.findall(r'\d+ passed[^\r\n]*',lock)[-1]
    data['frontend']=json.loads((OUT/'frontend.txt').read_text(encoding='utf-8-sig'))
    assert data['frontend']['passed']
    rows=[]
    for suffix,order in [('', '旧→新'),('-repeat','新→旧')]:
        for a,b in zip(data['before'+suffix]['cases'],data['after'+suffix]['cases']):
            x,y=a['sql_counts'][0],b['sql_counts'][0]
            rows.append(f"<tr><td>{order}</td><td>{a['channels']}</td><td>{'短时连续查询' if a['mode']=='burst' else '普通5秒刷新'}</td><td>{x['upserts']} → {y['upserts']}</td><td>{x['commits']} → {y['commits']}</td><td>{a['batch']['median_ms']:.3f} → {b['batch']['median_ms']:.3f}</td><td>{a['batch']['p95_ms']:.3f} → {b['batch']['p95_ms']:.3f}</td></tr>")
    body=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SCDFE 第十七轮优化与验证</title>
<style>body{{max-width:1150px;margin:32px auto;padding:0 22px;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif;background:#f3f7fa;color:#203b4d}}section{{padding:24px;background:white;border:1px solid #dce5ec;border-radius:12px;margin:20px 0}}h1,h2{{color:#007e89}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid #dce5ec;padding:9px}}th{{background:#edf5f6}}.scroll{{overflow:auto}}.note{{border-left:4px solid #b98425;padding-left:15px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}</style></head><body>
<h1>SCDFE 第十七轮优化与验证</h1><p>2026-11-15 · 纯 Python · S 独立库，C/T 共享业务库</p>
<section><h2>已定位并修复</h2><p>修改前四项定向测试失败：心跳/收发统计变化绕过限流；外层事务回滚后缓存仍判定已保存；旧并发采样可以覆盖较新的已提交状态；重复监控请求遇到别的写入者占锁时返回失败。</p>
<p>普通统计采用单调时钟限流，默认最短 1 秒；启停、连接状态、进程身份、错误/丢弃/重连计数变化和强制写入仍即时发布。跳过样本不编码诊断 JSON。S 收集器在申请 SQLite 写锁之前筛选，整批无到期样本时仅返回实时状态；需要保存时依然整体事务提交，失败不掩盖。</p>
<p>数据库事务先于发布器缓存锁，获得写锁后再次检查是否需要写入。自身事务提交成功才更新缓存；外层事务不被当作已确认提交。同一发布器按采样顺序防止延迟旧请求覆盖新状态，S 生命周期锁继续覆盖采集与发布，防止停止后被旧监控采样改回运行。没有修改数据库结构、业务点值写入、控制确认或逐条 SOE。</p>
<p>S 通信监控的计数和最近收发时间优先使用本次内存状态，零值有效，缺字段时兼容回退。API 顶层统计是实时采样，runtime_status 是已保存快照；其 sampled_at 保留真实采样时间。最短间隔不等于周期定时器，没有新调用就不强制保存最后一次被跳过的统计；不能将该快照机制用于指令或 SOE 队列。</p></section>
<section><h2>前后对比</h2><div class="scroll"><table><tr><th>执行顺序</th><th>通道数</th><th>场景</th><th>快照更新</th><th>提交/写锁申请</th><th>整组中位 ms</th><th>整组 P95 ms</th></tr>{''.join(rows)}</table></div>
<p>每组 20 次查询，预热 2 组、测量 8 组；IEC104/Modbus/CAN 混合通道，临时 SQLite WAL，真实监控采集与 SQL。短时场景采样时钟步长 51 ms，20 次位于一个限流窗口；普通场景时钟步长 5 秒。采样时钟受控，耗时使用真实高精度时钟，未计模拟等待；不是 HTTP 往返或协议吞吐测试。每组后另外触发一次到期保存，核对所有通道最终统计正确，该核对不计入表中耗时与事务数。</p>
<p>64 通道短时重复查询的更新数 1280 → 64、事务/写锁申请 20 → 1，均减少 95%；两次中位整组耗时 310.798 → 192.808 ms、204.864 → 54.736 ms。该收益针对短时间重复查询且无重要状态变化，不能用于推算现场全部通信速度。</p>
<p class="note">普通 5 秒刷新仍全部写入；64 通道两轮中位耗时 309.640 → 397.171 ms、236.063 → 300.945 ms，约增加 28.3%/27.5%。这一场景没有性能收益；新增事务顺序与缓存保护也有成本，机器后台负载会影响时延，尚未把差异逐项归因。后续优先分析常规刷新中的配置读取、诊断构造和数据库提交开销。单通道及 P95 也存在波动，不以最佳样本作为统一提升率。</p></section>
<section><h2>验证证据</h2><p>{html.escape(data['tests'])}；额外锁顺序用例 {html.escape(data['lock_test'])}；浏览器实时计数/零值/兼容回退/读取失败等 {len(data['frontend']['checks'])} 项检查通过。原有 S 启停按钮隔离回归另行执行，刷新不产生启停请求。</p>
<p>回归覆盖采样时钟回拨、生命周期/错误即时发布、强制写入、分通道采样、跳过诊断编码、事务整体回滚与重试、外层事务回滚、并发新旧状态、外层写入者与等待写入者锁顺序、实时 HTTP 字段和已保存快照的区别，以及 C/S/T 运行时和启停归因。</p>
<p>Modbus TCP 1/4 通道与 RTU socket://，IEC104 1/4 通道常用功能链路分别通过。SOE 独立进行 S 库 → C 持久化 → T 转发的真实回环 TCP 测试，共 {sum(p['expected'] for p in data['soe']['pipelines'])} 条，事件值、质量、原始时标和顺序一致；耗时见内嵌原始数据。协议验收同时存在回归负载，仅说明功能通过及当次测量，不作为本轮规约性能提升的前后证据。</p>
<p class="note">没有真实 CAN/RS485 故障注入、24/72 小时长稳、硬件断电或 Linux 实机验收。S 快照仍由状态采样触发，生命周期之外的统计可能晚于实时页；同一发布器的并发保护不意味着多个进程可无约束竞争同一个服务/通道的所有权。</p>
<p>基线、测试、性能、协议验收与发布记录：<code>output/efficiency_round17_20261115/</code>。使用说明同步更新。没有向运行站点发送合成控制。</p><details><summary>完整原始结果（内嵌）</summary><pre>{html.escape(json.dumps(data,ensure_ascii=False,indent=2))}</pre></details></section></body></html>'''
    target=ROOT/'docs/scdfe_efficiency_20261115_round17.html'
    target.write_text(body,encoding='utf8');print(target)


if __name__=='__main__':main()
