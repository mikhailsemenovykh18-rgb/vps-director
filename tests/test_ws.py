"""Task 07b item 1: the minimal RFC 6455 server side (vps_director/ws.py) with crafted frames."""
import base64
import io
import os
import socket
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vps_director import web, ws  # noqa: E402

KEY = "dGhlIHNhbXBsZSBub25jZQ=="  # the example from RFC 6455 section 1.3


def frame(opcode, payload=b"", fin=True, mask=b"\x01\x02\x03\x04", rsv=0, length=None):
    """A client frame (masked unless mask is None)."""
    head = bytes([(0x80 if fin else 0) | rsv | opcode])
    n = len(payload) if length is None else length
    bit = 0x80 if mask is not None else 0
    if n < 126:
        head += bytes([bit | n])
    elif n < 65536:
        head += bytes([bit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([bit | 127]) + struct.pack("!Q", n)
    if mask is None:
        return head + payload
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def server_frames(raw):
    """Decode unmasked server frames: [(opcode, payload)]."""
    out, i = [], 0
    while i < len(raw):
        op, n = raw[i] & 0x0F, raw[i + 1] & 0x7F
        i += 2
        if n == 126:
            n, i = struct.unpack("!H", raw[i:i + 2])[0], i + 2
        elif n == 127:
            n, i = struct.unpack("!Q", raw[i:i + 8])[0], i + 8
        out.append((op, raw[i:i + n]))
        i += n
    return out


class Conn:
    def __init__(self, data, throttle=None):
        self.out = io.BytesIO()
        self.c = ws.Connection(io.BytesIO(data), self.out, throttle=throttle or ws.Throttle(rate=0))

    def frames(self):
        return server_frames(self.out.getvalue())


HEADERS = {"Upgrade": "websocket", "Connection": "keep-alive, Upgrade", "Sec-WebSocket-Version": "13",
           "Sec-WebSocket-Key": KEY}


class HandshakeTest(unittest.TestCase):
    def test_accept_key_from_the_rfc(self):
        self.assertEqual(ws.accept_key(KEY), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_valid_upgrade(self):
        self.assertEqual(ws.handshake(HEADERS), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_refused_upgrades(self):
        for change in ({"Upgrade": "h2c"}, {"Connection": "keep-alive"}, {"Sec-WebSocket-Version": "8"},
                       {"Sec-WebSocket-Key": ""}, {"Sec-WebSocket-Key": "short"},
                       {"Sec-WebSocket-Key": base64.b64encode(b"x" * 17).decode()}):
            self.assertIsNone(ws.handshake(dict(HEADERS, **change)), change)

    def test_response_bytes(self):
        raw = ws.response_101("abc")
        self.assertTrue(raw.startswith(b"HTTP/1.1 101 Switching Protocols\r\n"))
        self.assertIn(b"Sec-WebSocket-Accept: abc\r\n", raw)
        self.assertTrue(raw.endswith(b"\r\n\r\n"))


class FrameTest(unittest.TestCase):
    def test_text_and_binary(self):
        c = Conn(frame(ws.TEXT, "привет".encode()) + frame(ws.BINARY, b"\x00\xff"))
        self.assertEqual(c.c.recv(), (ws.TEXT, "привет"))
        self.assertEqual(c.c.recv(), (ws.BINARY, b"\x00\xff"))

    def test_lengths_16_and_64_bit(self):
        big = b"a" * 300
        c = Conn(frame(ws.BINARY, big))
        self.assertEqual(c.c.recv(), (ws.BINARY, big))
        c = Conn(frame(ws.BINARY, b"b" * ws.MAX_FRAME))
        self.assertEqual(len(c.c.recv()[1]), ws.MAX_FRAME)

    def test_ping_gets_pong_and_pong_is_ignored(self):
        c = Conn(frame(ws.PING, b"hi") + frame(ws.PONG, b"x") + frame(ws.TEXT, b"after"))
        self.assertEqual(c.c.recv(), (ws.TEXT, "after"))
        self.assertEqual(c.frames(), [(ws.PONG, b"hi")])

    def test_close_is_echoed(self):
        c = Conn(frame(ws.CLOSE, struct.pack("!H", 1000) + b"bye"))
        self.assertIsNone(c.c.recv())
        self.assertEqual(c.frames(), [(ws.CLOSE, struct.pack("!H", 1000))])
        self.assertTrue(c.c.closed)

    def test_eof_is_a_disconnect(self):
        self.assertIsNone(Conn(b"").c.recv())
        self.assertIsNone(Conn(frame(ws.TEXT, b"abc")[:4]).c.recv())  # cut in the middle

    def test_fragmented_message(self):
        data = (frame(ws.TEXT, b"hel", fin=False) + frame(ws.PING, b"p") + frame(ws.CONT, b"lo ", fin=False)
                + frame(ws.CONT, b"world"))
        c = Conn(data)
        self.assertEqual(c.c.recv(), (ws.TEXT, "hello world"))
        self.assertEqual(c.frames(), [(ws.PONG, b"p")])

    def assert_closed_with(self, data, code):
        c = Conn(data)
        self.assertIsNone(c.c.recv())
        frames = c.frames()
        self.assertEqual(frames[-1][0], ws.CLOSE)
        self.assertEqual(struct.unpack("!H", frames[-1][1][:2])[0], code)
        self.assertTrue(c.c.closed)

    def test_unmasked_frame_is_a_protocol_error(self):
        self.assert_closed_with(frame(ws.TEXT, b"hi", mask=None), 1002)

    def test_malformed_frames(self):
        self.assert_closed_with(frame(ws.TEXT, b"hi", rsv=0x40), 1002)               # reserved bit
        self.assert_closed_with(frame(0x3, b"hi"), 1002)                             # unknown opcode
        self.assert_closed_with(frame(ws.PING, b"p" * 126), 1002)                    # control frame > 125
        self.assert_closed_with(frame(ws.PING, b"p", fin=False), 1002)               # fragmented control
        self.assert_closed_with(frame(ws.CONT, b"x"), 1002)                          # continuation of nothing
        self.assert_closed_with(frame(ws.TEXT, b"a", fin=False) + frame(ws.TEXT, b"b"), 1002)  # new message inside
        self.assert_closed_with(frame(ws.TEXT, b"\xff\xfe"), 1007)                   # not UTF-8
        self.assert_closed_with(frame(ws.CLOSE, b"\x03"), 1002)                      # 1-byte close payload

    def test_oversize(self):
        self.assert_closed_with(frame(ws.BINARY, b"", length=ws.MAX_FRAME + 1), 1009)
        self.assert_closed_with(frame(ws.BINARY, b"", length=2 ** 63 + 5), 1009)  # header only, nothing read
        half = b"x" * (ws.MAX_FRAME // 2 + 1)
        self.assert_closed_with(frame(ws.TEXT, half, fin=False) + frame(ws.CONT, half), 1009)  # whole message

    def test_server_frames_are_unmasked(self):
        c = Conn(b"")
        c.c.send_text("hi")
        c.c.send_binary(b"\x01" * 70000)
        frames = c.frames()
        self.assertEqual(frames[0], (ws.TEXT, b"hi"))
        self.assertEqual(sum(len(p) for op, p in frames[1:]), 70000)
        self.assertTrue(all(op == ws.BINARY and len(p) <= ws.MAX_FRAME for op, p in frames[1:]))
        self.assertEqual(c.out.getvalue()[1] & 0x80, 0)

    def test_close_once_and_nothing_after(self):
        c = Conn(b"")
        c.c.close(4000, "idle")
        c.c.close(1000)
        c.c.send_binary(b"late")
        self.assertEqual(c.frames(), [(ws.CLOSE, struct.pack("!H", 4000) + b"idle")])


class ThrottleTest(unittest.TestCase):
    def test_one_megabyte_per_second(self):
        now = [0.0]
        slept = []

        def sleep(s):
            slept.append(s)
            now[0] += s
        t = ws.Throttle(rate=1000, clock=lambda: now[0], sleep=sleep)
        t.take(500)
        t.take(500)
        self.assertEqual(slept, [])  # a one-second burst is free
        t.take(500)
        self.assertAlmostEqual(sum(slept), 0.5)
        now[0] += 10
        slept.clear()
        t.take(1000)
        self.assertEqual(slept, [])  # the allowance never grows above one second's worth

    def test_default_rate(self):
        self.assertEqual(ws.Throttle().rate, 1024 * 1024)

    def test_send_goes_through_the_throttle(self):
        taken = []

        class T:
            def take(self, n):
                taken.append(n)
        c = Conn(b"", throttle=T())
        c.c.send_binary(b"x" * 10)
        self.assertEqual(taken, [10])


class WSClient:
    """A tiny test client on 127.0.0.1: masked frames out, server frames in."""

    def __init__(self, port, path, headers=None, timeout=5):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        h = dict(HEADERS, Host="127.0.0.1:%d" % port, **(headers or {}))
        req = "GET %s HTTP/1.1\r\n%s\r\n" % (path, "".join("%s: %s\r\n" % kv for kv in h.items()))
        self.sock.sendall(req.encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self.buf += chunk
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        self.status = int(head.split(b" ")[1]) if head else 0
        self.head = head.decode("latin-1")
        if self.status != 101:  # read the rest of an HTTP error reply
            length = next((int(line.split(":")[1]) for line in self.head.split("\r\n")
                           if line.lower().startswith("content-length:")), 0)
            while len(self.buf) < length:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                self.buf += chunk
            self.body = self.buf[:length]

    def send(self, opcode, payload=b"", **kw):
        self.sock.sendall(frame(opcode, payload, **kw))

    def send_json(self, obj):
        import json
        self.send(TEXT_OP, json.dumps(obj).encode())

    def need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk

    def recv(self):
        """(opcode, payload) of the next server frame; raises EOFError when the socket closes."""
        self.need(2)
        op, n, i = self.buf[0] & 0x0F, self.buf[1] & 0x7F, 2
        if n == 126:
            self.need(4)
            n, i = struct.unpack("!H", self.buf[2:4])[0], 4
        elif n == 127:
            self.need(10)
            n, i = struct.unpack("!Q", self.buf[2:10])[0], 10
        self.need(i + n)
        out, self.buf = (op, self.buf[i:i + n]), self.buf[i + n:]
        return out

    def read_until(self, needle, limit=200):
        """Binary output until `needle` shows up; returns (all output, close frame or None)."""
        got = b""
        for _ in range(limit):
            try:
                op, data = self.recv()
            except (EOFError, OSError):
                return got, None
            if op == ws.BINARY:
                got += data
                if needle and needle in got:
                    return got, None
            elif op == ws.CLOSE:
                return got, data
        return got, None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


TEXT_OP = ws.TEXT


class LiveTest(unittest.TestCase):
    def setUp(self):
        def echo(conn):
            while True:
                msg = conn.recv()
                if msg is None:
                    return
                conn.send_binary(msg[1].encode() if msg[0] == ws.TEXT else msg[1])

        def route(query, headers, ip):
            return web.error(403, "no") if "deny" in query else echo
        self.server = web.serve(0, web.WebApp(root=os.path.dirname(__file__), sockets={"/ws/echo": route}))
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_echo_over_a_real_socket(self):
        c = WSClient(self.port, "/ws/echo")
        self.assertEqual(c.status, 101)
        self.assertIn("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", c.head)
        c.send(ws.TEXT, b"hello")
        self.assertEqual(c.recv(), (ws.BINARY, b"hello"))
        c.send(ws.PING, b"x")
        self.assertEqual(c.recv(), (ws.PONG, b"x"))
        c.send(ws.CLOSE, struct.pack("!H", 1000))
        self.assertEqual(c.recv(), (ws.CLOSE, struct.pack("!H", 1000)))
        c.close()

    def test_unmasked_frame_closes_1002(self):
        c = WSClient(self.port, "/ws/echo")
        c.send(ws.TEXT, b"hello", mask=None)
        op, data = c.recv()
        self.assertEqual((op, struct.unpack("!H", data[:2])[0]), (ws.CLOSE, 1002))
        c.close()

    def test_refusals_are_plain_http(self):
        c = WSClient(self.port, "/ws/echo?deny=1")
        self.assertEqual(c.status, 403)
        self.assertIn("Content-Security-Policy", c.head)
        c.close()
        c = WSClient(self.port, "/ws/nothing")
        self.assertEqual(c.status, 404)
        c.close()
        c = WSClient(self.port, "/ws/echo", headers={"Sec-WebSocket-Version": "12"})
        self.assertEqual(c.status, 400)
        c.close()


if __name__ == "__main__":
    unittest.main()
