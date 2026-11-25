"""Shared server-rendered navigation for standalone SCDFE Web pages."""
from html import escape
from pathlib import Path
import re

ASSET_DIR = Path(__file__).with_name("web_static")
ASSETS = {"workbench.css": "text/css; charset=utf-8",
          "ui-theme.css": "text/css; charset=utf-8",
          "ui-i18n.js": "application/javascript; charset=utf-8",
          "audit-client.js": "application/javascript; charset=utf-8",
          "workbench.js": "application/javascript; charset=utf-8",
          "layout-resizer.js": "application/javascript; charset=utf-8"}

# 统一线宽（1.75px）的极简矢量导航图标，替代零散字符符号。
NAV_ICONS = {
    "↗": '<svg class="ui-nav-icon" viewBox="0 0 24 24"><path d="M7 17 17 7M7 7h10v10"/></svg>',
    "◴": '<svg class="ui-nav-icon" viewBox="0 0 24 24"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg>',
    "▤": '<svg class="ui-nav-icon" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><circle cx="7" cy="7" r=".8"/><circle cx="7" cy="17" r=".8"/></svg>',
    "◉": '<svg class="ui-nav-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M5.6 18.4a9 9 0 0 1 0-12.8m12.8 0a9 9 0 0 1 0 12.8M8.5 15.5a5 5 0 0 1 0-7m7 0a5 5 0 0 1 0 7"/></svg>',
    "⇄": '<svg class="ui-nav-icon" viewBox="0 0 24 24"><path d="m17 3 4 4-4 4M3 7h18M7 21l-4-4 4-4m14 4H3"/></svg>',
    "◷": '<svg class="ui-nav-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    "≡": '<svg class="ui-nav-icon" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>',
    "∿": '<svg class="ui-nav-icon" viewBox="0 0 24 24"><path d="M3 17 8 11l4 4 5-7 4 4"/></svg>',
    "▦": '<svg class="ui-nav-icon" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>',
    "▥": '<svg class="ui-nav-icon" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg>',
    "▧": '<svg class="ui-nav-icon" viewBox="0 0 24 24"><path d="m14.7 6.3 3 3L8 19H5v-3l9.7-9.7z"/><path d="M12 20h9"/></svg>',
}


def _nav_icon(symbol: str) -> str:
    return NAV_ICONS.get(symbol, escape(symbol))

POINT_TRENDS_PAGE = ("point-trends.html", "/point-trends", "∿", "采样管理", "管理实时曲线采样与历史")

S_PAGES = [
    ("运行管理", [
        ("command-traces.html", "/command-traces", "↗", "指令追踪", "入库、选择、执行与确认阶段"),
        ("diagnostics.html", "/diagnostics", "◴", "运行诊断", "分段耗时与启动配置检查"),
        ("processes.html", "/processes", "▤", "进程监控", "按通信通道展示进程、PID、心跳和运行状态"),
        ("communication.html", "/communication", "◉", "通信监控", "连接、测点数量与通道状态"),
        ("connections.html", "/connections", "⇄", "客户端连接", "按连接查看客户端、交互内容、日志与报文"),
        ("soe.html", "/soe", "◷", "SOE 事件", "逐次变位、源时标与转发进度"),
        ("logs.html", "/logs", "≡", "日志报文", "按通信通道浏览运行日志和收发报文"),
        POINT_TRENDS_PAGE,
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
        POINT_TRENDS_PAGE,
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
        POINT_TRENDS_PAGE,
    ]),
    ("数据与定义", [
        ("channels.html", "/channels.html", "▦", "通道数据", "维护转发通道、规约和监听参数"),
        ("points.html", "/points.html", "▥", "四遥数据", "按通道维护四遥点及源测点映射"),
    ]),
]


D_PAGES = [
    ('运行管理', [('overview.html', '/', '◉', '设备采集', '设备量测采集状态与启停'),
                  ('logs.html', '/logs', '◷', '运行日志与记录', '查看采集/指令运行日志、采集批次与修改记录'),
                  ('diagnostics.html', '/diagnostics', '◴', '运行诊断', '设备采集耗时与源库检查'),
                  POINT_TRENDS_PAGE]),
    ('设备量测', [('measurements.html', '/measurements', '▦', '量测与参数', '按设备实时监视量测值或批量调整量测参数'),
                 ('mappings.html', '/mappings', '⇄', '量测定义', '查询和维护设备量测与 SCADA 测点的关联')]),
    ('设备控制', [('controls.html', '/controls', '↗', '指令下发', '实时监控并下发设备遥控与遥调指令'),
                 ('control-mappings.html', '/control-mappings', '▥', '指令定义', '查询和维护设备遥控/遥调与 scdfe_c 测点的关联')]),
]


