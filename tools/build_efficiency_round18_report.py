"""Build a self-contained report for grouped monitor reads and JSON encoding."""
import html
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/efficiency_round18_20261115'


def main():
    data={name:json.loads((OUT/(name+'.json')).read_text(encoding='utf8'))
          for name in ('before','after','before-repeat','after-repeat','profile-before','profile-final','soe')}
    for name in ('modbus','iec104'):
        data[name]=json.loads((OUT/name/'results.json').read_text(encoding='utf8'))
    assert all(data[n]['passed'] for n in ('before','after','before-repeat','after-repeat','modbus','iec104'))
    assert all(p['exact'] for p in data['soe']['pipelines'])
    data['tests']=[]
    for name in ('regression-final.txt','protocol-regression.txt'):
        text=(OUT/name).read_text(encoding='utf-8-sig')
        assert not re.search(r'\d+ failed',text)
        data['tests'].append(re.findall(r'\d+ passed[^\r\n]*',text)[-1])
    data['release']=json.loads((OUT/'release/verification.json').read_text(encoding='utf8'))
    data['final_state']=json.loads((OUT/'release/final-state.json').read_text(encoding='utf8'))
    rows=[]
    for suffix,order in [('', '旧→新'),('-repeat','新→旧')]:
        for a,b in zip(data['before'+suffix]['cases'],data['after'+suffix]['cases']):
            rows.append(f"<tr><td>{order}</td><td>{a['channels']}</td><td>{'短时连续查询' if a['mode']=='burst' else '普通5秒采样'}</td><td>{a['batch']['median_ms']:.3f} → {b['batch']['median_ms']:.3f}</td><td>{a['batch']['p95_ms']:.3f} → {b['batch']['p95_ms']:.3f}</td><td>{a['poll']['median_ms']:.3f} → {b['poll']['median_ms']:.3f}</td></tr>")
    body=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SCDFE 第十八轮优化与验证</title>
