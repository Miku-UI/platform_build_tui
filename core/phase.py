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

import re
import threading
import time

MARKER_PREFIX = "[miku-tui] phase="

_ANSI_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|][^\x07\x1b]*(?:\x07|\x1b\\)?)"
)
_EL_RE = re.compile(r"\x1b\[[0-9]*K")
_PROG_RE = re.compile(r"\[(?:[^\]]*?\s)?(\d+)/(\d+)(?:[^\]]*)\]")
_KATI_RE = re.compile(
    r"(?:including|initializing|finishing|writing) \S+"
)
_PACK_NEEDLES = (
    "Package OTA",
    "Build ota from target files",
    "Package Complete",
)

_RANK = {
    "phase_setup": 0,
    "phase_lunch": 1,
    "phase_clean": 2,
    "phase_config": 3,
    "phase_soong": 4,
    "phase_kati": 5,
    "phase_ninja": 6,
    "phase_package": 7,
}
_REWIND = frozenset({"phase_config", "phase_soong", "phase_kati"})


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\x08", "")


def _is_soong(line: str) -> bool:
    return (
        "analyzing Android.bp" in line
        or "bootstrap blueprint" in line
        or "Running globs" in line
        or "rerunning soong" in line
    )


def _is_package(line: str) -> bool:
    return any(needle in line for needle in _PACK_NEEDLES)


class PhaseTracker:
    """Incremental build-stage parser for TUI titles."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf = ""
        self.key = "phase_setup"
        self._make = False
        self.entered: dict[str, float] = {"phase_setup": time.time()}
        self.ninja_total: int | None = None
        self.ninja_done: int | None = None
        self.package_path: str | None = None

    def feed(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._buf += _EL_RE.sub("\n", text).replace("\r", "\n")
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._on_line(_plain(line).strip())
            tail = _plain(self._buf).strip()
            if tail:
                self._on_line(tail)

    def snapshot(self) -> tuple[str, dict[str, object]]:
        with self._lock:
            return self.key, {}

    def snapshot_report(
        self,
    ) -> tuple[str, dict[str, float], int | None, int | None, str | None]:
        with self._lock:
            return self.key, dict(self.entered), self.ninja_total, self.ninja_done, self.package_path

    def _advance(self, key: str) -> None:
        new = _RANK[key]
        cur = _RANK[self.key]
        if new < cur and key not in _REWIND:
            return
        if key not in self.entered:
            self.entered[key] = time.time()
        self.key = key

    def _on_line(self, line: str) -> None:
        if not line:
            return
        marker_at = line.find(MARKER_PREFIX)
        if marker_at != -1:
            rest = line[marker_at + len(MARKER_PREFIX) :].strip().split(None, 1)
            name = rest[0] if rest else ""
            if name == "setup":
                self._advance("phase_setup")
            elif name == "lunch":
                self._advance("phase_lunch")
            elif name == "clean":
                self._make = False
                self._advance("phase_clean")
            elif name == "make":
                self._make = True
                self._advance("phase_config")
            return
        if not self._make:
            return
        if line.startswith("Package Complete:"):
            path = line.split(":", 1)[1].strip()
            if path:
                self.package_path = path
        if _is_soong(line):
            self._advance("phase_soong")
            return
        if "Running product configuration" in line:
            self._advance("phase_config")
            return
        if _KATI_RE.search(line) or (
            "regenerating" in line and ("ninja" in line or "wildcard" in line)
        ):
            self._advance("phase_kati")
            return
        if "Starting ninja" in line:
            self._advance("phase_ninja")
            return
        prog = _PROG_RE.search(line)
        if prog:
            rest = line[prog.end() :]
            if _is_soong(line) or _is_soong(rest):
                self._advance("phase_soong")
                return
            if _KATI_RE.search(rest) or _KATI_RE.search(line):
                self._advance("phase_kati")
                return
            try:
                done = int(prog.group(1))
                total = int(prog.group(2))
            except ValueError:
                done = 0
                total = 0
            if "Package OTA" in line and _RANK[self.key] >= _RANK["phase_ninja"]:
                if total > 0:
                    self.ninja_total = total
                self.ninja_done = done
                self._advance("phase_package")
                return
            if _is_package(line) and _RANK[self.key] >= _RANK["phase_ninja"]:
                self._advance("phase_package")
                return
            self._advance("phase_ninja")
            if self.key == "phase_ninja":
                self.ninja_done = done
            return
        if _is_package(line) and _RANK[self.key] >= _RANK["phase_ninja"]:
            self._advance("phase_package")
