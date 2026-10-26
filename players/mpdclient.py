"""Minimal Music Player Daemon (MPD) protocol client.

This is a small synchronous implementation of the subset of the MPD
protocol the head-unit needs: library browsing, transport control, status
polling and album-art retrieval.
"""

from __future__ import annotations

import socket
import time
from typing import Iterable

from debuglog import getLogger

_log = getLogger("mpd.client")

# Commands slower than this are worth surfacing: they block the GUI thread.
_SLOW_COMMAND_MS = 250.0
# Larger binary chunks make artwork transfers an order of magnitude faster:
# MPD defaults to 8 KiB per response, so a 100 KiB cover needed 13 round trips.
_ARTWORK_CHUNK = 256 * 1024


def _preview(command: str, limit: int = 200) -> str:
    """Collapse a command (or command list) into one safe log line."""
    text = " ".join(command.split())
    if text.lower().startswith("password"):
        return 'password "***"'
    return text if len(text) <= limit else f"{text[:limit]}..."


class MpdError(Exception):
    """Raised when the daemon returns an error response."""


class MpdClient:
    """Blocking connection to a single MPD instance.

    The client is deliberately simple: every call opens with the current
    connection and reads the response to completion. Callers that need
    concurrency are expected to use their own lock.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 6600, timeout: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._socket: socket.socket | None = None
        self._buffer = bytearray()
        self._version = ""
        # After a failed connect, fail fast for a moment instead of paying the
        # full timeout on every command while the server stays unreachable.
        # The wait doubles up to a cap so a dead server cannot keep the UI
        # blocked for two seconds out of every two seconds.
        self._retryBase = max(1.0, min(5.0, timeout))
        self._retryMax = 10.0
        self._retryDelay = self._retryBase
        self._failedAt = 0.0
        self._failure: OSError | MpdError | None = None
        # Number of connection attempts since the last successful connect.
        self.attempts = 0
        self._binaryLimitSet = False

    # ------------------------------------------------------------------ #
    # Connection handling
    # ------------------------------------------------------------------ #
    @property
    def timeout(self) -> float:
        return self._timeout

    def setTimeout(self, seconds: float) -> None:
        """Change the socket timeout, e.g. for a bulk library transfer."""
        self._timeout = float(seconds)
        if self._socket is not None:
            self._socket.settimeout(self._timeout)

    def connect(self) -> None:
        self.close()
        self.attempts += 1
        started = time.monotonic()
        _log.debug(
            "connecting to %s:%s (timeout %.0f ms)",
            self._host,
            self._port,
            self._timeout * 1000,
        )
        try:
            sock = socket.create_connection(
                (self._host, self._port), timeout=self._timeout
            )
            sock.settimeout(self._timeout)
            self._socket = sock
            self._buffer = bytearray()
            greeting = self._readLine()
        except (OSError, MpdError) as exc:
            self.close()
            self._failedAt = time.monotonic()
            self._failure = exc
            _log.warning(
                "connect to %s:%s failed after %.0f ms: %s",
                self._host,
                self._port,
                (time.monotonic() - started) * 1000,
                exc,
            )
            raise
        if greeting.startswith("OK MPD "):
            self._version = greeting.split(" ", 2)[2]
        self._failedAt = 0.0
        self._failure = None
        self._retryDelay = self._retryBase
        self.attempts = 0
        _log.debug(
            "connected to %s:%s in %.0f ms (MPD %s)",
            self._host,
            self._port,
            (time.monotonic() - started) * 1000,
            self._version or "?",
        )

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
            _log.debug("connection to %s:%s closed", self._host, self._port)
        self._buffer = bytearray()
        self._binaryLimitSet = False

    def _ensureConnected(self) -> None:
        if self._socket is not None:
            return
        if self._failure is not None:
            if time.monotonic() - self._failedAt < self._retryDelay:
                raise self._failure
            self._failure = None
        try:
            self.connect()
        except (OSError, MpdError) as exc:
            self._failedAt = time.monotonic()
            self._failure = exc
            self._retryDelay = min(
                self._retryMax, max(self._retryBase, self._retryDelay * 2)
            )
            raise

    # ------------------------------------------------------------------ #
    # Low-level framing
    # ------------------------------------------------------------------ #
    def _recv(self, count: int) -> bytes:
        assert self._socket is not None
        while len(self._buffer) < count:
            chunk = self._socket.recv(max(4096, count - len(self._buffer)))
            if not chunk:
                raise MpdError("connection closed by daemon")
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def _readLine(self) -> str:
        assert self._socket is not None
        while b"\n" not in self._buffer:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise MpdError("connection closed by daemon")
            self._buffer.extend(chunk)
        index = self._buffer.index(b"\n")
        line = bytes(self._buffer[:index])
        del self._buffer[: index + 1]
        return line.decode("utf-8", errors="replace")

    def _send(self, command: str) -> None:
        assert self._socket is not None
        self._socket.sendall(command.encode("utf-8") + b"\n")

    # ------------------------------------------------------------------ #
    # Command interface
    # ------------------------------------------------------------------ #
    def command(self, command: str) -> list[str]:
        """Run a command and return the response lines (without OK)."""
        started = time.monotonic()
        try:
            self._ensureConnected()
            self._send(command)
            lines = self._readResponse()
        except MpdError as exc:
            # ACK is a command-level rejection; the socket is still usable.
            if not str(exc).startswith("ACK "):
                self.close()
            _log.warning(
                "command failed after %.0f ms: %s -> %s",
                (time.monotonic() - started) * 1000,
                _preview(command),
                exc,
            )
            raise
        except OSError as exc:
            self.close()
            _log.warning(
                "command failed after %.0f ms: %s -> %s",
                (time.monotonic() - started) * 1000,
                _preview(command),
                exc,
            )
            raise
        self._logDuration("command", command, started, len(lines))
        return lines

    def password(self, password: str) -> None:
        """Authenticate this connection when the MPD server requires it."""
        escaped = password.replace("\\", "\\\\").replace('"', '\\"')
        self.command(f'password "{escaped}"')

    def commandWithBinary(self, command: str) -> tuple[list[str], bytes | None]:
        """Run a command that may return a binary payload."""
        started = time.monotonic()
        try:
            self._ensureConnected()
            self._send(command)
            lines, binary = self._readResponseWithBinary()
        except MpdError as exc:
            if not str(exc).startswith("ACK "):
                self.close()
            _log.warning(
                "binary command failed after %.0f ms: %s -> %s",
                (time.monotonic() - started) * 1000,
                _preview(command),
                exc,
            )
            raise
        except OSError as exc:
            self.close()
            _log.warning(
                "binary command failed after %.0f ms: %s -> %s",
                (time.monotonic() - started) * 1000,
                _preview(command),
                exc,
            )
            raise
        self._logDuration(
            "binary command", command, started, len(lines), len(binary or b"")
        )
        return lines, binary

    def _logDuration(
        self,
        kind: str,
        command: str,
        started: float,
        lines: int,
        payload: int = 0,
    ) -> None:
        elapsed = (time.monotonic() - started) * 1000
        extra = f", {payload} bytes" if payload else ""
        if elapsed >= _SLOW_COMMAND_MS:
            _log.warning(
                "slow %s %.0f ms: %s (%d lines%s)",
                kind,
                elapsed,
                _preview(command),
                lines,
                extra,
            )
        else:
            _log.debug(
                "%s %.1f ms: %s (%d lines%s)",
                kind,
                elapsed,
                _preview(command),
                lines,
                extra,
            )

    def _readResponse(self) -> list[str]:
        lines: list[str] = []
        while True:
            line = self._readLine()
            if line == "OK":
                return lines
            if line.startswith("ACK "):
                raise MpdError(line)
            lines.append(line)

    def _readResponseWithBinary(self) -> tuple[list[str], bytes | None]:
        lines: list[str] = []
        binary: bytes | None = None
        while True:
            line = self._readLine()
            if line == "OK":
                return lines, binary
            if line.startswith("ACK "):
                raise MpdError(line)
            if line.startswith("binary:"):
                count = int(line.split(":", 1)[1])
                binary = self._recv(count)
                self._readLine()  # consume the trailing newline after the blob
                continue
            lines.append(line)

    def commandList(self, commands: Iterable[str]) -> list[str]:
        """Run several commands within a command list.

        Binary responses are not supported inside a command list, which is
        fine for all the batch operations used here.
        """
        body = "\n".join(commands)
        return self.command(f"command_list_begin\n{body}\ncommand_list_end")

    # ------------------------------------------------------------------ #
    # Album art helpers
    # ------------------------------------------------------------------ #
    def readPicture(self, uri: str) -> tuple[str, bytes | None]:
        """Fetch embedded cover art for a track URI.

        Returns ``("ok", data)`` when the server answered (data is None when
        the file has no artwork) and ``("error", None)`` on a transport error.
        """
        return self._fetchArt(f'readpicture "{uri}"')

    def albumArt(self, uri: str) -> tuple[str, bytes | None]:
        """Fetch cover art for a track or folder URI; see :meth:`readPicture`."""
        return self._fetchArt(f'albumart "{uri}"')

    def _ensureBinaryLimit(self) -> None:
        """Ask MPD for larger binary chunks (artwork transfers only)."""
        if self._binaryLimitSet:
            return
        self._binaryLimitSet = True
        try:
            self.command(f"binarylimit {_ARTWORK_CHUNK}")
        except (MpdError, OSError) as exc:
            # Older daemons do not know the command; the default chunk size
            # still works, just slower.
            _log.debug("binarylimit not available: %s", exc)

    def _fetchArt(self, baseCommand: str) -> tuple[str, bytes | None]:
        """Collect the full artwork payload for a base command."""
        started = time.monotonic()
        try:
            self._ensureConnected()
        except (OSError, MpdError) as exc:
            _log.debug("artwork connect failed for %s: %s", _preview(baseCommand), exc)
            return "error", None
        self._ensureBinaryLimit()

        collected = bytearray()
        try:
            while True:
                # MPD returns artwork in chunks; advance by the bytes received.
                lines, chunk = self.commandWithBinary(f"{baseCommand} {len(collected)}")
                if not chunk:
                    break
                collected.extend(chunk)
                total = 0
                for line in lines:
                    if line.startswith("size:"):
                        total = int(line.split(":", 1)[1])
                        break
                if total and len(collected) >= total:
                    break
        except (MpdError, OSError) as exc:
            _log.debug(
                "artwork fetch failed for %s after %.0f ms: %s",
                _preview(baseCommand),
                (time.monotonic() - started) * 1000,
                exc,
            )
            return "error", None
        _log.debug(
            "artwork %s: %d bytes in %.0f ms",
            _preview(baseCommand),
            len(collected),
            (time.monotonic() - started) * 1000,
        )
        return "ok", bytes(collected) or None
