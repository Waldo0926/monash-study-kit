"""最小 WebSocket 客户端：对着一个假的 DevTools 服务端验证握手、掩码、长度编码、分片和 ping。"""
import json
import socket
import struct
import threading

import pytest

from monash_study_kit.cdp import CDP, WebSocket, WebSocketClosed


def _read_frame(conn):
    b0, b1 = conn.recv(1)[0], conn.recv(1)[0]
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", conn.recv(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", conn.recv(8))[0]
    assert b1 & 0x80, "客户端发的帧必须加掩码"
    mask = conn.recv(4)
    data = b""
    while len(data) < n:
        data += conn.recv(n - len(data))
    return b0 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _frame(opcode, payload, fin=True):
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    head += bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n)
    return head + payload


@pytest.fixture
def server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = {}

    def run():
        conn, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(1024)
        got["request"] = req.decode()
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        op, data = _read_frame(conn)
        msg = json.loads(data)
        got["sent"] = msg
        conn.sendall(_frame(0x9, b"hi"))                                  # ping，客户端要回 pong
        conn.sendall(_frame(0x1, json.dumps({"method": "Some.event"}).encode()))   # 事件，要被跳过
        reply = json.dumps({"id": msg["id"], "result": {"cookies": [{"name": "x" * 200}]}}).encode()
        conn.sendall(_frame(0x1, reply[:50], fin=False) + _frame(0x0, reply[50:]))  # 分片
        got["pong"] = _read_frame(conn)
        conn.sendall(_frame(0x8, b""))
        conn.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    yield srv.getsockname()[1], got
    t.join(5)
    srv.close()


def test_call_roundtrip(server):
    port, got = server
    cdp = CDP(f"ws://127.0.0.1:{port}/devtools/browser/abc")
    result = cdp.call("Storage.getCookies", {"big": "y" * 300})      # >125 字节，走 16 位长度
    assert result["cookies"][0]["name"] == "x" * 200
    assert got["sent"]["method"] == "Storage.getCookies" and got["sent"]["params"]["big"] == "y" * 300
    assert "Origin" not in got["request"] and "GET /devtools/browser/abc" in got["request"]
    with pytest.raises(WebSocketClosed):
        cdp.ws.recv()
    assert got["pong"] == (0xA, b"hi")


def test_only_local_connections():
    with pytest.raises(ValueError):
        WebSocket("ws://example.com:9222/devtools/browser/x")