def render_workbench(data: bytes, module: str, filename: str) -> bytes:
    """Wrap our trusted static templates; page controls/scripts stay intact."""
    groups = {"s": S_PAGES, "c": C_PAGES, "t": T_PAGES, "d": D_PAGES}[module]
    if module == "t" and filename == "index.html":
        active = "communication.html"
    elif module == "d" and filename == "parameters.html":
        active = "measurements.html"
    elif module == "d" and filename == "history.html":
        active = "logs.html"
    else:
        active = filename
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
                    f'<span aria-hidden="true">{_nav_icon(icon)}</span><b>{label}</b></summary>'
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
                    f'<summary class="nav"><span aria-hidden="true">{_nav_icon(icon)}</span><b>{label}</b></summary>'
                    f'<nav class="nav-children" aria-label="通信测试子目录">{children}</nav></details>'
                )
                continue
            current = ' aria-current="page"' if name == active else ""
            links.append(f'<a class="nav" href="{href}"{current}>'
                         f'<span aria-hidden="true">{_nav_icon(icon)}</span><b>{label}</b></a>')
    label = {"s": "协议服务", "c": "运行工作台", "t": "四遥转发", "d": "设备量测"}[module]
    if module in ("s", "c"):
        api_link = '<a href="/api-docs">接口文档 ↗</a>'
    elif module == "d":
        api_link = '<a id="jump-portal-link" href="http://127.0.0.1:8981/" target="_blank" rel="noopener">监控门户 ↗</a>'
    else:
        api_link = ""
    topbar = f'''<a class="workbench-skip" href="#workbench-content">跳到页面内容</a>
<header class="workbench-topbar">
  <div class="workbench-brand"><span class="brand-icon" aria-hidden="true">S</span>
    <strong>SCADA <span>scdfe_{module} · {label}</span></strong></div>
  <div class="header-tools"><span id="connection" class="status" role="status">连接检查中…</span>{api_link}</div>
</header>'''
    sidebar = f'''<aside class="workbench-sidebar" aria-label="功能目录">
<nav aria-label="主导航">{''.join(links)}</nav></aside>'''
    html = data.decode("utf-8")
    html = html.replace("<html ", '<html data-theme="minimalist-light" ', 1)
    html = html.replace("<head>", '<head><script src="/static/audit-client.js"></script>', 1)
    head_extra = '<link rel="stylesheet" href="/static/workbench.css"><link rel="stylesheet" href="/static/database-selector.css">'
    if "/static/ui-theme.css" not in html:
        head_extra += '<link rel="stylesheet" href="/static/ui-theme.css">'
    if "/static/ui-i18n.js" not in html:
        head_extra += '<script defer src="/static/ui-i18n.js"></script>'
    head_extra += (
        '<script defer src="/static/database-selector.js"></script>'
        '<script defer src="/static/workbench.js"></script>'
        '<script defer src="/static/layout-resizer.js"></script>'
    )
    html = html.replace("</head>", head_extra + "</head>", 1)
    html = html.replace("<body", '<body class="workbench"', 1)
    html = re.sub(r"<header\b[^>]*>.*?</header>", lambda _: topbar, html, count=1, flags=re.S)
    # The former horizontal menu is replaced by actual page links in the sidebar.
    html = re.sub(r'<nav class="top-nav">.*?</nav>', "", html, flags=re.S)
    heading = f'<div class="page-heading"><h1 title="{escape(page[4])}">{escape(page[3])}</h1></div>'
    html = re.sub(r"<main\b[^>]*>", lambda _: '<div class="workbench-shell">' + sidebar +
                  '<main class="workbench-content" id="workbench-content" tabindex="-1">' + heading,
                  html, count=1)
    html = html.replace("</main>", "</main></div>", 1)
    return html.encode("utf-8")
