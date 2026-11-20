"""Shared server-rendered navigation for standalone C/S/T Web pages."""
from html import escape
from pathlib import Path
import re

ASSET_DIR = Path(__file__).with_name("web_static")
ASSETS = {"workbench.css": "text/css; charset=utf-8",
          "connections.css": "text/css; charset=utf-8",
          "connections.js": "application/javascript; charset=utf-8",
          "audit-client.js": "application/javascript; charset=utf-8",
          "workbench.js": "application/javascript; charset=utf-8",
          "log-browser.css": "text/css; charset=utf-8",
          "log-browser.js": "application/javascript; charset=utf-8",
          "layout-resizer.js": "application/javascript; charset=utf-8"}

S_PAGES = [
    ("运行管理", [
        ("command-traces.html", "/command-traces", "↗", "指令追踪", "入库、选择、执行与确认阶段"),
        ("diagnostics.html", "/diagnostics", "◴", "运行诊断", "分段耗时与启动配置检查"),
        ("processes.html", "/processes", "▤", "进程监控", "按通信通道展示进程、PID、心跳和运行状态"),
        ("communication.html", "/communication", "◉", "通信监控", "连接、测点数量与通道状态"),
        ("connections.html", "/connections", "⇄", "客户端连接", "按连接查看客户端、交互内容、日志与报文"),
        ("soe.html", "/soe", "◷", "SOE 事件", "逐次变位、源时标与转发进度"),
        ("logs.html", "/logs", "≡", "日志报文", "按通信通道浏览运行日志和收发报文"),
    ]),
    ("数据与定义", [
        ("channels.html", "/channels", "▦", "通道数据", "维护 RTU、通道和规约定义"),
        ("points.html", "/points", "▥", "四遥数据", "按通道维护遥测、遥信、遥控和遥调"),
    ]),
    ("通信测试", [
        ("test.html", "/test", "▧", "通信测试", "按 RTU 自动匹配规约、四遥、连接和日志"),
    ]),
]
S_TEST_SECTIONS = (
    ("channel", "通道配置"), ("points", "四遥数据"),
    ("connection", "连接信息"), ("logs", "日志报文"),
)
C_PAGES = [
    ("运行管理", [
        ("processes", "/#processes", "▤", "进程监控", "进程与运行状态"),
        ("scdfe", "/#scdfe", "◉", "通信监控", "通道通信状态"),
        ("logs", "/#logs", "≡", "运行日志", "运行日志与报文"),
        ("soe.html", "/soe", "◷", "SOE 事件", "逐次变位、源时标与转发进度"),
        ("diagnostics.html", "/diagnostics", "◴", "运行诊断", "分段耗时与启动配置检查"),
        ("command-traces.html", "/command-traces", "↗", "指令追踪", "入库、派发与确认阶段"),
    ]),
    ("数据与定义", [
        ("data", "/#data", "▦", "数据与定义", "通道、RTU 和四遥定义"),
    ]),
]
C_DATA_SECTIONS = (
    ("channels", "通道定义"), ("rtus", "RTU 定义"),
    ("points", "四遥定义"),
    ("parameters", "协议参数"), ("helpers", "辅助表"),
)
T_PAGES = [
    ("运行管理", [
        ("command-traces.html", "/command-traces", "↗", "指令追踪", "共享业务库中的命令派发与确认阶段"),
        ("diagnostics.html", "/diagnostics", "◴", "运行诊断", "分段耗时与启动配置检查"),
        ("processes.html", "/processes.html", "▤", "进程监控", "转发进程的启停、心跳和运行状态"),
        ("communication.html", "/communication.html", "◉", "通信监控", "转发通道的监听地址与通信状态"),
        ("connections.html", "/connections", "⇄", "客户端连接", "按连接查看客户端、交互内容、日志与报文"),
        ("soe.html", "/soe", "◷", "SOE 事件", "源事件历史与转发确认进度"),
        ("logs.html", "/logs.html", "≡", "日志报文", "按年月日小时浏览运行日志和收发报文文件"),
    ]),
    ("数据与定义", [
        ("channels.html", "/channels.html", "▦", "通道数据", "维护转发通道、规约和监听参数"),
        ("points.html", "/points.html", "▥", "四遥数据", "按通道维护四遥点及源测点映射"),
    ]),
]


