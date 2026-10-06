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

import os
import re
import shutil
import time
from pathlib import Path

FAST_INTERVAL = 2.0
DISK_INTERVAL = 30 * 60


def read_miku_rom_version(top: Path) -> str:
    mk = top / "vendor" / "miku" / "config" / "versioning.mk"
    try:
        text = mk.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "?"
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line.startswith("MIKU_ROM_VERSION"):
            continue
        if ":=" in line:
            return line.split(":=", 1)[1].strip() or "?"
        if "=" in line:
            return line.split("=", 1)[1].strip() or "?"
    return "?"


def read_distro() -> str:
    path = Path("/etc/os-release")
    try:
        data: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            data[key] = value.strip().strip('"')
        return data.get("PRETTY_NAME") or data.get("NAME") or "Linux"
    except OSError:
        return os.uname().sysname or "Linux"


def read_cpu_model() -> str:
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "CPU"
    keys = ("model name", "Hardware", "Processor", "cpu model")
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        left, right = raw.split(":", 1)
        if left.strip().lower() in keys and right.strip():
            return re.sub(r"\s+", " ", right.strip())
    return "CPU"


def read_cpu_mhz() -> float:
    freqs: list[float] = []
    root = Path("/sys/devices/system/cpu")
    try:
        for path in sorted(root.glob("cpu[0-9]*/cpufreq/scaling_cur_freq")):
            try:
                freqs.append(int(path.read_text().strip()) / 1000.0)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    if freqs:
        return sum(freqs) / len(freqs)
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0.0
    for raw in text.splitlines():
        if raw.lower().startswith("cpu mhz"):
            _, value = raw.split(":", 1)
            try:
                freqs.append(float(value.strip()))
            except ValueError:
                continue
    if freqs:
        return sum(freqs) / len(freqs)
    return 0.0


def read_cpu_times() -> tuple[int, int] | None:
    try:
        line = Path("/proc/stat").read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except OSError:
        return None
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    nums = [int(x) for x in parts[1:] if x.isdigit()]
    if len(nums) < 4:
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    total = sum(nums[:8])
    return total, idle


def read_mem() -> tuple[int, int]:
    info: dict[str, int] = {}
    try:
        for raw in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in raw:
                continue
            key, rest = raw.split(":", 1)
            num = rest.strip().split()[0]
            try:
                info[key] = int(num) * 1024
            except ValueError:
                continue
    except OSError:
        return 0, 0
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(0, total - avail)
    return used, total


def read_disk(path: Path) -> tuple[int, int]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return 0, 0
    return usage.used, usage.total


def fmt_pair(used: int, total: int) -> str:
    value = float(max(1, total))
    unit = "B"
    div = 1.0
    for name in ("B", "KiB", "MiB", "GiB", "TiB"):
        unit = name
        if value < 1024.0 or name == "TiB":
            break
        value /= 1024.0
        div *= 1024.0
    if unit == "B":
        return f"{used} / {total} B"
    return f"{used / div:.1f} / {total / div:.1f} {unit}"


def fmt_freq(mhz: float) -> str:
    if mhz <= 0:
        return ""
    if mhz >= 1000:
        return f"{mhz / 1000.0:.2f} GHz"
    return f"{mhz:.0f} MHz"


class HostMonitor:
    def __init__(self, top: Path) -> None:
        self.top = top
        self.rom = read_miku_rom_version(top)
        self.distro = read_distro()
        self.cpu_model = read_cpu_model()
        self.cpu_mhz = 0.0
        self.cpu_pct: float | None = None
        self.mem_used = 0
        self.mem_total = 0
        self.disk_used = 0
        self.disk_total = 0
        self._prev_times = read_cpu_times()
        now = time.monotonic()
        self._last_fast = now
        self.cpu_mhz = read_cpu_mhz()
        self.mem_used, self.mem_total = read_mem()
        self.disk_used, self.disk_total = read_disk(self.top)
        self._last_disk = now

    def tick(self, force: bool = False, force_disk: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_fast >= FAST_INTERVAL:
            self.cpu_mhz = read_cpu_mhz()
            self._sample_cpu()
            self.mem_used, self.mem_total = read_mem()
            self._last_fast = now
        if force or force_disk or now - self._last_disk >= DISK_INTERVAL:
            self.disk_used, self.disk_total = read_disk(self.top)
            self._last_disk = now

    def _sample_cpu(self) -> None:
        times = read_cpu_times()
        if times is None or self._prev_times is None:
            self._prev_times = times
            return
        total_d = times[0] - self._prev_times[0]
        idle_d = times[1] - self._prev_times[1]
        self._prev_times = times
        if total_d <= 0:
            return
        busy = 1.0 - (idle_d / total_d)
        self.cpu_pct = max(0.0, min(100.0, busy * 100.0))