<style>body{{max-width:1160px;margin:32px auto;padding:0 22px;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif;background:#f3f7fa;color:#203b4d}}section{{padding:24px;background:white;border:1px solid #dce5ec;border-radius:12px;margin:20px 0}}h1,h2{{color:#007e89}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid #dce5ec;padding:9px}}th{{background:#edf5f6}}.scroll{{overflow:auto}}.note{{border-left:4px solid #b98425;padding-left:15px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}</style></head><body>
<h1>SCDFE 第十八轮优化与验证</h1><p>2026-11-15 · 纯 Python · 多 RTU 监控数据库读取与诊断处理</p>
<section><h2>定位及修改</h2><p>上一轮普通 5 秒刷新没有事务数收益且测得耗时增加。本轮检查发现，64 个 IEC104/Modbus TCP/CAN 混合服务每次监控采集有 85 条配置读取 SQL、42 个独立读事务；通用协议的引擎诊断取得两次，同一响应和数据库快照可能不同。性能剖析还显示，诊断 JSON 的通用类型检查和递归转换是主要 CPU 调用来源之一。</p>
<p>S 按参数表分组读取所选 RTU 配置，RTU 名称/通道和参数共用一个读取快照。按 SQLite 变量限额分段，单 RTU 请求只取该站；没有跨请求配置缓存，下一次刷新可见外部修改。显式配置覆盖、缺省值、运行中绑定和合法性检查保留。单服务配置采用一次关联查询，仍保留默认 RTU 与参数的一致读取事务。</p>
<p>通用协议状态每次只读取一次引擎诊断，接口与保存路径复用。C/S/T 共用诊断编码先处理常见标量，避免大量不必要的类型检查；特殊类型及 JSON 兼容格式保持。运行状态使用一次 executemany 批量调用，保持原行数、版本、顺序保护、生命周期锁和事务原子性。上一轮的重要状态即时保存、普通统计限流和外层事务回滚重试仍通过回归。</p>
<p>修改前的隔离基线复现了逐行 SQL 调用、单服务两条配置查询、64 通道 85 条读取及引擎诊断重复读取。最初新增测试中有两处夹具错误（重复读取同一服务的全部行、使用了不存在的参数列），另有诊断夹具将属性写成方法；修正夹具后重新在基线上确认重复读取 2 次，新实现为 1 次。保留原始失败记录，不将夹具错误算作程序缺陷。</p></section>
<section><h2>性能对比</h2><div class="scroll"><table><tr><th>运行顺序</th><th>通道数</th><th>场景</th><th>20次查询总耗时中位 ms</th><th>整组 P95 ms</th><th>单次查询中位 ms</th></tr>{''.join(rows)}</table></div>
<p>每规模预热 2 组、测量 8 组，每组 20 次。保持与第十七轮同一基准脚本，临时 SQLite WAL、IEC104/Modbus/CAN 混合服务、真实配置采集与写库；短时样本时钟步长 51 ms、普通样本步长 5 秒，实际耗时由高精度时钟记录，不计模拟等待，不含 HTTP 网络。每组后另外核对最新统计落库，核对不计入耗时。旧→新及新→旧两轮在功能回归结束后顺序执行，后台仍有正常通信服务，机器非独占。</p>
<p>64 通道普通刷新中位总耗时两轮分别减少 13.7% 和 35.6%；短时重复查询分别减少 77.4% 和 24.1%。确定的 SQL 数量变化是每次配置读取 85 → 3（减少 96.5%），读事务 42 → 1。写入行数和提交次数维持上一轮规则：短时 20 次查询合计 64 行/1 次提交，普通场景 1280 行/20 次提交。</p>
<p class="note">单通道第二轮出现回退：普通场景 3.547 → 6.368 ms，短时场景 0.765 → 1.774 ms；绝对值较小且波动显著，不能据此声称全部负载均变快。性能剖析的总时间受插桩和系统负载影响，不作为加速率；其函数调用数从约 286 万降到 153 万。上一轮耗时回退没有逐项完成根因归因，本轮只证明所测多通道路径得到改善。查询数量随规约种类和分段数量变化，85 → 3 仅针对本基准布局。</p></section>
<section><h2>正确性与运行验证</h2><p>{html.escape('；'.join(data['tests']))}。覆盖分组与单服务配置完全一致、显式覆盖、缺省与运行中绑定、分段读同一快照、下一次可见外部修改、单站查询范围、错误配置不发布部分结果、一次诊断采样、特殊 JSON 类型，以及原有批次回滚、并发顺序、限流和 C/S/T 运行时。CAN 虚拟总线启停与收发测试通过。</p>
<p>Modbus TCP 1/4 通道和 RTU socket:// 常用功能验收通过；IEC104 1/4 通道总召、变位、质量、周期、遥控/遥调选择执行、直接执行、撤销及重连通过。SOE 独立测试 {sum(p['expected'] for p in data['soe']['pipelines'])} 条，经 S 独立库、C 持久化、T 转发，值、质量、原始时标与顺序一致。协议和 SOE 效率保留原始结果，未做规约优化前后对照，因此不宣称其端到端速度提升。</p>
<p>数据库已备份并通过 quick_check；三个 Web 健康接口均返回 200，C 原主进程/写库进程/命令服务恢复，S 原有 {data['final_state']['active_services']} 项服务及 {data['final_state']['connections']} 个连接核实，T 保持原停止状态。启停动作带审计原因及追踪记录，未向运行站点发送合成控制。本轮没有界面布局修改，API 和静态资源加载通过检查，源码未提交 Git。</p>
<p class="note">保留 S 独立库、C/T 共享业务库及数据库解耦；T 入口仍为 IEC104/Modbus TCP。没有真实 CAN/RS485 故障注入、24/72 小时长稳、Linux 实机或硬件断电验收。下一步优先检查监控响应大小与分页、只获取可见诊断明细，以及真实设备重连和写锁长尾。</p>
<p>基线、剖析、测试、性能和发布证据：<code>output/efficiency_round18_20261115/</code>。使用说明同步更新。</p><details><summary>完整原始结果（内嵌）</summary><pre>{html.escape(json.dumps(data,ensure_ascii=False,indent=2))}</pre></details></section></body></html>'''
    target=ROOT/'docs/scdfe_efficiency_20261115_round18.html'
    target.write_text(body,encoding='utf8');print(target)


if __name__=='__main__':main()
