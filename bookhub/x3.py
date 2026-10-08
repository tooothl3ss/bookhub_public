"""The X3 (CrossPoint) file-transfer service: reading positions, deletion, upload.

Protocol only: no configuration and no database. Every function takes the caller's
request(method, path, **kwargs) -> requests.Response, which raises on a network error.
The service answers only while the file-transfer screen is open on the device.
"""
import base64
import hashlib
import json
import logging
import os
import selectors
import socket
import struct
import time
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
TEXT, BINARY, CLOSE, PING, PONG = 0x1, 0x2, 0x8, 0x9, 0xA
WS_FRAME = 4096      # what the device's own page sends at a time
WS_CONNECT_SEC = 5
WS_SILENCE_SEC = 30  # the device writes to its card between answers
WS_HANGUP_SEC = 2    # after a failure: for the device to hang up and drop its partial file


class DeviceError(Exception):
    pass


def _ask(request, method, path, **kwargs):
    """The caller's request reports a network error in its own way; from here it is a DeviceError.
    An AssertionError stays what it is: a test's guard against touching the device."""
    try:
        return request(method, path, **kwargs)
    except (DeviceError, AssertionError):
        raise
    except Exception as exc:
        raise DeviceError(str(exc) or exc.__class__.__name__) from exc


def _words(response):
    return response.text[:200].strip() or f'HTTP {response.status_code}'


def cache_dir(path):
    """The firmware's cache directory of a book, by the book's absolute path on the card.

    The number is std::hash<std::string> of that path as libstdc++ computes it on the 32-bit
    device: MurmurHash2 with the seed 0xc70f6907.
    """
    data = ('/' + '/'.join(part for part in path.split('/') if part)).encode()
    m, bits = 0x5bd1e995, 0xffffffff
    h = 0xc70f6907 ^ len(data)
    whole = len(data) & ~3
    for (k,) in struct.iter_unpack('<I', data[:whole]):
        k = k * m & bits
        k = (k ^ k >> 24) * m & bits
        h = h * m & bits ^ k
    if len(data) > whole:
        h = (h ^ int.from_bytes(data[whole:], 'little')) * m & bits
    h = (h ^ h >> 13) * m & bits
    return f'/.crosspoint/epub_{h ^ h >> 15}'


def read_progress(request, path):
    """Where the device stands in a book; None when it has never opened it.

    progress.bin is little-endian: u16 spine index (equal to the spine count: finished), u16 page
    of the chapter and, depending on what wrote it, u16 pages in the chapter and u32 visible text
    offset in the chapter.
    """
    r = _ask(request, 'GET', '/download', params={'path': cache_dir(path) + '/progress.bin'}, timeout=3)
    if r.status_code == 404:
        return None
    if r.status_code != 200:  # a refusal is not "never opened": the caller keeps what it knew
        raise DeviceError(_words(r))
    data = r.content
    if len(data) not in (4, 6, 10):
        return None
    spine, page = struct.unpack_from('<HH', data)
    return dict(spine=spine, page=0 if page == 0xFFFF else page,  # 0xFFFF: a stale "last page" mark
                page_count=struct.unpack_from('<H', data, 4)[0] if len(data) >= 6 else None,
                visible_offset=struct.unpack_from('<I', data, 6)[0] if len(data) == 10 else None)


def delete(request, paths):
    """Remove files by their absolute paths; the device drops their caches itself.

    It works through the whole list before answering: after a DeviceError some may be gone.
    """
    paths = [paths] if isinstance(paths, str) else list(paths)
    if not paths:
        return
    r = _ask(request, 'POST', '/delete', data={'paths': json.dumps(paths, ensure_ascii=False)}, timeout=30)
    if r.status_code != 200:
        raise DeviceError(f'X3 не удалил файлы: {_words(r)}')


