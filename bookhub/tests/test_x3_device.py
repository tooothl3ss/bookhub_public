"""The X3 device module: cache directory hash, progress.bin, deletion, upload over the WebSocket with
the HTTP fallback. No device and no network: a fake WebSocket server on loopback, a fake request."""
import base64
import hashlib
import itertools
import json
import operator
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

import requests

from bookhub import x3

GUID='258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
BOOK='/piter_uotts-rifteri-1490777777.epub'
PROGRESS=bytes.fromhex('0a0036004b0078490000')  # read from the real device: spine 10, page 54 of 75, offset 18808

def reply(code=200,body=b''):
    r=requests.Response();r.status_code=code;r.encoding='utf-8'
    r._content=body.encode() if isinstance(body,str) else body
    return r

class Offline(Exception):
    """What the caller's request raises on a network error (shelf.X3Error)."""


class Http:
    """The caller's request callable: answers or raises, and keeps every call the way requests would send it."""
    def __init__(self,answer):
        self.answer=answer;self.calls=[]

    def __call__(self,method,path,timeout=None,**kwargs):
        sent=requests.Request(method,'http://x3'+path,**kwargs).prepare()
        self.calls.append(SimpleNamespace(method=method,path=path,timeout=timeout,url=sent.url,headers=sent.headers,body=sent.body))
        if isinstance(self.answer,Exception):raise self.answer
        return self.answer


def accept(key):
    return base64.b64encode(hashlib.sha1((key+GUID).encode()).digest()).decode()

def unmask(payload,key):
    return bytes(map(operator.xor,payload,itertools.cycle(key)))

def frame(opcode,payload=b''):
    """A server frame: unmasked."""
    n=len(payload)
    return bytes([0x80|opcode])+(bytes([n]) if n<126 else struct.pack('!BH',126,n))+payload

def closed_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]


