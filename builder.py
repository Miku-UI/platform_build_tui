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

import fcntl
import os
import pty
import select
import shlex
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from devices import Product
from phase import MARKER_PREFIX

MAKE_TARGET = "diva"


@dataclass
class BuildConfig:
    product: Product
    jobs: int
    gapps: bool
    ccache: bool
    clean: str
    release: str
    variant: str = "userdebug"

    @property
    def lunch_combo(self) -> str:
        return f"{self.product.product_name}-{self.release}-{self.variant}"


class BuildSession:
    def __init__(
        self,
        top: Path,
        config: BuildConfig,
        on_data: Callable[[str], None] | None = None,
        inherit_tty: bool = False,
    ) -> None:
        self.top = top
        self.config = config
        self.on_data = on_data
        self.inherit_tty = inherit_tty
        self.proc: subprocess.Popen[bytes] | None = None
        self.master_fd: int | None = None
        self._reader: threading.Thread | None = None
        self._returncode: int | None = None
        self.started_at = 0.0
        self.finished_at = 0.0
        self.stopped = False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def returncode(self) -> int | None:
        if self.proc is not None:
            code = self.proc.poll()
            if code is not None:
                self._returncode = code
        return self._returncode

    def start(self) -> None:
        env = _build_env(self.config)
        script = _build_script(self.top, self.config, markers=not self.inherit_tty)
        argv = ["bash", "-c", script]
        self.started_at = time.time()
        if self.inherit_tty:
            self.proc = subprocess.Popen(argv, cwd=self.top, env=env, start_new_session=True)
            return
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        env["TERM"] = "xterm-256color"
        packed = struct.pack("HHHH", 24, 80, 0, 0)
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, packed)
        except OSError:
            pass
        self.proc = subprocess.Popen(
            argv,
            cwd=self.top,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        self._reader = threading.Thread(target=self._read_pty, daemon=True)
        self._reader.start()

    def set_winsize(self, rows: int, cols: int) -> None:
        if self.master_fd is None:
            return
        rows = max(2, rows)
        cols = max(20, cols)
        packed = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)
        except OSError:
            pass

    def close(self) -> None:
        self._close_pty()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=0.2)

    def wait(self) -> int:
        if self.proc is None:
            return 1
        code = self.proc.wait()
        self._returncode = code
        self.finished_at = time.time()
        self.close()
        return code

    def stop(self) -> None:
        self.stopped = True
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            self._returncode = self.proc.returncode
            self.finished_at = time.time()
            self.close()
            return
        try:
            pgid = os.getpgid(self.proc.pid)
        except ProcessLookupError:
            self._returncode = self.proc.poll()
            self.finished_at = time.time()
            self.close()
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            if _wait_exit(self.proc, 2.0):
                break
        else:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _wait_exit(self.proc, 1.0)
        self._returncode = self.proc.poll()
        if self._returncode is None:
            self._returncode = -signal.SIGKILL
        self.finished_at = time.time()
        self.close()

    def _read_pty(self) -> None:
        fd = self.master_fd
        if fd is None:
            return
        leftover = b""
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                if self.proc is not None and self.proc.poll() is not None:
                    try:
                        chunk = os.read(fd, 8192)
                    except OSError:
                        chunk = b""
                    leftover += chunk
                    break
                continue
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                break
            if not chunk:
                break
            leftover += chunk
            text, leftover = _decode_chunks(leftover)
            if text and self.on_data:
                self.on_data(text)
        if leftover and self.on_data:
            self.on_data(leftover.decode("utf-8", errors="replace"))
        if self.proc is not None and self.proc.poll() is not None:
            self._returncode = self.proc.returncode
            self.finished_at = time.time()

    def _close_pty(self) -> None:
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


def run_cli(top: Path, config: BuildConfig) -> int:
    session = BuildSession(top, config, inherit_tty=True)
    session.start()
    try:
        return session.wait()
    except KeyboardInterrupt:
        session.stop()
        return 130


def _build_env(config: BuildConfig) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TARGET_PRODUCT", None)
    env.pop("TARGET_RELEASE", None)
    env.pop("TARGET_BUILD_VARIANT", None)
    if config.gapps:
        env["MIKU_GAPPS"] = "true"
    else:
        env.pop("MIKU_GAPPS", None)
    if config.ccache:
        env["USE_CCACHE"] = "1"
        env["CCACHE_COMPRESS"] = "1"
        env.setdefault("CCACHE_DIR", str(Path.home() / ".ccache"))
        ccache = shutil.which("ccache")
        if ccache:
            env["CCACHE_EXEC"] = ccache
    else:
        env.pop("USE_CCACHE", None)
        env.pop("CCACHE_EXEC", None)
    env.setdefault("NINJA_STATUS", "[%f/%t %e] ")
    if env.get("TERM") in (None, "", "dumb", "unknown"):
        env["TERM"] = "xterm-256color"
    return env


def _phase_printf(name: str) -> str:
    return "printf '\\n%s\\n' " + shlex.quote(MARKER_PREFIX + name)


def _build_script(top: Path, config: BuildConfig, *, markers: bool = False) -> str:
    jobs = max(1, int(config.jobs))
    lines = [
        "set +u",
        "set -o pipefail",
        f"cd {shlex.quote(str(top))}",
    ]
    if markers:
        lines.append(_phase_printf("setup"))
    lines.append("source build/envsetup.sh")
    if markers:
        lines.append(_phase_printf("lunch"))
    lines.append(f"lunch {shlex.quote(config.lunch_combo)} || exit $?")
    if config.clean == "clean":
        if markers:
            lines.append(_phase_printf("clean"))
        lines.append("make clean || exit $?")
    elif config.clean == "installclean":
        if markers:
            lines.append(_phase_printf("clean"))
        lines.append("make installclean || exit $?")
    if markers:
        lines.append(_phase_printf("make"))
    lines.append(f"make {MAKE_TARGET} -j{jobs}")
    lines.append("exit $?")
    return "\n".join(lines) + "\n"


def _decode_chunks(buf: bytes) -> tuple[str, bytes]:
    for keep in range(0, min(4, len(buf))):
        piece = buf if keep == 0 else buf[:-keep]
        rest = b"" if keep == 0 else buf[-keep:]
        try:
            return piece.decode("utf-8"), rest
        except UnicodeDecodeError:
            continue
    return buf.decode("utf-8", errors="replace"), b""


def _wait_exit(proc: subprocess.Popen[bytes], timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return proc.poll() is not None
