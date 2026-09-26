"""A small RFC6455 websocket client.

Stdlib only, to keep this repo dependency-free. It implements just what the
market-data streams need: a TLS client handshake, masked client frames,
fragmentation reassembly, ping/pong, and clean close. No extensions, no
compression -- neither venue requires them for these streams.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import ssl
import struct
import threading
from typing import Iterator
from urllib.parse import urlparse

log = logging.getLogger(__name__)

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA


class WebSocketClosed(RuntimeError):
    pass


class WebSocket:
    """Blocking client. One per thread."""

    def __init__(self, url: str, *, timeout: float = 300.0,
                 heartbeat: str | None = None, heartbeat_s: float = 20.0):
        """`timeout` is the socket read timeout. It must exceed the venue's own
        keepalive interval or a quiet stream looks like a dead one: Binance
        pings roughly every 3 minutes, so 30s would reconnect endlessly on a
        calm tape.

        `heartbeat` is an application-level ping payload sent on a timer.
        Bybit drops a client that does not send `{"op":"ping"}` every 20s;
        Binance needs nothing, since it pings us.
        """
        self.url = url
        self.timeout = timeout
        self.heartbeat = heartbeat
        self.heartbeat_s = heartbeat_s
        parts = urlparse(url)
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if parts.scheme == "wss" else 80)
        self.path = parts.path + (f"?{parts.query}" if parts.query else "")
        self._sock: ssl.SSLSocket | socket.socket | None = None
        # Bytes the server coalesced into the same TCP segment as the 101
        # response. Discarding them starts the frame reader mid-frame, which
        # yields a garbage opcode and a bogus 64-bit length -- an unbounded
        # read that never returns and is essentially undiagnosable.
        self._buf = b""
        self._send_lock = threading.RLock()
        self._closed = threading.Event()
        self._hb_thread: threading.Thread | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> "WebSocket":
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.url.startswith("wss"):
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        raw.settimeout(self.timeout)
        self._sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        raw.sendall(
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = raw.recv(4096)
            if not chunk:
                raise WebSocketClosed("server closed during handshake")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        self._buf = rest
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise WebSocketClosed(f"handshake rejected: {status}")
        if self.heartbeat:
            self._hb_thread = threading.Thread(
                target=self._heartbeat_loop, name="ws-heartbeat", daemon=True)
            self._hb_thread.start()
        return self

    def _heartbeat_loop(self) -> None:
        while not self._closed.wait(self.heartbeat_s):
            try:
                self.send_text(self.heartbeat or "")
            except Exception:
                return

    def close(self) -> None:
        """Stop the heartbeat first, then close under the send lock.

        Closing the fd outside the lock races the heartbeat thread mid-send:
        the descriptor can be closed and reused by another connection, and a
        masked ping lands in an unrelated socket."""
        self._closed.set()
        hb = self._hb_thread
        if hb and hb is not threading.current_thread():
            hb.join(timeout=2.0)
        with self._send_lock:
            sock, self._sock = self._sock, None
            if sock is None:
                return
            try:
                self._send_frame_locked(OP_CLOSE, b"\x03\xe8")
            except Exception:
                pass
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

    def __enter__(self) -> "WebSocket":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ── framing ───────────────────────────────────────────────────────────────

    def _recvn(self, n: int) -> bytes:
        sock = self._sock
        if sock is None:
            raise WebSocketClosed("socket is closed")
        out, self._buf = self._buf[:n], self._buf[n:]
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                raise WebSocketClosed("connection closed")
            out += chunk
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        # Held across the whole write: the heartbeat thread and the caller
        # share one socket, and interleaved frames would corrupt the stream.
        with self._send_lock:
            self._send_frame_locked(opcode, payload)

    def _send_frame_locked(self, opcode: int, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise WebSocketClosed("socket is closed")
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)  # client frames must be masked
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode())

    def send_json(self, obj: object) -> None:
        self.send_text(json.dumps(obj))

    def _read_frame(self) -> tuple[int, bool, bytes]:
        h = self._recvn(2)
        fin = bool(h[0] & 0x80)
        opcode = h[0] & 0x0F
        masked = bool(h[1] & 0x80)
        n = h[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._recvn(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._recvn(8))[0]
        mask = self._recvn(4) if masked else None
        payload = self._recvn(n) if n else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, fin, payload

    def messages(self) -> Iterator[str]:
        """Yield text messages, transparently handling control frames."""
        buffer = b""
        buffer_op = None
        while True:
            opcode, fin, payload = self._read_frame()

            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:                       # echo the close, per RFC6455
                    self._send_frame(OP_CLOSE, payload[:2])
                except Exception:
                    pass
                raise WebSocketClosed("server sent close")

            if opcode in (OP_TEXT, OP_BINARY):
                buffer, buffer_op = payload, opcode
            elif opcode == OP_CONT:
                buffer += payload
            else:
                continue

            if fin:
                if buffer_op == OP_TEXT:
                    yield buffer.decode("utf-8", "replace")
                buffer, buffer_op = b"", None
