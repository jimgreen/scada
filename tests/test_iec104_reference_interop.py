"""Optional independent peer; c104 is a test oracle, never a runtime dependency."""
import socket
import time

import pytest
from core import iec104_python as python104

reference = pytest.importorskip('c104', reason='optional legacy IEC104 interoperability oracle')


def wait(predicate):
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(.01)
    raise AssertionError('interoperability peer did not complete operation')


@pytest.mark.parametrize('python_server',[True,False])
def test_general_interrogation_spontaneous_and_selected_command(python_server):
    serving,collecting=(python104,reference) if python_server else (reference,python104)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    events=[]
    server=serving.Server(ip='127.0.0.1',port=port)
    station=server.add_station(common_address=1)
    analog=station.add_point(io_address=10,type=serving.Type.M_ME_NC_1)
    digital=station.add_point(io_address=11,type=serving.Type.M_DP_NA_1)
    command=station.add_point(io_address=20,type=serving.Type.C_SE_NC_1,
                              command_mode=serving.CommandMode.SELECT_AND_EXECUTE)
    def receive(point: reference.Point, previous_info: reference.Information,
                message: reference.IncomingMessage) -> reference.ResponseState:
        events.append(float(point.value))
        return serving.ResponseState.SUCCESS
    command.on_receive(receive)
    analog.value=12.5;digital.value=serving.Double.ON
    client=collecting.Client(command_timeout_ms=2000)
    connection=client.add_connection(ip='127.0.0.1',port=port,init=collecting.Init.NONE)
    remote=connection.add_station(common_address=1)
    a=remote.add_point(io_address=10,type=collecting.Type.M_ME_NC_1)
    d=remote.add_point(io_address=11,type=collecting.Type.M_DP_NA_1)
    c=remote.add_point(io_address=20,type=collecting.Type.C_SE_NC_1,
                       command_mode=collecting.CommandMode.SELECT_AND_EXECUTE)
    try:
        server.start();client.start()
        wait(lambda:connection.state==collecting.ConnectionState.OPEN)
        assert connection.interrogation(common_address=1,cause=collecting.Cot.ACTIVATION,qualifier=collecting.Qoi.STATION)
        wait(lambda:float(a.value)==12.5 and int(d.value)==2)
        analog.value=27.25;assert analog.transmit(serving.Cot.SPONTANEOUS)
        wait(lambda:float(a.value)==27.25)
        c.info=collecting.ShortCmd(target=45.5)
        assert c.transmit(collecting.Cot.ACTIVATION)
        wait(lambda:events==[45.5])
        assert connection.clock_sync(common_address=1)
    finally:
        client.stop();server.stop()
