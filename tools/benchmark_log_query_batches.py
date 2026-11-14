"""Compare C/S/T query implementations against identical isolated log files.

Example: python tools/benchmark_log_query_batches.py --baseline-file before.py
         --repeat 9 --output output/log-query-batches/performance.json
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.benchmark_log_query import load_source
from core.scdfe_control import InstanceLock


def collect(module, root, query, family, *, first_page=False):
    pages, cursor = [], None
    for _ in range(10000):
        page = module.search_logs(root,{**query,**({'cursor':cursor} if cursor else {})},family=family)
        assert page['scanned_bytes'] <= module.MAX_SCAN_BYTES
        pages.append(page)
        if first_page or page['next_cursor'] is None:
            return pages
        assert page['next_cursor'] != cursor, 'pagination stalled'
        cursor = page['next_cursor']
    raise AssertionError('pagination did not finish')


def fixtures(root):
    root.mkdir()
    cases = []
    def single(name,lines,queries):
        directory = root/name
        directory.mkdir()
        filename = 'scdfe_c-rtu-1-20261114-10.log'
        (directory/filename).write_text('\n'.join(lines)+'\n',encoding='utf8')
        for suffix,query in queries:
            cases.append((name+suffix,directory,'c',{'filename':filename,'limit':'5000',**query}))

    single('dense_changes', [
        f'10:00:{i//400:02d} [日志] @a1.1 业务事件 采集变化 四遥=遥测 点号={i} '
        f'旧值={i}.25 新值={i+1}.25 质量=0 来源=突发/变位 命令时标=2026-11-14T10:00:00Z'
        for i in range(12000)], [('',{'q':'业务事件','record':'runtime'}),('_preview',{'limit':'20'})])
    # The rare Chinese term and Unicode fallback read the very same long rows.
    single('sparse_changes', [
        f'10:{i//240:02d}:{i//4%60:02d} [日志] 业务事件 {"命中 Straße" if i%97==0 else "普通 Update"} '
        f'点号={i} 新值={i}.25 质量=0 '+('详情=数据库刷新 RTU状态正常 通信状态已连接 '*6)
        for i in range(12000)], [('_han',{'q':'命中'}),('_unicode',{'q':'STRASSE'}),
                                 ('_miss',{'q':'不存在'}),('_latin_miss',{'q':'absent'})])
    single('mixed_headers', [
        f'10:{i//240:02d}:{i//4%60:02d} [{("日志","警告","错误","进程")[i%4]}] '
        f'普通 record {i} value={i}.125 quality={i%4}' for i in range(12000)], [('',{})])
    for family in ('c','s','t'):
        directory = root/f'multi_{family}'
        directory.mkdir()
        for owner in range(1,61):
            process = f'scdfe_c-rtu-{owner}' if family=='c' else f'scdfe_{family}-channel-{owner}'
            (directory/f'{process}-20261114-10.log').write_text('\n'.join(
                f'10:00:{i//10:02d} [日志] @a{owner}.1 业务事件 变化上送 点号={i} 值={i}.25 质量=0'
                for i in range(200))+'\n',encoding='utf8')
        cases.append((f'multi_{family}',directory,family,{'q':'变化上送','limit':'5000','record':'runtime'}))
    return cases


def write_report(report,path):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    names = {'dense_changes':'12000 条变化明细 / 中文筛选','dense_changes_preview':'最新 20 条预览',
             'sparse_changes_han':'12000 条长日志 / 稀疏中文命中',
             'sparse_changes_unicode':'12000 条长日志 / Unicode 大小写匹配',
             'sparse_changes_miss':'12000 条长日志 / 中文未命中',
             'sparse_changes_latin_miss':'12000 条长日志 / 英文未命中',
             'mixed_headers':'12000 条混合类型日志 / 全部分页',
             'multi_c':'C / 60 RTU / 12000 条记录', 'multi_s':'S / 60 通道 / 12000 条记录',
             'multi_t':'T / 60 通道 / 12000 条记录'}
    rows = ''.join(f'<tr><td>{names[name]}</td><td>{data["before"]:.3f}</td>'
                   f'<td>{data["after"]:.3f}</td><td>{data["reduction_percent"]:.1f}%</td>'
                   f'<td>{report["cpu_mean_ms"][name]["before"]:.3f}</td>'
                   f'<td>{report["cpu_mean_ms"][name]["after"]:.3f}</td>'
                   f'<td>{report["checks"][name]["items"]}</td></tr>'
                   for name,data in report['medians_ms'].items())
    document = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>C/S/T 日志查询优化验证</title><style>
body{{font:16px/1.7 system-ui,sans-serif;max-width:1080px;margin:40px auto;padding:0 24px;color:#163044}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;border:1px solid #c5d6df;text-align:left}}
th{{background:#eaf5f5}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}}
</style><h1>C/S/T 日志查询优化验证</h1>
<p>同一进程、相同临时日志、相同查询；新旧代码各预热 3 次，随后交替执行 {report['repeat']} 轮。
下表为查询耗时中位数（毫秒）：最新预览只读取第一页，其余场景读取全部游标分页；正百分比表示耗时下降。
每轮逐项校验原文、顺序、时间、游标、扫描计数和分页结果一致。</p>
<table><thead><tr><th>场景</th><th>修改前 ms</th><th>修改后 ms</th><th>耗时下降</th>
<th>前 CPU ms</th><th>后 CPU ms</th><th>返回记录</th></tr></thead>
<tbody>{rows}</tbody></table>
<p>优化：复用同次请求内的日志头与日期/时钟转换；中文汉字及数字关键词先用 UTF-8 字节筛选，
其余关键词保留完整 Unicode 大小写匹配。缓存有界，不跨请求共享时区状态。
另修复业务正文引用“报文 RX/TX”被误判为报文的错误，独立回归用例验证。</p>
<p>范围：本机热文件缓存、Python 查询函数；不含 HTTP、浏览器绘制或网络通信时间。
未修改生产数据库，未重启运行中的服务；不代表现场设备吞吐率或总服务性能。
英文未命中等场景可能没有收益，应以实测数值为准。
同时保留进程 CPU 用时（各轮累计后取平均，含内核时间）；它与包含调度等待和 I/O 等待的实际耗时不同。
Windows CPU 计时的调度粒度较粗，短预览的 CPU 读数不适合做细微性能判断。原始样本均保留。</p>
<details><summary>完整环境、样本、代码散列及验证数据</summary><pre>{html.escape(json.dumps(report,ensure_ascii=False,indent=2))}</pre></details>
</html>'''
    path.with_suffix('.html').write_text(document,encoding='utf8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repeat',type=int,default=9)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error('repeat must be positive')
    repo = Path(__file__).resolve().parents[1]
    lock = InstanceLock(Path(tempfile.gettempdir())/'scada-benchmark',role='benchmark')
    if not lock.acquire():
        raise RuntimeError('another SCADA benchmark is running')
    try:
        with tempfile.TemporaryDirectory(prefix='scada-query-batches-') as temp:
            root = Path(temp)
            modules,hashes = {},{}
            for variant,source in (('before',args.baseline_file),('after',repo/'scdfe_c/web/log_query.py')):
                frozen = root/f'{variant}.py'
                frozen.write_bytes(source.read_bytes())
                modules[variant],hashes[variant] = load_source(frozen)
            cases = fixtures(root/'fixtures')
            expected,checks,samples = {},{},[]
            for name,directory,family,query in cases:
                calls = {}
                for variant,module in modules.items():
                    with patch.object(module,'_log_timestamp',wraps=module._log_timestamp) as convert:
                        pages = collect(module,directory,query,family,first_page=name.endswith('_preview'))
                        calls[variant] = convert.call_count
                    if variant=='before':
                        expected[name] = pages
                    assert pages==expected[name], f'response mismatch: {name}, {variant}'
                    for _ in range(3):
                        assert collect(module,directory,query,family,first_page=name.endswith('_preview'))==expected[name]
                checks[name] = dict(responses_equal=True,pages=len(pages),
                    items=sum(len(p['items']) for p in pages),timestamp_conversions=calls,
                    scanned_files=sum(p['scanned_files'] for p in pages),
                    scanned_bytes=sum(p['scanned_bytes'] for p in pages))
            for repeat in range(args.repeat):
                order = ('before','after') if repeat%2==0 else ('after','before')
                for name,directory,family,query in cases:
                    for variant in order:
                        started = time.perf_counter()
                        cpu_started = time.process_time()
                        pages = collect(modules[variant],directory,query,family,first_page=name.endswith('_preview'))
                        cpu_elapsed = (time.process_time()-cpu_started)*1000
                        elapsed = (time.perf_counter()-started)*1000
                        assert pages==expected[name], f'response drift: {name}, {variant}'
                        samples.append(dict(repeat=repeat+1,case=name,variant=variant,
                                            elapsed_ms=elapsed,cpu_ms=cpu_elapsed))
                print(f'completed round {repeat+1}/{args.repeat}',flush=True)
            medians,cpu_means = {},{}
            for name,_,_,_ in cases:
                medians[name] = {v:statistics.median(s['elapsed_ms'] for s in samples
                    if s['case']==name and s['variant']==v) for v in modules}
                values = medians[name]
                values['reduction_percent'] = 100*(1-values['after']/values['before'])
                cpu_means[name] = {v:statistics.mean(s['cpu_ms'] for s in samples
                    if s['case']==name and s['variant']==v) for v in modules}
            report = dict(created_at=datetime.now(timezone.utc).isoformat(),python=sys.version,
                platform=platform.platform(),repeat=args.repeat,baseline_file=str(args.baseline_file),
                source_sha256=hashes,benchmark_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                scope='isolated identical files; warm filesystem; preview first page, otherwise all pages; no HTTP',
                queries={n:dict(family=f,query=q) for n,_,f,q in cases},
                checks=checks,medians_ms=medians,cpu_mean_ms=cpu_means,samples=samples)
            write_report(report,args.output)
            print(json.dumps(medians,ensure_ascii=False,indent=2),flush=True)
    finally:
        lock.release()


if __name__=='__main__':
    main()
