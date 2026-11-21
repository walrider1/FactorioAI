"""Minimal Source RCON client (the protocol Factorio speaks on --rcon-port)."""

from __future__ import annotations

import socket
import struct
import threading

AUTH = 3
AUTH_RESPONSE = 2
EXEC = 2
RESPONSE = 0


class RconError(RuntimeError):
    pass


class RconClient:
    def __init__(self, host: str, port: int, password: str, timeout: float = 30.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._next_id = 1

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        rid = self._send(AUTH, self.password)
        while True:
            pid, ptype, _ = self._recv()
            if ptype == AUTH_RESPONSE:
                if pid == -1 or pid != rid:
                    self.close()
                    raise RconError("RCON authentication failed")
                return

    def command(self, cmd: str) -> str:
        with self._lock:
            if self._sock is None:
                raise RconError("not connected")
            rid = self._send(EXEC, cmd)
            while True:
                pid, _, body = self._recv()
                if pid == rid:
                    return body

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # ----------------------------------------------------------------- wire
    def _send(self, ptype: int, body: str) -> int:
        rid = self._next_id
        self._next_id = (self._next_id % 2_000_000_000) + 1
        payload = struct.pack("<ii", rid, ptype) + body.encode("utf-8") + b"\x00\x00"
        assert self._sock is not None
        self._sock.sendall(struct.pack("<i", len(payload)) + payload)
        return rid

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed by server")
            buf += chunk
        return bytes(buf)

    def _recv(self) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._recv_exact(4))
        if length < 10 or length > 64 * 1024 * 1024:
            raise RconError(f"bad packet length {length}")
        data = self._recv_exact(length)
        pid, ptype = struct.unpack("<ii", data[:8])
        return pid, ptype, data[8:-2].decode("utf-8", errors="replace")
