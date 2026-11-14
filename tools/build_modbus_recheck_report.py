"""Build the repeat Modbus acceptance report from measured JSON and JUnit evidence."""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "reports/modbus_common_acceptance_20261113/recheck_01"
DOC = ROOT / "docs/modbus_recheck_20261113.html"


def load(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("passed"):
        raise ValueError(f"Acceptance did not pass: {path}")
    return data


def table(headers, rows):
    def cells(tag, values):
        return "<tr>" + "".join(f"<{tag}>{html.escape(str(v))}</{tag}>" for v in values) + "</tr>"
    return '<div class="scroll" tabindex="0"><table><thead>' + cells("th", headers) + "</thead><tbody>" + "".join(cells("td", row) for row in rows) + "</tbody></table></div>"


def main():
    result = load(EVIDENCE / "results.json")
    large = load(EVIDENCE / "scale8/results.json")
    previous = load(EVIDENCE.parent / "results.json")
    suite = ET.parse(EVIDENCE / "regression.xml").getroot().find("testsuite")
    if suite is None or any(int(suite.get(k, "0")) for k in ("failures", "errors", "skipped")):
        raise ValueError("Regression evidence is missing or contains failures/skips")
    tests = len(suite.findall("testcase"))
    subtests = int(suite.get("tests")) - tests
    tcp = result["pipelines"] + large["pipelines"]
    rtu = result["rtu"]
    all_pipelines = tcp + [rtu]
    latency = table(["测试场景", "监视点数", "更新批数", "C 写库中位 / ms", "端到端中位 / ms", "P95 / ms", "最大 / ms"], [
        (f'TCP {p["channels"]} 通道' if p is not rtu else "RTU 帧软件回环",
         p["channels"] * (p["yc_per_rtu"] + p["yx_per_rtu"]), p["end_to_end_latency"]["samples"],
         p["c_commit_latency"]["median_ms"], p["end_to_end_latency"]["median_ms"],
         p["end_to_end_latency"]["p95_ms"], p["end_to_end_latency"]["max_ms"])
        for p in all_pipelines
    ])
    commands = table(["测试场景", "命令请求数", "S 收到命令中位 / ms", "C 回执落库中位 / ms", "回执 P95 / ms", "TCP 重启恢复 / ms"], [
        (f'TCP {p["channels"]} 通道' if p is not rtu else "RTU 帧软件回环", p["command_latency"]["samples"],
         p["command_latency"]["median_ms"], p["command_receipt_latency"]["median_ms"],
         p["command_receipt_latency"]["p95_ms"], p.get("restart_recovery_ms", "本场景未注入"))
        for p in all_pipelines
    ])
    poll = table(["C 主站四区采集调用", "中位 / ms", "P95 / ms", "最大 / ms"], [
        (f'TCP {p["channels"]} 通道中的第 1 个主站' if p is not rtu else "RTU 帧软件回环主站",
         p["four_bank_poll"]["median_ms"], p["four_bank_poll"]["p95_ms"], p["four_bank_poll"]["max_ms"])
        for p in all_pipelines
    ])
    single = result["comparison"]["single"]
    block = result["comparison"]["block"]
    percent = 100 * (1 - block["latency"]["median_ms"] / single["latency"]["median_ms"])
    byte_percent = 100 * (1 - block["adu_bytes"][0] / single["adu_bytes"][0])
    efficiency = table(["同样读取 500 个寄存器", "逐个读取", "每次 125 个分块读取"], [
        ("每轮请求数", single["requests"][0], block["requests"][0]),
        ("每轮请求 + 响应 ADU 字节", single["adu_bytes"][0], block["adu_bytes"][0]),
        ("中位耗时 / ms", single["latency"]["median_ms"], block["latency"]["median_ms"]),
        ("P95 / ms", single["latency"]["p95_ms"], block["latency"]["p95_ms"]),
    ])
    repeat = table(["相同点数的 TCP 场景", "前次批数", "本次批数", "前次端到端中位 / ms", "本次端到端中位 / ms"], [
        (f'{p["channels"]} 通道', old["end_to_end_latency"]["samples"], p["end_to_end_latency"]["samples"],
         old["end_to_end_latency"]["median_ms"], p["end_to_end_latency"]["median_ms"])
        for old, p in zip(previous["pipelines"], result["pipelines"], strict=True)
    ])
    features = table(["功能", "证据层次与结论"], [
        ("01 / 02 / 03 / 04 四区读取", "TCP 完整链路和 RTU 帧回环通过：线圈、离散输入、保持寄存器、输入寄存器；不同 RTU 同点号互不串值。"),
        ("05 / 06 / 15 / 16 写入", "下游 → T → 共享库 → C → S 完整链路通过；分别等待 S 值变化及 C 处理回执，并读取命令区核对值。功能码使用十进制，15/16 对应 0x0F/0x10。"),
        ("地址 / 事务号 / 站号", "完整链路验证 10001、30001、40001 地址基准与零基地址；抓取 TCP ADU 并检查协议标识、长度、事务标识符及站号匹配。RTU ADU 验证 CRC。"),
        ("数值类型与字序", "完整链路验证 float32 和 uint16；分层回归用独立字节样本检查有符号/无符号 16/32/64 位、float32 四种字序、位/字节切片及取反。并非每种编码均做完整 C/S/T 链路压测。"),
        ("数据库自动刷新", "直接修改独立 S 数据库，S 自主刷新；C 批量写共享库，T 自主读取再提供下游寄存器。测试不调用人机界面同步点值。"),
        ("异常与保护", "完整链路验证非法地址 02、写只读监视点 02、半个 float32 写入 03；回归验证回调失败、闭锁、越限、待处理命令防覆盖返回 04。"),
        ("批量写入原子性", "真实 T TCP 服务测试 FC15/16 后一源点失败时整批回滚：数据库值、命令时刻及协议可读值不部分更新。S 端同样验证批量事务回滚。"),
        ("超时 / 重连 / 连续轮询", "1/4/8 通道正常采集阶段错误计数均为 0；主动停止 S 后对应 C 通道记录一次预期失败，重启后恢复最新值。RTU 回归另验证 CRC 错误超时、无自动重发写入及多从站串行请求。"),
        ("C/T 解耦与并发", "无 C 进程时 T 可写入数据库；并发提交同点只允许一条待处理命令。C 启动消费既有命令的回归使用真实 ChannelWorker + 测试设备适配器，实际报文另由完整链路验证。"),
    ])
    evidence_files = [EVIDENCE / "results.json", EVIDENCE / "scale8/results.json", EVIDENCE / "regression.xml"]
    source_files = [ROOT / p for p in (
        "core/command_store.py", "core/protocol_capabilities.py", "client/modbus_tcp.py", "client/modbus_rtu.py",
        "scdfe_c/__init__.py", "scdfe_t/trans.py", "scdfe_t/web/service.py", "scdfe_s/web/protocol_service.py",
        "server/modbus_mapped_server.py", "server/modbus_trans_server.py", "server/modbus_server.py",
        "tools/verify_modbus_pipeline.py", "tests/test_database_command_exchange.py",
    )]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    status = subprocess.check_output(["git", "status", "--short", "--untracked-files=no"], cwd=ROOT, text=True)
    manifest = dict(git_head=head, tracked_status=status.splitlines(),
                    tcp_timestamp=result["timestamp"], scale8_timestamp=large["timestamp"],
                    junit=suite.attrib, cases=tests, subtests=subtests,
                    sha256={p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in source_files + evidence_files})
    (EVIDENCE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    style = '''*{box-sizing:border-box}body{margin:0;background:#f2f6f8;color:#183044;font:16px/1.8 "Microsoft YaHei",system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:28px 24px 50px}header,section{background:white;border:1px solid #dce5eb;border-radius:10px;padding:24px 28px;margin-bottom:18px}header{border-top:5px solid #087f8c}h1{font-size:30px;line-height:1.45;margin:12px 0}h2{font-size:22px;margin:0 0 14px}p{margin:10px 0}.meta,.note{font-size:14px;color:#526a7c}.callout{background:#eef8f4;padding:14px 18px;border-left:4px solid #228268}.limit{background:#fff7e7;border-left:4px solid #c58b2a;padding:14px 18px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}.card{padding:15px;background:#edf7f8;border-radius:8px}.card strong{display:block;font-size:27px;color:#087f8c}.card span{font-size:14px}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;vertical-align:top;padding:10px 12px;border-bottom:1px solid #dde6ec}th{background:#edf4f7;white-space:nowrap}code{font:14px/1.6 Consolas,monospace;background:#edf2f6;padding:2px 4px;overflow-wrap:anywhere}pre{overflow:auto;background:#183044;color:#f0f7fb;padding:16px;border-radius:8px;line-height:1.6;font-size:13px}a{color:#086d9b;overflow-wrap:anywhere;text-underline-offset:3px}nav{display:flex;flex-wrap:wrap;gap:10px 18px}li{margin:7px 0}@media(max-width:700px){main{padding:12px 9px}header,section{padding:18px 14px}h1{font-size:25px}.cards{grid-template-columns:1fr}th,td{padding:8px}}@media print{body{background:white}main{max-width:none;padding:0}section{break-inside:avoid}nav{display:none}}'''
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Modbus 常用功能复验与效率 · 2026-11-13</title><style>{style}</style></head><body><main>
<header><div class="meta">SCADA · 2026-11-13 · 数据库解耦后复验</div><h1>Modbus 常用功能正确性<br>与运行效率复验</h1><p>scdfe_c / scdfe_s：TCP 与 RTU 软件链路；scdfe_t：仅 Modbus TCP 转发。</p><div class="callout">本轮常用功能验证通过：<strong>{tests} 项测试、{subtests} 个子用例通过</strong>，回归耗时 {float(suite.get('time')):.2f} 秒。另完成 TCP 1/4/8 通道实际协议链路与 RTU 帧回环；未发现新的功能失败。</div><div class="cards"><div class="card"><strong>8 通道 · 1984 点</strong><span>60 批监视值更新逐批核对</span></div><div class="card"><strong>382.084 ms</strong><span>8 通道端到端中位延迟</span></div><div class="card"><strong>500 → 4</strong><span>同量连续寄存器读取请求数</span></div></div><nav><a href="#scope">解耦与测试方法</a><a href="#functions">正确性</a><a href="#latency">延迟</a><a href="#efficiency">效率对比</a><a href="#limits">验证边界</a><a href="#evidence">证据与复现</a></nav></header>
<section id="scope"><h2>解耦方式与测试方法</h2><p>C/T 共用 <code>scada.sqlite3</code>，通过数据库交换点值与待处理命令；S 保留独立 <code>scdfe_s.sqlite3</code>。各自负责报文与连接，T 不依赖 C 的 IPC 服务。</p><p>验收时 S 作为外部设备模拟端与 C 建立协议回环。数据路径为：S 独立库 → S 协议服务 → C 采集 → C/T 共享库 → T 协议服务 → 下游主站。此接线用于测试协议兼容，不引入生产内部报文转交。</p><p>T 肯定写响应只确认数据库已提交；C 随后读取、下发并写回处理状态。<code>cmd_time</code> 与 <code>cmd_handle_time</code> 区分受理和处理完成；同点已有待处理命令时拒绝覆盖。批量原子性只涵盖数据库受理，不代表多个设备动作构成事务。</p><p>全部测试使用临时数据库及本机随机端口；未变更业务数据库、生产服务或业务代码。本次新增报告与复验记录，保留此前未提交的解耦修改及其他工作区变更。</p></section>
<section id="functions"><h2>常用功能正确性</h2>{features}<p class="note">回归包含 Modbus 专项、T 运行时、数据库交换与受影响的 IEC104 接口兼容测试。85 项及 227 个子用例来自本次单次 pytest 运行，不与历史测试数累计；这不是全仓库测试或协议认证结论。</p></section>
<section id="latency"><h2>实际链路延迟</h2>{latency}<p>S 数据库扫描、C 轮询、T 扫描均配置 50 ms；C 批量写库间隔 100 ms、阈值 500 条；单次通信响应超时 500 ms。1/4 通道每 RTU 为 64 遥测 + 64 遥信；8 通道每 RTU 为 124 + 124；表中不包含额外命令点。</p><p>每批同时修改所有监视点，确认 C 入库及全部 T 下游值正确后再提交下一批；因此是有确认间隔的连续更新，不能代表无等待持续洪泛吞吐。端到端从 S 数据库提交后计时，包含 S/C/T 调度、写库、协议及观察开销；观察间隔 5 ms，P95 采用 nearest-rank。</p><p class="limit">8 通道端到端中位数 382.084 ms，P95 518.611 ms，最大 632.918 ms。<strong>50 ms 的轮询配置不是全链路 50 ms 的刷新保证。</strong>500 ms 单请求响应超时与完整链路时延也不是同一指标。本次没有预设时延合格阈值，功能通过不等于满足任意实时性要求。</p>
<h2>命令处理与断线恢复</h2>{commands}<p>四种命令逐次发送；同点下一条命令等待上一条 C 回执。S 值变化表示模拟设备收到并提交，不表示真实执行器已完成机械动作。每个 TCP 场景仅停止第 1 个 S 通道；其余通道保持运行。</p>
<h2>四区采集调用耗时</h2>{poll}<p>各 30 次额外采集调用，使用仍在轮询的生产主站连接，包含连接锁等待；多通道场景只计第 1 个主站。此指标不能代替所有通道完成一个采集周期的耗时。</p>
<h2>与前次记录对照</h2>{repeat}<p>前次与本次均为数据库解耦后的实现，样本数不同，Windows 并发负载与调度未严格控制。1 通道本次更慢、4 通道略快，不能据此断言版本性能回退或提升。多通道可能更早达到写库批量阈值，4 通道更低的中位数也不能证明线性扩展。</p></section>
<section id="efficiency"><h2>连续寄存器分块读取</h2>{efficiency}<p>同一 TCP 服务、同一连接、同样 500 个连续寄存器，逐个读取与每次 125 个读取交替各执行 8 轮，逐轮校验全部值一致。中位耗时下降 <strong>{percent:.2f}%</strong>，请求及响应 ADU 字节减少 <strong>{byte_percent:.2f}%</strong>。</p><p>这是访问策略对比，不是两个程序版本的性能差异。ADU 字节不含 TCP/IP 和链路层；连续分块需由采集配置定义，不表示程序已自动合并所有离散请求。跨站号、不同功能码及不允许读取的地址空洞不可直接套用该收益。</p></section>
<section id="limits"><h2>验证边界</h2><div class="limit">RTU 使用生产串口适配器及真实 RTU 帧、CRC，通过 <code>socket://</code> 承载字节流。配置 19200、8E1，但<strong>未连接实体串口或 RS485 设备</strong>；测量不能作为物理波特率、帧间静默、总线冲突或抗干扰结果。</div><p>1/4/8 通道分别执行 30/30/60 批更新，RTU 6 批；不是长时间稳定性试验。测试使用运行时线程和独立 C 批量写库进程，未测完整生产监督器部署的 CPU/内存曲线，也未覆盖网络抖动、断电恢复或极限并发。</p><p>Modbus 寄存器值不自带 IEC104 质量位、COT 或时间标签；需要透传时应配置相应寄存器。此次验证点值及命令处理，不能证明每次瞬时变化均按事件留存。RTU 的实际端口拔插恢复仍需硬件验收。</p></section>
<section id="evidence"><h2>环境、证据与复现</h2><p>工作目录 <code>D:\\codex\\scada</code>；基线 Git <code>{head[:12]}</code> 加当前未提交修改，具体源文件 SHA-256 见清单。TCP/RTU 运行开始：<code>{html.escape(result['timestamp'])}</code>；8 通道开始：<code>{html.escape(large['timestamp'])}</code>。报告显示北京时间；JUnit 保留宿主时区偏移。</p><p>Python 3.11.15；依赖：{html.escape(str(result['libraries']))}。</p><ul>
<li><a href="../reports/modbus_common_acceptance_20261113/recheck_01/results.json">1/4 通道、RTU 与分块对比原始结果</a>；<a href="../reports/modbus_common_acceptance_20261113/recheck_01/scale8/results.json">8 通道原始结果</a></li>
<li><a href="../reports/modbus_common_acceptance_20261113/recheck_01/regression.xml">本次 JUnit 回归记录</a>；<a href="../reports/modbus_common_acceptance_20261113/recheck_01/manifest.json">代码与结果文件校验清单</a></li>
<li><a href="../reports/modbus_common_acceptance_20261113/recheck_01/wire-tcp-4ch.jsonl">4 通道 ADU 抓取</a>；<a href="../reports/modbus_common_acceptance_20261113/recheck_01/scale8/wire-tcp-8ch.jsonl">8 通道 ADU 抓取</a>；<a href="../reports/modbus_common_acceptance_20261113/recheck_01/wire-rtu.jsonl">RTU 回调记录</a>（RTU 接收回调包含累积缓冲快照，不能直接逐条累加为线上字节数）</li>
<li><a href="modbus_common_acceptance_20261113.html">前次验收与解耦实现报告</a>；<a href="../tools/verify_modbus_pipeline.py">可复用验收脚本</a></li></ul>
<pre>Set-Location D:\\codex\\scada
&amp; D:\\anaconda3\\python.exe tools/verify_modbus_pipeline.py --rounds 30 --output reports/modbus_local_repeat
&amp; D:\\anaconda3\\python.exe tools/verify_modbus_pipeline.py --channels 8 --points 124 --rounds 60 --skip-rtu --skip-comparison --output reports/modbus_local_repeat/scale8</pre>
<p class="note">顺序执行性能测试，避免同时运行压测互相干扰。报告内容与样式全部内嵌，离线可读；链接证据为可选补充文件。</p></section></main></body></html>'''
    DOC.write_text(page, encoding="utf-8")
    print(json.dumps(dict(report=str(DOC), tests=tests, subtests=subtests, sha256=hashlib.sha256(DOC.read_bytes()).hexdigest()), ensure_ascii=False))


if __name__ == "__main__":
    main()
