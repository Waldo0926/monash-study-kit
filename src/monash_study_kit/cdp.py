"""和 Chrome DevTools 协议（CDP）说话用的最小 WebSocket 客户端。

标准库没有 WebSocket，这里只实现 CDP 用得到的部分：握手、发文本帧（客户端必须加掩码）、
收文本帧（含分片）、回 ping。不发 Origin 头——Chrome 只对带 Origin 的连接要求
--remote-allow-origins，不带就放行。只连 127.0.0.1。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.parse


class WebSocketClosed(ConnectionError):
    """浏览器关了（或者用户把登录窗口关了）。"""


class WebSocket:
    def __init__(self, url: str, timeout: float = 10):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "ws" or u.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("只连本机的 DevTools")
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketClosed("握手时连接断了")
            head += chunk
        head, self._buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"DevTools 拒绝了 WebSocket：{head[:200]!r}")

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketClosed("浏览器已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def send(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self) -> str:
        parts: list[bytes] = []
        while True:
            b0, b1 = self._read(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise WebSocketClosed("浏览器关闭了连接")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode in (0x1, 0x0):
                parts.append(data)
                if b0 & 0x80:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()


class CDP:
    """浏览器级的 CDP 会话：call() 发命令等结果，中间收到的事件丢掉。"""

    def __init__(self, ws_url: str):
        self.ws = WebSocket(ws_url)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result") or {}
        raise TimeoutError(f"{method} 没有回应")

    def close(self) -> None:
        self.ws.close()
