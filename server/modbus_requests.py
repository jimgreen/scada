"""Bounded request ownership for the pymodbus transport adapter.

Keep each decoded PDU through awaits; pymodbus's shared last_pdu is only a
decoder scratch slot. Writes are ordered per service, reads per connection.
"""
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import perf_counter

from pymodbus.constants import ExcCodes
from pymodbus.exceptions import ModbusIOException, NoSuchIdException
from pymodbus.pdu import ExceptionResponse
from pymodbus.server.requesthandler import ServerRequestHandler
from pymodbus.transaction import TransactionManager


class RequestDispatch:
    def __init__(self, owner):
        self.owner = owner
        self.accepting = True
        self.limit, self.peer_limit = 256, 32
        self.pending = self.peak = self.busy = self.abandoned = self.completed = 0
        self.tasks = set()
        self.write_lock = asyncio.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='modbus-command')
        self.recent = deque(maxlen=16)
        self.metrics_lock = Lock()

    async def commit(self, operation):
        # Only the synchronous application callback leaves the protocol loop.
        # The datastore and acknowledgement remain on that loop after commit.
        return await asyncio.get_running_loop().run_in_executor(self.executor, operation)

    def snapshot(self):
        with self.metrics_lock:
            return dict(pending=self.pending, peak=self.peak, limit=self.limit,
                        peer_limit=self.peer_limit, busy=self.busy,
                        abandoned=self.abandoned, completed=self.completed,
                        recent=list(self.recent))

    async def drain(self):
        self.accepting = False
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.executor.shutdown(wait=True)


class OwnedRequestHandler(ServerRequestHandler):
    def __init__(self, server, dispatch):
        super().__init__(server, server.trace_packet, server.trace_pdu, server.trace_connect)
        self.dispatch = dispatch
        self.queue = deque()
        self.worker = None
        self.disconnected = False
        self.pending = 0

    def callback_disconnected(self, exc):
        self.disconnected = True
        super().callback_disconnected(exc)

    def callback_connected(self):
        self.disconnected = False
        super().callback_connected()

    def callback_data(self, data, addr=None):
        consumed = 0
        # Consume every complete coalesced ADU now. Leave a partial tail to the
        # transport; it may not call callback_data again until more bytes arrive.
        while consumed < len(data):
            packet = data[consumed:]
            if self.dispatch.owner.tcp:
                if len(packet) < 7:
                    break
                length = int.from_bytes(packet[4:6], 'big')
                if packet[2:4] != b'\x00\x00' or not 2 <= length <= 254:
                    self.dispatch.owner._business('Modbus无效MBAP，关闭连接',
                        事务标识=int.from_bytes(packet[:2], 'big'), 长度=length)
                    self.disconnected = True
                    self.close()
                    return len(data)
                if len(packet) < length + 6:
                    break
                # A malformed PDU must not consume the next coalesced ADU.
                packet = packet[:length + 6]
            try:
                used = TransactionManager.callback_data(self, packet, addr)
            except ModbusIOException as exc:
                if self.dispatch.owner.tcp:
                    function = packet[7] if len(packet) > 7 else (exc.fcode or 0)
                    code = (ExcCodes.ILLEGAL_VALUE if function in self.server.decoder.pdu_table
                            else ExcCodes.ILLEGAL_FUNCTION)
                    response = ExceptionResponse(function, code,
                                                 device_id=exc.dev_id, transaction=exc.transaction_id)
                    self.server_send(response, addr)
                    consumed += len(packet)
                else:
                    # Modbus RTU 规范禁止对 CRC/帧格式错误的报文回复异常，逐字节滑窗跳过噪声防吞后续合法帧。
                    self.last_pdu = None
                    consumed += 1
                continue
            if self.last_pdu is not None:
                pdu, pdu_addr = self.last_pdu, self.last_addr
                self.last_pdu = None
                self._enqueue(pdu, pdu_addr)
            if not used:
                break
            consumed += used
        return consumed

    def _enqueue(self, request, addr):
        state = self.dispatch
        with state.metrics_lock:
            rejected = (not state.accepting or self.pending >= state.peer_limit
                        or state.pending >= state.limit)
            if rejected:
                state.busy += 1
            else:
                self.pending += 1
                state.pending += 1
                state.peak = max(state.peak, state.pending)
        if rejected:
            self._reply(request, addr, ExceptionResponse(request.function_code, ExcCodes.DEVICE_BUSY))
            return
        self.queue.append((request, addr, perf_counter()))
        if self.worker is None:
            self.worker = asyncio.create_task(self._drain())
            state.tasks.add(self.worker)
            self.worker.add_done_callback(state.tasks.discard)

    def _reply(self, request, addr, response):
        if not self.disconnected and self.transport is not None:
            response.transaction_id, response.dev_id = request.transaction_id, request.dev_id
            try:
                self.server_send(response, addr)
                return True
            except (OSError, ConnectionError):
                self.disconnected = True
        return False

    async def _drain(self):
        state = self.dispatch
        try:
            while self.queue:
                request, addr, received = self.queue.popleft()
                try:
                    if request.function_code in {5, 6, 15, 16, 22, 23}:
                        async with state.write_lock:
                            await self._execute(request, addr, received)
                    else:
                        await self._execute(request, addr, received)
                finally:
                    with state.metrics_lock:
                        state.pending -= 1
                        self.pending -= 1
        finally:
            self.worker = None

    async def _execute(self, request, addr, received):
        state = self.dispatch
        if self.disconnected or not state.accepting:
            with state.metrics_lock:state.abandoned += 1
            state.owner._business('Modbus未执行排队请求已放弃', 事务标识=request.transaction_id,
                                  站号=request.dev_id, 功能码=request.function_code,
                                  原因='客户端断开' if self.disconnected else '服务停止')
            return
        started = perf_counter()
        response = None
        try:
            if self.server.broadcast_enable and not request.dev_id:
                for device in self.server.context.device_ids():
                    await request.datastore_update(self.server.context, device)
            else:
                devices = getattr(self.server.context, 'devices', None)
                if devices is not None and request.dev_id not in devices and 0 not in devices:
                    raise NoSuchIdException(request.dev_id)
                response = await request.datastore_update(self.server.context, request.dev_id)
        except NoSuchIdException:
            if not self.server.ignore_missing_devices:
                response = ExceptionResponse(request.function_code, ExcCodes.GATEWAY_NO_RESPONSE)
        except Exception as exc:
            state.owner._business('Modbus请求执行失败', 事务标识=request.transaction_id,
                                  站号=request.dev_id, 功能码=request.function_code, 错误=str(exc))
            response = ExceptionResponse(request.function_code, ExcCodes.DEVICE_FAILURE)
        executed = perf_counter()
        error = getattr(response, 'exception_code', 0)
        submitted = self._reply(request, addr, response) if response is not None else False
        finished = perf_counter()
        with state.metrics_lock:
            state.completed += 1
            if request.function_code not in {1, 2, 3, 4} or error:
                state.recent.append(dict(transaction_id=request.transaction_id, unit_id=request.dev_id,
                    function_code=request.function_code, exception_code=error,
                    queue_ms=round((started-received)*1000, 3),
                    execution_ms=round((executed-started)*1000, 3),
                    response_submit_ms=round((finished-executed)*1000, 3),
                    total_ms=round((finished-received)*1000, 3),
                    response_submitted=submitted,
                    peer_disconnected=self.disconnected))
