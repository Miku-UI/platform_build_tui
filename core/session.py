# Copyright (C) 2026 Miku UI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import array
import fcntl
import json
import os
import select
import signal
import socket
import struct
import sys
import termios
import time
import traceback
from pathlib import Path
from typing import Callable

from core.prefs import tui_dir

SOCK_NAME = "session.sock"
LOCK_NAME = "session.lock"
LOG_NAME = "daemon.log"
_TIOCSCTTY = getattr(termios, "TIOCSCTTY", 0x540E)


def sock_path(top: Path) -> Path:
    return tui_dir(top) / SOCK_NAME


def lock_path(top: Path) -> Path:
    return tui_dir(top) / LOCK_NAME


def log_path(top: Path) -> Path:
    return tui_dir(top) / LOG_NAME


def launch(top: Path, make_app: Callable) -> int:
    path = sock_path(top)
    attached = client_attach(path, wait=False)
    if attached is not None:
        return attached
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        _daemonize(top)
        tui_dir(top, create=True)
        lock_fd = _try_lock(lock_path(top))
        if lock_fd is None:
            os._exit(0)
        try:
            listen = _bind_sock(path)
            app = make_app()
            app.run_daemon(listen)
        except Exception:
            try:
                log_path(top).write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass
            os._exit(1)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        os._exit(0)
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    attached = client_attach(path, wait=True)
    if attached is None:
        print("miku tui: failed to attach", file=sys.stderr)
        return 2
    return attached


def client_attach(path: Path, wait: bool = True) -> int | None:
    sock = _connect(path, retries=50 if wait else 1)
    if sock is None:
        return None
    try:
        tty = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        sock.close()
        print("miku tui: /dev/tty is not available", file=sys.stderr)
        return 2
    try:
        meta = {
            "pgrp": os.getpgrp(),
            "term": os.environ.get("TERM") or "xterm-256color",
        }
        _send_attach(sock, tty, meta)
        line = _recv_line(sock)
    finally:
        os.close(tty)
        sock.close()
    if line == "detach":
        return 0
    if line.startswith("exit "):
        try:
            return int(line.split(None, 1)[1])
        except ValueError:
            return 1
    if not line:
        return 1
    return 0


def accept_attach(conn: socket.socket) -> tuple[int, dict]:
    conn.settimeout(5.0)
    fd, meta = _recv_attach(conn)
    conn.settimeout(None)
    return fd, meta


def try_accept(listen: socket.socket) -> tuple[socket.socket, int, dict] | None:
    try:
        conn, _addr = listen.accept()
    except BlockingIOError:
        return None
    except OSError:
        return None
    try:
        fd, meta = accept_attach(conn)
    except OSError:
        try:
            conn.close()
        except OSError:
            pass
        return None
    return conn, fd, meta


def notify_client(conn: socket.socket | None, msg: str) -> None:
    if conn is None:
        return
    try:
        conn.sendall((msg + "\n").encode("ascii"))
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


def take_tty(tty_fd: int) -> None:
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    signal.signal(signal.SIGTTIN, signal.SIG_IGN)
    try:
        fcntl.ioctl(tty_fd, _TIOCSCTTY, 1)
    except OSError:
        pass
    try:
        os.tcsetpgrp(tty_fd, os.getpgrp())
    except OSError:
        pass
    os.dup2(tty_fd, 0)
    os.dup2(tty_fd, 1)
    os.dup2(tty_fd, 2)


def release_tty(tty_fd: int, client_pgrp: int) -> None:
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(tty_fd, client_pgrp)
    except OSError:
        pass


def client_gone(conn: socket.socket | None) -> bool:
    if conn is None:
        return False
    try:
        ready, _, _ = select.select([conn], [], [], 0)
    except (OSError, ValueError):
        return True
    if not ready:
        return False
    try:
        data = conn.recv(1, socket.MSG_PEEK)
    except OSError:
        return True
    return not data


def _daemonize(top: Path) -> None:
    os.setsid()
    os.chdir(top)
    devnull = os.open("/dev/null", os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    if devnull > 2:
        os.close(devnull)


def _try_lock(path: Path) -> int | None:
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    return fd


def _bind_sock(path: Path) -> socket.socket:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(os.fspath(path))
    os.chmod(path, 0o600)
    sock.listen(4)
    return sock


def _connect(path: Path, retries: int = 3) -> socket.socket | None:
    last_err = None
    for i in range(max(1, retries)):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(os.fspath(path))
            return sock
        except OSError as exc:
            last_err = exc
            sock.close()
            if i + 1 < retries:
                time.sleep(0.05)
    if last_err is not None:
        _maybe_unlink_stale(path)
    return None


def _maybe_unlink_stale(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _send_attach(sock: socket.socket, tty_fd: int, meta: dict) -> None:
    payload = json.dumps(meta).encode("utf-8")
    packet = struct.pack(">I", len(payload)) + payload
    fds = array.array("i", [tty_fd])
    sock.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])


def _recv_attach(sock: socket.socket) -> tuple[int, dict]:
    fds = array.array("i")
    buf, anc, _flags, _addr = sock.recvmsg(65536, socket.CMSG_LEN(64))
    for level, typ, data in anc:
        if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
            fds.frombytes(data[: len(data) - (len(data) % fds.itemsize)])
    if not fds:
        raise OSError("attach missing tty fd")
    if len(buf) < 4:
        raise OSError("attach short header")
    (n,) = struct.unpack(">I", buf[:4])
    raw = buf[4 : 4 + n]
    if len(raw) < n:
        raise OSError("attach short payload")
    meta = json.loads(raw.decode("utf-8"))
    if not isinstance(meta, dict):
        raise OSError("attach bad payload")
    return int(fds[0]), meta


def _recv_line(sock: socket.socket) -> str:
    data = b""
    while b"\n" not in data:
        chunk = sock.recv(256)
        if not chunk:
            break
        data += chunk
        if len(data) > 256:
            break
    if not data:
        return ""
    return data.split(b"\n", 1)[0].decode("ascii", "replace")
