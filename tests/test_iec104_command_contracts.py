"""Command semantics against independently encoded IEC104 TCP peers."""
from concurrent.futures import ThreadPoolExecutor
import socket
import struct
import unittest

import c104
from client.iec104 import IEC104Master, IEC104Error
from server.iec104_server import IEC104Server
from tools.verify_iec104_wire_contracts import RawPeer


CASES = [('yk',45,b'\x01'),('yk',46,b'\x02'),('yt',48,struct.pack('<hB',8192,0)),
         ('yt',49,struct.pack('<hB',123,0)),('yt',50,struct.pack('<fB',12.5,0))]
CASES += [(kind,typ+13,payload) for kind,typ,payload in list(CASES)]
STAMP=struct.pack('<HBBBBB',12345,34,12,14,11,26)


class CommandWireTests(unittest.TestCase):
    def fixture(self, *, timeout=1000, reject=False):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        events=[]
        server=IEC104Server(ip='127.0.0.1',port=port,tick_rate_ms=50,select_timeout_ms=timeout)
        self.addCleanup(server.stop)
        def receive(event): events.append(event); return not reject
        for no,(kind,typ,_) in enumerate(CASES,1):
            server.add_point(kind,no,data_type=typ,command_mode='SELECT_AND_EXECUTE',command_handler=receive)
        server.start()
        return server,self.peer(port),events

    def peer(self,port):
        sock=socket.create_connection(('127.0.0.1',port),timeout=3); self.addCleanup(sock.close)
        peer=RawPeer(sock); peer.u(7); peer.until(lambda b:b[2]==0x0b)
        return peer

    def command(self,peer,no,case,cot,*,select=False,changed=False,oa=7):
        kind,typ,raw=case; payload=bytearray(raw)
        if changed: payload[0]^=1
        payload[-1]|=128 if select else 0
        if typ>=58:payload.extend(STAMP)
        ioa=(0x6001 if kind=='yk' else 0x6201)+no-1
        mark=len(peer.frames)
        request=bytes((typ,1,cot,oa,1,0))+ioa.to_bytes(3,'little')+payload
        peer.asdu(request)
        answer=peer.until(lambda b:len(b)>15 and not b[2]&1 and b[6]==typ)
        peer.drain(.07)
        frames=[bytes.fromhex(r['hex']) for r in peer.frames[mark:] if r['direction']=='RX']
        return request,answer,[b for b in frames if len(b)>15 and not b[2]&1 and b[6]==typ]

    def test_pending_cancel_confirms_9_and_never_executes(self):
        _,peer,events=self.fixture()
        for no,case in enumerate(CASES,1):
            with self.subTest(type=case[1]):
                _,answer,_=self.command(peer,no,case,6,select=True)
                self.assertEqual(answer[8]&127,7)
                before=len(events)
                _,answer,frames=self.command(peer,no,case,8)
                self.assertEqual(answer[8]&127,9)
                self.assertFalse(any(b[8]&63==10 for b in frames))
                self.assertEqual(len(events),before+1)
                self.assertEqual(events[-1].cause,c104.Cot.DEACTIVATION)
                _,answer,_=self.command(peer,no,case,6)
                self.assertEqual(answer[8]&127,7|64)
                self.assertEqual(len(events),before+1)
                _,answer,_=self.command(peer,no,case,8)
                self.assertEqual(answer[8]&127,9|64)

    def test_execute_matches_selection_and_termination_echoes_execution(self):
        _,peer,events=self.fixture()
        for no,case in enumerate(CASES,1):
            with self.subTest(type=case[1]):
                self.command(peer,no,case,6,select=True)
                before=len(events)
                _,answer,_=self.command(peer,no,case,6,changed=True)
                self.assertEqual(answer[8]&127,7|64)
                self.assertEqual(len(events),before)
                request,answer,frames=self.command(peer,no,case,6)
                self.assertEqual(answer[8]&127,7)
                terms=[b for b in frames if b[8]&63==10]
                self.assertEqual(len(terms),1)
                expected=bytearray(request);expected[2]=10
                self.assertEqual(terms[0][6:],expected)
                self.assertEqual(len(events),before+1)

    def test_other_connection_and_originator_cannot_use_selection(self):
        server,owner,events=self.fixture(); other=self.peer(server.port)
        for no,case in enumerate(CASES,1):
            with self.subTest(type=case[1]):
                self.command(owner,no,case,6,select=True)
                before=len(events)
                _,answer,_=self.command(other,no,case,8)
                self.assertEqual(answer[8]&127,9|64)
                _,answer,_=self.command(owner,no,case,8,oa=8)
                self.assertEqual(answer[8]&127,9|64)
                self.assertEqual(len(events),before)
                _,answer,_=self.command(owner,no,case,6)
                self.assertEqual(answer[8]&127,7)

    def test_expired_selection_does_not_emit_execution_termination(self):
        _,peer,events=self.fixture(timeout=100)
        self.command(peer,1,CASES[0],6,select=True)
        expired=peer.drain(.25)
        self.assertFalse(any(len(b)>8 and b[8]&63==10 for b in expired))
        _,answer,_=self.command(peer,1,CASES[0],6)
        self.assertEqual(answer[8]&127,7|64)
        self.assertEqual(events,[])

    def test_failed_execution_has_no_success_termination(self):
        _,peer,events=self.fixture(reject=True)
        self.command(peer,1,CASES[0],6,select=True)
        _,answer,frames=self.command(peer,1,CASES[0],6)
        self.assertEqual(answer[8]&127,7|64)
        self.assertFalse(any(b[8]&63==10 for b in frames))
        self.assertEqual(len(events),1)

    def test_reselection_replaces_the_authorized_value(self):
        _,peer,events=self.fixture()
        for no in (1,5):
            case=CASES[no-1]
            with self.subTest(type=case[1]):
                self.command(peer,no,case,6,select=True)
                self.command(peer,no,case,6,select=True,changed=True)
                before=len(events)
                _,answer,_=self.command(peer,no,case,6)
                self.assertEqual(answer[8]&127,7|64)
                self.assertEqual(len(events),before)
                _,answer,frames=self.command(peer,no,case,6,changed=True)
                self.assertEqual(answer[8]&127,7)
                self.assertEqual(len(events),before+1)
                self.assertEqual(sum(b[8]&63==10 for b in frames),1)

    def test_rejected_cancel_keeps_the_pending_selection(self):
        server,peer,events=self.fixture()
        def reject_cancel(event):
            events.append(event)
            return event.cause != c104.Cot.DEACTIVATION
        server.set_command_handler('yk',1,reject_cancel)
        self.command(peer,1,CASES[0],6,select=True)
        _,answer,frames=self.command(peer,1,CASES[0],8)
        self.assertEqual(answer[8]&127,9|64)
        self.assertFalse(any(b[8]&63==10 for b in frames))
        _,answer,frames=self.command(peer,1,CASES[0],6)
        self.assertEqual(answer[8]&127,7)
        self.assertEqual(sum(b[8]&63==10 for b in frames),1)
        self.assertEqual([event.cause for event in events],
                         [c104.Cot.DEACTIVATION,c104.Cot.ACTIVATION])

    def test_client_cancel_sends_once_and_requires_correct_confirmation(self):
        for kind,typ,value in [('yk',45,True),('yt',50,12.5)]:
            for mode in ('DIRECT','SELECT_AND_EXECUTE'):
                for cot,success in ((9,True),(7,False),(9|64,False)):
                    with self.subTest(kind=kind,mode=mode,cot=cot),socket.socket() as listener:
                        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(3)
                        client=IEC104Master('127.0.0.1',listener.getsockname()[1],init='NONE',command_timeout_ms=600)
                        self.addCleanup(client.close)
                        client.configure([dict(kind=kind,pnt_no=1,type_id=typ,command_mode=mode)])
                        with ThreadPoolExecutor(1) as pool:
                            future=pool.submit(client.connect,3); sock,_=listener.accept()
                            with sock:
                                peer=RawPeer(sock);peer.until(lambda b:b[2]==7);peer.u(0x0b);future.result(3)
                                future=pool.submit(getattr(client,f'cancel_{kind}_point'),1,value)
                                request=peer.until(lambda b:len(b)>15 and not b[2]&1)
                                self.assertEqual(request[8]&63,8)
                                self.assertFalse(request[-1]&128)
                                answer=bytearray(request[6:]);answer[2]=cot;peer.asdu(answer)
                                if success: future.result(2)
                                else:
                                    with self.assertRaises(IEC104Error):future.result(2)
                                self.assertFalse(any(not b[2]&1 for b in peer.drain(.05)))
                        client.close()


if __name__=='__main__':unittest.main()
