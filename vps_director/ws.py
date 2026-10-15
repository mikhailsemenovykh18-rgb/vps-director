"""A minimal RFC 6455 WebSocket server side, on top of the 07a HTTP server (standard library only).

Only what the web console needs:
- the upgrade handshake (`handshake` checks the request headers and returns Sec-WebSocket-Accept);
- client frames must be masked (an unmasked one closes the connection with 1002), reserved bits
  must be clear, opcodes are text / binary / continuation / close / ping / pong;
- fragmented messages are reassembled (control frames may come in between);
- a frame, and a whole message, is at most 64 KB (1009 otherwise); the length is checked before
  anything is read, so a huge header costs nothing;
- text must be UTF-8 (1007 otherwise); ping gets a pong; close is echoed once;
- output goes through a simple throttle (1 MB/s with a one-second burst) and is split into frames
  of at most 64 KB. Server frames are never masked.

Sending is thread-safe (the console's output pump and the reader both write).
"""
import base64
import hashlib
import struct
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 64 * 1024
RATE = 1024 * 1024  # output bytes per second

CONT, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
DATA_OPS = (CONT, TEXT, BINARY)
CONTROL_OPS = (CLOSE, PING, PONG)


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def handshake(headers):
    """Sec-WebSocket-Accept for a valid upgrade request, else None."""
    if (headers.get("Upgrade") or "").strip().lower() != "websocket":
        return None
    if "upgrade" not in [p.strip().lower() for p in (headers.get("Connection") or "").split(",")]:
        return None
    if (headers.get("Sec-WebSocket-Version") or "").strip() != "13":
        return None
    key = (headers.get("Sec-WebSocket-Key") or "").strip()
    try:
        if len(base64.b64decode(key, validate=True)) != 16:
            return None
    except (ValueError, TypeError):
        return None
    return accept_key(key)


def response_101(accept):
    return ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\nCache-Control: no-store\r\n\r\n" % accept).encode("ascii")


def encode(opcode, payload=b""):
    """One unmasked server frame."""
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    return head + payload


class ProtocolError(Exception):
    def __init__(self, code, reason=""):
        super().__init__(reason)
        self.code, self.reason = code, reason


class Throttle:
    """At most `rate` bytes per second on average, bursts up to one second's worth; rate 0 = no limit."""

    def __init__(self, rate=RATE, clock=time.monotonic, sleep=time.sleep):
        self.rate, self.clock, self.sleep = rate, clock, sleep
        self.allowance = float(rate)
        self.last = clock()
        self.lock = threading.Lock()

    def take(self, n):
        if self.rate <= 0:
            return
        with self.lock:
            now = self.clock()
            self.allowance = min(float(self.rate), self.allowance + (now - self.last) * self.rate)
            self.last = now
            self.allowance -= n
            if self.allowance < 0:
                self.sleep(-self.allowance / self.rate)
                self.allowance = 0.0
                self.last = self.clock()


class Connection:
    """A WebSocket connection over a pair of binary files (the HTTP handler's rfile / wfile)."""

    def __init__(self, rfile, wfile, throttle=None, sock=None):
        self.rfile, self.wfile, self.sock = rfile, wfile, sock
        self.throttle = throttle if throttle is not None else Throttle()
        self.wlock = threading.Lock()
        self.closed = False      # a close frame was sent: nothing else goes out
        self.close_code = None   # what the client sent (or the error we closed with)

    # -- reading --

    def read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.rfile.read(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def frame(self):
        b0, b1 = self.read(2)
        fin, rsv, opcode = bool(b0 & 0x80), b0 & 0x70, b0 & 0x0F
        masked, n = bool(b1 & 0x80), b1 & 0x7F
        if rsv:
            raise ProtocolError(1002, "reserved bits")
        if opcode not in DATA_OPS + CONTROL_OPS:
            raise ProtocolError(1002, "unknown opcode")
        if not masked:
            raise ProtocolError(1002, "unmasked frame")
        if n == 126:
            n = struct.unpack("!H", self.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self.read(8))[0]
        if opcode in CONTROL_OPS and (n > 125 or not fin):
            raise ProtocolError(1002, "bad control frame")
        if n > MAX_FRAME:
            raise ProtocolError(1009, "frame too big")
        mask = self.read(4)
        data = self.read(n)
        return fin, opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(data))

    def recv(self):
        """The next message as (TEXT, str) or (BINARY, bytes); None when the connection is over
        (close frame, EOF, protocol error — the right close frame has been sent then)."""
        if self.closed:
            return None
        parts, kind, size = [], None, 0
        try:
            while True:
                fin, opcode, data = self.frame()
                if opcode == PING:
                    self.send(PONG, data)
                    continue
                if opcode == PONG:
                    continue
                if opcode == CLOSE:
                    if len(data) == 1:
                        raise ProtocolError(1002, "bad close payload")
                    self.close_code = struct.unpack("!H", data[:2])[0] if data else 1005
                    self.close(self.close_code if data else 1000)
                    return None
                if opcode == CONT and kind is None:
                    raise ProtocolError(1002, "continuation without a message")
                if opcode != CONT and kind is not None:
                    raise ProtocolError(1002, "new message inside a fragmented one")
                if kind is None:
                    kind = opcode
                size += len(data)
                if size > MAX_FRAME:
                    raise ProtocolError(1009, "message too big")
                parts.append(data)
                if fin:
                    break
        except EOFError:
            self.closed = True
            return None
        except ProtocolError as exc:
            self.close_code = exc.code
            self.close(exc.code, exc.reason)
            return None
        except OSError:
            self.closed = True
            return None
        payload = b"".join(parts)
        if kind == TEXT:
            try:
                return TEXT, payload.decode("utf-8")
            except UnicodeDecodeError:
                self.close_code = 1007
                self.close(1007, "not UTF-8")
                return None
        return BINARY, payload

    # -- writing --

    def send(self, opcode, payload=b""):
        with self.wlock:
            if self.closed:
                return False
            try:
                self.wfile.write(encode(opcode, payload))
                if hasattr(self.wfile, "flush"):
                    self.wfile.flush()
            except (OSError, ValueError):
                self.closed = True
                return False
            if opcode == CLOSE:
                self.closed = True
            return True

    def send_text(self, text):
        return self.send(TEXT, text.encode("utf-8"))

    def send_binary(self, data):
        self.throttle.take(len(data))
        for i in range(0, max(len(data), 1), MAX_FRAME):
            if not self.send(BINARY, data[i:i + MAX_FRAME]):
                return False
        return True

    def ping(self):
        return self.send(PING, b"")

    def close(self, code=1000, reason=""):
        """Send a close frame once (later calls do nothing)."""
        return self.send(CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120])
