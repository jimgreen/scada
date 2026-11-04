"""Command-line runners shared by the standalone ``scdfe_s`` programs.

The Web service and these command-line programs use the same protocol service
objects.  That keeps configuration loading, point-table selection, lifecycle
handling and shutdown behaviour identical between the browser and a terminal.
Each runner owns one RTU channel; it never starts the ``scdfe_c`` supervisor.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import signal
import sys
import threading
from typing import Any, Callable

from scdfe_c.web.service import DEFAULT_DB, SCHEMA_DIR, ScadaStore
from scdfe_s.web.protocol_service import (
    CanServerService,
    CanService,
    ModbusRtuService,
    ModbusRtuServerService,
    ModbusTcpService,
    ProtocolService,
    ProtocolServiceError,
)


def add_common_arguments(parser: argparse.ArgumentParser, *, modes: tuple[str, ...]) -> None:
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR, help="JSON 表定义目录")
    parser.add_argument("--rtu-id", type=int, required=True, help="要运行的 RTU 编号")
    parser.add_argument("--mode", choices=modes, default="master", help="主站或服务端模式")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="采集/快照周期（秒）")
    parser.add_argument("--max-cycles", type=int, default=None, help="测试用：运行指定周期后退出")
    simulation = parser.add_mutually_exclusive_group()
    simulation.add_argument("--simulation", dest="simulation", action="store_true",
                             help="服务端使用内存模拟（默认沿用配置）")
    simulation.add_argument("--real", dest="simulation", action="store_false",
                             help="服务端使用真实通信端口/串口")
    parser.set_defaults(simulation=None)


def add_tcp_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bind-ip", default=None, help="服务端监听地址")
    parser.add_argument("--listen-port", type=int, default=None, help="服务端监听端口")
    parser.add_argument("--remote-host", default=None, help="主站远端地址")
    parser.add_argument("--remote-port", type=int, default=None, help="主站远端端口")
    parser.add_argument("--unit-id", type=int, default=None, help="Modbus 从站地址")
    parser.add_argument("--connect-timeout", type=float, default=None)
    parser.add_argument("--response-timeout", type=float, default=None)
    parser.add_argument("--retry-count", type=int, default=None)
    keep_alive = parser.add_mutually_exclusive_group()
    keep_alive.add_argument("--keep-alive", dest="keep_alive", action="store_const", const=1)
    keep_alive.add_argument("--no-keep-alive", dest="keep_alive", action="store_const", const=0)
    parser.set_defaults(keep_alive=None)


def add_rtu_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--serial-port", default=None, help="串口名或 pyserial URL")
    parser.add_argument("--baudrate", type=int, default=None)
    parser.add_argument("--bytesize", type=int, default=None)
    parser.add_argument("--parity", choices=("N", "E", "O"), default=None)
    parser.add_argument("--stopbits", type=int, choices=(1, 2), default=None)
    parser.add_argument("--unit-id", type=int, default=None, help="Modbus 从站地址")
    parser.add_argument("--response-timeout", type=float, default=None)
    parser.add_argument("--retry-count", type=int, default=None)


def add_can_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--interface", default=None, help="python-can 接口，例如 virtual、socketcan")
    parser.add_argument("--channel", default=None, help="CAN 总线通道名")
    parser.add_argument("--bitrate", type=int, default=None)
    parser.add_argument("--data-bitrate", type=int, default=None)
    fd = parser.add_mutually_exclusive_group()
    fd.add_argument("--fd", dest="fd", action="store_const", const=1)
    fd.add_argument("--no-fd", dest="fd", action="store_const", const=0)
    own = parser.add_mutually_exclusive_group()
    own.add_argument("--receive-own-messages", dest="receive_own_messages", action="store_const", const=1)
    own.add_argument("--no-receive-own-messages", dest="receive_own_messages", action="store_const", const=0)
    extended = parser.add_mutually_exclusive_group()
    extended.add_argument("--extended", dest="is_extended_id", action="store_const", const=1)
    extended.add_argument("--standard", dest="is_extended_id", action="store_const", const=0)
    parser.add_argument("--receive-timeout", type=float, default=None)
    parser.add_argument("--retry-count", type=int, default=None)
    parser.set_defaults(fd=None, receive_own_messages=None, is_extended_id=None)


def _overrides(args: argparse.Namespace, names: tuple[str, ...]) -> dict[str, Any]:
    values: dict[str, Any] = {"rtu_id": args.rtu_id, "mode": args.mode}
    for name in names:
        value = getattr(args, name, None)
        if value is not None:
            values[name] = value
    if args.mode != "server" and args.simulation is not None:
        raise ProtocolServiceError("--simulation/--real 只适用于服务端模式")
    if args.simulation is not None:
        values["simulation"] = int(args.simulation)
    return values


def _install_stop_handlers(stop_event: threading.Event) -> dict[signal.Signals, Any]:
    previous: dict[signal.Signals, Any] = {}

    def request_stop(*_):
        stop_event.set()

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            previous[sig] = signal.signal(sig, request_stop)
    return previous


def _restore_stop_handlers(previous: dict[signal.Signals, Any]) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def run_service(
    service_factory: Callable[[ScadaStore], ProtocolService],
    args: argparse.Namespace,
    *,
    label: str,
    override_names: tuple[str, ...],
) -> int:
    if not math.isfinite(args.poll_interval) or args.poll_interval < 0:
        raise ProtocolServiceError("--poll-interval 必须是非负有限数")
    if args.max_cycles is not None and args.max_cycles <= 0:
        raise ProtocolServiceError("--max-cycles 必须为正整数")

    store = ScadaStore(args.db, args.schema_dir)
    service = service_factory(store)
    stop_event = threading.Event()
    previous = _install_stop_handlers(stop_event)
    try:
        values = _overrides(args, override_names)
        unsupported = set(values) - set(service.DEFAULT_CONFIG)
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ProtocolServiceError(f"当前 {args.mode} 模式不支持配置字段: {names}")
        service.configure(values)
        status = service.start()
        config = status.get("config", {})
        print(
            f"{label} 已启动: mode={config.get('mode')} RTU={args.rtu_id} "
            f"points={status.get('point_count', 0)}",
            flush=True,
        )
        cycle = 0
        while not stop_event.is_set():
            if args.max_cycles is not None and cycle >= args.max_cycles:
                break
            try:
                result = service.poll()
                print(f"{label} 周期 {cycle + 1}: {result.get('count', 0)} 个值", flush=True)
            except ProtocolServiceError as exc:
                # A channel error is recorded by the service.  Keep the runner
                # alive so a transient device outage does not kill the process.
                print(f"{label} 采集失败: {exc}", file=sys.stderr, flush=True)
            cycle += 1
            if stop_event.wait(args.poll_interval):
                break
        return 0
    except (ProtocolServiceError, OSError, ValueError) as exc:
        print(f"{label} 运行失败: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        service.close()
        _restore_stop_handlers(previous)


def tcp_service(store: ScadaStore) -> ProtocolService:
    return ModbusTcpService(store)


def tcp_server_service(store: ScadaStore) -> ProtocolService:
    from scdfe_s.web.protocol_service import ModbusTcpServerService

    return ModbusTcpServerService(store)


def rtu_service(store: ScadaStore) -> ProtocolService:
    return ModbusRtuService(store)


def rtu_server_service(store: ScadaStore) -> ProtocolService:
    return ModbusRtuServerService(store)


def can_service(store: ScadaStore) -> ProtocolService:
    # The CAN service has a separate server implementation because CAN does
    # not have a protocol-defined slave role.  Both modes still share the same
    # Web site and the same point/configuration model.
    return CanService(store)


def can_server_service(store: ScadaStore) -> ProtocolService:
    return CanServerService(store)


def choose_can_service(store: ScadaStore, mode: str) -> ProtocolService:
    return can_server_service(store) if mode == "server" else can_service(store)