def _mask(data, key):
    n = len(data)
    return (int.from_bytes(data, 'big') ^ int.from_bytes((key * (n // 4 + 1))[:n], 'big')).to_bytes(n, 'big')


class _WebSocket:
    """As much of RFC 6455 as the upload needs: the handshake, masked frames out, short frames in."""

    def __init__(self, host, port, connect_timeout, silence):
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        self.selector = selectors.DefaultSelector()
        self.pending = b''
        try:
            self.sock.settimeout(silence)
            self.selector.register(self.sock, selectors.EVENT_READ)
            key = base64.b64encode(os.urandom(16)).decode()
            name = f'[{host}]' if ':' in host else host
            self.sock.sendall((f'GET / HTTP/1.1\r\nHost: {name}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                               f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
            while b'\r\n\r\n' not in self.pending:
                if len(self.pending) > 8192:
                    raise DeviceError('handshake: the headers do not end')
                self.fill()
            # What follows the headers is already frames: the device pings right behind them.
            head, self.pending = self.pending.split(b'\r\n\r\n', 1)
            status, *lines = head.decode('latin-1').split('\r\n')
            headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(':') for line in lines)}
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            if status.split()[1:2] != ['101'] or headers.get('sec-websocket-accept') != accept:
                raise DeviceError(f'handshake rejected: {status}')
        except BaseException:
            self.drop()
            raise

    def fill(self):
        data = self.sock.recv(4096)
        if not data:
            raise DeviceError('the device hung up')
        self.pending += data

    def take(self, n):
        while len(self.pending) < n:
            self.fill()
        data, self.pending = self.pending[:n], self.pending[n:]
        return data

    def send(self, opcode, payload=b''):
        n, key = len(payload), os.urandom(4)
        if n < 126:
            head = struct.pack('!BB', 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack('!BBH', 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack('!BBQ', 0x80 | opcode, 0x80 | 127, n)
        self.sock.sendall(head + key + _mask(payload, key))

    def receive(self):
        """One frame from the device: the text of a message, None for anything else. A ping is answered."""
        first, second = self.take(2)
        n = second & 0x7f
        if n == 126:
            (n,) = struct.unpack('!H', self.take(2))
        elif n == 127:
            raise DeviceError('a frame too long for this protocol')
        key = self.take(4) if second & 0x80 else None
        payload = self.take(n)
        if key:
            payload = _mask(payload, key)
        opcode = first & 0x0f
        if opcode == PING:
            self.send(PONG, payload)
        elif opcode == CLOSE:
            raise DeviceError('the device closed the connection')
        elif opcode == TEXT:
            return payload.decode('utf-8', 'replace')
        return None

    def waiting(self):
        """The device has already said something: receive() will not wait for it."""
        return bool(self.pending or self.selector.select(0))

    def close(self, wait=0):
        """Send the close frame; with wait, give the device that long to hang up."""
        try:
            self.sock.settimeout(wait or 1)
            self.send(CLOSE, struct.pack('!H', 1000))
            end = time.monotonic() + wait
            while time.monotonic() < end:
                self.receive()  # ends by raising: the device's close frame, the end of the stream, silence
        except (OSError, DeviceError):
            pass
        self.drop()

    def drop(self):
        self.selector.close()
        self.sock.close()


def _upload_ws(base_url, directory, filename, source, timeout):
    """START:<name>:<size>:<dir> -> READY; binary frames -> PROGRESS:<received>:<total> ... DONE.
    ERROR:<why> may come at any point."""
    url = urlsplit(base_url if '//' in base_url else '//' + base_url)
    if not url.hostname or ':' in filename:  # the device splits START at colons
        raise DeviceError('no WebSocket upload for this address or file name')
    left = os.fstat(source.fileno()).st_size
    ws = _WebSocket(url.hostname, (url.port or 80) + 1, min(timeout, WS_CONNECT_SEC), min(timeout, WS_SILENCE_SEC))
    done = False

    def hear():
        text = ws.receive()
        if text and text.startswith('ERROR:'):
            raise DeviceError(text[6:].strip() or text)
        return text

    try:
        ws.send(TEXT, f'START:{filename}:{left}:{directory}'.encode())
        said = None
        while said not in ('READY', 'DONE'):  # DONE at once: an empty file
            said = hear()
        while said != 'DONE':
            if left:
                chunk = source.read(min(WS_FRAME, left))
                if not chunk:
                    raise DeviceError('the file ended early')
                ws.send(BINARY, chunk)
                left -= len(chunk)
            # The device answers PROGRESS every 64 KiB it has written. Look after every frame:
            # its answers never pile up unread and an ERROR stops the sending at once. TCP holds
            # the sending back to the device's pace. With everything sent, wait for DONE.
            while said != 'DONE' and (not left or ws.waiting()):
                said = hear()
        done = True
    finally:
        # The device removes an unfinished file when it sees the connection go: wait for that,
        # or the HTTP attempt that follows meets "File already exists".
        ws.close(0 if done else WS_HANGUP_SEC)


def upload(request, base_url, directory, filename, source_path, timeout):
    """Put a file into a directory of the device; returns the way that worked, 'ws' or 'http'.

    First the device's WebSocket on the HTTP port + 1, which is faster; after any failure there,
    the multipart POST /upload. timeout is that of the POST; over the WebSocket it also bounds
    how long the device may stay silent, 30 seconds at most. Neither way overwrites a file, and
    neither is verified here: the caller checks a fresh listing.
    """
    with open(source_path, 'rb') as source:
        try:
            _upload_ws(base_url, directory, filename, source, timeout)
            return 'ws'
        except (OSError, ValueError, DeviceError) as exc:
            log.info('X3 WebSocket upload of %s failed, trying HTTP: %r', filename, exc)
        source.seek(0)
        r = _ask(request, 'POST', '/upload', params={'path': directory},
                 files={'file': (filename, source, 'application/epub+zip')}, timeout=timeout)
    if not r.ok:
        raise DeviceError(f'X3 не принял файл: {_words(r)}')
    return 'http'