def render_workbench(data: bytes, module: str, filename: str) -> bytes:
    """Wrap our trusted static templates; page controls/scripts stay intact."""
    groups = {"s": S_PAGES, "c": C_PAGES, "t": T_PAGES}[module]
    active = "communication.html" if module == "t" and filename == "index.html" else filename
    page = next((item for _, items in groups for item in items if item[0] == active),
                (filename, "/api-docs", "?", "接口文档", "通信服务接口说明"))
    links = []
    for title, items in groups:
        links.append(f'<div class="side-title">{title}</div>')
        for name, href, icon, label, _ in items:
            if module == "c" and name == "data":
                children = ''.join(
                    f'<a class="nav" href="/#{key}"><b>{title}</b></a>'
                    for key, title in C_DATA_SECTIONS
                )
                links.append(
                    f'<details class="nav-branch"><summary class="nav">'
                    f'<span aria-hidden="true">{icon}</span><b>{label}</b></summary>'
                    f'<nav class="nav-children" aria-label="数据与定义子目录">{children}</nav></details>'
                )
                continue
            if module == "s" and name == "test.html":
                selected = name == active
                children = ''.join(
                    f'<a class="nav" href="{"" if selected else href}#{key}" '
                    f'data-test-page="{key}"'
                    + (f' aria-controls="test-panel-{key}"' if selected else '')
                    + f'><b>{title}</b></a>'
                    for key, title in S_TEST_SECTIONS
                )
                links.append(
                    f'<details class="nav-branch{" is-active" if selected else ""}"{" open" if selected else ""}>'
                    f'<summary class="nav"><span aria-hidden="true">{icon}</span><b>{label}</b></summary>'
                    f'<nav class="nav-children" aria-label="通信测试子目录">{children}</nav></details>'
                )
                continue
            current = ' aria-current="page"' if name == active else ""
            links.append(f'<a class="nav" href="{href}"{current}>'
                         f'<span aria-hidden="true">{icon}</span><b>{label}</b></a>')
    label = {"s": "协议服务", "c": "运行工作台", "t": "四遥转发"}[module]
    api_link = '<a href="/api-docs">接口文档 ↗</a>' if module in ("s", "c") else ""
    topbar = f'''<a class="workbench-skip" href="#workbench-content">跳到页面内容</a>
<header class="workbench-topbar">
  <div class="workbench-brand"><span class="brand-icon" aria-hidden="true">S</span>
    <strong>SCADA <span>scdfe_{module} · {label}</span></strong></div>
  <div class="header-tools"><span id="connection" class="status" role="status">连接检查中…</span>{api_link}</div>
</header>'''
    sidebar = f'''<aside class="workbench-sidebar" aria-label="功能目录">
<nav aria-label="主导航">{''.join(links)}</nav></aside>'''
    html = data.decode("utf-8")
    html = html.replace("<head>", '<head><script src="/static/audit-client.js"></script>', 1)
    html = html.replace("</head>", '<link rel="stylesheet" href="/static/workbench.css">'
                        '<link rel="stylesheet" href="/static/database-selector.css">'
                        '<script defer src="/static/database-selector.js"></script>'
                        '<script defer src="/static/workbench.js"></script>'
                        '<script defer src="/static/layout-resizer.js"></script></head>', 1)
    html = html.replace("<body", '<body class="workbench"', 1)
    html = re.sub(r"<header\b[^>]*>.*?</header>", lambda _: topbar, html, count=1, flags=re.S)
    # The former horizontal menu is replaced by actual page links in the sidebar.
    html = re.sub(r'<nav class="top-nav">.*?</nav>', "", html, flags=re.S)
    heading = f'<div class="page-heading"><h1>{escape(page[3])}</h1><p>{escape(page[4])}</p></div>'
    html = re.sub(r"<main\b[^>]*>", lambda _: '<div class="workbench-shell">' + sidebar +
                  '<main class="workbench-content" id="workbench-content" tabindex="-1">' + heading,
                  html, count=1)
    html = html.replace("</main>", "</main></div>", 1)
    return html.encode("utf-8")
