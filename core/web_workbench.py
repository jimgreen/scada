"""Shared server-rendered navigation for the s/t standalone Web pages."""
from html import escape
from pathlib import Path
import re

ASSET_DIR = Path(__file__).with_name("web_static")
ASSETS = {"workbench.css": "text/css; charset=utf-8",
          "workbench.js": "application/javascript; charset=utf-8",
          "log-browser.css": "text/css; charset=utf-8",
          "log-browser.js": "application/javascript; charset=utf-8"}

S_PAGES = [
    ("运行管理", [
        ("processes.html", "/processes", "▤", "进程监控", "各协议服务的运行状态与启停"),
        ("communication.html", "/communication", "◉", "通信监控", "连接、测点数量与通道状态"),
        ("logs.html", "/logs", "≡", "日志与报文", "按年月日小时浏览运行日志和收发报文文件"),
    ]),
    ("数据与定义", [
        ("channels.html", "/channels", "▦", "通道数据", "维护 RTU、通道和规约定义"),
        ("points.html", "/points", "▥", "四遥数据", "按通道维护遥测、遥信、遥控和遥调"),
    ]),
    ("通信测试", [
        ("index.html", "/", "▧", "测试总览", "选择主站或服务端进行通信测试"),
        ("iec104.html", "/iec104", "104", "IEC104", "服务端监听、四遥更新和客户端命令"),
        ("modbus_tcp.html", "/modbus-tcp", "TCP", "Modbus TCP 主站", "连接设备、采集与命令下发"),
        ("modbus_tcp_server.html", "/modbus-tcp-server", "TCP", "Modbus TCP 从站", "本地从站监听与读写联调"),
        ("modbus_rtu.html", "/modbus-rtu", "RTU", "Modbus RTU 主站", "串口配置、采集与命令下发"),
        ("modbus_rtu_server.html", "/modbus-rtu-server", "RTU", "Modbus RTU 从站", "串口从站与模拟测试"),
        ("can.html", "/can", "CAN", "CAN 主站", "帧收发、周期采集与总线诊断"),
        ("can_server.html", "/can-server", "CAN", "CAN 服务端", "自动应答与虚拟总线测试"),
        ("bacnet_server.html", "/bacnet-server", "BAC", "BACnet 服务端", "服务端对象与四遥点测试"),
        ("opc_server.html", "/opc-server", "OPC", "OPC UA 服务端", "服务端节点与四遥点测试"),
    ]),
]
T_PAGES = [
    ("运行管理", [
        ("processes.html", "/processes.html", "▤", "进程监控", "转发进程的启停、心跳和运行状态"),
        ("communication.html", "/communication.html", "◉", "通信监控", "转发通道的监听地址与通信状态"),
        ("logs.html", "/logs.html", "≡", "日志与报文", "按年月日小时浏览运行日志和收发报文文件"),
    ]),
    ("数据与定义", [
        ("channels.html", "/channels.html", "▦", "通道数据", "维护转发通道、规约和监听参数"),
        ("points.html", "/points.html", "▥", "四遥数据", "按通道维护四遥点及源测点映射"),
    ]),
]


def render_workbench(data: bytes, module: str, filename: str) -> bytes:
    """Wrap our trusted static templates; page controls/scripts stay intact."""
    groups = S_PAGES if module == "s" else T_PAGES
    active = "communication.html" if module == "t" and filename == "index.html" else filename
    page = next((item for _, items in groups for item in items if item[0] == active),
                (filename, "/api-docs", "?", "接口文档", "通信服务接口说明"))
    links = []
    for title, items in groups:
        links.append(f'<div class="side-title">{title}</div>')
        for name, href, icon, label, _ in items:
            current = ' aria-current="page"' if name == active else ""
            links.append(f'<a class="nav" href="{href}"{current}>'
                         f'<span aria-hidden="true">{icon}</span><b>{label}</b></a>')
    label = "协议服务" if module == "s" else "四遥转发"
    api_link = '<a href="/api-docs">接口文档 ↗</a>' if module == "s" else ""
    topbar = f'''<a class="workbench-skip" href="#workbench-content">跳到页面内容</a>
<header class="workbench-topbar">
  <div class="workbench-brand"><span class="brand-icon" aria-hidden="true">S</span>
    <strong>SCADA <span>scdfe_{module} · {label}</span></strong></div>
  <div class="header-tools"><span id="connection" class="status" role="status">连接检查中…</span>{api_link}</div>
</header>'''
    sidebar = f'''<aside class="workbench-sidebar" aria-label="功能目录">
<nav aria-label="主导航">{''.join(links)}</nav>
<div class="side-note">本地数据库<code id="workbench-database">读取中…</code></div></aside>'''
    html = data.decode("utf-8")
    html = html.replace("</head>", '<link rel="stylesheet" href="/static/workbench.css">'
                        '<script defer src="/static/workbench.js"></script></head>', 1)
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