class Device:
    """The firmware's WebSocket upload (CrossPointWebServer::onWebSocketEvent) on a loopback port, one connection.

    mode: ok; exists - the file is already on the card; full - the card fills up after 128 KiB;
    cancelled - READY and at once an ERROR; silent - START is never answered; rejected - no upgrade;
    impostor - a wrong Sec-WebSocket-Accept.
    """
    def __init__(self,mode='ok'):
        self.mode=mode;self.request=self.start=self.directory=self.closed=self.failure=None
        self.headers={};self.received=bytearray();self.sizes=[];self.pongs=[];self.unmasked=0;self.buffer=b''
        self.listener=socket.create_server(('127.0.0.1',0))
        self.listener.settimeout(10)
        self.port=self.listener.getsockname()[1]
        self.url=f'http://127.0.0.1:{self.port-1}'
        self.thread=threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def finish(self):
        self.thread.join(10)
        if self.thread.is_alive():raise AssertionError('the fake device is still serving')
        if self.failure:raise self.failure

    def run(self):
        try:
            with self.listener:
                conn,_=self.listener.accept()
            with conn:
                conn.settimeout(10)
                self.serve(conn)
        except (EOFError,ConnectionError):
            pass  # the client hung up
        except Exception as exc:
            self.failure=exc

    def take(self,conn,n):
        while len(self.buffer)<n:
            data=conn.recv(65536)
            if not data:raise EOFError
            self.buffer+=data
        data,self.buffer=self.buffer[:n],self.buffer[n:]
        return data

    def serve(self,conn):
        while b'\r\n\r\n' not in self.buffer:
            data=conn.recv(4096)
            if not data:raise EOFError
            self.buffer+=data
        head,self.buffer=self.buffer.split(b'\r\n\r\n',1)
        self.request,*lines=head.decode().split('\r\n')
        self.headers={k.lower():v.strip() for k,v in (line.split(':',1) for line in lines)}
        if self.mode=='rejected':
            conn.sendall(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n');return
        key='impostor' if self.mode=='impostor' else self.headers['sec-websocket-key']
        # The device's library pings right behind its answer; the payload is here to see it echoed.
        conn.sendall(('HTTP/1.1 101 Switching Protocols\r\nServer: arduino-WebSocketsServer\r\nUpgrade: websocket\r\n'
                      'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Accept: '+accept(key)+'\r\n\r\n').encode()+frame(0x9,b'x3'))
        say=lambda text:conn.sendall(frame(0x1,text.encode()))
        size=marked=0;active=False
        while True:
            first,second=self.take(conn,2)
            n=second&0x7f
            if n==126:n,=struct.unpack('!H',self.take(conn,2))
            elif n==127:n,=struct.unpack('!Q',self.take(conn,8))
            if not second&0x80:self.unmasked+=1
            key=self.take(conn,4) if second&0x80 else bytes(4)
            payload=self.take(conn,n);opcode=first&0x0f
            if opcode==0x2 and not active:
                self.sizes.append(n);say('ERROR:No upload in progress');continue
            payload=unmask(payload,key)
            if opcode==0x8:
                self.closed=payload
                conn.sendall(frame(0x8,payload));return
            if opcode==0xA:self.pongs.append(payload)
            elif opcode==0x1:
                self.start=payload.decode()
                name,size,self.directory=self.start[6:].split(':',2)
                size=int(size)
                if self.mode=='silent':continue
                if self.mode=='exists':say('ERROR:File already exists: '+name)
                elif self.mode=='cancelled':conn.sendall(frame(0x1,b'READY')+frame(0x1,b'ERROR:Upload aborted'))
                elif size==0:say('DONE')
                else:active=True;say('READY')
            elif opcode==0x2:
                self.sizes.append(n);self.received+=payload
                if self.mode=='full' and len(self.received)>=131072:
                    active=False;say('ERROR:Write failed - disk full?');continue
                if len(self.received)-marked>=65536 or len(self.received)>=size:
                    marked=len(self.received);say(f'PROGRESS:{marked}:{size}')
                if len(self.received)>=size:
                    active=False;say('DONE')


class CacheDirTests(unittest.TestCase):
    def test_cache_directory_is_the_firmware_hash_of_the_path(self):
        for path,number in ((BOOK,1798888161),('/mik_gerron-hromie_koni-65dc47e1e2c8d.epub',2500023608),
                            ('/endi_uir-proekt_ave_mariya-66216d3327f0a.epub',2822384903)):
            self.assertEqual(f'/.crosspoint/epub_{number}',x3.cache_dir(path))

    def test_path_is_hashed_the_way_the_firmware_spells_it(self):
        # One leading slash, single separators: a books directory of "/" joined with a name must not change the hash.
        self.assertEqual(x3.cache_dir(BOOK),x3.cache_dir('/'+BOOK))
        self.assertEqual(x3.cache_dir(BOOK),x3.cache_dir(BOOK[1:]))
        self.assertEqual(x3.cache_dir('/Books/Книга.epub'),x3.cache_dir('/Books//Книга.epub'))
        self.assertNotEqual(x3.cache_dir('/Books/Книга.epub'),x3.cache_dir('/Книга.epub'))


class ProgressTests(unittest.TestCase):
    def read(self,body,code=200):
        http=Http(reply(code,body))
        return x3.read_progress(http,BOOK),http

    def test_ten_bytes_carry_the_visible_offset(self):
        progress,http=self.read(PROGRESS)
        self.assertEqual(dict(spine=10,page=54,page_count=75,visible_offset=18808),progress)
        call,=http.calls
        self.assertEqual(('GET','/download',3),(call.method,call.path,call.timeout))
        self.assertEqual({'path':['/.crosspoint/epub_1798888161/progress.bin']},parse_qs(urlsplit(call.url).query))

    def test_older_layouts_have_less(self):
        self.assertEqual(dict(spine=10,page=54,page_count=75,visible_offset=None),self.read(PROGRESS[:6])[0])
        self.assertEqual(dict(spine=10,page=54,page_count=None,visible_offset=None),self.read(PROGRESS[:4])[0])

    def test_stale_page_sentinel_is_the_first_page(self):
        self.assertEqual(dict(spine=3,page=0,page_count=75,visible_offset=70000),
                         self.read(struct.pack('<HHHI',3,0xFFFF,75,70000))[0])

    def test_unknown_length_and_missing_file_are_no_progress(self):
        for body in (b'',PROGRESS[:5],PROGRESS[:8],PROGRESS+b'\0\0'):
            self.assertIsNone(self.read(body)[0],body)
        self.assertIsNone(self.read('Item not found',404)[0])
        self.assertIsNone(self.read(PROGRESS,404)[0])

    def test_refusal_is_an_error_not_a_book_never_opened(self):
        # Only "not found" means there is no saved position; after a refusal the caller keeps what it knew.
        for code,text in ((500,'Failed to open file'),(400,'Invalid path'),(403,'Cannot access system files')):
            with self.assertRaises(x3.DeviceError) as exc:
                self.read(text,code)
            self.assertEqual(text,str(exc.exception))

    def test_network_error_is_a_device_error(self):
        with self.assertRaises(x3.DeviceError) as exc:
            x3.read_progress(Http(Offline('X3 не отвечает')),BOOK)
        self.assertEqual('X3 не отвечает',str(exc.exception))
        self.assertIsInstance(exc.exception.__cause__,Offline)
        # A fake device that refuses a request it does not expect must still fail its test.
        with self.assertRaises(AssertionError):
            x3.read_progress(Http(AssertionError('no device calls')),BOOK)


class DeleteTests(unittest.TestCase):
    def test_paths_go_as_a_json_array_in_a_form(self):
        paths=['/Книга один-1a2b3c.epub','/Books/a&b=c+d.epub']
        http=Http(reply(200,'All items deleted successfully'))
        self.assertIsNone(x3.delete(http,paths))
        call,=http.calls
        self.assertEqual(('POST','/delete'),(call.method,call.path))
        self.assertEqual('application/x-www-form-urlencoded',call.headers['Content-Type'])
        self.assertEqual(['paths'],list(parse_qs(call.body)))
        self.assertEqual(paths,json.loads(parse_qs(call.body)['paths'][0]))

    def test_nothing_to_delete_is_no_request(self):
        http=Http(reply(400,'No paths provided'))
        x3.delete(http,[])
        self.assertEqual([],http.calls)

    def test_refusal_carries_the_device_text(self):
        with self.assertRaises(x3.DeviceError) as exc:
            x3.delete(Http(reply(500,'Failed to delete some items: /a.epub (not found); ')),['/a.epub','/b.epub'])
        self.assertIn('Failed to delete some items: /a.epub (not found);',str(exc.exception))
        with self.assertRaises(x3.DeviceError) as exc:
            x3.delete(Http(reply(404)),['/a.epub'])
        self.assertIn('HTTP 404',str(exc.exception))
        with self.assertRaises(x3.DeviceError):
            x3.delete(Http(Offline('X3 не отвечает')),['/a.epub'])


class UploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.payload=os.urandom(2*1024*1024+1234)  # 512 full frames and a short one
        cls.path=Path(cls.temp.name)/'book.epub'
        cls.path.write_bytes(cls.payload)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def device(self,mode='ok'):
        device=Device(mode);self.addCleanup(device.finish)
        return device

    def assert_http_upload(self,http,directory,name,timeout):
        call,=http.calls
        self.assertEqual(('POST','/upload',timeout),(call.method,call.path,call.timeout))
        self.assertEqual({'path':[directory]},parse_qs(urlsplit(call.url).query))
        self.assertIn(f'name="file"; filename="{name}"'.encode(),call.body)
        self.assertIn(b'Content-Type: application/epub+zip\r\n\r\n'+self.payload+b'\r\n--',call.body)

    def test_file_goes_over_the_websocket_byte_for_byte(self):
        device=self.device();http=Http(reply())
        self.assertEqual('ws',x3.upload(http,device.url,'/Books','Книга-1a2b3c4d5e6f.epub',self.path,10))
        device.finish()
        self.assertEqual([],http.calls)
        self.assertEqual('GET / HTTP/1.1',device.request)
        self.assertEqual(f'127.0.0.1:{device.port}',device.headers['host'])
        self.assertEqual(('websocket','upgrade','13'),tuple(device.headers[name].lower() for name in ('upgrade','connection','sec-websocket-version')))
        self.assertEqual(16,len(base64.b64decode(device.headers['sec-websocket-key'])))
        self.assertEqual(f'START:Книга-1a2b3c4d5e6f.epub:{len(self.payload)}:/Books',device.start)
        self.assertEqual(self.payload,bytes(device.received))
        self.assertEqual(([4096]*512,1234),(device.sizes[:-1],device.sizes[-1]))
        self.assertEqual(0,device.unmasked)
        self.assertEqual([b'x3'],device.pongs)
        self.assertEqual(struct.pack('!H',1000),device.closed)

    def test_empty_file_is_done_without_data(self):
        empty=Path(self.temp.name)/'empty.epub';empty.write_bytes(b'')
        device=self.device()
        self.assertEqual('ws',x3.upload(Http(reply()),device.url,'/','empty.epub',empty,10))
        device.finish()
        self.assertEqual(('START:empty.epub:0:/',[]),(device.start,device.sizes))

    def test_error_from_the_device_falls_back_to_http(self):
        for mode,reason,kept in (('exists','File already exists: book.epub',0),('full','Write failed - disk full?',131072)):
            with self.subTest(mode):
                device=self.device(mode);http=Http(reply(200,'File uploaded successfully: book.epub'))
                with self.assertLogs('bookhub.x3','INFO') as logs:
                    self.assertEqual('http',x3.upload(http,device.url,'/','book.epub',self.path,7))
                device.finish()
                self.assertIn(reason,logs.output[0])
                self.assertEqual(kept,len(device.received))
                # The whole file from its first byte, whatever part of it went over the WebSocket.
                self.assert_http_upload(http,'/','book.epub',7)
                # The goodbye is what makes the device drop an unfinished file before the HTTP attempt.
                # After 'full' the fake may still be answering queued frames when the client stops waiting.
                if mode=='exists':self.assertEqual(struct.pack('!H',1000),device.closed)

    def test_error_stops_the_sending_within_a_few_frames(self):
        device=self.device('cancelled');http=Http(reply())
        self.assertEqual('http',x3.upload(http,device.url,'/','book.epub',self.path,7))
        device.finish()
        # 513 frames were to go; what the device says is read after every one of them.
        self.assertLessEqual(len(device.sizes),4)
        self.assert_http_upload(http,'/','book.epub',7)

    def test_fake_device_speaks_rfc_6455(self):
        # Its examples: the accept value of the sample key (1.3), "Hello" under the sample mask (5.7).
        self.assertEqual('s3pPLMBiTxaQ9kYGzzhZRbK+xOo=',accept('dGhlIHNhbXBsZSBub25jZQ=='))
        self.assertEqual(b'Hello',unmask(bytes.fromhex('7f9f4d5158'),bytes.fromhex('37fa213d')))

    def test_failed_handshake_and_silence_fall_back_to_http(self):
        for mode,timeout,reason in (('rejected',7,'handshake rejected: HTTP/1.1 400'),
                                    ('impostor',7,'handshake rejected: HTTP/1.1 101'),('silent',0.3,'timed out')):
            with self.subTest(mode):
                device=self.device(mode);http=Http(reply())
                with self.assertLogs('bookhub.x3','INFO') as logs:
                    self.assertEqual('http',x3.upload(http,device.url,'/','book.epub',self.path,timeout))
                device.finish()
                self.assertIn(reason,logs.output[0])
                self.assert_http_upload(http,'/','book.epub',timeout)
                self.assertEqual(b'',bytes(device.received))

    def test_refused_connection_falls_back_to_http(self):
        http=Http(reply())
        with self.assertLogs('bookhub.x3','INFO') as logs:
            self.assertEqual('http',x3.upload(http,f'http://127.0.0.1:{closed_port()-1}','/Books','book.epub',self.path,7))
        self.assertIn('ConnectionRefusedError',logs.output[0])
        self.assert_http_upload(http,'/Books','book.epub',7)

    def test_websocket_is_on_the_http_port_plus_one(self):
        for url,address in (('http://192.0.2.10',('192.0.2.10',81)),('http://x3.local:8080/',('x3.local',8081)),
                            ('192.0.2.10',('192.0.2.10',81))):
            with patch.object(x3.socket,'create_connection',side_effect=ConnectionRefusedError) as connect:
                self.assertEqual('http',x3.upload(Http(reply()),url,'/','book.epub',self.path,7))
            self.assertEqual((address,5),(connect.call_args.args[0],connect.call_args.kwargs['timeout']))

    def test_name_with_a_colon_does_not_go_over_the_websocket(self):
        # The device splits START at colons and would take half of the name for the size.
        with patch.object(x3.socket,'create_connection') as connect:
            self.assertEqual('http',x3.upload(Http(reply()),'http://192.0.2.10','/','a:b.epub',self.path,7))
        connect.assert_not_called()

    def test_both_ways_failing_is_a_device_error(self):
        url=f'http://127.0.0.1:{closed_port()-1}'
        with self.assertRaises(x3.DeviceError) as exc:
            x3.upload(Http(reply(400,'File already exists: book.epub')),url,'/','book.epub',self.path,7)
        self.assertIn('File already exists: book.epub',str(exc.exception))
        with self.assertRaises(x3.DeviceError) as exc:
            x3.upload(Http(Offline('X3 не отвечает')),url,'/','book.epub',self.path,7)
        self.assertEqual('X3 не отвечает',str(exc.exception))
        device=self.device('exists')
        with self.assertRaises(x3.DeviceError):
            x3.upload(Http(reply(400,'File already exists: book.epub')),device.url,'/','book.epub',self.path,7)


if __name__=='__main__':
    unittest.main()
