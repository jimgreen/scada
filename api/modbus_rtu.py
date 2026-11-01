"""Modbus RTU 主站；串口、CRC、收发和超时由 pymodbus/pyserial 处理。

共用 modbus_tcp.py 中的应用层点表转换（不依赖其 TCP 连接对象）。
COM3、9600、8E1 等参数显式配置；不枚举或自动占用硬件串口。
"""
import math

from .modbus_tcp import _ModbusOperations, _ModbusMasterOperations, ModbusError, field, integer

__all__ = ['ModbusRtuMaster', 'ModbusError']


class _ModbusRtuTransport(_ModbusOperations):
    """同步 RTU 主站，一条串口总线使用一个实例，通过 unit_id 访问各从站。"""

    def __init__(self, port, *, baudrate=9600, bytesize=8, parity='E', stopbits=1,
                 unit_id=1, timeout=3.0, rtu_id=None):
        from pymodbus.client import ModbusSerialClient
        from pymodbus import FramerType
        self._init_operations(unit_id, rtu_id)
        if not isinstance(port, str) or not port:
            raise ValueError('port 必须是串口名或 pyserial URL')
        integer(baudrate, 'baudrate', 1, 10000000)
        if bytesize != 8 or parity not in ('N', 'E', 'O') or stopbits not in (1, 2):
            raise ValueError('RTU 使用 8 数据位，校验 N/E/O，停止位 1/2')
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('timeout 必须是 > 0 的有限数')
        self.port = port
        self.native = ModbusSerialClient(port, framer=FramerType.RTU, baudrate=baudrate,
                                         bytesize=bytesize, parity=parity, stopbits=stopbits,
                                         timeout=timeout, retries=0)

    @property
    def connected(self):
        return self.native.connected

    def connect(self):
        with self._lock:
            if not self.native.connect():
                raise ModbusError(f'无法打开 RTU 串口 {self.port}')
        return self

    def close(self):
        with self._lock:
            self.native.close()

    def _invoke(self, name, address, argument, unit_id):
        from pymodbus.exceptions import ModbusException
        names = {'write_single_coil': 'write_coil', 'write_single_register': 'write_register',
                 'write_multiple_coils': 'write_coils', 'write_multiple_registers': 'write_registers'}
        with self._lock:
            if not self.connected:
                raise ModbusError('未连接，请先 connect() 或使用 with')
            try:
                method = getattr(self.native, names.get(name, name))
                if name.startswith('read_'):
                    result = method(address, count=argument, device_id=unit_id)
                else:
                    result = method(address, argument, device_id=unit_id)
            except (ModbusException, OSError) as exc:
                raise ModbusError(f'{name} 失败: {exc}') from exc
            if result is None or result.isError():
                raise ModbusError(f'{name} 失败: {result}',
                                  exception_code=getattr(result, 'exception_code', None))
            if name in ('read_coils', 'read_discrete_inputs'):
                return list(result.bits[:argument])
            if name.startswith('read_'):
                return list(result.registers)
            return True


class ModbusRtuMaster(_ModbusMasterOperations, _ModbusRtuTransport):
    """串口主站：共用 TCP 主站点表 API，传输完全由 pymodbus RTU 处理。

    configure/collect_once/poll_loop/send_yk_point/send_yt_point 与 TCP 主站相同。
    from_store 使用协议参数表中的 serial_port；串口参数通过参数表或关键字显式传入。
    一条物理串口总线应共用一个实例，可通过 unit_id/irtuaddr 访问多个从站。
    构造不打开串口；connect 或 with 打开，写命令不自动重发。
    """

    CHANNEL_TYPE = 'modbus-rtu'
    PROTOCOL_NAME = 'Modbus RTU'
    PARAMETER_TABLE = 'fes_channel_modbus_rtu_define'

    def __init__(self, port, *, baudrate=9600, bytesize=8, parity='E', stopbits=1,
                 unit_id=1, timeout=3.0, rtu_id=None, calls=None, points=None,
                 definitions=None):
        super().__init__(port, baudrate=baudrate, bytesize=bytesize, parity=parity,
                         stopbits=stopbits, unit_id=unit_id, timeout=timeout, rtu_id=rtu_id)
        self._init_master(calls, points, definitions)

    @classmethod
    def from_channel(cls, channel, **kwargs):
        """协议参数中的 serial_port 填 COM3 等串口名。"""
        kwargs.setdefault('rtu_id', field(channel, 'rtu_id'))
        kwargs.setdefault('unit_id', field(channel, 'irtuaddr', 1))
        return cls(field(channel, 'serial_port'), **kwargs)
