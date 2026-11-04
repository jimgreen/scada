"""Modbus TCP/RTU server transports.

This module owns the client-side slave/server implementation of Modbus.  The synchronous master/client
adapters remain in :mod:`client.modbus_tcp` and :mod:`client.modbus_rtu`.
"""
from __future__ import annotations

import asyncio
import threading

from .point_store import PointStore


def build_context(unit_id=1, size=1000):
    """Build a deterministic four-bank context for standalone test servers."""
    from pymodbus.datastore import (
        ModbusDeviceContext,
        ModbusServerContext,
        ModbusSequentialDataBlock,
    )
    context = ModbusDeviceContext(
        co=ModbusSequentialDataBlock(1, [False] * size),
        di=ModbusSequentialDataBlock(1, [False] * size),
        hr=ModbusSequentialDataBlock(1, [0] * size),
        ir=ModbusSequentialDataBlock(1, [0] * size),
    )
    return ModbusServerContext({int(unit_id): context}, single=False)


class ModbusAsyncServer:
    """Run a pymodbus 3.x server on a dedicated event-loop thread."""

    def __init__(self, context, *, tcp, log, host=None, port=None, serial=None, **serial_options):
        self.context, self.tcp, self.log = context, tcp, log
        self.host, self.port, self.serial, self.serial_options = host, port, serial, serial_options
        self.thread = None
        self.loop = None
        self.server = None
        self.ready = threading.Event()
        self.error = None
        self.connections = 0

    @property
    def connected(self):
        return self.server is not None and self.error is None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="modbus-server", daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise TimeoutError("Modbus 服务启动超时")
        if self.error:
            raise self.error

    def _run(self):
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            self.error = exc
            self.ready.set()

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        from pymodbus import FramerType
        from pymodbus.server import ModbusSerialServer, ModbusTcpServer

        kwargs = {
            "context": self.context,
            "framer": FramerType.SOCKET if self.tcp else FramerType.RTU,
            "trace_connect": self._trace_connect,
        }
        if self.tcp:
            kwargs["address"] = (self.host, self.port)
            self.server = ModbusTcpServer(**kwargs)
        else:
            kwargs.update({"port": self.serial, **self.serial_options})
            self.server = ModbusSerialServer(**kwargs)
        await self.server.serve_forever(background=True)
        self.ready.set()
        self.log("INFO", "Modbus 从站已监听")
        await self.server.serving

    def _trace_connect(self, connected):
        self.connections = max(0, self.connections + (1 if connected else -1))

    def close(self):
        if self.server is not None and self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.server.shutdown(), self.loop).result(5)
        elif self.server is not None:
            self.server.close()
        if self.thread is not None:
            self.thread.join(5)


class ModbusTcpServer(ModbusAsyncServer):
    """Standalone Modbus-TCP server with a ready-to-use register model."""

    def __init__(self, *, host="127.0.0.1", port=1502, unit_id=1, context=None,
                 log=lambda level, message: None, **kwargs):
        super().__init__(context or build_context(unit_id), tcp=True, host=host,
                         port=int(port), log=log, **kwargs)
        self.unit_id = int(unit_id)

    def update_point(self, kind, point_no, value):
        address = max(0, int(point_no) - 1)
        _set_context_value(self.server.context if self.server is not None else self.context, self.unit_id, kind, address, value)
        return value


class ModbusRtuServer(ModbusAsyncServer):
    """Standalone Modbus-RTU server over a serial or pyserial URL."""

    def __init__(self, *, serial="COM1", unit_id=1, context=None,
                 log=lambda level, message: None, **serial_options):
        super().__init__(context or build_context(unit_id), tcp=False, serial=serial,
                         log=log, **serial_options)
        self.unit_id = int(unit_id)

    def update_point(self, kind, point_no, value):
        address = max(0, int(point_no) - 1)
        _set_context_value(self.server.context if self.server is not None else self.context, self.unit_id, kind, address, value)
        return value


def _set_context_value(context, unit_id, kind, address, value):
    """Update the pymodbus 3.16 simulator data without relying on removed APIs."""
    if hasattr(context, "devices"):
        runtime = context.devices[int(unit_id)]
        bank = "c" if kind == "yk" else "d" if kind == "yx" else "h"
        start, _count, registers, _flags = runtime.block[bank]
        registers[int(address) + 1 - start] = bool(value) if bank in {"c", "d"} else int(float(value))
        return
    device = context._devices[int(unit_id)]  # pymodbus keeps this as the live datastore
    bank = 0 if kind == "yk" else 1 if kind == "yx" else 2 if kind in {"yc", "yt"} else None
    if bank is None:
        raise ValueError("kind 必须是 yc、yx、yk 或 yt")
    values = device.simdevice.simdata[bank][0].values
    values[int(address)] = bool(value) if bank < 2 else int(float(value))


class ModbusTcpServerSimulator(ModbusTcpServer):
    """Named simulator entry point for local Modbus-TCP loopback tests."""


class ModbusRtuServerSimulator(ModbusRtuServer):
    """In-memory Modbus-RTU simulator for HMI and protocol tests."""

    def __init__(self, *, unit_id=1, points=None, log=lambda level, message: None, **kwargs):
        self.unit_id = int(unit_id)
        self.point_store = PointStore(points)
        self._log = log
        self._running = False
        self.connections = 0

    @property
    def connected(self):
        return self._running

    def start(self):
        self._running = True
        self._log("INFO", "Modbus-RTU 模拟服务端已启动（内存模式）")
        return self

    def close(self):
        self._running = False
        self._log("INFO", "Modbus-RTU 模拟服务端已停止")

    def update_point(self, kind, point_no, value):
        return self.point_store.update(kind, point_no, value).public()

    def define_point(self, point):
        return self.point_store.define(point)

    def snapshot(self):
        return self.point_store.snapshot()
