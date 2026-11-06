"""Modbus TCP/RTU server transports.

This module owns the client-side slave/server implementation of Modbus.  The synchronous master/client
adapters remain in :mod:`client.modbus_tcp` and :mod:`client.modbus_rtu`.
"""
from __future__ import annotations

import asyncio
import threading

from .point_store import PointStore
from core.packet_trace import PacketTrace


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


class ModbusAsyncServer(PacketTrace):
    """Run a pymodbus 3.x server on a dedicated event-loop thread."""

    def __init__(self, context, *, tcp, log, host=None, port=None, serial=None, points=None, **serial_options):
        self.context, self.tcp, self.log = context, tcp, log
        self.PACKET_PROTOCOL = "Modbus-TCP" if tcp else "Modbus-RTU"
        self.host, self.port, self.serial, self.serial_options = host, port, serial, serial_options
        self._packet_endpoint = f"listen={host}:{port}" if tcp else f"serial={serial}"
        self.thread = None
        self.loop = None
        self.server = None
        self.ready = threading.Event()
        self.error = None
        self.connections = 0
        self.point_store = PointStore(points)

    def _on_datastore(self, operation):
        """Serialize Web-side mutations with the live protocol event loop."""
        if self.loop is not None and self.loop.is_running() and threading.current_thread() is not self.thread:
            async def execute():
                return operation()
            return asyncio.run_coroutine_threadsafe(execute(), self.loop).result(5)
        return operation()

    def update_point(self, kind, point_no, value):
        point_no = int(point_no)
        if point_no < 1:
            raise ValueError("点号必须是正整数")
        context = self.server.context if self.server is not None else self.context
        self._on_datastore(lambda: _set_context_value(context, self.unit_id, kind, point_no - 1, value))
        self.point_store.update(kind, point_no, value)
        return value

    def snapshot(self):
        context = self.server.context if self.server is not None else self.context
        def read():
            rows = self.point_store.snapshot()
            for row in rows:
                row["value"] = _get_context_value(context, self.unit_id, row["kind"], row["pnt_no"] - 1)
            return rows
        return self._on_datastore(read)

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
            "trace_packet": self._trace_packet,
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

class ModbusRtuServer(ModbusAsyncServer):
    """Standalone Modbus-RTU server over a serial or pyserial URL."""

    def __init__(self, *, serial="COM1", unit_id=1, context=None,
                 log=lambda level, message: None, **serial_options):
        super().__init__(context or build_context(unit_id), tcp=False, serial=serial,
                         log=log, **serial_options)
        self.unit_id = int(unit_id)

def _context_slot(context, unit_id, kind, address):
    banks = {"yk": ("c", 0), "yx": ("d", 1), "yc": ("h", 2), "yt": ("h", 2)}
    if kind not in banks:
        raise ValueError("kind 必须是 yc、yx、yk 或 yt")
    bank, bank_no = banks[kind]
    if hasattr(context, "devices"):
        runtime = context.devices[int(unit_id)]
        start, _count, values, _flags = runtime.block[bank]
        bits = bank in {"c", "d"}
        # SimCore stores coils/discretes as packed 16-bit words. Register
        # addresses are already zero-based; the legacy +1 adjustment is wrong.
        index = (address // 16 if bits and runtime.use_bit_addressing else address) - start
        mask = 1 << (address % 16) if bits else None
    else:
        device = context._devices[int(unit_id)]
        block = device.simdevice.simdata[bank_no][0]
        values = block.values
        index, mask = address - block.address, None
    if not 0 <= index < len(values):
        raise ValueError("测点地址超出模拟数据区")
    return values, index, mask


def _set_context_value(context, unit_id, kind, address, value):
    """Update raw words without shifting register addresses or adjacent bits."""
    values, index, mask = _context_slot(context, unit_id, kind, int(address))
    if mask is not None:
        values[index] = (int(values[index]) | mask) if value else (int(values[index]) & ~mask)
    else:
        values[index] = bool(value) if kind in {"yx", "yk"} else int(float(value))


def _get_context_value(context, unit_id, kind, address):
    values, index, mask = _context_slot(context, unit_id, kind, int(address))
    return bool(int(values[index]) & mask) if mask is not None else values[index]


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
