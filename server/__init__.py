"""SCADA 从站/服务端协议接口。"""

from .iec104_server import IEC104Command, IEC104Server, IEC104ServerError
from .modbus_server import (
    ModbusAsyncServer, ModbusRtuServer, ModbusRtuServerSimulator,
    ModbusTcpServer, ModbusTcpServerSimulator,
)
# The transmission adapter depends on pymodbus at import time.  Keep the rest of
# the server package (IEC-104, CAN, OPC, BACnet) importable on installations
# that intentionally omit Modbus extras; the adapter itself raises the normal
# dependency error when a Modbus channel is started.
try:
    from .modbus_trans_server import ModbusTcpTransServer
except ImportError:  # pragma: no cover - optional protocol dependency
    ModbusTcpTransServer = None
from .can_server import CanServer, CanServerError, CanServerSimulator
from .opc_server import OpcUaServer, OpcUaServerError, OpcUaServerSimulator
from .bacnet_server import BacnetServer, BacnetServerError, BacnetServerSimulator

__all__ = [
    "IEC104Command", "IEC104Server", "IEC104ServerError",
    "ModbusAsyncServer", "ModbusTcpServer", "ModbusRtuServer",
    "ModbusTcpServerSimulator", "ModbusRtuServerSimulator",
    "ModbusTcpTransServer",
    "CanServer", "CanServerError", "CanServerSimulator",
    "OpcUaServer", "OpcUaServerError", "OpcUaServerSimulator",
    "BacnetServer", "BacnetServerError", "BacnetServerSimulator",
]
