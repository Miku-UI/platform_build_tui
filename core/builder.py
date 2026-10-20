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
import json
import os
import pty
import re
import secrets
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
from datetime import datetime, timezone
from pathlib import Path

from core.devices import Product
from core.phase import MARKER_PREFIX

MAKE_TARGET = "diva"
GSI_TARGET = "systemimage"
FASTBOOT_TARGET = "updatepackage"

TARGET_OTA = "ota"
TARGET_FASTBOOT = "fastboot"
TARGET_CUSTOM = "custom"
TARGET_KINDS = (TARGET_OTA, TARGET_FASTBOOT, TARGET_CUSTOM)
TARGET_GSI_KINDS = (TARGET_OTA, TARGET_CUSTOM)

MEM_LIMIT_OFF = "off"
MEM_LIMIT_STANDARD = "standard"
MEM_LIMIT_AGGRESSIVE = "aggressive"
MEM_LIMIT_KINDS = (MEM_LIMIT_OFF, MEM_LIMIT_STANDARD, MEM_LIMIT_AGGRESSIVE)
MEM_LIMIT_HIGH = {
    MEM_LIMIT_STANDARD: "80%",
    MEM_LIMIT_AGGRESSIVE: "60%",
}
MEM_LIMIT_UNIT = "miku-tui-build"


def memory_high_spec(kind: str) -> str | None:
    return MEM_LIMIT_HIGH.get(kind)


def mem_limit_tag(kind: str) -> str:
    spec = memory_high_spec(kind)
    return f"mem{spec}" if spec else ""


def make_upgrade_build_number() -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{day}-{secrets.token_hex(2)}"

_MAKE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+/-]*$")
CUSTOM_TARGET_MAX = 128
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_NAME_MAX = 64
ENV_VALUE_MAX = 512
ENV_VARS_MAX = 32


def sanitize_make_targets(raw: str) -> str:
    if len(raw) > CUSTOM_TARGET_MAX:
        return ""
    tokens: list[str] = []
    for tok in raw.split():
        if not _MAKE_TOKEN_RE.fullmatch(tok):
            return ""
        tokens.append(tok)
    return " ".join(tokens)


def default_make_target(product: Product | None = None) -> str:
    if product is not None and product.is_gsi:
        return GSI_TARGET
    return MAKE_TARGET


def target_kinds_for(product: Product | None) -> tuple[str, ...]:
    if product is not None and product.is_gsi:
        return TARGET_GSI_KINDS
    return TARGET_KINDS


def resolve_make_target(kind: str, custom: str, product: Product | None = None) -> str:
    if kind == TARGET_FASTBOOT:
        return FASTBOOT_TARGET
    if kind == TARGET_CUSTOM:
        cleaned = sanitize_make_targets(custom)
        if cleaned:
            return cleaned
    return default_make_target(product)


def quote_make_target(raw: str) -> str:
    tokens = sanitize_make_targets(raw).split() or [MAKE_TARGET]
    return " ".join(shlex.quote(tok) for tok in tokens)


def sanitize_env_name(raw: str) -> str:
    name = raw.strip()
    if not name or len(name) > ENV_NAME_MAX or not _ENV_NAME_RE.fullmatch(name):
        return ""
    return name


def sanitize_env_value(raw: str) -> str | None:
    if len(raw) > ENV_VALUE_MAX or "\n" in raw or "\r" in raw or "\0" in raw:
        return None
    return raw


def sanitize_env_vars(raw: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        return ()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        val = item.get("value")
        if not isinstance(name, str) or not isinstance(val, str):
            continue
        name = sanitize_env_name(name)
        value = sanitize_env_value(val)
        if not name or value is None or name in seen:
            continue
        seen.add(name)
        out.append((name, value))
        if len(out) >= ENV_VARS_MAX:
            break
    return tuple(out)


@dataclass
class BuildConfig:
    product: Product
    jobs: int
    gapps: bool
    ccache: bool
    clean: str
    release: str
    variant: str = "userdebug"
    keep_going: bool = False
    make_target: str = MAKE_TARGET
    show_sha256: bool = True
    mem_limit: str = MEM_LIMIT_OFF
    env_vars: tuple[tuple[str, str], ...] = ()
    mark_upgrade: bool = False
    build_number: str = ""
    lunch: bool = True

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
        env = _build_env(self.config, self.top)
        script = _build_script(self.top, self.config, markers=not self.inherit_tty)
        argv = ["bash", "-c", script]
        high = memory_high_spec(self.config.mem_limit)
        if high:
            argv = _scope_argv(argv, high)
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
        if memory_high_spec(self.config.mem_limit):
            _stop_mem_scope()
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


def _scope_argv(inner: list[str], high: str) -> list[str]:
    run = shutil.which("systemd-run")
    if not run:
        raise FileNotFoundError("systemd-run")
    _stop_mem_scope()
    return [
        run,
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        f"--unit={MEM_LIMIT_UNIT}",
        "-p",
        f"MemoryHigh={high}",
        "-p",
        "MemorySwapMax=infinity",
        "--",
        *inner,
    ]


def _stop_mem_scope() -> None:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return
    unit = f"{MEM_LIMIT_UNIT}.scope"
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "check": False}
    subprocess.run([systemctl, "--user", "stop", unit], **kwargs)
    subprocess.run([systemctl, "--user", "reset-failed", unit], **kwargs)


def soong_used_env_path(top: Path, product_name: str) -> Path:
    out = os.environ.get("OUT_DIR") or "out"
    out_dir = Path(out)
    if not out_dir.is_absolute():
        out_dir = top / out_dir
    return out_dir / "soong" / f"soong.environment.used.{product_name}.build"


def load_soong_used_env(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, list):
        return {}
    out: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("Key")
        val = item.get("Value")
        if isinstance(key, str) and isinstance(val, str):
            out[key] = val
    return out


def _build_env(config: BuildConfig, top: Path) -> dict[str, str]:
    env = os.environ.copy()
    if not config.lunch:
        for key, val in load_soong_used_env(
            soong_used_env_path(top, config.product.product_name)
        ).items():
            if val:
                env[key] = val
            else:
                env.pop(key, None)
    env["TARGET_PRODUCT"] = config.product.product_name
    env["TARGET_RELEASE"] = config.release
    env["TARGET_BUILD_VARIANT"] = config.variant
    env["TARGET_BUILD_TYPE"] = "release"
    env.pop("TARGET_BUILD_APPS", None)
    product = config.product.product_name
    if product.startswith("miku_"):
        env["MIKU_BUILD"] = product[len("miku_") :]
    else:
        env.pop("MIKU_BUILD", None)
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
    for name, value in config.env_vars:
        key = sanitize_env_name(name)
        val = sanitize_env_value(value)
        if key and val is not None:
            env[key] = val
    if config.mark_upgrade and config.build_number:
        env["BUILD_NUMBER"] = config.build_number
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
    if config.mark_upgrade and config.build_number:
        lines.append("printf 'BUILD_NUMBER=%s\\n' " + shlex.quote(config.build_number))
    if markers:
        lines.append(_phase_printf("setup"))
    lines.append("source build/envsetup.sh")
    if config.lunch:
        if markers:
            lines.append(_phase_printf("lunch"))
        lines.append(f"lunch {shlex.quote(config.lunch_combo)} || exit $?")
    else:
        lines.append("set_sequence_number")
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
    keep = " -k 0" if config.keep_going else ""
    lines.append(f"make {quote_make_target(config.make_target)} -j{jobs}{keep}")
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
