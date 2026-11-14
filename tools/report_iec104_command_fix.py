"""Render the measured IEC104 command/receive-cache verification as one HTML."""
import html
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'reports/iec104_command_fix_20261114'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def table(headers, rows):
    return '<div class="table"><table><thead><tr>' + ''.join(
        f'<th>{html.escape(str(v))}</th>' for v in headers) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join(f'<td>{html.escape(str(v))}</td>' for v in row) + '</tr>' for row in rows
    ) + '</tbody></table></div>'


def main():
    cache = read(EVIDENCE / 'snapshot_benchmark.json')
    main_run = read(EVIDENCE / 'pipeline/results.json')
    scale = read(EVIDENCE / 'scale8/results.json')
    wire = read(EVIDENCE / 'final_wire/wire_contracts.json')
    manifest = read(EVIDENCE / 'build_manifest.json')
    baseline = read(ROOT / 'reports/iec104_common_acceptance_20261113/recheck_01/final/results.json')
    baseline_scale = read(ROOT / 'reports/iec104_common_acceptance_20261113/recheck_01/scale8/results.json')
    assert all(r['passed'] for r in (cache, main_run, scale, wire))
    regression = ET.parse(EVIDENCE / 'regression.xml').getroot().find('testsuite')
    assert all(regression.get(k) == '0' for k in ('failures', 'errors', 'skipped'))
    summary = re.search(r'\d+ passed[^\r\n]*', (EVIDENCE / 'regression.log').read_text(encoding='utf-8-sig'))
    assert summary
    runs = main_run['pipelines'] + scale['pipelines']
    previous = {p['channels']: p for p in baseline['pipelines'] + baseline_scale['pipelines']}
    paired = []
    for key, label in [('paired_before', '旧轮询方法'), ('paired_after', '新轮询方法')]:
        run = read(EVIDENCE / key / 'results.json')
        assert run['passed']
        r = run['pipelines'][0]
        paired.append([label, r['end_to_end_latency']['samples'],
                       r['end_to_end_latency']['median_ms'], r['end_to_end_latency']['p95_ms'],
                       r['c_commit_latency']['median_ms'], r['s_gi']['median_ms'], r['t_gi']['median_ms']])
    paired_table = table(['同环境 8 通道', '批次', '端到端中位 ms', '端到端 P95 ms',
                          'C 写库中位 ms', 'S 总召中位 ms', 'T 总召中位 ms'], paired)
    checks = len(wire['checks'])
    cache_table = table(['监视点数', '修改前中位 ms', '修改后中位 ms', '耗时下降', '修改后 P95 ms'], [
        [r['points'], r['before']['median_ms'], r['after']['median_ms'],
         str(r['reduction_percent']) + '%', r['after']['p95_ms']] for r in cache['cases']])
    latency_table = table(['通道 / 总点数', '历史中位 ms', '本轮中位 ms', '本轮 P95 ms', '本轮最大 ms'], [
        [f"{r['channels']} / {r['channels'] * (r['yc_per_rtu'] + r['yx_per_rtu'])}",
         previous[r['channels']]['end_to_end_latency']['median_ms'],
         r['end_to_end_latency']['median_ms'], r['end_to_end_latency']['p95_ms'],
         r['end_to_end_latency']['max_ms']] for r in runs])
    control_table = table(['通道', 'C 写库中位 ms', 'S 总召中位 ms', 'T 总召中位 ms',
                           '命令到 S 中位 ms', 'C 回执落库中位 ms', '重连恢复 ms'], [
        [r['channels'], r['c_commit_latency']['median_ms'], r['s_gi']['median_ms'],
         r['t_gi']['median_ms'], r['command_latency']['median_ms'],
         r['command_receipt_latency']['median_ms'], r['upstream_restart_recovery_ms']] for r in runs])
    batch = main_run['comparison']
    batch_table = table(['512 点发送策略', 'I 帧', 'APDU 字节', '中位 ms', 'P95 ms'], [
        [label, batch[key]['wire'][0]['frames'], batch[key]['wire'][0]['bytes'],
         batch[key]['latency']['median_ms'], batch[key]['latency']['p95_ms']]
        for key, label in [('single', '逐点发送'), ('batch', '现有批量发送')]])
    wire_table = table(['独立报文检查', '结果'], [[c['name'], '通过' if c['passed'] else '失败'] for c in wire['checks']])
    package_table = table(['构建文件', '字节', 'SHA-256'], [
        [Path(p).name, v['bytes'], v['sha256']] for p, v in manifest['files'].items()])
    page = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IEC104 命令修复与性能复验 · 2026-11-14</title><link rel="icon" href="data:,">
