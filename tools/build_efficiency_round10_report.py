"""Build the standalone round 10 report from saved, verified measurements."""
import html
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/efficiency_round10_20261115'


def read(name):return json.loads((OUT/name).read_text(encoding='utf8'))


def main():
    before,after=read('t-before-repeat.json'),read('t-after-repeat.json')
    old,new=read('iec-before/results.json'),read('iec-after/results.json')
    modbus,soe=read('modbus/results.json'),read('soe.json')
    assert all(x['passed'] for x in (before,after,old,new,modbus))
    assert all(x['exact'] for x in soe['pipelines'])
    rows=[]
    def row(name,a,b):
        change=(b/a-1)*100
        rows.append(f'<tr><td>{name}</td><td>{a:.3f}</td><td>{b:.3f}</td><td>{change:+.2f}%</td></tr>')
    for mode,label in [('idle','T 无变化刷新'),('sparse','T 单点变化刷新'),('dense','T 8000 点同时变化刷新')]:
        row(label,before[mode]['refresh']['median_ms'],after[mode]['refresh']['median_ms'])
    row('源库 8000 点变化事务',before['dense']['source_write']['median_ms'],after['dense']['source_write']['median_ms'])
    t_table=''.join(rows);rows=[]
    for a,b in zip(old['pipelines'],new['pipelines']):
        row(f"IEC104 {a['channels']} 通道端到端中位数",a['end_to_end_latency']['median_ms'],b['end_to_end_latency']['median_ms'])
        row(f"IEC104 {a['channels']} 通道端到端 P95",a['end_to_end_latency']['p95_ms'],b['end_to_end_latency']['p95_ms'])
    iec_table=''.join(rows)
    soe_rows=''.join(f"<tr><td>{x['channels']}</td><td>{x['expected']}</td><td>{x['c_persisted']}</td><td>{sum(x['t_received'])}</td><td>{x['end_to_end_ack_ms']}</td></tr>" for x in soe['pipelines'])
    raw=html.escape(json.dumps(dict(t_before=before,t_after=after,iec_before=old,iec_after=new,modbus=modbus,soe=soe),ensure_ascii=False,indent=2))
    text=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SCDFE 第十轮优化与验证 · 2026-11-15</title><style>
body{{max-width:1100px;margin:32px auto;padding:0 24px;color:#183547;background:#f4f7f9;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}}section{{padding:24px;background:white;border:1px solid #d9e3e8;border-radius:10px;margin:20px 0}}h1,h2{{color:#007a86}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #d9e3e8}}th{{background:#edf5f7}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}code{{color:#005f73}}.note{{border-left:4px solid #c6922b;padding-left:16px}}
</style><h1>SCDFE 第十轮优化与验证</h1><p>2026-11-15 · 纯 Python 规约实现 · S 独立数据库，C/T 共用业务库</p>
<section><h2>本轮完成内容</h2><p>T 将 YC/YX 全量扫描改为按映射源点读取变化，独立维护游标和缓存；发布失败保留待重试目标。删除、空值、重新插入、仅刷新时间变化、映射重建与多读取者均有测试。SOE 保持独立逐条记录，不使用最新值合并索引。</p>
<p>C/S/T 新增“运行诊断”页面与 <code>GET /api/diagnostics</code>：保存并展示分段耗时、最近 256 样本分位数和快照时刻。C 写库统计为全局统计；S/T 为服务或通道统计。手动配置检查提示监听冲突、本地模拟和串口回环；启动监听失败给出具体地址和原因。不会自动改端口或主动连接设备。</p>
<p>页面 5 秒自动刷新耗时，隐藏时暂停，避免并发重复请求，未变化内容保留 DOM，失败保留上一批结果。已更新单文件使用手册。</p></section>
<section><h2>T 刷新与源库写入</h2><table><thead><tr><th>场景</th><th>之前 ms</th><th>之后 ms</th><th>耗时变化</th></tr></thead><tbody>{t_table}</tbody></table>
<p>16 RTU × 1000 点（各 500 YC、500 YX），1 个 T 通道映射全部点。每场景 40 次，表中为中位数；实际执行 T 生产刷新、IEC104 本地缓存更新与运行状态写库，没有 TCP 下游。每轮核对首末点值。使用本轮前工作区完整快照作为对照，包含上一轮未提交的修改；不是用 Git HEAD 冒充前一轮状态。</p>
<p class="note">首轮及复测原始记录均保留；表中采用没有并行回归测试的第二轮配对结果。全量变化刷新耗时增加约 15.7%，源库事务耗时增加约 64.9%；增量索引维护存在写入代价。不能将低变化率下的读取收益解释为所有负载都提速。历史点键（包括删除标记）占用仍会随频繁更换点号增长。</p></section>
<section><h2>真实 IEC104 链路</h2><table><thead><tr><th>场景</th><th>之前 ms</th><th>之后 ms</th><th>耗时变化</th></tr></thead><tbody>{iec_table}</tbody></table>
<p>临时 S/C 数据库、本机 TCP、实际 C 写库子进程，1/4 通道各 64 YC + 64 YX，前后各 12 批变化；相同验收程序、相同参数。覆盖总召确认/数据/结束、地址隔离、变位与质量变化、周期上送、遥控遥调直接执行/选择执行/撤销、重连恢复及帧序号。</p>
<p>每批记录 S 提交返回、C 报文接收、C 落库观察、T 下游接收的单调时钟偏移；数据库观察周期 5 ms，多通道阶段存在重叠。生产诊断各阶段分位数不能相加为端到端分位数；提交发送不等于远方确认。12 个样本的 P95/P99接近最大值，仅作本次对照。</p></section>
<section><h2>Modbus 与 SOE 正确性</h2><p>Modbus TCP 1/4 通道各 8 轮变化验收通过，并通过生产串口适配器的 RTU 字节传输测试（socket://，不等同真实串口硬件）。SOE 独立测试经过 S 数据库→TCP→C 事件库→T 数据库游标→TCP；逐条核对状态、质量、CP56 字节和顺序。</p>
<table><thead><tr><th>SOE 通道数</th><th>生成事件</th><th>C 入库</th><th>T 下游接收</th><th>提交至确认 ms</th></tr></thead><tbody>{soe_rows}</tbody></table>
<p>SOE 数字为当前版验收实测，非优化前后比较；T 使用受控 5 ms 泵送。两组 Python 回归分别 86 项/29 子项、85 项/6 子项通过（含重复复测），浏览器交互 13 项通过。没有进行现场硬件、Linux 实机或长时间浸泡测试。</p></section>
<section><h2>交付与复现</h2><p>入口：C <code>http://127.0.0.1:8877/diagnostics</code>，S <code>http://127.0.0.1:8878/diagnostics</code>，T <code>http://127.0.0.1:8879/diagnostics</code>。T 未启动时没有耗时样本，但可查看配置检查。</p>
<p>脚本：<code>tools/benchmark_efficiency_round10.py</code>、<code>tools/verify_iec104_pipeline.py</code>、<code>tools/verify_modbus_pipeline.py</code>、<code>tools/benchmark_iec104_soe.py</code>、<code>tests/frontend_efficiency_round10.cjs</code>。原始结果与部署记录：<code>output/efficiency_round10_20261115/</code>。修改仍在本地工作区；本轮未提交或推送。</p>
<details><summary>完整原始测量数据</summary><pre>{raw}</pre></details></section></html>'''
    target=ROOT/'docs/scdfe_efficiency_20261115_round10.html'
    target.write_text(text,encoding='utf8');print(target)


if __name__=='__main__':main()