<style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#edf3f7;color:#183246;font:16px/1.75 system-ui,"Microsoft YaHei",sans-serif}}
main{{max-width:1140px;margin:auto;padding:32px 24px}}header,section{{background:#fff;border:1px solid #d8e3eb;border-radius:14px;padding:26px;margin-bottom:20px}}
header{{border-top:6px solid #087d83}}h1{{font-size:30px;line-height:1.35;margin:8px 0 16px}}h2{{font-size:22px;margin:0 0 14px}}p{{margin:10px 0}}
.eyebrow,.muted{{color:#52697a}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:20px 0}}.card{{background:#eaf5f3;padding:16px;border-radius:10px}}.card strong{{font-size:25px;display:block;color:#006c68}}
nav{{display:flex;flex-wrap:wrap;gap:14px}}a{{color:#006c86}}code,pre{{font:14px/1.65 Consolas,monospace;background:#edf3f6;overflow-wrap:anywhere}}pre{{padding:15px;white-space:pre-wrap;border-radius:8px}}
.table{{max-width:100%;overflow-x:auto;margin:14px 0}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;border-bottom:1px solid #dbe5ec;padding:11px 9px;vertical-align:top;overflow-wrap:anywhere}}th{{background:#eef5f7;color:#254d63}}td{{min-width:80px}}.limit{{border-left:5px solid #b97820}}.ok{{color:#006e62}}
li{{margin:7px 0}}footer{{padding:10px 4px;color:#52697a}}@media(max-width:650px){{main{{padding:12px}}header,section{{padding:18px}}h1{{font-size:25px}}.cards{{grid-template-columns:1fr}}.card strong{{font-size:23px}}}}
</style></head><body><main>
<header><p class="eyebrow">D:\\codex\\scada · Windows / CPython 3.11 · 2026-11-14</p>
<h1>IEC104 命令修复与性能复验</h1>
<p>修复独立对端暴露的撤销确认、选择执行结束帧问题，并降低 C 端快照轮询开销。S 保留独立模拟数据库，C/T 通过共享业务数据库交互。</p>
<div class="cards"><div class="card"><strong>{checks} / {checks}</strong>独立报文检查通过</div><div class="card"><strong>81.02%—85.34%</strong>快照轮询中位耗时下降</div><div class="card"><strong>8192 点 × 2 客户端</strong>突发更新与随后总召完整</div></div>
<p class="muted">新依赖已打包并在隔离环境验证。生产 C/S/T 进程仍使用原环境；本轮没有重启或替换其已加载原生扩展。</p>
<nav><a href="#correctness">正确性</a><a href="#cache">轮询对比</a><a href="#latency">整链路时延</a><a href="#limits">边界</a><a href="#evidence">复现与证据</a></nav></header>
<section id="correctness"><h2>修复内容与验证</h2>
<ul><li>撤销只发送一次 COT=8，等待肯定 COT=9。COT=7、否定确认不能当作撤销成功。</li>
<li>执行和撤销核对连接、发起地址、类型、IOA、值及限定词，排除 S/E 和 CP56 时间字段。覆盖单控、双控、归一化／标度／浮点设点及其 CP56 版本，共 10 类命令。</li>
<li>选择执行成功时依次返回 ACTCON 和 ACTTERM，结束帧回显执行请求且 S/E=0。选择过期、撤销、掉线和执行失败不再伪造成功执行结束。</li>
<li>已完成或过期的选择再撤销，返回否定 COT=9。撤销不会重新选择、不会重复执行，也不会回滚已写入数据库的命令。</li>
<li>原生高、低优先级队列各从 100 ASDU 调为 1024。相同 50 ms 接收检查条件下，旧 scada.1 两客户端均停在 4880／8192 点并因无进度失败；新库完整接收两批不同值／质量及随后总召，旧快照仍保持原值。</li></ul>
<p><strong>回归：</strong>{html.escape(summary.group())}。XML 共记录 {regression.get('tests')} 个测试及子用例，错误、失败、跳过均为 0。</p>
<p>独立报文检查使用单独编码的 TCP 对端；原先 6 个失败项全部通过。未放宽标准 COT 或 S/E 断言。</p>
<p class="muted">扩大回归首轮记录保存在 regression_first.xml／log：修正了原始报文异步观察回调的等待条件，并将 8192 点接收验证改为生产的 50 ms 检查周期；仍要求全部点正确，5 秒无进度或总计超过 60 秒即失败。正确性等待上限不作为性能承诺。</p>
{wire_table}</section>
<section id="cache"><h2>快照轮询：相同负载直接对比</h2>
<p>两客户端先从真实 TCP 接收相同完整数据，再交替运行保存的修改前实现与当前实现。原生库均为本轮 scada.2，10 次预热，60 组样本，每组 10 次调用。计时不含网络等待和断言，只比较完整接收后的 collect_once。</p>
{cache_table}<p>当前实现一次加锁获取不可变监视快照，减少逐点加锁和元组复制；received_at 保留实际接收时间。未接收点仍返回无效质量，旧映射回调不能覆盖新映射。随后发送值和质量变化，两客户端均验证完整接收；8192 点时各收到 16384 个信息对象。</p>
<p class="muted">准备阶段使用接收缓存判定完整性，避免频繁调用较慢的旧轮询器与接收线程争用。高频旧轮询下的超时诊断单独保留，未计为成功样本。</p></section>
<section id="latency"><h2>整链路正确性与实测时延</h2>
<p>S 数据库提交 → S 自主上送 → C 接收及批量写库 → T 扫描业务库 → 下游 IEC104 客户端。1／4／8 通道各执行 60 批 YC/YX 更新，正常阶段采集错误均为 0；同时验证质量单独变化／恢复、未变化点不重复突发上送、周期上送和上下游断线重连总召。</p>
{latency_table}
<p class="limit">历史列来自前一轮同规模验收，并非本轮交替运行的对照。整链路本轮没有证明稳定提速，部分指标较历史更慢；不能将快照环节 81%—85% 的降耗解释为全链路提速。调度、批量写入和操作系统负载仍影响端到端时延。</p>
<h2>同环境追加对照</h2><p>为检查历史时延差异，追加 8 通道各 20 批更新，先旧后新，保持同一 scada.2 原生库、应用代码和数据库配置，仅替换快照轮询方法。单次顺序对照仍受系统负载和调度影响，不给出整体提速保证。</p>{paired_table}
{control_table}
<p>S/C/T 扫描周期 50 ms，S 原生 tick 为 100 ms、T 为 50 ms，C 写库批量窗口 100 ms，k=12、w=8。50 ms 配置不等于全链路 50 ms。总召各通道重复 5 次；命令每通道覆盖 YC/YX 之外的 YK/YT 直接与选择执行，表中命令到 S 与 C 回执时间是两个不同终点。</p>
<h2>既有批量组帧效果复验</h2>{batch_table}
<p>512 点、各 8 轮，批量方式将 512 个 I 帧减少至 11 个，APDU 字节减少 73.39%。这是既有批量策略的对照复验，不是本轮新增的优化收益。</p></section>
<section id="limits" class="limit"><h2>能力及部署边界</h2><ul>
<li>T 只做 IEC104 / Modbus TCP 转发。T 的成功确认表示数据库提交；真实设备执行结果须看 C 的状态与 cmd_handle_time。</li>
<li>1024 项是有限队列，更大突发、慢连接或持续超载仍可能丢失；缓冲内存随连接数增加。本轮没有实现背压或无限事件缓存。</li>
<li>快照只保留最新值：21 次线上的遥信变位在一次 collect_once 中仍是 1 个最新样本。本轮不提供中间变位全部落库的 SOE 保证。</li>
<li>CP24Time2a、独立“仅选择后稍后执行”的公开适配接口仍未支持；本轮未扩展 Modbus/CAN 功能。</li>
<li>验证使用临时数据库和本机真实 TCP；未进行物理设备互操作认证、长时间稳定性试验或 Linux 构建验证。</li>
<li>新 wheel 为 Windows x64 / CPython 3.11。requirements 已固定 scada.2，隔离环境验证成功不代表生产进程已升级。部署时须用实际服务解释器安装依赖并重启对应进程。</li></ul></section>
<section id="evidence"><h2>复现与原始证据</h2>
<p>原生补丁在 vendor/c104/command_contracts.patch，可审查精确改动。backport.py 校验原始 PyPI 源包 SHA-256，严格匹配上下文；最终 sdist 重建后字节一致。</p>
{package_table}<pre>output/iec104-native-20261114/venv/Scripts/python.exe -m pytest -q tests/test_iec104_command_contracts.py tests/test_iec104_receive_snapshots.py
output/iec104-native-20261114/venv/Scripts/python.exe tools/verify_iec104_wire_contracts.py --output reports/iec104_command_fix_20261114/reproduce-wire
output/iec104-native-20261114/venv/Scripts/python.exe tools/verify_iec104_pipeline.py --rounds 60 --output reports/iec104_command_fix_20261114/reproduce-pipeline
output/iec104-native-20261114/venv/Scripts/python.exe tools/benchmark_iec104_receive_cache.py --rounds 60 --before reports/iec104_command_fix_20261114/before/client__iec104.py.snapshot --output reports/iec104_command_fix_20261114/reproduce-cache.json</pre>
<p>证据目录：<code>reports/iec104_command_fix_20261114</code>。关键文件：regression.xml／regression.log、final_wire/wire_contracts.json、snapshot_benchmark.json、pipeline/results.json、scale8/results.json、paired_before/results.json、paired_after/results.json、queue_test_before.xml／queue_test_after.xml、queue_updated_fixture_before.xml／log、build_manifest.json。原始 APDU 在各链路结果目录的 wire-*.jsonl。旧库失败记录作为负向对照保留，不计入新库通过数。</p>
<p>命令撤销原因与 ACTTERM 的含义参考原生库公开接口：<a href="https://support.mz-automation.de/doc/lib60870/latest/group__COMMON.html">lib60870 COT 定义</a>、<a href="https://support.mz-automation.de/doc/lib60870/latest/group__IMASTER__CONNECTION.html">服务端应答接口</a>。实现以包内固定版本源码及独立帧断言为依据。</p></section>
<footer>本报告样式、数据、正文均内嵌，可离线打开；外部链接仅作来源参考。日期按工作站 Pacific/Auckland，JSON 保留各工具原始时区。</footer>
</main></body></html>'''
    (ROOT / 'docs/iec104_command_fix_20261114.html').write_text(page, encoding='utf-8')
    print(summary.group())
    print('Wrote docs/iec104_command_fix_20261114.html')


if __name__ == '__main__':
    main()
